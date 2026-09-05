"""#233: `tool/issue ready` needs a cheap answer to "does this branch owe class A" without
checking the branch out -- `tool/ci_owed.py <ref> <base>` diffs two refs already on `origin`
against the gate and trigger lists of `tests/scope.toml`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_OWED = REPO_ROOT / "tool" / "ci_owed.py"

SCOPE_TOML = """
gate = ["tool/checks/test"]
trigger = ["contracts/ddl/", "db/views/"]
docs = []
map = {}
"""


def _git(repo: Path) -> tuple[list[str], dict[str, str]]:
    return ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(repo)], {
        k: v for k, v in os.environ.items() if not k.startswith("GIT_")
    }


def commit(repo: Path, path: str, text: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    git, env = _git(repo)
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run([*git, "commit", "-qm", f"chore: {path}"], check=True, capture_output=True, env=env)
    return subprocess.run(
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def ref(repo: Path, name: str, sha: str) -> None:
    git, env = _git(repo)
    subprocess.run([*git, "update-ref", name, sha], check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    git, env = _git(root)
    subprocess.run(
        [*git[:-2], "init", "-q", "-b", "main", str(root)], check=True, capture_output=True, env=env
    )
    commit(root, "tests/scope.toml", SCOPE_TOML)
    commit(root, "tool/change_scope.py", (REPO_ROOT / "tool" / "change_scope.py").read_text(encoding="utf-8"))
    return root


def run_owed(repo: Path, branch_ref: str, base_ref: str) -> str:
    done = subprocess.run(
        [sys.executable, str(CI_OWED), branch_ref, base_ref],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_a_branch_that_never_touches_trigger_or_gate_is_not_owed(repo: Path):
    base = commit(repo, "README.md", "base\n")
    ref(repo, "refs/remotes/origin/main", base)
    tip = commit(repo, "app/thing.py", "print(1)\n")
    ref(repo, "refs/remotes/origin/tool/11-thing", tip)
    assert run_owed(repo, "origin/tool/11-thing", "origin/main") == "not owed"


def test_a_branch_touching_the_trigger_list_is_owed(repo: Path):
    base = commit(repo, "README.md", "base\n")
    ref(repo, "refs/remotes/origin/main", base)
    tip = commit(repo, "db/views/pipeline_health.sql", "-- a view\n")
    ref(repo, "refs/remotes/origin/tool/12-thing", tip)
    assert run_owed(repo, "origin/tool/12-thing", "origin/main") == "owed"


def test_a_branch_touching_the_gate_list_is_owed(repo: Path):
    base = commit(repo, "README.md", "base\n")
    ref(repo, "refs/remotes/origin/main", base)
    tip = commit(repo, "tool/checks/test", "#!/bin/sh\n")
    ref(repo, "refs/remotes/origin/tool/13-thing", tip)
    assert run_owed(repo, "origin/tool/13-thing", "origin/main") == "owed"


def test_no_origin_main_is_not_owed_rather_than_a_crash(repo: Path):
    tip = commit(repo, "app/thing.py", "print(1)\n")
    ref(repo, "refs/remotes/origin/tool/14-thing", tip)
    assert run_owed(repo, "origin/tool/14-thing", "origin/main") == "not owed"
