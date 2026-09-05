"""What verifying this change costs: `tool/change_scope.py <base>` (#215, #231).

Four lines on stdout, for `tool/checks/test --changed <base>` to read:

    <A|B|C|N>
    <the reason, in one line>
    <the test paths to run, space separated>
    <what a class-A verdict owes locally, space separated (#232 Work 2)>

A is the whole suite, B is the computed set (the mapped tests, the readers map, the import closure
and the smoke set, unioned), C is the format/lint/lang checks plus those test paths, and N is a tree
identical to the base -- format and lint, and nothing recorded.

The fourth line matters only when the verdict is A: CI now runs class A on every push (#232), so a
local push only owes the computed set the change would have earned as class B -- the gate list, a
dynamic-import root, a joining merge and the trigger list are all "A-owed" this way. It is empty
when the change is truly unanswerable (no base, an unmapped path, a vanished test file): there is
no smaller question to compute an answer to, so it stays class A locally too.

The questions are asked in that order of authority: the gate list first (unconditional -- no proof
talks it down), a dynamic-import root next (also unconditional -- nothing traces those imports),
a joining merge next, then the trigger list (#230: A unless tool/checks/invariants proves every
changed trigger-list file moved no code). Everything left goes through the map, the readers map
and the import closure together, per file; only a file none of those three claim falls back to the
no-answer paths (#231 item 3) -- checked last, not first, so a none-list path a test still reads by
name (`contracts/README.md`) is never preempted by the fact that it is also, say, a README (review
2026-09-05 blocker 1). Only then the cheap class for what is left -- so a path whose blast radius is
the repository can never be talked down by a guess about what its diff happens to look like (#215
review C1). Class C is not a guess either -- it is what tool/checks/invariants proved, file by
file, with every string constant compared as code.

`tool/change_scope.py --unreachable` prints, one per line, every tracked `tests/**/test_*.py` that
no map entry, reader entry, closure root or the smoke set could ever select for any change other
than the file's own (#231 Work 6; `tool/issue audit` calls this).
"""

from __future__ import annotations

import ast
import fnmatch
import subprocess
import sys
import tomllib
from pathlib import Path

FULL = "A"
PACKAGE = "B"
DOCS = "C"
NOTHING = "N"

# analysis.registry.load_implementations() imports these by name at runtime, not by any import a
# static closure can trace, so a change to either one has to cost the whole tree (#231 Work 1).
DYNAMIC_IMPORT_ROOTS = ("analysis/registry.py", "analysis/__init__.py")

# The packages a first-party import can name. `tool/` sits here for the record (option 10's list
# names it) but nothing imports `tool.*` today -- its files are scripts run by path, not modules
# (no `tool/__init__.py`), so no import ever resolves into it.
FIRST_PARTY_PACKAGES = ("analysis", "collectors", "cosmai", "db", "portal", "tool")


def git(*args: str, check: bool = True) -> str:
    return subprocess.run(["git", *args], check=check, capture_output=True, text=True).stdout.strip()


def toplevel() -> Path:
    return Path(git("rev-parse", "--show-toplevel"))


def load(root: Path) -> dict[str, object]:
    return tomllib.loads((root / "tests" / "scope.toml").read_text(encoding="utf-8"))


def is_test_file(path: str) -> bool:
    return path.startswith("tests/") and path.rsplit("/", 1)[-1].startswith("test_") and path.endswith(".py")


def exists_at_head(path: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"HEAD:{path}"], capture_output=True).returncode == 0


def is_none_path(path: str, none_list: list[str]) -> bool:
    """#231 item 3: a path nothing at runtime reads -- class C, no tests, comment-only or not.

    This is consulted only as the last resort, after the map, the readers map and the closure have
    all had a chance to claim the path (review 2026-09-05 blocker 1): `contracts/README.md` is read
    by name by several tests, and the none verdict must never preempt that.
    """
    name = path.rsplit("/", 1)[-1]
    for entry in none_list:
        if entry.endswith("/"):
            if path.startswith(entry):
                return True
        elif "." not in entry:
            # A bare name like "README" or "LICENSE" names the repository's own file, never a
            # same-named file nested somewhere else (contracts/README.md is not this) -- no "/"
            # in the path at all (review 2026-09-05 blocker 1).
            if "/" in path:
                continue
            if name == entry or name.startswith(entry + "."):
                return True
        elif name == entry:
            return True
    return False


