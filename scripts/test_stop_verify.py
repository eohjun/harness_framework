"""
.claude/hooks/stop_verify.py 테스트.
Claude Code와 같은 방식(stdin JSON, CLAUDE_PROJECT_DIR, 종료 코드)으로 실제 스크립트를 실행한다.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "stop_verify.py"

pytestmark = pytest.mark.skipif(shutil.which("npm") is None, reason="npm required")


def run_hook(project: Path, payload: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def make_project(tmp_path: Path, test_script: str) -> Path:
    scripts = {"lint": "true", "build": "true", "test": test_script}
    (tmp_path / "package.json").write_text(json.dumps({"name": "t", "scripts": scripts}))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def commit_all(project: Path):
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-qm",
            "init",
        ],
        cwd=project,
        check=True,
    )


def test_blocks_when_check_fails(tmp_path):
    project = make_project(tmp_path, "echo boom && exit 1")
    r = run_hook(project, {"stop_hook_active": False})
    assert r.returncode == 2
    assert "npm run test" in r.stderr
    assert "boom" in r.stderr


def test_passes_when_checks_pass(tmp_path):
    project = make_project(tmp_path, "true")
    assert run_hook(project, {"stop_hook_active": False}).returncode == 0


def test_second_stop_is_not_blocked_again(tmp_path):
    project = make_project(tmp_path, "exit 1")
    assert run_hook(project, {"stop_hook_active": True}).returncode == 0


def test_skips_without_package_json(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "x.txt").write_text("change")
    assert run_hook(tmp_path, {}).returncode == 0


def test_skips_clean_tree(tmp_path):
    project = make_project(tmp_path, "exit 1")
    commit_all(project)
    assert run_hook(project, {"stop_hook_active": False}).returncode == 0
