"""#215, #231: the risk classifier is only as honest as `tests/scope.toml`.

A directory nobody mapped falls to class A, which is safe but silent -- the map would rot into
"everything is class A" one unmapped package at a time, and nobody would notice because the gate
stays green. So every code directory must be named here, and every test path named here must exist:
a renamed test file would otherwise take its package's whole scope down with it, and pytest exits 4
on a path that is not there, which reads as a broken gate rather than a stale map.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCOPE = REPO_ROOT / "tests" / "scope.toml"

# A test that discovers one of these directories' files by glob/rglob/iterdir rather than by name
# is invisible to the readers map (#231 Work 5) -- it has to be pinned in tests/scope.toml's
# [readers] table by hand instead. contracts/ and db/ are not in this set: every glob-discovering
# test there reads contracts/ddl/, db/views/, db/migrate.sh or db/bootstrap*.sql, which are on the
# trigger list -- a real change there is class A regardless of any reader, and a comment-only one
# short-circuits to class C before the readers map is even consulted, so pinning them here would
# name tests the classifier can never actually reach through this path.
GLOB_READER_DIRS = ("stack", "playbook")
ASSIGN_DIR = re.compile(r'(\w+)\s*=\s*[^\n]*["\'](' + "|".join(GLOB_READER_DIRS) + r')["\']')
GLOB_CALL = re.compile(r"(\w+)\.(?:glob|rglob|iterdir)\(")

# A directory holds code when it holds one of these; anything else is data or prose, and the
# classifier reaches those through class C or through class A's unmapped arm.
CODE_SUFFIXES = frozenset({".py", ".sql", ".sh", ".js", ".mjs", ".cjs"})
# tests/ is the one code directory with no entry: a changed `tests/**/test_*.py` is its own scope,
# and anything else under tests/ (a fixture, a snapshot, conftest.py) is class A.
NOT_MAPPED = frozenset({"tests"})


def scope() -> dict[str, object]:
    return tomllib.loads(SCOPE.read_text(encoding="utf-8"))


def tracked() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"], check=True, capture_output=True, text=True
    ).stdout
    return out.splitlines()


def code_directories() -> set[str]:
    found: set[str] = set()
    for path in tracked():
        head, _, rest = path.partition("/")
        if not rest or head in NOT_MAPPED:
            continue
        if Path(path).suffix in CODE_SUFFIXES or "." not in Path(path).name:
            found.add(head)
    return found


def test_every_top_level_code_directory_has_an_entry():
    mapped = set(scope()["map"])  # type: ignore[arg-type]
    missing = sorted(d for d in code_directories() if f"{d}/" not in mapped)
    assert not missing, f"tests/scope.toml maps no tests for {missing}; they would all be class A"


def test_every_analysis_package_has_an_entry():
    # A package is where a change is small enough for class B to be worth anything, so this is the
    # granularity the map exists for.
    mapped = set(scope()["map"])  # type: ignore[arg-type]
    packages = sorted(
        f"analysis/{p.name}/" for p in (REPO_ROOT / "analysis").iterdir() if (p / "__init__.py").exists()
    )
    missing = [p for p in packages if p not in mapped]
    assert not missing, f"tests/scope.toml has no entry for {missing}"


def test_every_mapped_test_path_exists():
    entries: dict[str, list[str]] = scope()["map"]  # type: ignore[assignment]
    missing = sorted(
        f"{key} -> {path}"
        for key, paths in entries.items()
        for path in paths
        if not (REPO_ROOT / path).exists()
    )
    assert not missing, f"stale entries in tests/scope.toml: {missing}"


def test_the_class_c_test_paths_exist():
    missing = [path for path in scope()["docs"] if not (REPO_ROOT / path).exists()]  # type: ignore[union-attr]
    assert not missing, f"tests/scope.toml `docs` names a test that is gone: {missing}"


def test_the_trigger_list_names_paths_that_are_really_there():
    # An entry with a typo in it silently stops forcing the full suite for the path it meant.
    # `db/bootstrap` and `db/migrate.sh` are intentionally not full paths with an extension of
    # their own directory -- they name a file prefix, not a path that has to resolve on its own.
    for path in scope()["trigger"]:  # type: ignore[union-attr]
        assert (REPO_ROOT / path).exists() or list(REPO_ROOT.glob(f"{path}*")), (
            f"tests/scope.toml `trigger` names a path that does not exist: {path}"
        )


def test_the_smoke_set_names_paths_that_are_really_there():
    missing = [path for path in scope()["smoke"] if not (REPO_ROOT / path).exists()]  # type: ignore[union-attr]
    assert not missing, f"tests/scope.toml `smoke` names a test that is gone: {missing}"


def test_every_glob_reader_is_listed():
    readers: dict[str, list[str]] = scope()["readers"]  # type: ignore[assignment]
    entries: dict[str, list[str]] = scope()["map"]  # type: ignore[assignment]
    listed_by_dir: dict[str, set[str]] = {d: set() for d in GLOB_READER_DIRS}
    for pattern, tests in readers.items():
        top = pattern.split("/", 1)[0]
        if top in listed_by_dir:
            listed_by_dir[top].update(tests)
    missing = []
    for test_path in tracked():
        if not (test_path.startswith("tests/") and Path(test_path).name.startswith("test_")):
            continue
        text = (REPO_ROOT / test_path).read_text(encoding="utf-8")
        dirs_by_token = dict(ASSIGN_DIR.findall(text))
        for match in GLOB_CALL.finditer(text):
            directory = dirs_by_token.get(match.group(1))
            if not directory or test_path in listed_by_dir[directory]:
                continue
            # Already reachable through the [map] entry for the same directory (e.g. a test under
            # tests/stack/ globbing stack/ -- the "stack/" -> ["tests/stack"] entry already selects
            # it), so it is not really the gap the readers map has to close by hand.
            mapped_covers = entries.get(f"{directory}/", [])
            if any(test_path.startswith(c) for c in mapped_covers):
                continue
            missing.append(f"{test_path} reads {directory}/ by glob but is not in [readers]")
    assert not missing, missing


def test_the_gate_list_names_paths_that_are_really_there():
    missing = [path for path in scope()["gate"] if not (REPO_ROOT / path).exists()]  # type: ignore[union-attr]
    assert not missing, f"tests/scope.toml `gate` names a path that does not exist: {missing}"


def test_the_gate_itself_is_class_a():
    # The classifier deciding its own change is small is the failure this list exists to prevent
    # (#230: unconditional -- unlike `full`, no proof talks a gate file down from class A).
    gate = set(scope()["gate"])  # type: ignore[arg-type]
    for path in ("tests/scope.toml", "tool/change_scope.py", "tool/checks/test", "tests/conftest.py"):
        assert path in gate, f"{path} must force the full suite unconditionally"