def tests_subdir_target(path: str) -> list[str] | None:
    """#231 item 3: no path under tests/ is unanswerable. tests/snapshots/ names the one test that
    reads the snapshots; tests/fixtures/ is answered by the readers map alone (a fixture nobody
    names by basename really is unmeasured); anything else under a tests/ subdirectory runs that
    whole directory, the same as a mapped package.
    """
    if not path.startswith("tests/"):
        return None
    if path.startswith("tests/snapshots/"):
        return ["tests/test_cli_help.py"]
    if path.startswith("tests/fixtures/"):
        return None
    rest = path[len("tests/") :]
    if "/" not in rest:
        return None
    top = rest.split("/", 1)[0]
    return [f"tests/{top}"]


def scope_of(path: str, entries: dict[str, list[str]]) -> tuple[str, list[str]] | None:
    """The map entry covering one changed file and its tests, or None when nothing claims it."""
    for key in sorted(entries, key=len, reverse=True):
        if path.startswith(key):
            return key, entries[key]
    return None


def all_test_files(root: Path) -> list[str]:
    """Every `tests/**/test_*.py` tracked at HEAD -- what the readers map and the closure search.

    Fix-when-touched (review 2026-09-05): a reader that lives inside a `tests/conftest.py` fixture
    rather than a `test_*.py` file is invisible to both this scan and the readers map it feeds --
    `tests/conftest.py` is on the gate list, so a change to the fixture itself is already class A,
    but a change to whatever non-code file the fixture globs would slip past the readers map today.
    """
    out = git("ls-tree", "-r", "--name-only", "HEAD", "--", "tests").split("\n")
    return sorted(p for p in out if p.rsplit("/", 1)[-1].startswith("test_") and p.endswith(".py"))


# A basename this generic is a substring of nearly every test file in the tree -- "test", "status"
# and "issue" alone selected 174 files for the #232 push (#239). Kept as a named constant, one
# comment: extend it when a new bare-word basename shows the same over-selection, never inline.
GENERIC_BASENAMES = frozenset(
    {
        "test",
        "status",
        "issue",
        "format",
        "lint",
        "paths",
        "todo",
        "js",
        "journal",
        "ownership",
        "prerequisite",
        "invariants",
    }
)


def _is_generic_basename(basename: str) -> bool:
    """A basename that would over-match by itself: no extension at all, five characters or fewer,
    or a plain dictionary word from the hand-picked list above (#239).
    """
    return "." not in basename or len(basename) <= 5 or basename in GENERIC_BASENAMES


def readers_of(
    root: Path, resource: str, test_files: list[str], glob_readers: dict[str, list[str]]
) -> set[str]:
    """Every test that names `resource` by basename or by repo-relative path, plus the hand-listed
    glob readers (#231 Work 1b, 5) -- a test that discovers the file by glob/rglob/iterdir instead
    of naming it cannot be found by a text search, so those are pinned in tests/scope.toml.

    A generic basename (#239) is never matched bare: "test" or "status" alone is a substring of
    nearly every test file, so those are matched only by the full repo-relative path
    ("tool/checks/test") and by the path's last two segments ("checks/test") -- never the basename.
    """
    found: set[str] = set()
    basename = resource.rsplit("/", 1)[-1]
    segments = resource.split("/")
    last_two = "/".join(segments[-2:]) if len(segments) >= 2 else None
    generic = _is_generic_basename(basename)
    for tf in test_files:
        try:
            text = (root / tf).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if generic:
            if resource in text or (last_two is not None and last_two in text):
                found.add(tf)
        elif resource in text or basename in text:
            found.add(tf)
    for pattern, tests in glob_readers.items():
        if fnmatch.fnmatch(resource, pattern):
            found.update(tests)
    return found


def module_ancestors(name: str) -> list[str]:
    """Importing `a.b.c` also imports `a` and `a.b` -- a change to either package's `__init__.py`
    reaches every importer of the submodule the same way (#231 Work 1c).
    """
    parts = name.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


