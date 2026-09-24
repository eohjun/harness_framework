"""
.claude/hooks/dangerous_cmd_guard.py 테스트.
Claude Code와 같은 방식(stdin JSON, 종료 코드)으로 실제 스크립트를 실행해 검증한다.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "dangerous_cmd_guard.py"


def run_guard(command: str) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    return subprocess.run(
        [sys.executable, str(GUARD)], input=payload, capture_output=True, text=True
    )


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf src",
        "rm -fr src",
        "rm -Rf src",
        "rm -r -f src",
        "rm --recursive --force src",
        "sudo rm -rf /",
        "rm -rf node_modules src",
        "cd app && rm -rf src",
        "find . -name x -exec rm -rf {} +",
        "bash -c 'rm -rf src'",
        "git reset --hard HEAD~1",
        "git push --force origin main",
        "git push -f origin main",
        "psql -c 'DROP TABLE users'",
        "sqlite3 db 'drop table users'",
    ],
)
def test_blocks_destructive(command):
    r = run_guard(command)
    assert r.returncode == 2, command
    assert "BLOCKED" in r.stderr


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "rm -rf .next",
        "rm -rf node_modules",
        "rm -rf ./dist/ build",
        "rm -rf /tmp/claude-x/scratch",
        "rm -r src",
        "rm -f file.txt",
        "echo 'rm -rf src'",
        "git log --grep 'reset --hard'",
        "git commit -m 'docs: explain push -f'",
        "git push origin feat-mvp",
        "git reset HEAD -- file",
    ],
)
def test_allows_safe(command):
    r = run_guard(command)
    assert r.returncode == 0, (command, r.stderr)


def test_invalid_json_passes_through():
    r = subprocess.run(
        [sys.executable, str(GUARD)], input="not json", capture_output=True, text=True
    )
    assert r.returncode == 0
