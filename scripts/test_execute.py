"""
execute.py 리팩터링 안전망 테스트.
리팩터링 전후 동작이 동일한지 검증한다.
"""

import json
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import execute as ex

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_project(tmp_path):
    """phases/, AGENTS.md, docs/ 를 갖춘 임시 프로젝트 구조."""
    phases_dir = tmp_path / "phases"
    phases_dir.mkdir()

    rules = tmp_path / "AGENTS.md"
    rules.write_text("# Rules\n- rule one\n- rule two")

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "arch.md").write_text("# Architecture\nSome content")
    (docs_dir / "guide.md").write_text("# Guide\nAnother doc")

    return tmp_path


@pytest.fixture
def phase_dir(tmp_project):
    """step 3개를 가진 phase 디렉토리."""
    d = tmp_project / "phases" / "0-mvp"
    d.mkdir()

    index = {
        "project": "TestProject",
        "phase": "mvp",
        "steps": [
            {"step": 0, "name": "setup", "status": "completed", "summary": "프로젝트 초기화 완료"},
            {"step": 1, "name": "core", "status": "completed", "summary": "핵심 로직 구현"},
            {"step": 2, "name": "ui", "status": "pending"},
        ],
    }
    (d / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False))
    (d / "step2.md").write_text("# Step 2: UI\n\nUI를 구현하세요.")

    return d


@pytest.fixture
def top_index(tmp_project):
    """phases/index.json (top-level)."""
    top = {
        "phases": [
            {"dir": "0-mvp", "status": "pending"},
            {"dir": "1-polish", "status": "pending"},
        ]
    }
    p = tmp_project / "phases" / "index.json"
    p.write_text(json.dumps(top, indent=2))
    return p


@pytest.fixture
def executor(tmp_project, phase_dir):
    """테스트용 StepExecutor 인스턴스. git 호출은 별도 mock 필요."""
    with patch.object(ex, "ROOT", tmp_project):
        inst = ex.StepExecutor("0-mvp")
    # 내부 경로를 tmp_project 기준으로 재설정
    inst._root = str(tmp_project)
    inst._phases_dir = tmp_project / "phases"
    inst._phase_dir = phase_dir
    inst._phase_dir_name = "0-mvp"
    inst._index_file = phase_dir / "index.json"
    inst._top_index_file = tmp_project / "phases" / "index.json"
    return inst


# ---------------------------------------------------------------------------
# _stamp (= 이전 now_iso)
# ---------------------------------------------------------------------------


class TestStamp:
    def test_returns_kst_timestamp(self, executor):
        result = executor._stamp()
        assert "+0900" in result

    def test_format_is_iso(self, executor):
        result = executor._stamp()
        dt = datetime.strptime(result, "%Y-%m-%dT%H:%M:%S%z")
        assert dt.tzinfo is not None

    def test_is_current_time(self, executor):
        before = datetime.now(ex.StepExecutor.TZ).replace(microsecond=0)
        result = executor._stamp()
        after = datetime.now(ex.StepExecutor.TZ).replace(microsecond=0) + timedelta(seconds=1)
        parsed = datetime.strptime(result, "%Y-%m-%dT%H:%M:%S%z")
        assert before <= parsed <= after


# ---------------------------------------------------------------------------
# _read_json / _write_json
# ---------------------------------------------------------------------------


