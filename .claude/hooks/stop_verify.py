#!/usr/bin/env python3
"""
Stop 훅 — 코드가 바뀐 턴이 끝날 때 lint/build/test를 실행하고, 실패하면 종료를 막아 Claude가 고치게 한다.

Claude Code는 Stop 훅이 exit 2로 끝날 때만 종료를 막고 stderr를 Claude에게 전달한다.
한 번 막은 뒤 다시 멈출 때는 stop_hook_active가 true로 오므로 통과시켜 무한 루프를 막는다.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

COMMANDS = (["npm", "run", "lint"], ["npm", "run", "build"], ["npm", "run", "test"])


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    if data.get("stop_hook_active"):
        return 0

    root = Path(os.environ.get("CLAUDE_PROJECT_DIR", "."))
    if not (root / "package.json").exists():
        return 0  # 아직 스캐폴딩 전이면 검증할 대상이 없다

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True
    )
    if status.returncode == 0 and not status.stdout.strip():
        return 0  # 커밋되지 않은 변경이 없으면 이번 턴에 검증할 코드도 없다

    for cmd in COMMANDS:
        r = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
        if r.returncode != 0:
            print(
                f"`{' '.join(cmd)}` 실패 (exit {r.returncode}). 고친 뒤 종료하라:\n"
                f"{(r.stdout + r.stderr)[-3000:]}",
                file=sys.stderr,
            )
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
