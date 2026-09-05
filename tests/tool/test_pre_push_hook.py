"""#214: the push event stopped being the unit of verification -- a tree is.

`.githooks/pre-push` runs the suite only for a pushed commit whose tree was never green, so the
worker's own run, the coordinator's merge of the identical tree and the wave-branch push cost
nothing. The cache is written by `tool/checks/tested-tree`, and this file drives the real one from
a fake suite script, so the write and the read are checked against each other rather than
separately against a fixture.

Isolation is a throwaway repository plus PATH-free relative paths: the hook calls `tool/checks/test`
by a repository-relative path, so the fixture repo carries its own.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / ".githooks" / "pre-push"
TESTED_TREE = REPO_ROOT / "tool" / "checks" / "tested-tree"
# The hook works out what a push earns before it trusts a cache entry (#215 review C2), so the
# fixture repo carries the real classifier and the real map, not a stand-in for either.
CARRIED = (
    "tests/scope.toml",
    "tool/change_scope.py",
    "tool/invariants.py",
    "tool/checks/invariants",
    "tool/checks/prerequisite",
)

ZERO = "0" * 40

# Records that it ran, then records the tree exactly as tool/checks/test does -- through the real
# fragment, so a change to the cache format cannot pass here and fail in the hook.
FAKE_SUITE = """#!/bin/sh
set -e
if [ -r tool/checks/tested-tree ]; then
    . tool/checks/tested-tree
    tested_tree_capture
fi
printf 'fake suite ran\\n'
printf '%s\\n' "$*" >> "$SUITE_MARKER"
if [ "${FAKE_SUITE_FAILS:-0}" = 1 ]; then
    exit 1
fi
if [ -r tool/checks/tested-tree ]; then
    if [ "${FAKE_SUITE_CLASS:-A}" = none ]; then
        tested_tree_record
    else
        tested_tree_record "${FAKE_SUITE_CLASS:-A}"
    fi
