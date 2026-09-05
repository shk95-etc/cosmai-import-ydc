"""#233 (#228 D5'): a deploy takes only a tree whose class A run is already green.

`tool/checks/deploy-gate [<ref>]` exits 0 only when the ref's tree has a green class-A record
-- the CI check run named `class-a` on that commit, or (when `gh` cannot answer) the local
tested-tree cache (#214) recording that tree at exactly class A -- and the working tree is
clean. `COSMAI_FORCE_DEPLOY=1` turns a refusal into a warning and exits 0 anyway.

Isolation is a throwaway git repo carrying the real `tool/checks/tested-tree` fragment and a
fake `gh` first on PATH, the same shape `tests/tool/test_pre_push_hook.py` and
`tests/tool/test_issue_tool.py` already use for their own fragments.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "tool" / "checks" / "deploy-gate"
TESTED_TREE = REPO_ROOT / "tool" / "checks" / "tested-tree"

FAKE_GH = """#!/bin/sh
case "$*" in
  *"/check-runs"*)
    if [ -n "$FAKE_GH_CI_FAIL" ]; then echo "fake gh: the API said no" >&2; exit 1; fi
    printf '%s\\n' "${FAKE_GH_CI_CONCLUSION:-success}"
    exit 0
    ;;
esac
echo "fake gh: no fixture for: $*" >&2
exit 1
"""


def _git(repo: Path) -> tuple[list[str], dict[str, str]]:
    return ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(repo)], {
        k: v for k, v in os.environ.items() if not k.startswith("GIT_")
    }


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repo carrying the real tested-tree fragment and a fake origin remote."""
    root = tmp_path / "repo"
    git, env = _git(root)
    subprocess.run(
        [*git[:-2], "init", "-q", "-b", "main", str(root)], check=True, capture_output=True, env=env
    )
    checks = root / "tool" / "checks"
    checks.mkdir(parents=True)
    (checks / "tested-tree").write_text(TESTED_TREE.read_text(encoding="utf-8"), encoding="utf-8")
    (root / "file.txt").write_text("one\n", encoding="utf-8")
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run([*git, "commit", "-qm", "chore: seed"], check=True, capture_output=True, env=env)
    subprocess.run(
        [*git, "remote", "add", "origin", "https://example.invalid/slopindustries/cosmai.git"],
        check=True,
        capture_output=True,
        env=env,
    )
    return root


def tree_of(repo: Path, ref: str = "HEAD") -> str:
    git, env = _git(repo)
    return subprocess.run(
        [*git, "rev-parse", f"{ref}^{{tree}}"], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def record_local_class(repo: Path, klass: str, ref: str = "HEAD") -> None:
    """Writes a tested-tree entry directly, the same shape tool/checks/test would leave."""
    cache = repo / ".git" / "cosmai-tested"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / tree_of(repo, ref)).write_text(f"2026-09-05T00:00:00Z class {klass}\n", encoding="utf-8")


# This host has a real, authenticated `gh` on PATH -- a PATH that keeps the real dirs wholesale
# would let a "no gh" test reach the real GitHub API instead of proving the fallback (the
# technique tests/tool/test_status_tool.py's `test_ci_section_is_unavailable_without_gh` uses).
def _shadow_path_without_gh(tmp_path: Path, extra_front: Path | None = None) -> str:
    hide = {"gh"}
    shadow_root = tmp_path / "no-gh-path"
    shadow_dirs = [str(extra_front)] if extra_front is not None else []
    for index, directory in enumerate(os.environ.get("PATH", "").split(os.pathsep)):
        if not directory or not os.path.isdir(directory):
            continue
        shadow = shadow_root / f"d{index}"
        shadow.mkdir(parents=True)
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        for name in entries:
            if name in hide:
                continue
            try:
                (shadow / name).symlink_to(os.path.join(directory, name))
            except OSError:
                continue
        shadow_dirs.append(str(shadow))
    return os.pathsep.join(shadow_dirs)


def run_gate(repo: Path, path: str, *args: str, **extra_env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(GATE), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
            "PATH": path,
            **extra_env,
        },
    )