class TestJsonHelpers:
    def test_roundtrip(self, tmp_path):
        data = {"key": "값", "nested": [1, 2, 3]}
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, data)
        loaded = ex.StepExecutor._read_json(p)
        assert loaded == data

    def test_save_ensures_ascii_false(self, tmp_path):
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, {"한글": "테스트"})
        raw = p.read_text()
        assert "한글" in raw
        assert "\\u" not in raw

    def test_save_indented(self, tmp_path):
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, {"a": 1})
        raw = p.read_text()
        assert "\n" in raw

    def test_load_nonexistent_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ex.StepExecutor._read_json(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# _load_guardrails
# ---------------------------------------------------------------------------


class TestLoadGuardrails:
    def test_loads_agents_md_and_docs(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "# Rules" in result
        assert "rule one" in result
        assert "# Architecture" in result
        assert "# Guide" in result

    def test_sections_separated_by_divider(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "---" in result

    def test_docs_sorted_alphabetically(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        arch_pos = result.index("arch")
        guide_pos = result.index("guide")
        assert arch_pos < guide_pos

    def test_no_agents_md(self, executor, tmp_project):
        (tmp_project / "AGENTS.md").unlink()
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "AGENTS.md" not in result
        assert "Architecture" in result

    def test_no_docs_dir(self, executor, tmp_project):
        import shutil

        shutil.rmtree(tmp_project / "docs")
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "Rules" in result
        assert "Architecture" not in result

    def test_empty_project(self, tmp_path):
        with patch.object(ex, "ROOT", tmp_path):
            # executor가 필요 없는 static-like 동작이므로 임시 인스턴스
            phases_dir = tmp_path / "phases" / "dummy"
            phases_dir.mkdir(parents=True)
            idx = {"project": "T", "phase": "t", "steps": []}
            (phases_dir / "index.json").write_text(json.dumps(idx))
            inst = ex.StepExecutor.__new__(ex.StepExecutor)
            result = inst._load_guardrails()
        assert result == ""


# ---------------------------------------------------------------------------
# _build_step_context
# ---------------------------------------------------------------------------


class TestBuildStepContext:
    def test_includes_completed_with_summary(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text())
        result = ex.StepExecutor._build_step_context(index)
        assert "Step 0 (setup): 프로젝트 초기화 완료" in result
        assert "Step 1 (core): 핵심 로직 구현" in result

    def test_excludes_pending(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text())
        result = ex.StepExecutor._build_step_context(index)
        assert "ui" not in result

    def test_excludes_completed_without_summary(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text())
        del index["steps"][0]["summary"]
        result = ex.StepExecutor._build_step_context(index)
        assert "setup" not in result
        assert "core" in result

    def test_empty_when_no_completed(self):
        index = {"steps": [{"step": 0, "name": "a", "status": "pending"}]}
        result = ex.StepExecutor._build_step_context(index)
        assert result == ""

    def test_has_header(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text())
        result = ex.StepExecutor._build_step_context(index)
        assert result.startswith("## 이전 Step 산출물")


# ---------------------------------------------------------------------------
# _build_preamble
# ---------------------------------------------------------------------------


class TestBuildPreamble:
    def test_includes_project_name(self, executor):
        result = executor._build_preamble("", "")
        assert "TestProject" in result

    def test_includes_guardrails(self, executor):
        result = executor._build_preamble("GUARD_CONTENT", "")
        assert "GUARD_CONTENT" in result

    def test_includes_step_context(self, executor):
        ctx = "## 이전 Step 산출물\n\n- Step 0: done"
        result = executor._build_preamble("", ctx)
        assert "이전 Step 산출물" in result

    def test_forbids_session_commit(self, executor):
        result = executor._build_preamble("", "")
        assert "git commit/push를 하지 마라" in result
        assert "모든 변경사항을 커밋하라" not in result

    def test_forbids_lint_suppression(self, executor):
        result = executor._build_preamble("", "")
        for marker in ("# noqa", "# type: ignore", "eslint-disable", "@ts-ignore"):
            assert marker in result
        assert "우회하지 마라" in result

    def test_includes_rules(self, executor):
        result = executor._build_preamble("", "")
        assert "작업 규칙" in result
        assert "AC" in result

    def test_no_retry_section_by_default(self, executor):
        result = executor._build_preamble("", "")
        assert "이전 시도 실패" not in result

    def test_retry_section_with_prev_error(self, executor):
        result = executor._build_preamble("", "", prev_error="타입 에러 발생")
        assert "이전 시도 실패" in result
        assert "타입 에러 발생" in result
        assert "그 커맨드를 먼저 실행" in result

    def test_no_in_session_retry_count(self, executor):
        # 재시도 횟수는 execute.py만 관리한다 (세션 내 재시도와 곱해지지 않도록)
        result = executor._build_preamble("", "")
        assert "회 수정 시도" not in result

    def test_includes_index_path(self, executor):
        result = executor._build_preamble("", "")
        assert "/phases/0-mvp/index.json" in result


# ---------------------------------------------------------------------------
# _update_top_index
# ---------------------------------------------------------------------------


class TestUpdateTopIndex:
    def test_completed(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("completed")
        data = json.loads(top_index.read_text())
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "completed"
        assert "completed_at" in mvp

    def test_error(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("error")
        data = json.loads(top_index.read_text())
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "error"
        assert "failed_at" in mvp

    def test_blocked(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("blocked")
        data = json.loads(top_index.read_text())
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "blocked"
        assert "blocked_at" in mvp

    def test_other_phases_unchanged(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("completed")
        data = json.loads(top_index.read_text())
        polish = next(p for p in data["phases"] if p["dir"] == "1-polish")
        assert polish["status"] == "pending"

    def test_nonexistent_dir_is_noop(self, executor, top_index):
        executor._top_index_file = top_index
        executor._phase_dir_name = "no-such-dir"
        original = json.loads(top_index.read_text())
        executor._update_top_index("completed")
        after = json.loads(top_index.read_text())
        for p_before, p_after in zip(original["phases"], after["phases"]):
            assert p_before["status"] == p_after["status"]

    def test_no_top_index_file(self, executor, tmp_path):
        executor._top_index_file = tmp_path / "nonexistent.json"
        executor._update_top_index("completed")  # should not raise


# ---------------------------------------------------------------------------
# _checkout_branch (mocked)
# ---------------------------------------------------------------------------


class TestCheckoutBranch:
    def _mock_git(self, executor, responses):
        call_idx = {"i": 0}

        def fake_git(*args):
            idx = call_idx["i"]
            call_idx["i"] += 1
            if idx < len(responses):
                return responses[idx]
            return MagicMock(returncode=0, stdout="", stderr="")

        executor._run_git = fake_git

    def test_already_on_branch(self, executor):
        self._mock_git(
            executor,
            [
                MagicMock(returncode=0, stdout="feat-mvp\n", stderr=""),
            ],
        )
        executor._checkout_branch()  # should return without checkout

    def test_branch_exists_checkout(self, executor):
        self._mock_git(
            executor,
            [
                MagicMock(returncode=0, stdout="main\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ],
        )
        executor._checkout_branch()

    def test_branch_not_exists_create(self, executor):
        self._mock_git(
            executor,
            [
                MagicMock(returncode=0, stdout="main\n", stderr=""),
                MagicMock(returncode=1, stdout="", stderr="not found"),
                MagicMock(returncode=0, stdout="", stderr=""),
            ],
        )
        executor._checkout_branch()

    def test_checkout_fails_exits(self, executor):
        self._mock_git(
            executor,
            [
                MagicMock(returncode=0, stdout="main\n", stderr=""),
                MagicMock(returncode=1, stdout="", stderr=""),
                MagicMock(returncode=1, stdout="", stderr="dirty tree"),
            ],
        )
        with pytest.raises(SystemExit) as exc_info:
            executor._checkout_branch()
        assert exc_info.value.code == 1

    def test_no_git_exits(self, executor):
        self._mock_git(
            executor,
            [
                MagicMock(returncode=1, stdout="", stderr="not a git repo"),
            ],
        )
        with pytest.raises(SystemExit) as exc_info:
            executor._checkout_branch()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _commit_step (mocked)
# ---------------------------------------------------------------------------


class TestCommitStep:
    def test_code_commit_failure_returns_message_and_skips_chore(self, executor):
        calls = []

        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                return MagicMock(returncode=1)
            if args[0] == "commit":
                return MagicMock(
                    returncode=1, stdout="\x1b[0;31mruff: F401 unused import\x1b[0m", stderr=""
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        executor._run_git = fake_git

        err = executor._commit_step(2, "ui")

        assert "코드 커밋 실패" in err
        assert "F401" in err
        assert "\x1b" not in err
        assert len([c for c in calls if c[0] == "commit"]) == 1

    def test_success_returns_none(self, executor):
        executor._run_git = lambda *a: MagicMock(returncode=0, stdout="", stderr="")
        assert executor._commit_step(2, "ui") is None

    def test_two_phase_commit(self, executor):
        calls = []

        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")

        executor._run_git = fake_git

        executor._commit_step(2, "ui")

        commit_calls = [c for c in calls if c[0] == "commit"]
        assert len(commit_calls) == 2
        assert "feat(mvp):" in commit_calls[0][2]
        assert "chore(mvp):" in commit_calls[1][2]

    def test_no_code_changes_skips_feat_commit(self, executor):
        call_count = {"diff": 0}
        calls = []

        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                call_count["diff"] += 1
                if call_count["diff"] == 1:
                    return MagicMock(returncode=0)
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")

        executor._run_git = fake_git

        executor._commit_step(2, "ui")

        commit_msgs = [c[2] for c in calls if c[0] == "commit"]
        assert len(commit_msgs) == 1
        assert "chore" in commit_msgs[0]


# ---------------------------------------------------------------------------
# _invoke_claude (mocked)
# ---------------------------------------------------------------------------


class TestInvokeClaude:
    def test_invokes_claude_with_correct_args(self, executor):
        mock_result = MagicMock(returncode=0, stdout='{"result": "ok"}', stderr="")
        step = {"step": 2, "name": "ui"}
        preamble = "PREAMBLE\n"

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            executor._invoke_claude(step, preamble)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--output-format" in cmd
        # 프롬프트는 argv가 아니라 stdin으로 전달한다 (인자 길이 제한, stdin 대기 회피)
        prompt = mock_run.call_args[1]["input"]
        assert "PREAMBLE" in prompt
        assert "UI를 구현하세요" in prompt
        assert not any("PREAMBLE" in arg for arg in cmd)

    def test_saves_output_json(self, executor):
        mock_result = MagicMock(returncode=0, stdout='{"ok": true}', stderr="")
        step = {"step": 2, "name": "ui"}

        with patch("subprocess.run", return_value=mock_result):
            executor._invoke_claude(step, "preamble")

        output_file = executor._phase_dir / "step2-output.json"
        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert data["step"] == 2
        assert data["name"] == "ui"
        assert data["exitCode"] == 0

    def test_nonexistent_step_file_exits(self, executor):
        step = {"step": 99, "name": "nonexistent"}
        with pytest.raises(SystemExit) as exc_info:
            executor._invoke_claude(step, "preamble")
        assert exc_info.value.code == 1

    def test_timeout_is_1800(self, executor):
        mock_result = MagicMock(returncode=0, stdout="{}", stderr="")
        step = {"step": 2, "name": "ui"}

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            executor._invoke_claude(step, "preamble")

        assert mock_run.call_args[1]["timeout"] == 1800


# ---------------------------------------------------------------------------
# progress_indicator (= 이전 Spinner)
# ---------------------------------------------------------------------------


class TestProgressIndicator:
    def test_context_manager(self):
        import time

        with ex.progress_indicator("test") as pi:
            time.sleep(0.15)
        assert pi.elapsed >= 0.1

    def test_elapsed_increases(self):
        import time

        with ex.progress_indicator("test") as pi:
            time.sleep(0.2)
        assert pi.elapsed > 0


# ---------------------------------------------------------------------------
# main() CLI 파싱 (mocked)
# ---------------------------------------------------------------------------


class TestMainCli:
    def test_no_args_exits(self):
        with patch("sys.argv", ["execute.py"]):
            with pytest.raises(SystemExit) as exc_info:
                ex.main()
            assert exc_info.value.code == 2  # argparse exits with 2

    def test_invalid_phase_dir_exits(self, tmp_path):
        with (
            patch("sys.argv", ["execute.py", "nonexistent"]),
            patch.object(ex, "ROOT", tmp_path / "fake_nonexistent"),
            pytest.raises(SystemExit) as exc_info,
        ):
            ex.main()
        assert exc_info.value.code == 1

    def test_missing_index_exits(self, tmp_project):
        (tmp_project / "phases" / "empty").mkdir()
        with (
            patch("sys.argv", ["execute.py", "empty"]),
            patch.object(ex, "ROOT", tmp_project),
            pytest.raises(SystemExit) as exc_info,
        ):
            ex.main()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _check_blockers (= 이전 main() error/blocked 체크)
# ---------------------------------------------------------------------------


class TestCheckBlockers:
    def _make_executor_with_steps(self, tmp_project, steps):
        d = tmp_project / "phases" / "test-phase"
        d.mkdir(exist_ok=True)
        index = {"project": "T", "phase": "test", "steps": steps}
        (d / "index.json").write_text(json.dumps(index))

        with patch.object(ex, "ROOT", tmp_project):
            inst = ex.StepExecutor.__new__(ex.StepExecutor)
        inst._root = str(tmp_project)
        inst._phases_dir = tmp_project / "phases"
        inst._phase_dir = d
        inst._phase_dir_name = "test-phase"
        inst._index_file = d / "index.json"
        inst._top_index_file = tmp_project / "phases" / "index.json"
        inst._phase_name = "test"
        inst._total = len(steps)
        return inst

    def test_error_step_exits_1(self, tmp_project):
        steps = [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "bad", "status": "error", "error_message": "fail"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        with pytest.raises(SystemExit) as exc_info:
            inst._check_blockers()
        assert exc_info.value.code == 1

    def test_blocked_step_exits_2(self, tmp_project):
        steps = [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "stuck", "status": "blocked", "blocked_reason": "API key"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        with pytest.raises(SystemExit) as exc_info:
            inst._check_blockers()
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# _extract_ac / _run_ac
# ---------------------------------------------------------------------------

STEP_MD_WITH_AC = textwrap.dedent("""\
    # Step 2: UI

    ## 작업

    ```bash
    echo not-ac
    ```

    ## Acceptance Criteria

    ```bash
    {ac}
    ```

    ## 금지사항

    - 없음
    """)


class TestExtractAc:
    def test_extracts_bash_block(self):
        md = STEP_MD_WITH_AC.format(ac="npm run build\nnpm test")
        assert ex.StepExecutor._extract_ac(md) == "npm run build\nnpm test\n"

    def test_ignores_bash_blocks_outside_ac_section(self):
        md = STEP_MD_WITH_AC.format(ac="true")
        assert "not-ac" not in ex.StepExecutor._extract_ac(md)

    def test_none_without_section(self):
        assert ex.StepExecutor._extract_ac("# Step\n\n## 작업\n\n```bash\ntrue\n```\n") is None

    def test_none_without_bash_block(self):
        assert (
            ex.StepExecutor._extract_ac(
                "## Acceptance Criteria\n\n빌드가 돼야 한다\n\n## 금지사항\n"
            )
            is None
        )


class TestRunAc:
    def test_pass_returns_none(self, executor):
        assert executor._run_ac("true\n") is None

    def test_runs_in_project_root(self, executor, tmp_project):
        assert executor._run_ac("test -f AGENTS.md\n") is None

    def test_earlier_failure_is_not_masked(self, executor):
        err = executor._run_ac("false\ntrue\n")
        assert err is not None and "AC 재검증 실패" in err

    def test_pipe_failure_is_not_masked(self, executor):
        assert executor._run_ac("false | cat\n") is not None

    def test_timeout_returns_message(self, executor):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("bash", 1)):
            assert "끝나지 않음" in executor._run_ac("sleep 9\n")


# ---------------------------------------------------------------------------
# _check_clean_tree (real git)
# ---------------------------------------------------------------------------


class TestCheckCleanTree:
    @pytest.fixture
    def repo(self, executor, tmp_project):
        def git(*args):
            subprocess.run(["git", *args], cwd=tmp_project, check=True, capture_output=True)

        git("init", "-q")
        git(
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        )
        git("add", "AGENTS.md", "docs")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "docs")
        return tmp_project

    def test_untracked_phases_allowed(self, executor, repo):
        executor._check_clean_tree()  # phases/ 는 아직 untracked 여도 통과

    def test_dirty_outside_phases_exits(self, executor, repo):
        (repo / "src.txt").write_text("stray change")
        with pytest.raises(SystemExit) as exc_info:
            executor._check_clean_tree()
        assert exc_info.value.code == 1

    def test_modified_tracked_file_exits(self, executor, repo):
        (repo / "AGENTS.md").write_text("edited")
        with pytest.raises(SystemExit):
            executor._check_clean_tree()


# ---------------------------------------------------------------------------
# _commit_step outcome
# ---------------------------------------------------------------------------


class TestCommitStepOutcome:
    @pytest.mark.parametrize("outcome", ["error", "blocked"])
    def test_non_completed_uses_wip(self, executor, outcome):
        calls = []

        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")

        executor._run_git = fake_git

        executor._commit_step(2, "ui", outcome=outcome)

        first_msg = next(c for c in calls if c[0] == "commit")[2]
        assert first_msg.startswith("wip(mvp):")
        assert f"({outcome})" in first_msg


# ---------------------------------------------------------------------------
# _invoke_claude failure modes
# ---------------------------------------------------------------------------


class TestInvokeClaudeFailures:
    def test_timeout_is_recorded_not_raised(self, executor):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 1800)):
            output = executor._invoke_claude({"step": 2, "name": "ui"}, "p")
        assert output["timedOut"] is True
        assert output["exitCode"] is None
        assert (
            json.loads((executor._phase_dir / "step2-output.json").read_text())["timedOut"] is True
        )

    def test_missing_cli_exits_1(self, executor):
        with (
            patch("subprocess.run", side_effect=FileNotFoundError("claude")),
            pytest.raises(SystemExit) as exc_info,
        ):
            executor._invoke_claude({"step": 2, "name": "ui"}, "p")
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _execute_single_step (state machine, claude/git mocked)
# ---------------------------------------------------------------------------


class TestExecuteSingleStep:
    STEP: ClassVar[dict] = {"step": 2, "name": "ui", "status": "pending"}
    OK_OUTPUT: ClassVar[dict] = {"exitCode": 0, "timedOut": False, "stderr": ""}

    @pytest.fixture
    def run(self, executor, phase_dir):
        """session(index_dict) 가 step 세션의 index 수정을 흉내낸다. 호출 기록을 반환."""
        rec = {"prompts": [], "commits": [], "commit_errors": []}

        def fake_commit(num, name, outcome="completed"):
            rec["commits"].append(outcome)
            return rec["commit_errors"].pop(0) if rec["commit_errors"] else None

        executor._commit_step = fake_commit

        def setup(session, ac="true", raw_session=None):
            (phase_dir / "step2.md").write_text(STEP_MD_WITH_AC.format(ac=ac))

            def fake_invoke(step, preamble):
                rec["prompts"].append(preamble)
                if raw_session:
                    raw_session(executor._index_file)
                else:
                    idx = json.loads(executor._index_file.read_text())
                    session(next(s for s in idx["steps"] if s["step"] == 2))
                    executor._index_file.write_text(json.dumps(idx, ensure_ascii=False))
                return dict(self.OK_OUTPUT)

            executor._invoke_claude = fake_invoke
            return rec

        return setup

    def _step(self, executor):
        return next(
            s for s in json.loads(executor._index_file.read_text())["steps"] if s["step"] == 2
        )

    def test_completed_and_ac_passes(self, executor, run):
        rec = run(lambda s: s.update(status="completed", summary="UI 완료"))
        assert executor._execute_single_step(self.STEP, "") is True
        assert rec["commits"] == ["completed"]
        assert "completed_at" in self._step(executor)
        assert len(rec["prompts"]) == 1

    def test_false_completed_claim_is_retried_then_errors(self, executor, run):
        rec = run(lambda s: s.update(status="completed", summary="거짓 완료"), ac="false")
        with pytest.raises(SystemExit) as exc_info:
            executor._execute_single_step(self.STEP, "")
        assert exc_info.value.code == 1
        step = self._step(executor)
        assert step["status"] == "error"
        assert "AC 재검증 실패" in step["error_message"]
        assert len(rec["prompts"]) == ex.StepExecutor.MAX_RETRIES
        assert "AC 재검증 실패" in rec["prompts"][1]  # 다음 시도에 피드백됨
        assert rec["commits"] == ["error"]

    def test_recovers_on_retry(self, executor, run):
        attempts = {"n": 0}

        def session(s):
            attempts["n"] += 1
            if attempts["n"] == 1:
                s.update(status="error", error_message="타입 에러")
            else:
                s.update(status="completed", summary="고침")

        rec = run(session)
        assert executor._execute_single_step(self.STEP, "") is True
        assert "타입 에러" in rec["prompts"][1]
        assert rec["commits"] == ["completed"]

    def test_blocked_exits_2_and_commits_wip(self, executor, run):
        rec = run(lambda s: s.update(status="blocked", blocked_reason="API 키 필요"))
        with pytest.raises(SystemExit) as exc_info:
            executor._execute_single_step(self.STEP, "")
        assert exc_info.value.code == 2
        assert "blocked_at" in self._step(executor)
        assert rec["commits"] == ["blocked"]

    def test_corrupted_index_is_restored_and_retried(self, executor, run):
        calls = {"n": 0}

        def raw(index_file):
            calls["n"] += 1
            if calls["n"] == 1:
                index_file.write_text("{ broken")
            else:
                idx = json.loads(index_file.read_text())
                idx["steps"][2].update(status="completed", summary="ok")
                index_file.write_text(json.dumps(idx))

        rec = run(None, raw_session=raw)
        assert executor._execute_single_step(self.STEP, "") is True
        assert "index.json을 파손함" in rec["prompts"][1]

    def test_session_crash_message_is_fed_back(self, executor, run):
        rec = run(lambda s: None)
        crashed = {"exitCode": 1, "timedOut": False, "stderr": "boom"}
        orig = executor._invoke_claude
        executor._invoke_claude = lambda step, p: (orig(step, p), crashed)[1]
        with pytest.raises(SystemExit):
            executor._execute_single_step(self.STEP, "")
        assert "비정상 종료 (exit 1): boom" in rec["prompts"][1]

    def test_missing_ac_block_exits_before_invoking(self, executor, run, phase_dir):
        rec = run(lambda s: None)
        (phase_dir / "step2.md").write_text("# Step 2\n\n## 작업\n\n뭔가 하라\n")
        with pytest.raises(SystemExit) as exc_info:
            executor._execute_single_step(self.STEP, "")
        assert exc_info.value.code == 1
        assert rec["prompts"] == []

    def test_suppression_is_retried_before_commit(self, executor, run):
        rec = run(lambda s: s.update(status="completed", summary="ok"))
        found = ["억제 주석 추가 금지 (작업 규칙 7) — ...:\nhello.py:1: print('hi')  # noqa"]
        executor._find_suppressions = lambda: found.pop(0) if found else None
        assert executor._execute_single_step(self.STEP, "") is True
        assert rec["commits"] == ["completed"]  # 억제 주석이 남은 시도는 커밋하지 않는다
        assert "hello.py:1" in rec["prompts"][1]

    def test_code_commit_failure_is_retried_with_hook_output(self, executor, run):
        rec = run(lambda s: s.update(status="completed", summary="ok"))
        rec["commit_errors"].append("코드 커밋 실패 (exit 1):\nruff E999")
        assert executor._execute_single_step(self.STEP, "") is True
        assert rec["commits"] == ["completed", "completed"]
        assert "ruff E999" in rec["prompts"][1]
        assert "completed_at" in self._step(executor)

    def test_elapsed_is_reported(self, executor, run, capsys):
        run(lambda s: s.update(status="completed", summary="ok"))
        orig = executor._invoke_claude

        def slow_invoke(step, preamble):
            time.sleep(1.05)
            return orig(step, preamble)

        executor._invoke_claude = slow_invoke
        executor._execute_single_step(self.STEP, "")
        assert "[1s]" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _finalize
# ---------------------------------------------------------------------------


class TestFinalize:
    def test_commit_failure_exits_without_push(self, executor):
        calls = []

        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                return MagicMock(returncode=1)
            if args[0] == "commit":
                return MagicMock(returncode=1, stdout="hook failed", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        executor._run_git = fake_git
        executor._auto_push = True
        with pytest.raises(SystemExit) as exc_info:
            executor._finalize()
        assert exc_info.value.code == 1
        assert not any(c[0] == "push" for c in calls)


# ---------------------------------------------------------------------------
# _find_suppressions (real git)
# ---------------------------------------------------------------------------


class TestFindSuppressions:
    @pytest.fixture
    def repo(self, executor, tmp_project):
        def git(*args):
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                cwd=tmp_project,
                check=True,
                capture_output=True,
            )

        git("init", "-q")
        (tmp_project / "old.txt").write_text("x = 1  # noqa: E501\n")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        return tmp_project

    @pytest.mark.parametrize(
        "name, content",
        [
            ("a.py", 'print("hi")  # noqa: T201\n'),
            ("a.py", "x: int = 'a'  # type: ignore\n"),
            ("a.py", "x = 1  # pyright: ignore\n"),
            ("a.py", "# pylint: disable=invalid-name\n"),
            ("src/a.ts", "// @ts-ignore\nconst x: number = 'a';\n"),
            ("src/a.ts", "// @ts-expect-error\n"),
            ("src/a.tsx", "/* eslint-disable */\n"),
            ("src/a.js", "// biome-ignore lint: reason\n"),
            ("src/a.js", "// prettier-ignore\nconst m = [1,0,\n0,1];\n"),
            ("src/a.css", "/* prettier-ignore */\n"),
            ("a.py", "# fmt: off\nm = [1,0,\n0,1]\n# fmt: on\n"),
            ("a.py", "m = [1,0,  0,1]  # fmt: skip\n"),
        ],
    )
    def test_detects_added_suppression(self, executor, repo, name, content):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        err = executor._find_suppressions()
        assert err is not None
        assert "작업 규칙 7" in err
        assert f"{name}:" in err

    def test_reports_line_number(self, executor, repo):
        (repo / "old.txt").write_text("x = 1  # noqa: E501\ny = 2\nz = 3  # noqa\n")
        err = executor._find_suppressions()
        assert "old.txt:3: z = 3  # noqa" in err
        assert "old.txt:1" not in err  # 기존 줄은 이번 step이 추가한 것이 아니다

    def test_ignores_user_diff_prefix_config(self, executor, repo):
        # diff.mnemonicPrefix가 켜져 있으면 헤더가 "+++ i/a.py"가 된다
        subprocess.run(["git", "config", "diff.mnemonicPrefix", "true"], cwd=repo, check=True)
        (repo / "a.py").write_text("x = 1  # noqa\n")
        err = executor._find_suppressions()
        assert err is not None
        assert "a.py:1:" in err

    def test_ignores_user_external_diff_config(self, executor, repo):
        # diff.external이 설정되면 git diff가 patch 대신 외부 도구 출력을 낸다
        subprocess.run(["git", "config", "diff.external", "true"], cwd=repo, check=True)
        (repo / "a.py").write_text("x = 1  # noqa\n")
        err = executor._find_suppressions()
        assert err is not None
        assert "a.py:1:" in err

    def test_ignores_textconv_driver(self, executor, repo):
        # textconv 드라이버가 걸리면 git diff가 변환된 내용을 낸다 — 억제 주석을 지워 숨길 수 있다
        (repo / ".gitattributes").write_text("*.py diff=hide\n")
        subprocess.run(
            ["git", "config", "diff.hide.textconv", "sed s/noqa//"], cwd=repo, check=True
        )
        (repo / "a.py").write_text("x = 1  # noqa\n")
        err = executor._find_suppressions()
        assert err is not None
        assert "a.py:1:" in err

    @pytest.mark.parametrize("attr", ["-diff", "binary"])
    def test_ignores_binary_attribute(self, executor, repo, attr):
        # 바이너리로 표시되면 git diff가 "Binary files ... differ"만 내어 추가된 줄이 사라진다
        (repo / ".gitattributes").write_text(f"*.py {attr}\n")
        (repo / "a.py").write_text("x = 1  # noqa\n")
        err = executor._find_suppressions()
        assert err is not None
        assert "a.py:1:" in err

    def test_non_utf8_file_does_not_crash(self, executor, repo):
        (repo / "k.py").write_bytes("# 한글\nx = 1  # noqa\n".encode("cp949"))
        err = executor._find_suppressions()
        assert err is not None
        assert "k.py:2:" in err

    def test_binary_file_passes(self, executor, repo):
        (repo / "i.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe")
        assert executor._find_suppressions() is None

    def test_detects_suppression_in_non_ascii_path(self, executor, repo):
        # core.quotePath 기본값이면 헤더가 '+++ "b/\355\225\234...py"'로 따옴표 처리된다
        (repo / "한글.py").write_text("x = 1  # noqa\n")
        err = executor._find_suppressions()
        assert err is not None
        assert "한글.py:1:" in err

    def test_added_line_starting_with_plus_plus_is_not_a_header(self, executor, repo):
        # "++ y"를 추가하면 diff 줄이 "+++ y"가 된다 — 헤더로 읽으면 뒤 줄을 놓친다
        (repo / "a.py").write_text("x = 1\n++ y\nz = 2  # noqa\n")
        err = executor._find_suppressions()
        assert err is not None
        assert "a.py:3:" in err

    def test_unparseable_header_fails_closed(self, executor, repo):
        # 따옴표가 든 파일명은 quotePath=false여도 '+++ "b/q\"uote.py"'로 인용된다
        (repo / 'q"uote.py').write_text("x = 1\n")
        err = executor._find_suppressions()
        assert err is not None
        assert "diff 헤더" in err

    def test_deleted_file_passes(self, executor, repo):
        (repo / "old.txt").unlink()
        assert executor._find_suppressions() is None

    def test_clean_change_passes(self, executor, repo):
        (repo / "a.py").write_text("import sys\n\nsys.stdout.write('hi')\n")
        assert executor._find_suppressions() is None

    def test_docs_and_phases_are_excluded(self, executor, repo):
        (repo / "docs" / "lint.md").write_text("`# noqa`를 쓰지 마라\n")
        (repo / "phases" / "0-mvp" / "step9.md").write_text("# noqa 금지\n")
        assert executor._find_suppressions() is None

    def test_no_changes_passes(self, executor, repo):
        assert executor._find_suppressions() is None