def named_first_party_modules(text: str) -> set[str]:
    """Every dotted name a module's imports could resolve to, first-party packages only. `from a.b
    import c` is ambiguous between "the submodule a.b.c" and "the name c inside module a.b" without
    resolving it against the tree, so both readings are kept -- an import edge that turns out not to
    resolve to a real file is simply dropped later, which only ever adds a false module, never
    misses one that exists (#215 review C1's bias applies here too).
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FIRST_PARTY_PACKAGES:
                    names.update(module_ancestors(alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue  # no relative imports in this tree (checked); skip rather than guess
            if node.module.split(".")[0] in FIRST_PARTY_PACKAGES:
                names.update(module_ancestors(node.module))
                for alias in node.names:
                    names.update(module_ancestors(f"{node.module}.{alias.name}"))
    return names


def module_to_file(root: Path, name: str) -> str | None:
    plain = name.replace(".", "/") + ".py"
    package = name.replace(".", "/") + "/__init__.py"
    if (root / plain).is_file():
        return plain
    if (root / package).is_file():
        return package
    return None


def tracked_python_files(root: Path) -> list[str]:
    out = git("ls-tree", "-r", "--name-only", "HEAD").split("\n")
    return [p for p in out if p.endswith(".py") and p.split("/", 1)[0] in FIRST_PARTY_PACKAGES]


def build_dependents(root: Path) -> dict[str, set[str]]:
    """file -> the first-party files that import it, one hop. #231 Work 1c's closure is the
    transitive reverse of this graph, from a changed module to every test that reaches it.
    """
    dependents: dict[str, set[str]] = {}
    for path in tracked_python_files(root) + all_test_files(root):
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for name in named_first_party_modules(text):
            target = module_to_file(root, name)
            if target and target != path:
                dependents.setdefault(target, set()).add(path)
    return dependents


def import_closure(dependents: dict[str, set[str]], changed: str, test_files: set[str]) -> set[str]:
    """Every `tests/**/test_*.py` that transitively imports the changed module (#231 Work 1c)."""
    seen: set[str] = set()
    frontier = {changed}
    while frontier:
        nxt: set[str] = set()
        for f in frontier:
            for dep in dependents.get(f, ()):
                if dep not in seen:
                    seen.add(dep)
                    nxt.add(dep)
        frontier = nxt
    return seen & test_files


def invariant_failures(root: Path, base: str, files: list[str]) -> list[str]:
    """Which of these files `tool/checks/invariants` could NOT prove moved no code, in the order
    the check itself reports them -- naming the file that actually failed, not just the first one
    changed (#231 Work 7d).
    """
    if not files:
        return []
    done = subprocess.run(
        ["sh", str(root / "tool" / "checks" / "invariants"), base, *files],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode == 0:
        return []
    failing = [f for line in done.stdout.splitlines() for f in files if line.startswith(f"{f}: ")]
    return failing or list(files)


def merge_trigger(root: Path, base: str) -> str | None:
    """A merge at HEAD whose two sides touch different top-level areas is a wave or a fork PR
    joining several channels, and earns class A the same as a trigger-list file (#231 Work 2). A
    merge whose second parent only brings in main is not: when the second parent IS the commit
    `base` resolves to, the merge is a branch catching itself up, not two channels meeting.

    Bailing only requires BOTH sides to be empty (review 2026-09-05 blocker 2): the ordinary shape
    -- branch off an unmoved main, merge straight back -- has an empty first side (main moved
    nothing of its own) and a real second side, and is exactly the wave/fork join this rule exists
    to catch, not the exception. An empty side differs from any non-empty one by definition, so it
    is treated as a trigger candidate the same as two non-empty, disjoint sides.
    """
    parents = [p for p in git("rev-parse", "HEAD^@", check=False).split("\n") if p]
    if len(parents) != 2:
        return None
    p1, p2 = parents
    base_commit = git("rev-parse", "--verify", "--quiet", base, check=False)
    if base_commit and p2 == base_commit:
        return None
    merge_base = git("merge-base", p1, p2, check=False)
    if not merge_base:
        return None
    side1 = [f for f in git("diff", "--name-only", "--no-renames", merge_base, p1).split("\n") if f]
    side2 = [f for f in git("diff", "--name-only", "--no-renames", merge_base, p2).split("\n") if f]
    if not side1 and not side2:
        return None
    if not side1 or not side2:
        return "a merge joins several channels"
    keys1 = {f.split("/", 1)[0] for f in side1}
    keys2 = {f.split("/", 1)[0] for f in side2}
    if keys1 - keys2 and keys2 - keys1:
        return "a merge joins several channels"
    return None


# Directories readers_of's basename/path scan is meant for -- data a test reads by name rather
# than code it imports (#231 Work 1b, review 2026-09-05 blocker 4). playbook/ returned here in
# #239: its snippets/ subdirectory carries copies of tool/checks/{test,format,lint,prerequisite}
# named with exactly those four generic basenames, which used to make a naive basename scan match
# nearly every test file in the tree (the word "test" alone); readers_of's generic-basename rule
# now matches those only by path ("tool-checks/test"), which none of those copies' actual readers
# use, so the scan is harmless and the audit sees the directory again.
READER_SCAN_DIRS = ("contracts/", "db/", "stack/", "eval/", "playbook/")


def non_code_reader_candidates(root: Path, trigger: list[str]) -> list[str]:
    """Every tracked non-`.py` file the readers scan could ever be asked about for a real change --
    the same universe `readers_of` is consulted over in `classify()`, minus whatever the trigger
    list would intercept first: a real change under `contracts/ddl/` never reaches the readers map
    at all (it is class A on its own), and a comment-only one short-circuits to class C before the
    readers map is consulted either, so a reader of such a file is not reachable through this path.
    """
    tracked = [p for p in git("ls-tree", "-r", "--name-only", "HEAD").split("\n") if p]
    out = []
    for path in tracked:
        if path.endswith(".py") or any(path.startswith(prefix) for prefix in trigger):
            continue
        if any(path.startswith(d) for d in READER_SCAN_DIRS) or "/fixtures/" in path:
            out.append(path)
    return out


def unreachable_tests(root: Path) -> list[str]:
    config = load(root)
    entries: dict[str, list[str]] = config["map"]  # type: ignore[assignment]
    glob_readers: dict[str, list[str]] = config.get("readers", {})  # type: ignore[assignment]
    docs: list[str] = config.get("docs", [])  # type: ignore[assignment]
    smoke: list[str] = config.get("smoke", [])  # type: ignore[assignment]
    trigger: list[str] = config.get("trigger", [])  # type: ignore[assignment]
    test_files_list = all_test_files(root)
    test_files = set(test_files_list)

    def mark(reachable: set[str], targets: list[str]) -> None:
        for target in targets:
            if target.endswith(".py"):
                reachable.add(target)
            else:
                # A directory entry (e.g. "tests/tool") selects everything under it, the same as
                # handing it to pytest -- not just a path equal to the string itself.
                reachable.update(f for f in test_files_list if f.startswith(target))

    reachable: set[str] = set()
    mark(reachable, smoke)
    mark(reachable, docs)
    for tests in entries.values():
        mark(reachable, tests)
    for tests in glob_readers.values():
        mark(reachable, tests)
    # The readers map's basename/path scan: the same rule readers_of applies per changed file,
    # run here over every candidate that could ever be the changed file (review blocker 4).
    for resource in non_code_reader_candidates(root, trigger):
        reachable.update(readers_of(root, resource, test_files_list, {}))
    # The import closure: a test reachable by SOME change needs only one resolvable first-party
    # import of its own -- a deeper (transitive) path always starts with a direct one, so a test
    # that is nobody's direct importer is nobody's indirect importer either.
    dependents = build_dependents(root)
    for deps in dependents.values():
        reachable.update(d for d in deps if is_test_file(d))
    return sorted(t for t in test_files if t not in reachable)


def try_computed_set(
    root: Path,
    files: list[str],
    entries: dict[str, list[str]],
    glob_readers: dict[str, list[str]],
    none_list: list[str],
    smoke: list[str],
) -> tuple[list[str], str | None]:
    """What a still-class-A change owes locally once CI, not this push, covers the whole tree
    (#232 Work 2): the same map/readers/closure pass `classify()` runs for class B, but over the
    full change -- trigger and gate files included -- rather than only what is left after they are
    stripped out. Empty when some file in the change is genuinely unclaimed (an unmapped path, or a
    test file that no longer exists): that change has no computed set, and stays class A locally
    too -- the second element names that file, for the classifier's reason line to blame (#232
    review), `None` when every file answered.
    """
    test_files_list = all_test_files(root)
    test_files = set(test_files_list)
    dependents = build_dependents(root) if any(p.endswith(".py") for p in files) else {}
    mapped: set[str] = set()
    readers: set[str] = set()
    closure: set[str] = set()
    for path in files:
        if is_test_file(path):
            if not exists_at_head(path):
                return [], path
            mapped.add(path)
            continue
        found = False
        covered = scope_of(path, entries)
        if covered is not None:
            _, covers = covered
            if covers:
                mapped.update(covers)
                found = True
        found_readers = readers_of(root, path, test_files_list, glob_readers)
        if found_readers:
            readers.update(found_readers)
            found = True
        if path.endswith(".py"):
            found_closure = import_closure(dependents, path, test_files)
            if found_closure:
                closure.update(found_closure)
                found = True
        if found:
            continue
        if is_none_path(path, none_list):
            continue
        if path.endswith(".md"):
            continue
        subdir_target = tests_subdir_target(path)
        if subdir_target is not None:
            mapped.update(subdir_target)
            continue
        return [], path
    return sorted(mapped | readers | closure | set(smoke)), None


def classify(base: str) -> tuple[str, str, list[str], list[str]]:
    root = toplevel()
    config = load(root)
    gate: list[str] = config["gate"]  # type: ignore[assignment]
    trigger: list[str] = config["trigger"]  # type: ignore[assignment]
    docs: list[str] = config["docs"]  # type: ignore[assignment]
    smoke: list[str] = config.get("smoke", [])  # type: ignore[assignment]
    entries: dict[str, list[str]] = config["map"]  # type: ignore[assignment]
    glob_readers: dict[str, list[str]] = config.get("readers", {})  # type: ignore[assignment]
    none_list: list[str] = config.get("none", [])  # type: ignore[assignment]

    if not git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False):
        # No files are known either, so there is nothing to feed try_computed_set: unanswerable,
        # locally as much as centrally (#232 Work 2).
        return FULL, f"the base {base} is not in this checkout, so nothing about the change is known", [], []

    # --no-renames so a rename arrives as a delete and an add, each judged on its own.
    files = [f for f in git("diff", "--name-only", "--no-renames", f"{base}...HEAD").split("\n") if f]
    if not files:
        return NOTHING, f"this tree is {base}'s own: there is no change to verify", [], []

    # A class-A reason plus what it owes locally (#232 review): the blocker, when the owed set
    # came back empty, says which file made it so -- "the gate list" alone does not say why a gate
    # change that should have had a computed set did not.
    def owed_for(reason: str) -> tuple[str, list[str]]:
        owed, blocker = try_computed_set(root, files, entries, glob_readers, none_list, smoke)
        if not owed and blocker:
            reason = f"{reason} (no computed set: {blocker} is unanswerable)"
        return reason, owed

    # The gate's own machinery decides before anything else is asked, and no proof talks it down: a
    # broken classifier that calls itself class C is the one failure nothing downstream can catch.
    # It still may have a computed set to owe locally (#232 Work 2) -- being unconditional is not
    # the same as being unanswerable.
    for path in files:
        for prefix in gate:
            if path.startswith(prefix):
                reason, owed = owed_for(f"{path} is on the gate list in tests/scope.toml")
                return FULL, reason, [], owed

    for path in files:
        if path in DYNAMIC_IMPORT_ROOTS:
            reason, owed = owed_for(
                f"{path} is a dynamic-import module: nothing traces that import statically"
            )
            return FULL, reason, [], owed

    joined = merge_trigger(root, base)
    if joined:
        reason, owed = owed_for(joined)
        return FULL, reason, [], owed

    # The trigger list is next, but #230 lets tool/checks/invariants prove a changed trigger-list
    # file moved no code before it forces A -- a comment-only edit to contracts/ddl/ is not still an
    # edit to the database (#215 review C1 is why this is proof, not a guess from the diff's shape).
    trigger_files = [p for p in files if any(p.startswith(prefix) for prefix in trigger)]
    failing = invariant_failures(root, base, trigger_files)
    if failing:
        reason, owed = owed_for(f"{failing[0]} is on the trigger list in tests/scope.toml")
        return FULL, reason, [], owed
    rest = [p for p in files if p not in trigger_files]

    test_files_list = all_test_files(root)
    test_files = set(test_files_list)
    dependents = build_dependents(root) if any(p.endswith(".py") for p in rest) else {}
    mapped: set[str] = set()
    readers: set[str] = set()
    closure: set[str] = set()
    keys: list[str] = []
    prose_tests: set[str] = set()
    code: list[str] = []
    none_only: list[str] = []
    for path in rest:
        if is_test_file(path):
            if not exists_at_head(path):
                return (
                    FULL,
                    f"{path} was a test file and is gone: nothing left in the tree measures that",
                    [],
                    [],
                )
            mapped.add(path)
            code.append(path)
            continue
        found = False
        covered = scope_of(path, entries)
        if covered is not None:
            key, covers = covered
            keys.append(key)
            if covers:
                # An entry that maps to no tests at all (e.g. "playbook/" = []) has not really
                # been claimed by anything -- leave `found` for the readers map, the closure, or
                # the none list to decide, so a solo playbook/ edit still gets the zero-test verdict
                # rather than the general docs bundle.
                mapped.update(covers)
                found = True
                if path.endswith(".md"):
                    prose_tests.update(covers)
        found_readers = readers_of(root, path, test_files_list, glob_readers)
        if found_readers:
            readers.update(found_readers)
            found = True
        if path.endswith(".py"):
            found_closure = import_closure(dependents, path, test_files)
            if found_closure:
                closure.update(found_closure)
                found = True
        if found:
            if not path.endswith(".md"):
                code.append(path)
            continue
        # No-answer paths (#231 item 3), consulted only now that the map, the readers map and the
        # closure have all had their say (review 2026-09-05 blocker 1) -- a real .gitignore/README/
        # .github/playbook edit that nothing else claims is class C, no tests, comment-only or not.
        # Checked before the generic Markdown skip below: playbook/ is prose too, and it must reach
        # the zero-test verdict the same as a non-Markdown none path, not the general docs bundle.
        if is_none_path(path, none_list):
            none_only.append(path)
            continue
        if path.endswith(".md"):
            continue  # prose with no test behind it costs nothing to verify
        subdir_target = tests_subdir_target(path)
        if subdir_target is not None:
            mapped.update(subdir_target)
            code.append(path)
            continue
        return FULL, f"{path} maps to no entry in tests/scope.toml", [], []

    # Nothing else in the change contributed a single test: whatever it touched, none of it is read
    # by anything (#231 item 3's "none" class carries no tests at all, not even the docs bundle).
    if none_only and not mapped and not readers and not closure and not code:
        return DOCS, f"{len(files)} file(s): none of them are read by any test", [], []

    # Only now the cheap class, and only for what is left: Markdown, plus code that tool/checks/
    # invariants proves moved nothing -- comments and docstrings, with every string constant compared.
    if not invariant_failures(root, base, code):
        return (
            DOCS,
            f"{len(files)} file(s): prose, or code tool/checks/invariants proves is unmoved",
            sorted(set(docs) | prose_tests | readers | closure),
            [],
        )
    where = ", ".join(sorted(set(keys))) or "(no mapped package)"
    tests = sorted(mapped | readers | closure | set(smoke))
    reason = (
        f"{len(files)} file(s) under {where}: "
        f"{len(mapped)} mapped · {len(readers)} readers · {len(closure)} closure · "
        f"{len(smoke)} smoke"
    )
    return PACKAGE, reason, tests, []


def main(argv: list[str]) -> int:
    if argv == ["--unreachable"]:
        for path in unreachable_tests(toplevel()):
            print(path)
        return 0
    if len(argv) != 1:
        print("usage: change_scope.py <base> | change_scope.py --unreachable", file=sys.stderr)
        return 2
    verdict, reason, tests, owed = classify(argv[0])
    print(verdict)
    print(reason)
    print(" ".join(tests))
    print(" ".join(owed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