@pytest.fixture
def gh_path(tmp_path: Path):
    """PATH with a fake `gh` in front and the real `gh` shadowed out behind it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    return _shadow_path_without_gh(tmp_path, extra_front=bin_dir)


@pytest.fixture
def no_gh_path(tmp_path: Path) -> str:
    """PATH with every real binary except `gh` -- proves the local-record fallback, not a stub."""
    return _shadow_path_without_gh(tmp_path)


def test_a_green_ci_check_run_passes(repo: Path, gh_path: str):
    done = run_gate(repo, gh_path, FAKE_GH_CI_CONCLUSION="success")
    assert done.returncode == 0, done.stderr


def test_a_red_ci_check_run_refuses_with_the_reason(repo: Path, gh_path: str):
    done = run_gate(repo, gh_path, FAKE_GH_CI_CONCLUSION="failure")
    assert done.returncode == 1
    assert "run red" in done.stderr, done.stderr


def test_a_pending_ci_check_run_refuses_with_the_reason(repo: Path, gh_path: str):
    done = run_gate(repo, gh_path, FAKE_GH_CI_CONCLUSION="pending")
    assert done.returncode == 1
    assert "run pending" in done.stderr, done.stderr


def test_no_ci_check_run_refuses_with_the_reason(repo: Path, gh_path: str):
    done = run_gate(repo, gh_path, FAKE_GH_CI_CONCLUSION="none")
    assert done.returncode == 1
    assert "no run" in done.stderr, done.stderr


def test_a_local_class_a_record_passes_without_gh(repo: Path, no_gh_path: str):
    record_local_class(repo, "A")
    done = run_gate(repo, no_gh_path)
    assert done.returncode == 0, done.stderr


def test_gh_unavailable_and_no_local_record_refuses(repo: Path, no_gh_path: str):
    done = run_gate(repo, no_gh_path)
    assert done.returncode == 1
    assert "gh unavailable and no local record" in done.stderr, done.stderr


def test_a_dirty_tree_refuses(repo: Path, gh_path: str):
    (repo / "file.txt").write_text("dirty\n", encoding="utf-8")
    done = run_gate(repo, gh_path, FAKE_GH_CI_CONCLUSION="success")
    assert done.returncode == 1
    assert "dirty tree" in done.stderr, done.stderr


def test_forced_deploy_warns_and_passes_over_every_refusal(repo: Path, no_gh_path: str):
    done = run_gate(repo, no_gh_path, COSMAI_FORCE_DEPLOY="1")
    assert done.returncode == 0, done.stderr
    assert "journal" in done.stderr, done.stderr


# ---------------------------------------------------------------------------------------------
# #233: db/migrate.sh calls the gate only on the production default (no --container).
# ---------------------------------------------------------------------------------------------

MIGRATE = REPO_ROOT / "db" / "migrate.sh"

# Exits 1 the instant it is asked anything, never reaching a real or throwaway daemon -- the
# gate's own pass/fail is not this fixture's question, only whether it was called at all.
FAKE_DOCKER_FAILS_FAST = """#!/bin/sh
exit 1
"""


def _fake_gate(marker: Path, exit_code: str = "0") -> str:
    return f"""#!/bin/sh
: > {marker}
exit {exit_code}
"""


@pytest.fixture
def migrate_fixture(tmp_path: Path) -> Path:
    """A directory shaped like the repo root db/migrate.sh expects: db/migrate.sh + a fake gate."""
    root = tmp_path / "migrate-fixture"
    (root / "db").mkdir(parents=True)
    (root / "tool" / "checks").mkdir(parents=True)
    (root / "db" / "migrate.sh").write_text(MIGRATE.read_text(encoding="utf-8"), encoding="utf-8")
    (root / "db" / "migrate.sh").chmod(0o755)
    return root


def run_migrate(root: Path, *args: str, gate_exit: str = "0") -> subprocess.CompletedProcess:
    marker = root / "gate-was-called"
    (root / "tool" / "checks" / "deploy-gate").write_text(_fake_gate(marker, gate_exit), encoding="utf-8")
    (root / "tool" / "checks" / "deploy-gate").chmod(0o755)
    bin_dir = root / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker").write_text(FAKE_DOCKER_FAILS_FAST, encoding="utf-8")
    (bin_dir / "docker").chmod(0o755)
    (root / "secret_file").write_text("", encoding="utf-8")
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "COSMAI_SECRET_FILE": str(root / "secret_file"),
    }
    return subprocess.run(
        ["sh", str(root / "db" / "migrate.sh"), *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_migrate_sh_calls_the_gate_with_no_container_given(migrate_fixture: Path):
    run_migrate(migrate_fixture)
    assert (migrate_fixture / "gate-was-called").exists()


def test_migrate_sh_never_calls_the_gate_with_container_given(migrate_fixture: Path):
    run_migrate(migrate_fixture, "--container", "throwaway")
    assert not (migrate_fixture / "gate-was-called").exists()


def test_migrate_sh_stops_when_the_gate_refuses(migrate_fixture: Path):
    done = run_migrate(migrate_fixture, gate_exit="1")
    assert done.returncode == 1
    assert (migrate_fixture / "gate-was-called").exists()
