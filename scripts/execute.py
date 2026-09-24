#!/usr/bin/env python3
"""
Harness Step Executor — phase 내 step을 순차 실행하고 자가 교정한다.

Usage:
    python3 scripts/execute.py <phase-dir> [--push]
"""

import argparse
import contextlib
import json
import re
import subprocess
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
# git 훅 출력의 색상 코드 — index.json과 재시도 프롬프트에 섞이지 않게 제거한다
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@contextlib.contextmanager
def progress_indicator(label: str):
    """터미널 진행 표시기. with 문으로 사용하며 .elapsed 로 경과 시간을 읽는다."""
    frames = "◐◓◑◒"
    stop = threading.Event()
    t0 = time.monotonic()

    def _animate():
        idx = 0
        while not stop.wait(0.12):
            sec = int(time.monotonic() - t0)
            sys.stderr.write(f"\r{frames[idx % len(frames)]} {label} [{sec}s]")
            sys.stderr.flush()
            idx += 1
        sys.stderr.write("\r" + " " * (len(label) + 20) + "\r")
        sys.stderr.flush()

    th = threading.Thread(target=_animate, daemon=True)
    th.start()
    info = types.SimpleNamespace(elapsed=0.0)
    try:
        yield info
    finally:
        stop.set()
        th.join()
        info.elapsed = time.monotonic() - t0