fi
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo carrying its own tool/checks, isolated from THIS checkout (#60 GIT_DIR).

    The marker the fake suite writes lives OUTSIDE the repo: the cache refuses to record a run over
    a dirty checkout, and an untracked marker would make every push here dirty.
    """
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    checks = root / "tool" / "checks"
    checks.mkdir(parents=True)
    for carried in CARRIED:
        target = root / carried
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / carried, target)
    (checks / "tested-tree").write_text(TESTED_TREE.read_text(encoding="utf-8"), encoding="utf-8")
    suite = checks / "test"
    suite.write_text(FAKE_SUITE, encoding="utf-8")
    suite.chmod(0o755)
    return root


def commit(repo: Path, text: str) -> str:
    (repo / "file.txt").write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--no-verify", "-m", f"chore: {text}"], check=True
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def tree_of(repo: Path, sha: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"{sha}^{{tree}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def run_hook(
    repo: Path, *shas: str, fails: bool = False, force: bool = False, klass: str = "A"
) -> subprocess.CompletedProcess:
    stdin = "".join(f"refs/heads/main {sha} refs/heads/main {ZERO}\n" for sha in shas)
    return subprocess.run(
        ["sh", str(HOOK)],
        cwd=str(repo),
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "HOME": str(repo),
            "SUITE_MARKER": str(repo.parent / "marker"),
            "FAKE_SUITE_FAILS": "1" if fails else "0",
            "COSMAI_FORCE_SUITE": "1" if force else "0",
            "FAKE_SUITE_CLASS": klass,
        },
    )


def suite_runs(repo: Path) -> int:
    marker = repo.parent / "marker"
    return len(marker.read_text(encoding="utf-8").splitlines()) if marker.exists() else 0


def origin_main(repo: Path, sha: str) -> None:
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", sha], check=True)


def test_an_untested_tree_runs_the_suite(repo: Path):
    sha = commit(repo, "one")
    done = run_hook(repo, sha)
    assert done.returncode == 0, done.stderr
    assert "untested for class" in done.stdout, done.stdout
    assert suite_runs(repo) == 1


def test_a_tree_the_suite_already_proved_green_skips_the_suite(repo: Path):
    sha = commit(repo, "one")
    first = run_hook(repo, sha)
    assert suite_runs(repo) == 1, first.stdout

    second = run_hook(repo, sha)
    assert second.returncode == 0, second.stderr
    assert suite_runs(repo) == 1, "the second push re-ran a suite over an already green tree"
    assert "skipping the suite" in second.stdout, second.stdout
    assert "tested 2" in second.stdout, "the skip line must name when the tree was proved green"


def test_the_cache_entry_is_named_for_the_tree_and_holds_the_time(repo: Path):
    sha = commit(repo, "one")
    run_hook(repo, sha)
    entry = repo / ".git" / "cosmai-tested" / tree_of(repo, sha)
    assert entry.exists(), "tool/checks/tested-tree recorded nothing for a green run"
    stamp = entry.read_text(encoding="utf-8").strip()
    assert stamp.startswith("20") and " class " in stamp, stamp


def test_a_merge_commit_carrying_an_already_tested_tree_is_free(repo: Path):
    # The coordinator's merge of a worker's branch has the worker's tree and a different sha; that
    # identity is the whole point of keying the cache on the tree (#214).
    sha = commit(repo, "one")
    run_hook(repo, sha)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--no-verify", "--allow-empty", "-m", "chore: merge"],
        check=True,
    )
    other = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert other != sha
    done = run_hook(repo, other)
    assert "skipping the suite" in done.stdout, done.stdout
    assert suite_runs(repo) == 1


def test_a_commit_already_on_origin_main_skips_the_suite(repo: Path):
    # #197: what is on origin/main was verified before it got there, cache entry or not.
    old = commit(repo, "one")
    new = commit(repo, "two")
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", new], check=True)
    done = run_hook(repo, old)
    assert done.returncode == 0, done.stderr
    assert "skipping the suite" in done.stdout, done.stdout
    assert suite_runs(repo) == 0


def test_a_commit_ahead_of_origin_main_still_runs_the_suite(repo: Path):
    old = commit(repo, "one")
    new = commit(repo, "two")
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", old], check=True)
    done = run_hook(repo, new)
    assert "untested for class" in done.stdout, done.stdout
    assert suite_runs(repo) == 1


def test_one_untested_ref_among_tested_ones_runs_the_suite(repo: Path):
    tested = commit(repo, "one")
    run_hook(repo, tested)
    fresh = commit(repo, "two")
    done = run_hook(repo, tested, fresh)
    assert "untested for class" in done.stdout, done.stdout
    assert suite_runs(repo) == 2


def test_deletions_only_still_skip_the_suite(repo: Path):
    commit(repo, "one")
    done = run_hook(repo, ZERO)
    assert done.returncode == 0, done.stderr
    assert suite_runs(repo) == 0


def test_nothing_on_stdin_runs_the_suite(repo: Path):
    # The safe default is to test: an unexpected stdin is not evidence of a green tree.
    commit(repo, "one")
    done = run_hook(repo)
    assert done.returncode == 0, done.stderr
    assert suite_runs(repo) == 1


def test_a_failing_suite_blocks_the_push_and_records_nothing(repo: Path):
    sha = commit(repo, "one")
    done = run_hook(repo, sha, fails=True)
    assert done.returncode == 1, done.stdout
    assert "Push blocked" in done.stderr, done.stderr
    assert not (repo / ".git" / "cosmai-tested" / tree_of(repo, sha)).exists()


def test_exactly_one_decision_line_is_printed(repo: Path):
    sha = commit(repo, "one")
    done = run_hook(repo, sha)
    decisions = [line for line in done.stdout.splitlines() if line.startswith("pre-push: ")]
    assert len(decisions) == 1, done.stdout


def test_a_forced_push_runs_the_suite_over_a_cached_tree(repo: Path):
    # AGENTS.md forbids --no-verify, and the cache has already been shown to be able to hold a bad
    # entry, so there has to be one sanctioned way to make the gate re-verify a green tree.
    sha = commit(repo, "one")
    run_hook(repo, sha)
    assert suite_runs(repo) == 1

    done = run_hook(repo, sha, force=True)
    assert done.returncode == 0, done.stderr
    assert suite_runs(repo) == 2, "COSMAI_FORCE_SUITE=1 did not re-run the suite"
    assert "forced by COSMAI_FORCE_SUITE=1, running the suite" in done.stdout, done.stdout


def test_a_forced_push_runs_the_suite_over_a_commit_on_origin_main(repo: Path):
    # Forcing has to beat BOTH skips, or the ancestor arm quietly outranks the escape hatch.
    old = commit(repo, "one")
    new = commit(repo, "two")
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", new], check=True)
    done = run_hook(repo, old, force=True)
    assert suite_runs(repo) == 1, done.stdout
    assert "forced by COSMAI_FORCE_SUITE=1" in done.stdout, done.stdout


def test_the_origin_main_skip_survives_an_unreadable_cache(repo: Path):
    # R3 phrased the two skips as independent alternatives. Nested inside the cache branch, #197's
    # skip would silently disappear on any checkout whose fragment is missing.
    old = commit(repo, "one")
    new = commit(repo, "two")
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", new], check=True)
    (repo / "tool" / "checks" / "tested-tree").unlink()
    done = run_hook(repo, old)
    assert done.returncode == 0, done.stderr
    assert "skipping the suite" in done.stdout, done.stdout
    assert suite_runs(repo) == 0


def test_an_unreadable_cache_still_runs_the_suite_for_anything_else(repo: Path):
    sha = commit(repo, "one")
    (repo / "tool" / "checks" / "tested-tree").unlink()
    done = run_hook(repo, sha)
    assert "running the suite" in done.stdout, done.stdout
    assert suite_runs(repo) == 1


def test_the_hook_asks_for_the_verification_this_change_needs(repo: Path):
    # #215: the push pays for the class of its own change, not for every suite in the repository.
    sha = commit(repo, "one")
    run_hook(repo, sha)
    marker = (repo.parent / "marker").read_text(encoding="utf-8").strip()
    assert marker == "--changed origin/main", marker


def prose(repo: Path, text: str) -> str:
    """A commit whose only change is prose with no test behind it: the cheapest class there is."""
    (repo / "notes.md").write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--no-verify", "-m", "docs: notes"], check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def ddl(repo: Path, text: str) -> str:
    target = repo / "contracts" / "ddl" / "needs" / "030_a.sql"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--no-verify", "-m", "feat: ddl"], check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_a_class_c_entry_does_not_skip_a_push_that_earns_class_a(repo: Path):
    # #215 review C2: the entry named the class but the hook read only the stamp, so 38 seconds of
    # format, lint and three snapshot tests skipped the suite for a DDL change. The real repo's map
    # answers `contracts/ddl/` (#232 review: `try_computed_set` owes a computed set here, so what
    # this earns is B, not A -- either still outranks a class-C entry, which is the point).
    base = commit(repo, "one")
    origin_main(repo, base)
    sha = ddl(repo, "CREATE TABLE a (id int);\n")
    run_hook(repo, sha, klass="C")
    assert suite_runs(repo) == 1

    done = run_hook(repo, sha)
    assert suite_runs(repo) == 2, "a class-C entry skipped a push that earned more"
    assert "untested for class" in done.stdout, done.stdout
    assert "untested for class C" not in done.stdout, done.stdout


def test_a_class_a_entry_skips_a_push_that_earns_class_c(repo: Path):
    # The other direction is sound: a tree the whole suite proved green answers the smaller question.
    base = commit(repo, "one")
    origin_main(repo, base)
    sha = prose(repo, "# notes\n")
    run_hook(repo, sha, klass="A")
    assert suite_runs(repo) == 1

    done = run_hook(repo, sha)
    assert suite_runs(repo) == 1, done.stdout
    assert "skipping the suite" in done.stdout, done.stdout


def test_a_class_c_entry_skips_another_class_c_push(repo: Path):
    # The skip line names the class too: "tested" alone reads as "fully tested" every time.
    base = commit(repo, "one")
    origin_main(repo, base)
    sha = prose(repo, "# notes\n")
    run_hook(repo, sha, klass="C")
    done = run_hook(repo, sha)
    assert suite_runs(repo) == 1, done.stdout
    assert "class C, skipping the suite" in done.stdout, done.stdout


def test_an_entry_from_before_the_class_was_recorded_never_skips(repo: Path):
    # Rank 0: an entry that names no class is not evidence about any class.
    base = commit(repo, "one")
    origin_main(repo, base)
    sha = prose(repo, "# notes\n")
    run_hook(repo, sha, klass="none")
    assert suite_runs(repo) == 1
    run_hook(repo, sha)
    assert suite_runs(repo) == 2, "a class-less entry was read as evidence"


# ---------------------------------------------------------------------------------------------
# #232 Work 2: CI runs class A on every push now, so the hook never runs it locally on its own.
# These tests carry the REAL tool/checks/test (not the fake) so the downgrade -- which lives
# there, gated on COSMAI_GATE=local -- is exercised through the actual hook. A PATH that mirrors
# the real one but hides docker/uv/pg_isready lets the real script run far enough to print its
# verification line and then stop at `require_command`, before it would ever start a container.
# ---------------------------------------------------------------------------------------------

REAL_CARRIED = CARRIED + ("tool/checks/suite-lock",)


@pytest.fixture
def gate_repo(tmp_path: Path) -> Path:
    """Like `repo`, but tool/checks/test is the real script -- the one place #232's downgrade lives."""
    root = tmp_path / "gate-repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    for carried in REAL_CARRIED:
        target = root / carried
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / carried, target)
    real_test = root / "tool" / "checks" / "test"
    shutil.copy2(REPO_ROOT / "tool" / "checks" / "test", real_test)
    real_test.chmod(0o755)
    (root / "tool" / "checks" / "tested-tree").write_text(
        TESTED_TREE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return root


@pytest.fixture(scope="module")
def no_docker_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A PATH with every real command this run needs, minus docker/uv/pg_isready -- so the real
    tool/checks/test reaches `require_command` and stops there instead of starting a container.
    """
    hide = {"docker", "uv", "pg_isready"}
    base = tmp_path_factory.mktemp("no-docker-path")
    shadow_dirs = []
    for index, directory in enumerate(os.environ.get("PATH", "").split(os.pathsep)):
        if not directory or not os.path.isdir(directory):
            continue
        shadow = base / f"d{index}"
        shadow.mkdir()
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


def run_gate_hook(
    repo: Path, no_docker_path: str, *shas: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    stdin = "".join(f"refs/heads/main {sha} refs/heads/main {ZERO}\n" for sha in shas)
    env = {
        "HOME": str(repo),
        "PATH": no_docker_path,
        **(extra_env or {}),
    }
    return subprocess.run(
        ["sh", str(HOOK)],
        cwd=str(repo),
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def write_and_commit(repo: Path, path: str, text: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--no-verify", "-m", "feat: x"], check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_a_trigger_change_pushes_the_computed_set_and_prints_it_is_owed(gate_repo: Path, no_docker_path: str):
    base = write_and_commit(gate_repo, "contracts/ddl/needs/001.sql", "CREATE TABLE a (id int);\n")
    origin_main(gate_repo, base)
    sha = write_and_commit(gate_repo, "contracts/ddl/needs/002.sql", "CREATE TABLE b (id int);\n")
    done = run_gate_hook(gate_repo, no_docker_path, sha)
    assert "verification: class B" in done.stdout, done.stdout
    assert "class A owed — CI runs it on this push" in done.stdout, done.stdout


def test_cosmai_force_suite_runs_a_real_class_a_locally(gate_repo: Path, no_docker_path: str):
    base = write_and_commit(gate_repo, "contracts/ddl/needs/001.sql", "CREATE TABLE a (id int);\n")
    origin_main(gate_repo, base)
    sha = write_and_commit(gate_repo, "contracts/ddl/needs/002.sql", "CREATE TABLE b (id int);\n")
    done = run_gate_hook(gate_repo, no_docker_path, sha, extra_env={"COSMAI_FORCE_SUITE": "1"})
    assert "verification: class A" in done.stdout, done.stdout
    assert "class A owed" not in done.stdout, done.stdout


def test_an_unanswerable_change_still_runs_class_a_locally(gate_repo: Path, no_docker_path: str):
    base = write_and_commit(gate_repo, "somewhere/file.py", "x = 1\n")
    origin_main(gate_repo, base)
    sha = write_and_commit(gate_repo, "somewhere/file.py", "x = 2\n")
    done = run_gate_hook(gate_repo, no_docker_path, sha)
    assert "verification: class A" in done.stdout, done.stdout
    assert "class A owed" not in done.stdout, done.stdout
    assert "maps to no entry" in done.stdout, done.stdout


# ---------------------------------------------------------------------------------------------
# Review 2026-09-05 (fix round): $earned must reflect what a local push actually RUNS (the
# downgrade), or a tree recorded as B-with-owed never satisfies its own cache and every re-push
# of the identical tree re-runs the suite -- the tested-tree cache's whole reason to exist (#214).
# The `repo` fixture's FAKE_SUITE (not the real tool/checks/test) records whatever class the test
# tells it to; the real classifier (carried into the fixture) is what computes $earned itself.
# ---------------------------------------------------------------------------------------------


def test_a_tree_recorded_as_b_with_owed_skips_a_later_push_of_the_same_tree(repo: Path):
    # contracts/ddl/ is a trigger the real map also answers (#232 review): the classifier says A
    # with a non-empty owed line, so this push's $earned is B, not A -- exactly what
    # COSMAI_GATE=local would have made the real tool/checks/test run and record.
    base = commit(repo, "one")
    origin_main(repo, base)
    sha = ddl(repo, "CREATE TABLE a (id int);\n")
    first = run_hook(repo, sha, klass="B")
    assert "untested for class B" in first.stdout, first.stdout
    assert suite_runs(repo) == 1, first.stdout

    second = run_hook(repo, sha)
    assert suite_runs(repo) == 1, "a tree recorded at its own earned class re-ran the suite"
    assert "skipping the suite" in second.stdout, second.stdout
    assert "class B" in second.stdout, second.stdout


def test_a_forced_push_still_runs_over_a_tree_recorded_as_b_with_owed(repo: Path):
    base = commit(repo, "one")
    origin_main(repo, base)
    sha = ddl(repo, "CREATE TABLE a (id int);\n")
    run_hook(repo, sha, klass="B")
    assert suite_runs(repo) == 1

    forced = run_hook(repo, sha, force=True)
    assert suite_runs(repo) == 2, "COSMAI_FORCE_SUITE=1 did not re-run a B-recorded tree"
    assert "forced by COSMAI_FORCE_SUITE=1" in forced.stdout, forced.stdout
