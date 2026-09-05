"""#215: what a change costs to verify is decided here, so this is where it has to be wrong loudly.

`tool/change_scope.py <base>` reads `tests/scope.toml` and answers with three lines -- the class, the
reason, and the test paths to run. Class A is the whole suite, class B is the mapped tests plus the
DB-free suite, class C is the format/lint/lang checks plus the snapshot tests. The bias is one way
only: anything the map does not cover, and anything that touches the database, the packaging or the
gate itself, is class A. A wrong class C is a change that reached main unverified, so every rule
below is a rule about what must NOT be allowed to look small.

Fixture repositories carrying the real scope.toml and the real invariant check: the classifier reads
git history, and this checkout's history is not a fixture anybody can pin.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLASSIFIER = REPO_ROOT / "tool" / "change_scope.py"
CARRIED = (
    "tests/scope.toml",
    "tool/invariants.py",
    "tool/checks/invariants",
    "tool/checks/prerequisite",
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repo carrying the real map and the real invariant check (#60 GIT_DIR)."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    for path in CARRIED:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / path, target)
    return root


def write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--no-verify", "-m", f"chore: {message}"], check=True
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


class Scope:
    def __init__(self, done: subprocess.CompletedProcess[str]) -> None:
        self.raw = done.stdout
        self.stderr = done.stderr
        self.code = done.returncode
        lines = done.stdout.splitlines()
        self.klass = lines[0] if lines else ""
        self.reason = lines[1] if len(lines) > 1 else ""
        self.tests = lines[2].split() if len(lines) > 2 else []
        # #232 Work 2: the fourth line names what a local push owes when it stays class A -- the
        # computed set the hook can run instead, empty when the classifier truly cannot answer.
        self.owed = lines[3].split() if len(lines) > 3 else []


def classify(repo: Path, base: str) -> Scope:
    done = subprocess.run(
        [sys.executable, str(CLASSIFIER), base],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    )
    return Scope(done)


def change(repo: Path, path: str, before: str, after: str) -> Scope:
    """Commits `before`, replaces it with `after`, and asks what verifying that costs."""
    write(repo, path, before)
    base = commit(repo, "before")
    write(repo, path, after)
    commit(repo, "after")
    return classify(repo, base)


PY_BEFORE = '"""A module."""\nLIMIT = 3\n\n\ndef run(rows):\n    # the old wording\n    return rows[:LIMIT]\n'
PY_RETOLD = '"""Said better."""\nLIMIT = 3\n\n\ndef run(rows):\n    # a better why\n    return rows[:LIMIT]\n'
PY_CHANGED = PY_BEFORE.replace("LIMIT = 3", "LIMIT = 4")


def test_a_ddl_change_is_class_a(repo: Path):
    scope = change(
        repo, "contracts/ddl/needs/030_x.sql", "CREATE TABLE a (id int);\n", "CREATE TABLE a (id bigint);\n"
    )
    assert scope.klass == "A", scope.raw
    assert "contracts/ddl/" in scope.reason, scope.reason


def test_the_lockfile_and_the_project_file_are_class_a(repo: Path):
    assert change(repo, "uv.lock", "a = 1\n", "a = 2\n").klass == "A"
    assert change(repo, "pyproject.toml", "[project]\n", "[project]\nx = 1\n").klass == "A"


def test_the_gate_deciding_its_own_change_is_small_is_refused(repo: Path):
    # The one classification nobody else can catch: a broken classifier that calls itself class C.
    scope = change(repo, "tool/change_scope.py", "x = 1\n", "x = 2\n")
    assert scope.klass == "A", scope.raw


def test_a_markdown_only_change_is_class_c(repo: Path):
    scope = change(repo, "docs.md", "# One\n\nSee §2 and #214.\n", "# One, retold\n\nSee §2 and #214.\n")
    assert scope.klass == "C", scope.raw
    assert "tests/test_cli_help.py" in scope.tests, scope.tests


def test_a_python_file_whose_code_did_not_move_is_class_c(repo: Path):
    scope = change(repo, "analysis/polarity/pipeline.py", PY_BEFORE, PY_RETOLD)
    assert scope.klass == "C", scope.raw
    assert "invariant" in scope.reason.lower(), scope.reason


def test_the_same_file_with_one_number_changed_is_class_b(repo: Path):
    scope = change(repo, "analysis/polarity/pipeline.py", PY_BEFORE, PY_CHANGED)
    assert scope.klass == "B", scope.raw
    assert "tests/test_polarity.py" in scope.tests, scope.tests
    assert "tests/test_linker.py" not in scope.tests, "class B ran another package's tests"


def test_the_longest_matching_prefix_wins(repo: Path):
    scope = change(repo, "analysis/linker/rules.py", PY_BEFORE, PY_CHANGED)
    assert scope.klass == "B", scope.raw
    assert "tests/test_linker.py" in scope.tests, scope.tests
    assert "tests/test_polarity.py" not in scope.tests, scope.tests


def test_a_change_in_two_packages_runs_both_sets(repo: Path):
    write(repo, "analysis/linker/rules.py", PY_BEFORE)
    write(repo, "analysis/trend/rules.py", PY_BEFORE)
    base = commit(repo, "before")
    write(repo, "analysis/linker/rules.py", PY_CHANGED)
    write(repo, "analysis/trend/rules.py", PY_CHANGED)
    commit(repo, "after")
    scope = classify(repo, base)
    assert scope.klass == "B", scope.raw
    assert "tests/test_linker.py" in scope.tests and "tests/test_trend_pipeline.py" in scope.tests, (
        scope.tests
    )


def test_a_changed_test_file_is_its_own_scope(repo: Path):
    scope = change(
        repo, "tests/test_linker.py", "def test_a():\n    assert 1\n", "def test_a():\n    assert 2\n"
    )
    assert scope.klass == "B", scope.raw
    # The smoke set (#231) always rides along; the changed test file itself is what proves it is
    # its own scope.
    assert "tests/test_linker.py" in scope.tests, scope.tests


def test_a_changed_conftest_is_class_a(repo: Path):
    # Every test in the suite runs through it, so its blast radius is the suite.
    scope = change(repo, "tests/conftest.py", "x = 1\n", "x = 2\n")
    assert scope.klass == "A", scope.raw


def test_a_fixture_under_tests_is_not_a_test_and_falls_to_class_a(repo: Path):
    scope = change(repo, "tests/fixtures/rows.json", '{"a": 1}\n', '{"a": 2}\n')
    assert scope.klass == "A", scope.raw


def test_an_unmapped_file_is_class_a(repo: Path):
    # An unmapped file is not a small change, it is an unmeasured one.
    scope = change(repo, "newthing/main.py", PY_BEFORE, PY_CHANGED)
    assert scope.klass == "A", scope.raw
    assert "newthing/main.py" in scope.reason, scope.reason


def test_a_prose_change_next_to_a_code_change_costs_the_code_change(repo: Path):
    write(repo, "docs.md", "# One\n")
    write(repo, "analysis/linker/rules.py", PY_BEFORE)
    base = commit(repo, "before")
    write(repo, "docs.md", "# Two\n")
    write(repo, "analysis/linker/rules.py", PY_CHANGED)
    commit(repo, "after")
    scope = classify(repo, base)
    assert scope.klass == "B", scope.raw


def test_a_branch_with_nothing_on_it_verifies_nothing(repo: Path):
    # #215 review C2: a tree identical to the base is the base's own tree. Calling that class C ran
    # three snapshot tests and then recorded the tree as green, which is how any tree became green.
    write(repo, "a.py", PY_BEFORE)
    base = commit(repo, "one")
    scope = classify(repo, base)
    assert scope.klass == "N", scope.raw
    assert scope.tests == [], scope.tests


def test_a_base_that_does_not_exist_is_class_a(repo: Path):
    # A fresh clone has no origin/main; guessing small on no information is how a gate stops being one.
    write(repo, "a.py", PY_BEFORE)
    commit(repo, "one")
    scope = classify(repo, "origin/nowhere")
    assert scope.klass == "A", scope.raw
    assert "origin/nowhere" in scope.reason, scope.reason


def test_the_class_and_the_reason_are_the_first_two_lines(repo: Path):
    scope = change(repo, "docs.md", "# One\n", "# Two\n")
    assert scope.code == 0, scope.stderr
    assert scope.klass in {"A", "B", "C"}, scope.raw
    assert scope.reason, "the class without a reason is a verdict nobody can check"


def test_an_unknown_argument_to_the_gate_is_refused():
    # A mistyped flag that quietly ran the whole suite would be a twenty-minute surprise; one that
    # quietly ran nothing would be worse. The gate takes `--changed [<base>]` or nothing at all.
    done = subprocess.run(
        ["sh", str(REPO_ROOT / "tool" / "checks" / "test"), "--whatever"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 2, done.stdout + done.stderr
    assert "usage:" in done.stderr, done.stderr


def test_a_classifier_that_cannot_answer_falls_back_to_the_whole_suite(tmp_path: Path):
    # No answer is not the same as "small": a classifier that crashes must cost the full suite, not
    # silently let a change through on the cheapest class.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (bin_dir / "python3").chmod(0o755)
    (bin_dir / "git").symlink_to(shutil.which("git") or "/usr/bin/git")
    done = subprocess.run(
        ["/bin/sh", str(REPO_ROOT / "tool" / "checks" / "test"), "--changed", "origin/main"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": str(bin_dir), "HOME": str(tmp_path)},
    )
    assert "verification: class A" in done.stdout, done.stdout + done.stderr
    # And it stops there: with no uv and no docker on this PATH, the run is unverified (69), never
    # a pass. A green here would mean the fallback ran nothing at all.
    assert done.returncode == 69, done.stdout + done.stderr


def test_a_comment_only_ddl_change_is_class_c(repo: Path):
    # #230: the full-suite list no longer forces A once tool/checks/invariants proves the edit moved
    # no code -- it earns the same class C a comment-only Python edit does.
    before = "-- the old wording\nCREATE TABLE a (id int);\n"
    scope = change(repo, "contracts/ddl/needs/001_needs.sql", before, before.replace("old wording", "why"))
    assert scope.klass == "C", scope.raw


def test_a_ddl_change_with_a_real_token_moved_is_still_class_a(repo: Path):
    scope = change(
        repo,
        "contracts/ddl/needs/001_needs.sql",
        "CREATE TABLE a (id int);\n",
        "CREATE TABLE a (id bigint);\n",
    )
    assert scope.klass == "A", scope.raw
    assert "contracts/ddl/" in scope.reason, scope.reason


def test_a_comment_only_db_backfill_change_is_class_c(repo: Path):
    before = "-- the old wording\nUPDATE app.need SET body = body;\n"
    scope = change(repo, "db/backfill/010_fix.sql", before, before.replace("old wording", "why"))
    assert scope.klass == "C", scope.raw


def test_a_comment_only_docker_compose_change_is_class_c(repo: Path):
    before = "# the old wording\nservices:\n  app:\n    image: cosmai:latest\n"
    scope = change(
        repo, "stack/docker-compose.yml", before, before.replace("# the old wording", "# what runs")
    )
    assert scope.klass == "C", scope.raw


def test_a_docker_compose_value_change_is_class_b(repo: Path):
    before = "# the old wording\nservices:\n  app:\n    image: cosmai:latest\n"
    scope = change(repo, "stack/docker-compose.yml", before, before.replace("latest", "v2"))
    assert scope.klass == "B", scope.raw
    assert "tests/stack" in scope.tests, scope.tests


def test_a_comment_only_gitignore_change_is_class_c(repo: Path):
    scope = change(repo, ".gitignore", "# the old wording\n*.pyc\n", "# build output\n*.pyc\n")
    assert scope.klass == "C", scope.raw


def test_a_gitignore_pattern_change_is_class_c(repo: Path):
    # #231 item 3: nothing at runtime reads a .gitignore, so a real pattern edit costs no test
    # either -- #230's empty map entry made this B without saying so anywhere; this is where the
    # decision belongs, and it belongs at C, not B.
    scope = change(repo, ".gitignore", "# the old wording\n*.pyc\n", "# the old wording\n*.pyo\n")
    assert scope.klass == "C", scope.raw
    assert scope.tests == [], scope.tests


def test_every_gate_file_stays_class_a_even_for_a_comment_only_edit(repo: Path):
    # The gate's own files earn no proof -- #230 split them out of `full` into `gate` for exactly this.
    before = '# old\n[project]\nname = "a"\n'
    scope = change(repo, "pyproject.toml", before, before.replace("# old", "# new"))
    assert scope.klass == "A", scope.raw
    assert "gate list" in scope.reason, scope.reason


def test_a_string_only_change_in_a_package_is_class_b(repo: Path):
    # A SQL predicate inside a string is behaviour: it earns the package's tests and the DB-free
    # suite, never the three snapshot tests.
    scope = change(
        repo,
        "analysis/polarity/q.py",
        'QUERY = "SELECT id FROM app.need WHERE tenant = %s"\n',
        'QUERY = "SELECT id FROM app.need WHERE 1=1"\n',
    )
    assert scope.klass == "B", scope.raw
    assert "tests/test_polarity.py" in scope.tests, scope.tests


def test_a_string_only_change_in_conftest_is_class_a(repo: Path):
    scope = change(
        repo,
        "tests/conftest.py",
        'TEST_DB_URL_ENV = "TEST_POSTGRES_URL"\n',
        'TEST_DB_URL_ENV = "TEST_PG_URL"\n',
    )
    assert scope.klass == "A", scope.raw


def test_a_default_model_and_a_regex_are_code_too(repo: Path):
    assert change(repo, "analysis/judge/m.py", 'M = "gpt-4o-mini"\n', 'M = "gpt-3.5-turbo"\n').klass == "B"
    scope = change(repo, "analysis/extractor/r.py", 'P = r"^\\d{4}$"\n', 'P = r"^\\d{2}$"\n')
    assert scope.klass == "B", scope.raw


def test_markdown_a_test_parses_runs_that_test(repo: Path):
    # #215 review I4: contracts/ownership.md is data to tests/test_ownership.py, so editing it is
    # prose that a test reads. Class C, plus the tests the map puts behind that file.
    scope = change(repo, "contracts/ownership.md", "# Owners\n\n- a: b\n", "# Owners\n\n- a: c\n")
    assert scope.klass == "C", scope.raw
    assert "tests/test_ownership.py" in scope.tests, scope.tests
    assert "tests/test_cli_help.py" in scope.tests, "the prose tests still run"


def test_a_deleted_test_file_is_class_a(repo: Path):
    # #215 review, minor 9: removing coverage is not a small change, and an empty test list used to
    # buy it the DB-free suite alone.
    write(repo, "tests/test_linker.py", "def test_a():\n    assert 1\n")
    base = commit(repo, "before")
    (repo / "tests" / "test_linker.py").unlink()
    commit(repo, "after")
    scope = classify(repo, base)
    assert scope.klass == "A", scope.raw


# ---------------------------------------------------------------------------------------------
# #231: the computed set -- readers map, import closure, smoke, the trigger list, no-answer paths.
# ---------------------------------------------------------------------------------------------


def test_a_readers_map_hit_selects_the_test_that_names_the_file(repo: Path):
    # The 2026-09-04 break this issue exists to close: contracts/formats.md is data to a test that
    # names it in its own source, and the map's directory prefix alone never saw that.
    write(repo, "tests/test_names_the_resource.py", 'RESOURCE = "contracts/formats.md"\n')
    write(repo, "contracts/formats.md", "# Formats\n\nOld body.\n")
    base = commit(repo, "before")
    write(repo, "contracts/formats.md", "# Formats\n\nNew body.\n")
    commit(repo, "after")
    scope = classify(repo, base)
    assert "tests/test_names_the_resource.py" in scope.tests, scope.tests


def test_the_import_closure_finds_an_indirect_importer_and_nothing_else(repo: Path):
    # analysis/helper.py is imported only by analysis/user.py, which tests/test_indirect.py imports
    # in turn -- the closure has to cross that one hop, and it must not drag in an unrelated test.
    write(repo, "analysis/helper.py", "def calc(x):\n    return x\n")
    write(
        repo, "analysis/user.py", "from analysis import helper\n\n\ndef run(x):\n    return helper.calc(x)\n"
    )
    write(
        repo,
        "tests/test_indirect.py",
        "from analysis import user\n\n\ndef test_a():\n    assert user.run(1) == 1\n",
    )
    write(repo, "tests/test_unrelated.py", "def test_b():\n    assert True\n")
    base = commit(repo, "before")
    write(repo, "analysis/helper.py", "def calc(x):\n    return x + 1\n")
    commit(repo, "after")
    scope = classify(repo, base)
    assert scope.klass == "B", scope.raw
    assert "tests/test_indirect.py" in scope.tests, scope.tests
    assert "tests/test_unrelated.py" not in scope.tests, scope.tests


def test_every_computed_change_carries_the_smoke_set(repo: Path):
    scope = change(repo, "analysis/linker/rules.py", PY_BEFORE, PY_CHANGED)
    assert scope.klass == "B", scope.raw
    for smoke_test in (
        "tests/test_cli_help.py",
        "tests/test_version_strings.py",
        "tests/test_registry_loading.py",
        "tests/test_scope_map.py",
        "tests/stack/test_stack_wiring.py",
    ):
        assert smoke_test in scope.tests, scope.tests


def test_a_merge_that_joins_two_channels_is_class_a(repo: Path):
    write(repo, "root.py", "x = 1\n")
    base = commit(repo, "root")
    subprocess.run(["git", "-C", str(repo), "branch", "side-a"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "side-a"], check=True)
    write(repo, "analysis/one.py", "y = 1\n")
    commit(repo, "analysis change")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
    subprocess.run(["git", "-C", str(repo), "branch", "side-b"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "side-b"], check=True)
    write(repo, "collectors/two.py", "z = 1\n")
    commit(repo, "collectors change")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
    subprocess.run(["git", "-C", str(repo), "merge", "--no-ff", "-m", "merge a", "side-a"], check=True)
    subprocess.run(["git", "-C", str(repo), "merge", "--no-ff", "-m", "merge b", "side-b"], check=True)
    scope = classify(repo, base)
    assert scope.klass == "A", scope.raw
    assert "merge" in scope.reason.lower(), scope.reason


def test_a_merge_that_only_brings_in_main_is_not_a_trigger(repo: Path):
    write(repo, "analysis/one.py", "y = 1\n")
    base = commit(repo, "root")
    subprocess.run(["git", "-C", str(repo), "branch", "feature"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "feature"], check=True)
    write(repo, "collectors/two.py", "z = 1\n")
    commit(repo, "feature change")
    subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-ff", "-m", "merge base into feature", base], check=True
    )
    scope = classify(repo, base)
    assert scope.klass != "A" or "merge" not in scope.reason.lower(), scope.raw


def test_a_none_class_change_set_runs_no_tests(repo: Path):
    write(repo, ".gitignore", "*.pyc\n")
    write(repo, "playbook/x.md", "# Old\n")
    write(repo, ".github/x.yml", "name: old\n")
    base = commit(repo, "before")
    write(repo, ".gitignore", "*.pyo\n")
    write(repo, "playbook/x.md", "# New\n")
    write(repo, ".github/x.yml", "name: new\n")
    commit(repo, "after")
    scope = classify(repo, base)
    assert scope.klass == "C", scope.raw
    assert scope.tests == [], scope.tests


def test_tests_snapshots_maps_to_the_cli_help_snapshot_test(repo: Path):
    scope = change(repo, "tests/snapshots/help.txt", "old\n", "new\n")
    assert "tests/test_cli_help.py" in scope.tests, scope.tests


def test_a_comment_only_trigger_file_next_to_a_real_mapped_change_is_class_b(repo: Path):
    # A trigger file proven unmoved does not raise the ceiling for the rest of the change: the real
    # code change decides the class on its own.
    write(repo, "contracts/ddl/needs/001.sql", "-- old\nCREATE TABLE a (id int);\n")
    write(repo, "analysis/linker/rules.py", PY_BEFORE)
    base = commit(repo, "before")
    write(repo, "contracts/ddl/needs/001.sql", "-- new\nCREATE TABLE a (id int);\n")
    write(repo, "analysis/linker/rules.py", PY_CHANGED)
    commit(repo, "after")
    scope = classify(repo, base)
    assert scope.klass == "B", scope.raw
    assert "tests/test_linker.py" in scope.tests, scope.tests


def test_a_comment_only_trigger_file_next_to_a_real_trigger_change_is_class_a(repo: Path):
    write(repo, "contracts/ddl/needs/001.sql", "-- old\nCREATE TABLE a (id int);\n")
    write(repo, "contracts/ddl/needs/002.sql", "CREATE TABLE b (id int);\n")
    base = commit(repo, "before")
    write(repo, "contracts/ddl/needs/001.sql", "-- new\nCREATE TABLE a (id int);\n")
    write(repo, "contracts/ddl/needs/002.sql", "CREATE TABLE b (id bigint);\n")
    commit(repo, "after")
    scope = classify(repo, base)
    assert scope.klass == "A", scope.raw
    assert "002.sql" in scope.reason, scope.reason


# ---------------------------------------------------------------------------------------------
# #232 Work 2: the fourth line -- what a still-class-A change owes locally once CI covers the
# whole tree. A trigger change, a gate change and a dynamic-import-root change all have a
# computed set; a truly unanswerable change (no base, an unmapped path) has none.
# ---------------------------------------------------------------------------------------------


def test_a_trigger_change_next_to_a_mapped_change_owes_the_computed_set(repo: Path):
    write(repo, "contracts/ddl/needs/001.sql", "CREATE TABLE a (id int);\n")
    write(repo, "analysis/linker/rules.py", PY_BEFORE)
    base = commit(repo, "before")
    write(repo, "contracts/ddl/needs/001.sql", "CREATE TABLE a (id bigint);\n")
    write(repo, "analysis/linker/rules.py", PY_CHANGED)
    commit(repo, "after")
    scope = classify(repo, base)
    assert scope.klass == "A", scope.raw
    assert "tests/test_linker.py" in scope.owed, scope.raw
    assert "tests/test_cli_help.py" in scope.owed, "the smoke set rides along in the owed set too"


def test_the_gate_deciding_its_own_change_owes_the_computed_set(repo: Path):
    scope = change(repo, "tool/change_scope.py", "x = 1\n", "x = 2\n")
    assert scope.klass == "A", scope.raw
    assert scope.owed, "the gate's own change still has a computed set to owe locally"


def test_a_dynamic_import_root_owes_the_computed_set(repo: Path):
    scope = change(repo, "analysis/registry.py", "x = 1\n", "x = 2\n")
    assert scope.klass == "A", scope.raw
    assert scope.owed, "a dynamic-import root still has a computed set to owe locally"


def test_an_unmapped_path_owes_nothing_it_is_unanswerable(repo: Path):
    scope = change(repo, "newthing/main.py", PY_BEFORE, PY_CHANGED)
    assert scope.klass == "A", scope.raw
    assert scope.owed == [], "an unmapped path has no computed set -- it stays A locally too"


def test_a_missing_base_owes_nothing_it_is_unanswerable(repo: Path):
    write(repo, "a.py", PY_BEFORE)
    commit(repo, "one")
    scope = classify(repo, "origin/nowhere")
    assert scope.klass == "A", scope.raw
    assert scope.owed == [], "no base means nothing about the change is known"


# ---------------------------------------------------------------------------------------------
# Review 2026-09-05 fix round: the none list must not preempt the readers map, and one-sided
# merges are trigger candidates too.
# ---------------------------------------------------------------------------------------------


def _load_real_module():
    """The module under test, loaded from THIS checkout's own tool/change_scope.py -- blocker 1
    asks for verification against the real repo, not only a fixture, because the real readers map
    is what has to actually name tests/test_corpus_import.py.
    """
    spec = importlib.util.spec_from_file_location("change_scope_real", CLASSIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_nested_readme_is_selected_by_the_real_repos_readers_map():
    # Review 2026-09-05 blocker 1: contracts/README.md is read by name (Path("contracts") /
    # "README.md") in tests/test_corpus_import.py -- the none list's bare "README" entry must not
    # swallow it before the readers map gets a look.
    module = _load_real_module()
    test_files = module.all_test_files(REPO_ROOT)
    found = module.readers_of(REPO_ROOT, "contracts/README.md", test_files, {})
    assert "tests/test_corpus_import.py" in found, found


def test_a_root_readme_only_change_is_class_c_with_no_tests(repo: Path):
    # A repository-root README has no test of its own and nothing nests inside it, unlike
    # contracts/README.md -- the bare "README" none-list entry is scoped to exactly this.
    scope = change(repo, "README.md", "# Old\n", "# New\n")
    assert scope.klass == "C", scope.raw
    assert scope.tests == [], scope.tests


def test_unreachable_does_not_flag_a_tool_star_reader_or_a_docs_test():
    # Review 2026-09-05 blocker 3 and 4: tests/test_ruff_extend_include.py is now pinned under
    # "tool/*" in [readers], and tests/test_agents_md.py is named by AGENTS.md and sits in `docs` --
    # `--unreachable`'s model has to see both, not just the map/readers-table/closure/smoke union.
    module = _load_real_module()
    unreachable = set(module.unreachable_tests(REPO_ROOT))
    assert "tests/test_ruff_extend_include.py" not in unreachable, unreachable
    assert "tests/test_agents_md.py" not in unreachable, unreachable


def test_an_ordinary_merge_into_an_unmoved_main_is_still_a_joining_merge(repo: Path):
    # Review 2026-09-05 blocker 2: the shape almost every wave merge and fork PR actually has --
    # main has not moved since the branch forked, so the first side of the merge is empty. The old
    # "both sides must be non-empty" rule let exactly this shape through as small.
    write(repo, "root.py", "x = 1\n")
    base = commit(repo, "root")
    subprocess.run(["git", "-C", str(repo), "branch", "feature"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "feature"], check=True)
    write(repo, "tool/thing.py", "y = 1\n")
    write(repo, "tests/test_thing.py", "def test_a():\n    assert 1\n")
    commit(repo, "feature change")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
    subprocess.run(["git", "-C", str(repo), "merge", "--no-ff", "-m", "merge feature", "feature"], check=True)
    scope = classify(repo, base)
    assert scope.klass == "A", scope.raw
    assert "merge" in scope.reason.lower(), scope.reason


# ---------------------------------------------------------------------------------------------
# #239: a short or extensionless basename ("test", "status", ...) is matched by its path, never
# by the bare basename -- the #232 push selected 174 test files this way.
# ---------------------------------------------------------------------------------------------


def test_tool_checks_test_selects_its_own_reader_and_not_a_daisomall_test():
    module = _load_real_module()
    test_files = module.all_test_files(REPO_ROOT)
    found = module.readers_of(REPO_ROOT, "tool/checks/test", test_files, {})
    assert "tests/tool/test_pre_push_hook.py" in found, found
    assert "tests/collectors/commerce/sources/test_daisomall.py" not in found, found


def test_tool_status_selects_its_own_reader_and_not_an_unrelated_test():
    module = _load_real_module()
    test_files = module.all_test_files(REPO_ROOT)
    found = module.readers_of(REPO_ROOT, "tool/status", test_files, {})
    assert "tests/tool/test_status_tool.py" in found, found
    assert "tests/retrieval/test_bm25.py" not in found, found


def test_an_ordinary_basename_still_matches_by_name_not_only_path():
    # contracts/formats.md is an ordinary name (not short, not extensionless, not on the generic
    # list) -- it keeps the basename rule that #231 built.
    module = _load_real_module()
    test_files = module.all_test_files(REPO_ROOT)
    found = module.readers_of(REPO_ROOT, "contracts/formats.md", test_files, {})
    assert "tests/test_corpus_import.py" in found, found


def test_unreachable_names_no_more_than_the_three_files_it_named_before():
    module = _load_real_module()
    unreachable = set(module.unreachable_tests(REPO_ROOT))
    before = {
        "tests/test_conftest_guard.py",
        "tests/test_lineage_reader_grants.py",
        "tests/test_no_orphaned_test_files.py",
    }
    assert unreachable <= before, unreachable