class StepExecutor:
    """Phase 디렉토리 안의 step들을 순차 실행하는 하네스."""

    MAX_RETRIES = 3
    TIMEOUT = 1800
    FEAT_MSG = "feat({phase}): step {num} — {name}"
    WIP_MSG = "wip({phase}): step {num} — {name} ({outcome})"
    CHORE_MSG = "chore({phase}): step {num} output"
    SUPPRESSION_RE = re.compile(
        r"#\s*noqa\b|#\s*type:\s*ignore|#\s*pyright:\s*ignore|#\s*pylint:\s*disable"
        r"|#\s*fmt:\s*(?:off|skip)\b"
        r"|eslint-disable|@ts-(?:ignore|nocheck|expect-error)|biome-ignore|prettier-ignore"
    )
    TZ = timezone(timedelta(hours=9))

    def __init__(self, phase_dir_name: str, *, auto_push: bool = False):
        self._root = str(ROOT)
        self._phases_dir = ROOT / "phases"
        self._phase_dir = self._phases_dir / phase_dir_name
        self._phase_dir_name = phase_dir_name
        self._top_index_file = self._phases_dir / "index.json"
        self._auto_push = auto_push

        if not self._phase_dir.is_dir():
            print(f"ERROR: {self._phase_dir} not found")
            sys.exit(1)

        self._index_file = self._phase_dir / "index.json"
        if not self._index_file.exists():
            print(f"ERROR: {self._index_file} not found")
            sys.exit(1)

        idx = self._read_json(self._index_file)
        self._project = idx.get("project", "project")
        self._phase_name = idx.get("phase", phase_dir_name)
        self._total = len(idx["steps"])

    def run(self):
        self._print_header()
        self._check_blockers()
        self._check_clean_tree()
        self._checkout_branch()
        guardrails = self._load_guardrails()
        self._ensure_created_at()
        self._execute_all_steps(guardrails)
        self._finalize()

    # --- timestamps ---

    def _stamp(self) -> str:
        return datetime.now(self.TZ).strftime("%Y-%m-%dT%H:%M:%S%z")

    # --- JSON I/O ---

    @staticmethod
    def _read_json(p: Path) -> dict:
        return json.loads(p.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(p: Path, data: dict):
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # --- git ---

    def _run_git(self, *args, text: bool = True) -> subprocess.CompletedProcess:
        cmd = ["git", *args]
        return subprocess.run(cmd, cwd=self._root, capture_output=True, text=text)

    def _checkout_branch(self):
        branch = f"feat-{self._phase_name}"

        r = self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        if r.returncode != 0:
            print("  ERROR: git을 사용할 수 없거나 git repo가 아닙니다.")
            print(f"  {r.stderr.strip()}")
            sys.exit(1)

        if r.stdout.strip() == branch:
            return

        r = self._run_git("rev-parse", "--verify", branch)
        r = (
            self._run_git("checkout", branch)
            if r.returncode == 0
            else self._run_git("checkout", "-b", branch)
        )

        if r.returncode != 0:
            print(f"  ERROR: 브랜치 '{branch}' checkout 실패.")
            print(f"  {r.stderr.strip()}")
            print("  Hint: 변경사항을 stash하거나 commit한 후 다시 시도하세요.")
            sys.exit(1)

        print(f"  Branch: {branch}")

    def _check_clean_tree(self):
        """phases/ 밖에 커밋되지 않은 변경이 있으면 중단한다. step 커밋이 git add -A로 전부 담기 때문이다."""
        r = self._run_git("status", "--porcelain", "--", ".", ":(exclude)phases")
        if r.returncode != 0:
            print("  ERROR: git을 사용할 수 없거나 git repo가 아닙니다.")
            print(f"  {r.stderr.strip()}")
            sys.exit(1)
        if r.stdout.strip():
            print(
                "  ERROR: phases/ 밖에 커밋되지 않은 변경이 있습니다. step 커밋에 섞이지 않도록 commit 또는 stash 후 다시 실행하세요."
            )
            print(r.stdout.rstrip())
            sys.exit(1)

    def _commit_step(
        self, step_num: int, step_name: str, outcome: str = "completed"
    ) -> Optional[str]:
        """코드와 메타데이터를 나눠 커밋한다. 코드 커밋이 실패하면 (pre-commit 훅 등) 에러 메시지를 반환한다."""
        output_rel = f"phases/{self._phase_dir_name}/step{step_num}-output.json"
        index_rel = f"phases/{self._phase_dir_name}/index.json"

        self._run_git("add", "-A")
        self._run_git("reset", "HEAD", "--", output_rel)
        self._run_git("reset", "HEAD", "--", index_rel)

        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            if outcome == "completed":
                msg = self.FEAT_MSG.format(phase=self._phase_name, num=step_num, name=step_name)
            else:
                msg = self.WIP_MSG.format(
                    phase=self._phase_name, num=step_num, name=step_name, outcome=outcome
                )
            r = self._run_git("commit", "-m", msg)
            if r.returncode != 0:
                output = ANSI_RE.sub("", r.stdout + r.stderr)
                return f"코드 커밋 실패 (exit {r.returncode}):\n{output[-2000:]}"
            print(f"  Commit: {msg}")

        self._run_git("add", "-A")
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = self.CHORE_MSG.format(phase=self._phase_name, num=step_num)
            r = self._run_git("commit", "-m", msg)
            if r.returncode != 0:
                # phases/ 파일만 남으므로 다음 커밋이 함께 담는다
                print(f"  WARN: housekeeping 커밋 실패: {(r.stdout + r.stderr).strip()[-500:]}")
        return None

    # --- top-level index ---

    def _update_top_index(self, status: str):
        if not self._top_index_file.exists():
            return
        top = self._read_json(self._top_index_file)
        ts = self._stamp()
        for phase in top.get("phases", []):
            if phase.get("dir") == self._phase_dir_name:
                phase["status"] = status
                ts_key = {
                    "completed": "completed_at",
                    "error": "failed_at",
                    "blocked": "blocked_at",
                }.get(status)
                if ts_key:
                    phase[ts_key] = ts
                break
        self._write_json(self._top_index_file, top)

    # --- guardrails & context ---

    def _load_guardrails(self) -> str:
        sections = []
        rules = ROOT / "AGENTS.md"
        if rules.exists():
            sections.append(f"## 프로젝트 규칙 (AGENTS.md)\n\n{rules.read_text(encoding='utf-8')}")
        docs_dir = ROOT / "docs"
        if docs_dir.is_dir():
            for doc in sorted(docs_dir.glob("*.md")):
                sections.append(f"## {doc.stem}\n\n{doc.read_text(encoding='utf-8')}")
        return "\n\n---\n\n".join(sections) if sections else ""

    @staticmethod
    def _build_step_context(index: dict) -> str:
        lines = [
            f"- Step {s['step']} ({s['name']}): {s['summary']}"
            for s in index["steps"]
            if s["status"] == "completed" and s.get("summary")
        ]
        if not lines:
            return ""
        return "## 이전 Step 산출물\n\n" + "\n".join(lines) + "\n\n"

    def _build_preamble(
        self, guardrails: str, step_context: str, prev_error: Optional[str] = None
    ) -> str:
        retry_section = ""
        if prev_error:
            retry_section = (
                f"\n## ⚠ 이전 시도 실패 — 아래 에러를 반드시 참고하여 수정하라\n\n"
                f"{prev_error}\n\n"
                f"에러 출력이 수정 커맨드(예: `ruff format <file>`, `... --fix`)를 제시하면 "
                f"추측으로 고치지 말고 그 커맨드를 먼저 실행한 뒤 AC를 다시 확인하라.\n\n---\n\n"
            )
        return (
            f"당신은 {self._project} 프로젝트의 개발자입니다. 아래 step을 수행하세요.\n\n"
            f"{guardrails}\n\n---\n\n"
            f"{step_context}{retry_section}"
            f"## 작업 규칙\n\n"
            f"1. 이전 step에서 작성된 코드를 확인하고 일관성을 유지하라.\n"
            f"2. 이 step에 명시된 작업만 수행하라. 추가 기능이나 파일을 만들지 마라.\n"
            f"3. 기존 테스트를 깨뜨리지 마라.\n"
            f"4. AC(Acceptance Criteria) 검증을 직접 실행하라.\n"
            f"5. /phases/{self._phase_dir_name}/index.json의 해당 step status를 업데이트하라:\n"
            f'   - AC 통과 → "completed" + "summary" 필드에 이 step의 산출물을 한 줄로 요약\n'
            f'   - 수정해도 AC를 통과시키지 못함 → "error" + "error_message"에 실패 원인을 구체적으로 기록 '
            f"(execute.py가 이 메시지를 전달해 새 세션으로 재시도한다)\n"
            f'   - 사용자 개입이 필요한 경우 (API 키, 인증, 수동 설정 등) → "blocked" + "blocked_reason" 기록 후 즉시 중단\n'
            f"6. git commit/push를 하지 마라. 커밋은 execute.py가 step 종료 후 코드와 메타데이터를 나눠 수행한다.\n"
            f"7. lint·타입 검사를 억제 주석(`# noqa`, `# type: ignore`, `eslint-disable`, `@ts-ignore` 등)이나 "
            f'린트 설정 변경으로 우회하지 마라. 규칙 위반은 코드를 고쳐서 해결하고, 고칠 수 없으면 "error"로 기록하고 사유를 남겨라.\n\n---\n\n'
        )

    # --- Claude 호출 ---

    def _invoke_claude(self, step: dict, preamble: str) -> dict:
        step_num, step_name = step["step"], step["name"]
        step_file = self._phase_dir / f"step{step_num}.md"

        if not step_file.exists():
            print(f"  ERROR: {step_file} not found")
            sys.exit(1)

        prompt = preamble + step_file.read_text(encoding="utf-8")
        timed_out = False
        try:
            result = subprocess.run(
                [
                    "claude",
                    "-p",
                    "--dangerously-skip-permissions",
                    "--output-format",
                    "json",
                ],
                input=prompt,
                cwd=self._root,
                capture_output=True,
                text=True,
                timeout=self.TIMEOUT,
            )
            exit_code, stdout, stderr = result.returncode, result.stdout, result.stderr
        except FileNotFoundError:
            print("  ERROR: claude CLI를 찾을 수 없습니다. PATH를 확인하세요.")
            sys.exit(1)
        except subprocess.TimeoutExpired as e:
            timed_out = True
            exit_code, stdout, stderr = None, e.stdout or "", e.stderr or ""
            print(f"\n  WARN: Claude가 {self.TIMEOUT}s 안에 끝나지 않아 종료됨")

        if exit_code not in (0, None):
            print(f"\n  WARN: Claude가 비정상 종료됨 (code {exit_code})")
            if stderr:
                print(f"  stderr: {stderr[:500]}")

        output = {
            "step": step_num,
            "name": step_name,
            "exitCode": exit_code,
            "timedOut": timed_out,
            "stdout": stdout,
            "stderr": stderr,
        }
        out_path = self._phase_dir / f"step{step_num}-output.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        return output

    # --- 헤더 & 검증 ---

    def _print_header(self):
        print(f"\n{'=' * 60}")
        print("  Harness Step Executor")
        print(f"  Phase: {self._phase_name} | Steps: {self._total}")
        if self._auto_push:
            print("  Auto-push: enabled")
        print(f"{'=' * 60}")

    def _check_blockers(self):
        index = self._read_json(self._index_file)
        for s in reversed(index["steps"]):
            if s["status"] == "error":
                print(f"\n  ✗ Step {s['step']} ({s['name']}) failed.")
                print(f"  Error: {s.get('error_message', 'unknown')}")
                print("  Fix and reset status to 'pending' to retry.")
                sys.exit(1)
            if s["status"] == "blocked":
                print(f"\n  ⏸ Step {s['step']} ({s['name']}) blocked.")
                print(f"  Reason: {s.get('blocked_reason', 'unknown')}")
                print("  Resolve and reset status to 'pending' to retry.")
                sys.exit(2)
            if s["status"] != "pending":
                break

    def _ensure_created_at(self):
        index = self._read_json(self._index_file)
        if "created_at" not in index:
            index["created_at"] = self._stamp()
            self._write_json(self._index_file, index)

    # --- Acceptance Criteria ---

    @staticmethod
    def _extract_ac(step_md: str) -> Optional[str]:
        """'## Acceptance Criteria' 절의 첫 ```bash 블록을 반환한다. 없으면 None."""
        section = re.search(
            r"^## Acceptance Criteria[ \t]*\n(.*?)(?=^## |\Z)", step_md, re.M | re.S
        )
        if not section:
            return None
        block = re.search(r"^```(?:bash|sh)[ \t]*\n(.*?)^```", section.group(1), re.M | re.S)
        return block.group(1) if block else None

    def _run_ac(self, script: str) -> Optional[str]:
        """AC 커맨드를 직접 실행한다. 통과면 None, 실패면 에러 메시지.
        step 세션의 completed 보고를 그대로 믿지 않기 위한 재검증이다."""
        try:
            r = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", script],
                cwd=self._root,
                capture_output=True,
                text=True,
                timeout=self.TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return f"AC 재검증이 {self.TIMEOUT}s 안에 끝나지 않음"
        if r.returncode == 0:
            return None
        return f"AC 재검증 실패 (exit {r.returncode}):\n{(r.stdout + r.stderr)[-2000:]}"

    def _find_suppressions(self) -> Optional[str]:
        """이번 step이 추가한 줄에서 lint·타입 억제 주석을 찾는다 (작업 규칙 7의 강제). 없으면 None.
        phases/와 문서(*.md)는 규칙을 설명할 수 있으므로 제외한다."""
        self._run_git("add", "-A")
        diff = self._run_git(
            "-c",
            "core.quotePath=false",
            "diff",
            "--cached",
            "-U0",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            # -diff/binary 속성으로 추가된 줄이 숨지 않게 한다
            "--text",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--",
            ".",
            ":(exclude)phases",
            ":(exclude)*.md",
            text=False,
        ).stdout.decode("utf-8", "replace")
        hits, path, line_no, in_header = [], None, 0, False
        # text 모드는 \r을, splitlines()는 \x0c 등을 줄바꿈으로 취급해 바이너리 바이트가 헤더로 읽히거나
        # 주석이 줄에서 떨어져 나간다 — bytes로 받아 \n으로만 나눈다
        for line in diff.split("\n"):
            if line.startswith("diff --git "):
                path, in_header = None, True
            elif in_header and line.startswith("+++ "):
                if line.startswith("+++ b/"):
                    path = line[6:]
                elif line != "+++ /dev/null":
                    # 해석 못 한 헤더를 건너뛰면 그 파일의 억제 주석이 조용히 통과한다
                    return (
                        "diff 헤더를 해석할 수 없어 억제 주석을 검사하지 못함 — "
                        f"파일명에 따옴표·탭·백슬래시가 있으면 이름을 바꿔라: {line}"
                    )
            elif line.startswith("@@"):
                in_header = False
                line_no = int(re.search(r"\+(\d+)", line).group(1))
            elif not in_header and line.startswith("+"):
                if path and self.SUPPRESSION_RE.search(line):
                    hits.append(f"{path}:{line_no}: {line[1:].strip()}")
                line_no += 1
        if not hits:
            return None
        return "억제 주석 추가 금지 (작업 규칙 7) — 주석을 지우고 코드를 고쳐라:\n" + "\n".join(
            hits[:20]
        )

    # --- 실행 루프 ---

    def _reload_index(self, snapshot: str) -> tuple:
        """step 세션 종료 후 index를 다시 읽는다. 세션이 JSON을 파손했으면 실행 전 상태로 복구하고 에러 메시지를 함께 반환한다."""
        try:
            return self._read_json(self._index_file), None
        except json.JSONDecodeError as e:
            self._index_file.write_text(snapshot, encoding="utf-8")
            return self._read_json(self._index_file), (
                f"step 세션이 index.json을 파손함 ({e}). 실행 전 상태로 복구했다. JSON 형식을 지켜 수정하라."
            )

    def _describe_no_status(self, output: dict) -> str:
        if output.get("timedOut"):
            return f"step 세션이 {self.TIMEOUT}s 안에 끝나지 않음"
        if output.get("exitCode") != 0:
            return f"step 세션 비정상 종료 (exit {output.get('exitCode')}): {output.get('stderr', '')[-500:]}"
        return "Step did not update status"

    def _commit_wip_or_warn(self, step_num: int, step_name: str, outcome: str):
        err = self._commit_step(step_num, step_name, outcome=outcome)
        if err:
            print(
                f"  WARN: 부분 작업 커밋 실패 — 재실행 전에 직접 commit 또는 stash가 필요합니다.\n{err}"
            )

    def _execute_single_step(self, step: dict, guardrails: str) -> bool:
        """단일 step 실행 (재시도 포함). 완료되면 True, 실패/차단이면 프로세스를 종료한다."""
        step_num, step_name = step["step"], step["name"]
        step_file = self._phase_dir / f"step{step_num}.md"
        if not step_file.exists():
            print(f"  ERROR: {step_file} not found")
            sys.exit(1)
        ac_script = self._extract_ac(step_file.read_text(encoding="utf-8"))
        if ac_script is None:
            print(f"  ERROR: {step_file}의 '## Acceptance Criteria'에 ```bash 블록이 없습니다.")
            sys.exit(1)
        done = sum(
            1 for s in self._read_json(self._index_file)["steps"] if s["status"] == "completed"
        )
        prev_error = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            index = self._read_json(self._index_file)
            step_context = self._build_step_context(index)
            preamble = self._build_preamble(guardrails, step_context, prev_error)

            tag = f"Step {step_num}/{self._total - 1} ({done} done): {step_name}"
            if attempt > 1:
                tag += f" [retry {attempt}/{self.MAX_RETRIES}]"

            snapshot = self._index_file.read_text(encoding="utf-8")
            with progress_indicator(tag) as pi:
                output = self._invoke_claude(step, preamble)
            elapsed = int(pi.elapsed)

            index, err_msg = self._reload_index(snapshot)
            s = next(x for x in index["steps"] if x["step"] == step_num)
            status = s.get("status", "pending")
            ts = self._stamp()

            if status == "completed":
                err_msg = self._run_ac(ac_script) or self._find_suppressions()
                if err_msg is None:
                    s["completed_at"] = ts
                    self._write_json(self._index_file, index)
                    err_msg = self._commit_step(step_num, step_name)
                    if err_msg is None:
                        print(f"  ✓ Step {step_num}: {step_name} [{elapsed}s]")
                        return True

            if status == "blocked":
                s["blocked_at"] = ts
                self._write_json(self._index_file, index)
                print(f"  ⏸ Step {step_num}: {step_name} blocked [{elapsed}s]")
                print(f"    Reason: {s.get('blocked_reason', '')}")
                self._update_top_index("blocked")
                self._commit_wip_or_warn(step_num, step_name, "blocked")
                sys.exit(2)

            if err_msg is None:
                err_msg = s.get("error_message") or self._describe_no_status(output)

            if attempt < self.MAX_RETRIES:
                s["status"] = "pending"
                for key in ("error_message", "summary", "completed_at"):
                    s.pop(key, None)
                self._write_json(self._index_file, index)
                prev_error = err_msg
                print(f"  ↻ Step {step_num}: retry {attempt}/{self.MAX_RETRIES} — {err_msg}")
            else:
                s["status"] = "error"
                s["error_message"] = f"[{self.MAX_RETRIES}회 시도 후 실패] {err_msg}"
                s["failed_at"] = ts
                self._write_json(self._index_file, index)
                print(
                    f"  ✗ Step {step_num}: {step_name} failed after {self.MAX_RETRIES} attempts [{elapsed}s]"
                )
                print(f"    Error: {err_msg}")
                self._update_top_index("error")
                self._commit_wip_or_warn(step_num, step_name, "error")
                sys.exit(1)

        return False  # unreachable

    def _execute_all_steps(self, guardrails: str):
        while True:
            index = self._read_json(self._index_file)
            pending = next((s for s in index["steps"] if s["status"] == "pending"), None)
            if pending is None:
                print("\n  All steps completed!")
                return

            step_num = pending["step"]
            for s in index["steps"]:
                if s["step"] == step_num and "started_at" not in s:
                    s["started_at"] = self._stamp()
                    self._write_json(self._index_file, index)
                    break

            self._execute_single_step(pending, guardrails)

    def _finalize(self):
        index = self._read_json(self._index_file)
        index["completed_at"] = self._stamp()
        self._write_json(self._index_file, index)
        self._update_top_index("completed")

        self._run_git("add", "-A")
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = f"chore({self._phase_name}): mark phase completed"
            r = self._run_git("commit", "-m", msg)
            if r.returncode != 0:
                print(f"\n  ERROR: 완료 커밋 실패: {(r.stdout + r.stderr).strip()[-2000:]}")
                sys.exit(1)
            print(f"  ✓ {msg}")

        if self._auto_push:
            branch = f"feat-{self._phase_name}"
            r = self._run_git("push", "-u", "origin", branch)
            if r.returncode != 0:
                print(f"\n  ERROR: git push 실패: {r.stderr.strip()}")
                sys.exit(1)
            print(f"  ✓ Pushed to origin/{branch}")

        print(f"\n{'=' * 60}")
        print(f"  Phase '{self._phase_name}' completed!")
        print(f"{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(description="Harness Step Executor")
    parser.add_argument("phase_dir", help="Phase directory name (e.g. 0-mvp)")
    parser.add_argument("--push", action="store_true", help="Push branch after completion")
    args = parser.parse_args()

    StepExecutor(args.phase_dir, auto_push=args.push).run()


if __name__ == "__main__":
    main()
