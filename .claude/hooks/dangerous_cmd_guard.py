#!/usr/bin/env python3
"""
PreToolUse(Bash) 가드 — 되돌릴 수 없는 명령을 차단한다.

execute.py는 step 세션을 --dangerously-skip-permissions로 실행하므로 이 훅이 유일한 차단 장치다.
Claude Code는 도구 입력을 stdin JSON으로 넘기고, exit 2일 때만 호출을 막는다(stderr는 Claude에게 전달).

한계: 셸 문자열만 본다. 이름으로 호출한 스크립트 안의 명령이나 난독화된 명령은 통과한다.
"""

import json
import re
import shlex
import sys
from pathlib import PurePosixPath

# 빌드/설치가 다시 만드는 경로 — 삭제가 일상적인 작업이다.
REGENERABLE = {
    "node_modules",
    ".next",
    "out",
    "dist",
    "build",
    "coverage",
    ".turbo",
    "__pycache__",
    ".pytest_cache",
    ".venv",
}
SCRATCH_PREFIXES = ("/tmp/", "/private/tmp/", "$TMPDIR", "${TMPDIR}")
DROP_TABLE_RE = re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE)


def _segments(cmd: str):
    """따옴표를 존중하며 ; && || | ( ) 기준으로 명령을 토큰 리스트 단위로 나눈다."""
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError:
        tokens = cmd.split()
    seg = []
    for tok in tokens:
        if tok and set(tok) <= set(";&|()<>"):
            yield seg
            seg = []
        else:
            seg.append(tok)
    yield seg


def _is_exempt(target: str) -> bool:
    if target.startswith(SCRATCH_PREFIXES):
        return True
    return PurePosixPath(target.rstrip("/")).name in REGENERABLE


def _check_rm(args: list) -> bool:
    """rm 인자 목록이 재귀+강제 삭제이고 예외 경로가 아닌 대상을 포함하면 True."""
    short, long_flags, targets = "", set(), []
    end_of_opts = False
    for a in args:
        if end_of_opts or not a.startswith("-") or a == "-":
            targets.append(a)
        elif a == "--":
            end_of_opts = True
        elif a.startswith("--"):
            long_flags.add(a)
        else:
            short += a[1:]
    recursive = "r" in short or "R" in short or "--recursive" in long_flags
    force = "f" in short or "--force" in long_flags
    if not (recursive and force):
        return False
    return not targets or not all(_is_exempt(t) for t in targets)


def check(cmd: str):
    """차단 사유를 반환한다. 통과면 None."""
    if DROP_TABLE_RE.search(cmd):
        return "DROP TABLE"
    for seg in _segments(cmd):
        for i, tok in enumerate(seg):
            if tok == "rm" and _check_rm(seg[i + 1 :]):
                return "rm -rf"
            if tok == "-c" and i + 1 < len(seg):  # bash -c "..." 등 내부 명령
                reason = check(seg[i + 1])
                if reason:
                    return reason
        if "git" in seg:
            if "reset" in seg and "--hard" in seg:
                return "git reset --hard"
            if "push" in seg and ("-f" in seg or "--force" in seg):
                return "git push --force"
    return None


def main():
    try:
        cmd = json.load(sys.stdin)["tool_input"]["command"]
    except (ValueError, KeyError, TypeError):
        return 0
    reason = check(cmd)
    if reason:
        print(
            f"BLOCKED: 위험한 명령어가 감지되었습니다 ({reason}). "
            f"필요하면 step을 blocked로 기록하고 사용자에게 넘겨라.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
