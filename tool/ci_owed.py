"""Whether class A is owed for one open branch's diff against main (#233, #228 D4').

`tool/issue ready` shows the CI status of an issue branch's tip next to whether it *should*
be green at A -- a branch that never touched the trigger or gate list can merge on its
computed set, but one that did owes the whole-suite question the merge checklist has to wait
for. This is cheaper than `tool/change_scope.py`'s own classify(): it never checks the branch
out, only diffs two refs already on `origin`, so `ready` can afford it for every open branch.

`tool/change_scope.py` is read, never edited, through the same dynamic-import the tests use
(`importlib.util.spec_from_file_location`) -- it has no `tool/__init__.py`, so it is not an
importable package, and its `git()`/`load()` helpers are the ones this reuses rather than
duplicating the subprocess plumbing.

Usage: `python3 tool/ci_owed.py <ref> <base>` -- prints "owed" or "not owed" on stdout.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType


def _load_change_scope(root: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("change_scope", root / "tool" / "change_scope.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _merge_joins_channels(cs: ModuleType, ref: str, base: str) -> bool:
    """A cheap stand-in for `cs.merge_trigger`, parametrized on `ref` instead of hardcoded HEAD --
    the same disjoint-top-level-directory test, since #233 never checks the branch out.
    """
    parents = [p for p in cs.git("rev-parse", f"{ref}^@", check=False).split("\n") if p]
    if len(parents) != 2:
        return False
    p1, p2 = parents
    base_commit = cs.git("rev-parse", "--verify", "--quiet", base, check=False)
    if base_commit and p2 == base_commit:
        return False
    merge_base = cs.git("merge-base", p1, p2, check=False)
    if not merge_base:
        return False
    side1 = [f for f in cs.git("diff", "--name-only", "--no-renames", merge_base, p1).split("\n") if f]
    side2 = [f for f in cs.git("diff", "--name-only", "--no-renames", merge_base, p2).split("\n") if f]
    if not side1 and not side2:
        return False
    if not side1 or not side2:
        return True
    keys1 = {f.split("/", 1)[0] for f in side1}
    keys2 = {f.split("/", 1)[0] for f in side2}
    return bool(keys1 - keys2 and keys2 - keys1)


def owed(ref: str, base: str) -> bool:
    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True
        ).stdout.strip()
    )
    cs = _load_change_scope(root)
    if not cs.git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False):
        return False
    if not cs.git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False):
        return False
    config = cs.load(root)
    gate: list[str] = config.get("gate", [])  # type: ignore[assignment]
    trigger: list[str] = config.get("trigger", [])  # type: ignore[assignment]
    prefixes = [*gate, *trigger]
    files = [f for f in cs.git("diff", "--name-only", "--no-renames", f"{base}...{ref}").split("\n") if f]
    if any(path.startswith(prefix) for path in files for prefix in prefixes):
        return True
    return _merge_joins_channels(cs, ref, base)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: ci_owed.py <ref> <base>", file=sys.stderr)
        return 2
    print("owed" if owed(argv[0], argv[1]) else "not owed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
