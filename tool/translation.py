"""The translation reviewer's mechanical checks, one script and one exit status (#234).

The 2026-09-04 translation waves (#207 #209 #210 #211, #206 part 2) each needed two or three review
rounds on fidelity, and every first round re-ran the same three checks the worker had already built
ad hoc: did any code move (`tool/invariants.py --strings-blanked`), does every `§` anchor still
resolve (the #206 resolver, made permanent here), and did a comment get dropped rather than carried
across. This makes the three checks one command a worker runs before the push and a reviewer starts
from.

Four checks, in report order:
  anchors     every `§` heading reference in the scanned directories resolves to a heading in
              contracts/*.md or to a name declared in contracts/section-names.md -- the ledger #206
              part 2 left behind as the declaration site
  invariants  `tool/invariants.py --strings-blanked <base>` over the changed files -- code movement
              hiding behind a re-worded string is still code movement
  hangul      a per-file count of Hangul lines removed and added between base and HEAD; an added line
              outside `tool/checks/lang`'s allowlist is a failure (that check owns the allowlist, this
              one only reads its verdict)
  compose     when a file under stack/ changed, `docker compose config` renders the same thing at base
              and at HEAD -- skipped with a note where docker is not installed

A file this cannot read at either end fails closed, same as tool/invariants.py.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import invariants  # noqa: E402  (sibling module, tool/invariants.py)

SCOPE_PATHS = ("contracts", "tests", "tool", "analysis", "db", "stack")

# A whole-document reference ("the contract's §entrypoints") names the file, not one of its headings.
CONTRACTS_BASENAMES = {
    "entrypoints",
    "interfaces",
    "formats",
    "ownership",
    "secrets",
    "versioning",
    "anon_exposure",
    "section-names",
    "README",
}

# A heading line's text, cut at the first parenthetical or dash aside -- "## Sensitivity and backtest
# (fork #41, ...)" names a heading "Sensitivity and backtest", not the whole line.
HEADING_LINE = re.compile(r"^#+\s+(.*)$")
HEADING_CUT = re.compile(r" \(| — | -- ")

# A `§` immediately after a GitHub issue number points into that issue's own numbering, not into this
# tree -- section-names.md documents the same exemption for its own examples.
ISSUE_ANCHOR = re.compile(r"#\d+\s*$")

# A `§` naming a section of a document this ledger does not track (STATE.md, a TEAM_DECISIONS proposal,
# an architect/ note, a skill's own sections) is out of this resolver's scope -- none of those
# numbering schemes are declared here, and none should be.
EXTERNAL_DOC = re.compile(
    r"(STATE\.md|TEAM_DECISIONS(?:_v[\d.]+)?|architect/\S*|claude-api\s+skill|\bskill)\s*$",
    re.IGNORECASE,
)

# A backtick-quoted filename right before a `§` names the document the heading lives in -- when that
# file is not one of contracts/*.md, the heading is that other document's own, not this ledger's.
BACKTICK_MD_REF = re.compile(r"`([^`]+\.md)`\s*$")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def show(rev: str, path: str) -> str | None:
    done = subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True)
    if done.returncode != 0:
        return None
    try:
        return done.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def merge_base_or(base: str) -> str:
    done = subprocess.run(["git", "merge-base", base, "HEAD"], capture_output=True, text=True)
    return done.stdout.strip() or base


def is_hangul(ch: str) -> bool:
    if not ch:
        return False
    o = ord(ch)
    return (
        0x1100 <= o <= 0x11FF
        or 0x3130 <= o <= 0x318F
        or 0xA960 <= o <= 0xA97F
        or 0xAC00 <= o <= 0xD7A3
        or 0xFFA0 <= o <= 0xFFDC
    )


def is_word_char(ch: str) -> bool:
    return bool(ch) and (ch.isalnum() or is_hangul(ch))


def ls_tree(rev: str, *paths: str) -> list[str]:
    out = git("ls-tree", "-r", "--name-only", rev, "--", *paths)
    return [line for line in out.split("\n") if line]


# ---------- (b) the `§` resolver ----------


def build_anchor_vocab(rev: str) -> set[str]:
    """Every declared heading name at `rev`: contracts/*.md headings plus section-names.md's ledger."""
    vocab: set[str] = set(CONTRACTS_BASENAMES)
    ledger = show(rev, "contracts/section-names.md")
    if ledger:
        vocab |= set(re.findall(r"`§([^`]+)`", ledger))
    for path in ls_tree(rev, "contracts"):
        if not path.endswith(".md") or path == "contracts/section-names.md":
            continue
        text = show(rev, path)
        if not text:
            continue
        for line in text.split("\n"):
            match = HEADING_LINE.match(line)
            if not match:
                continue
            name = HEADING_CUT.split(match.group(1), 1)[0].strip()
            if name:
                vocab.add(name)
    return vocab


def resolve_anchor(rest_flat: str, vocab_sorted: list[str]) -> bool:
    for name in vocab_sorted:
        if rest_flat.startswith(name):
            nxt = rest_flat[len(name) : len(name) + 1]
            if is_word_char(nxt):
                continue
            return True
    return False


def python_prose_lines(text: str) -> dict[int, str]:
    """The docstring lines and `#` comment lines of a Python file -- its only prose.

    An ordinary string literal (a test fixture's invented anchor, this module's own format strings)
    is a placeholder or data, not an anchor a translation review should judge (#234 review, blocker 1).
    """
    import io
    import tokenize

    lines = text.split("\n")
    prose: dict[int, str] = {}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            body = node.body
            first = body[0] if body else None
            if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)):
                continue
            if not isinstance(first.value.value, str):
                continue
            start, end = first.value.lineno, getattr(first.value, "end_lineno", first.value.lineno)
            for ln in range(start, end + 1):
                if 1 <= ln <= len(lines):
                    prose[ln] = lines[ln - 1]
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                prose[tok.start[0]] = lines[tok.start[0] - 1]
    except Exception:  # noqa: BLE001 -- unparsable python: comments are lost, docstrings still counted
        pass
    return prose


def prose_lines_for(path: str, kind: str, text: str) -> dict[int, str]:
    """The lines of `path` a translation review actually judges -- not a code or fixture string.

    A comment or docstring under `tool/` or `tests/` TALKS ABOUT anchors (this very module's own
    examples, a test's invented-anchor fixture) rather than citing one -- never scanned, no matter what
    it says (#234 review round 2, blocker 1 was still reachable through that gap).
    """
    lines = text.split("\n")
    if kind == "markdown":
        return dict(enumerate(lines, start=1))
    if path.startswith(("tool/", "tests/")):
        return {}
    if kind == "python":
        return python_prose_lines(text)
    if kind in ("sql", "shell", "js", "hash", "dockerfile"):
        return {i: line for i, line in enumerate(lines, start=1) if is_comment(line, kind)}
    return {}


def out_of_scope(before: str, prev_line: str) -> bool:
    """True when this `§` points outside this ledger's territory, not at one of its own headings."""
    if ISSUE_ANCHOR.search(before) or EXTERNAL_DOC.search(before):
        return True
    match = BACKTICK_MD_REF.search(before)
    if match:
        stem = match.group(1).rsplit("/", 1)[-1].removesuffix(".md")
        if stem not in CONTRACTS_BASENAMES:
            return True
    # `§` opens the line's real content (only a comment marker precedes it) -- the reference may
    # continue a wrapped line, so the PREVIOUS line's own ending decides instead ("...#10\n§A-2").
    if not before.strip(" \t#/*-") and prev_line:
        return ISSUE_ANCHOR.search(prev_line) is not None or EXTERNAL_DOC.search(prev_line) is not None
    return False


def anchor_tokens(path: str, prose: dict[int, str], all_lines: list[str]) -> list[tuple[int, str, str]]:
    """(line, the raw text right after `§`, the display snippet) for every in-scope token in `prose`."""
    found: list[tuple[int, str, str]] = []
    for i in sorted(prose):
        line = prose[i]
        for m in re.finditer("§", line):
            pos = m.start()
            rest = line[pos + 1 :]
            if not rest or not (rest[0].isalpha() or is_hangul(rest[0])):
                continue  # not a name token: a bare `§`, or one followed by a digit/punctuation
            before = line[:pos]
            prev_line = all_lines[i - 2] if i >= 2 else ""
            if out_of_scope(before, prev_line):
                continue
            found.append((i, rest, rest[:40].rstrip()))
    return found


def check_anchors(rev: str, paths: list[str]) -> tuple[int, list[str]]:
    """(count of anchor tokens judged, "path:line: snippet" for each in `paths` that does not resolve)."""
    vocab_sorted = sorted(build_anchor_vocab(rev), key=len, reverse=True)
    judged = 0
    failures: list[str] = []
    for path in paths:
        text = show(rev, path)
        if text is None:
            continue
        kind = invariants.kind_of(path, text)
        prose = prose_lines_for(path, kind, text)
        if not prose:
            continue
        all_lines = text.split("\n")
        for i, rest, snippet in anchor_tokens(path, prose, all_lines):
            judged += 1
            rest_flat = rest.replace("`", "")
            if not resolve_anchor(rest_flat, vocab_sorted):
                failures.append(f"{path}:{i}: §{snippet}")
    return judged, failures


# ---------- (a) invariants ----------


def string_literal_positions(text: str) -> list[tuple[int, str]] | None:
    """Every string `Constant`'s (line, value), in `ast.walk` order -- position i means the same thing
    in two trees whose blanked fingerprint already matched (#234 review, blocker 2)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    invariants.strip_docstrings(tree)
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def moved_literals(before_text: str, after_text: str) -> tuple[list[int], int] | None:
    """(head-side lines of a moved literal, count of literals translated in place) or None if unusable.

    `--strings-blanked` proves the shape is unchanged but blanks every literal alike, so a pure swap
    between two positions reads as "0 differ" -- a changed position whose new value is some OTHER
    changed position's old value moved rather than translated.
    """
    before = string_literal_positions(before_text)
    after = string_literal_positions(after_text)
    # An f-string whose interpolation count changed shifts every later Constant's list index even
    # though `--strings-blanked` (`Blind.visit_JoinedStr`) already judged the structure identical --
    # this skips per-position pairing for the whole file rather than risk a wrong pairing (fix-when-
    # touched, #234 review round 2: no reported case yet, only a length-mismatch escape hatch).
    if before is None or after is None or len(before) != len(after):
        return None
    changed = [i for i, ((_, ov), (_, nv)) in enumerate(zip(before, after, strict=True)) if ov != nv]
    old_by_value: dict[str, list[int]] = {}
    for i in changed:
        old_by_value.setdefault(before[i][1], []).append(i)

    moved_lines: list[int] = []
    for i in changed:
        new_value = after[i][1]
        if any(j != i for j in old_by_value.get(new_value, [])):
            moved_lines.append(after[i][0])
    translated = len(changed) - len(moved_lines)
    return sorted(set(moved_lines)), translated


def check_invariants(base: str, files: list[str]) -> tuple[list[str], dict[str, int]]:
    start = merge_base_or(base)
    failures: list[str] = []
    translated_counts: dict[str, int] = {}
    for path in files:
        reason = invariants.differs(start, "HEAD", path, blank_strings=True)
        if reason:
            failures.append(f"{path}: {reason}")
            continue
        before = invariants.blob(start, path)
        after = invariants.blob("HEAD", path)
        if before is None or after is None or invariants.kind_of(path, before) != "python":
            continue
        moved = moved_literals(before, after)
        if moved is None:
            continue
        moved_lines, translated = moved
        for line in moved_lines:
            failures.append(f"{path}:{line}: a string literal moved rather than translated")
        if translated:
            translated_counts[path] = translated
    return failures, translated_counts


# ---------- (c) the Hangul-line ledger ----------


def hangul_changed_lines(base: str, path: str, prefix: str) -> int:
    diff = git("diff", "-U0", "--no-color", base, "HEAD", "--", path)
    count = 0
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if not line.startswith(prefix):
            continue
        if any(is_hangul(ch) for ch in line[1:]):
            count += 1
    return count


def python_comment_lines(text: str) -> set[int]:
    """1-based line numbers holding a real `#` comment -- not a docstring line that starts with one.

    A docstring line quoting an issue ("#138 fixed...") is prose, and `tokenize` is what tells the two
    apart; a plain `line.startswith("#")` check cannot (#234 review, this ledger's own false positive).
    """
    import io
    import tokenize

    lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                lines.add(tok.start[0])
    except Exception:  # noqa: BLE001 -- unparsable python fails open here; invariants.py fails it closed
        return set()
    return lines


def is_comment(line: str, kind: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if kind == "sql":
        return stripped.startswith("--") or stripped.startswith("/*") or stripped.startswith("*")
    if kind in ("shell", "js", "hash", "dockerfile"):
        return invariants.is_comment_line(stripped, kind)
    return False


HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def dropped_comment_hunks(base: str, path: str, kind: str, before_text: str, after_text: str) -> list[int]:
    """The old-side line number of every hunk that removed a comment line and added none."""
    py_before = python_comment_lines(before_text) if kind == "python" else None
    py_after = python_comment_lines(after_text) if kind == "python" else None

    diff = git("diff", "-U0", "--no-color", base, "HEAD", "--", path)
    hunks: list[int] = []
    old_start = 0
    removed: list[tuple[int, str]] = []
    added: list[tuple[int, str]] = []

    def line_is_comment(no: int, text: str, side_lines: set[int] | None) -> bool:
        return no in side_lines if side_lines is not None else is_comment(text, kind)

    def flush() -> None:
        removed_comment = any(line_is_comment(n, t, py_before) for n, t in removed)
        added_comment = any(line_is_comment(n, t, py_after) for n, t in added)
        if removed_comment and not added_comment:
            hunks.append(old_start)

    in_hunk = False
    old_line = new_line = 0
    for line in diff.splitlines():
        header = HUNK_HEADER.match(line)
        if header:
            flush()
            old_start = int(header.group(1))
            old_line, new_line = old_start, int(header.group(3))
            removed, added = [], []
            in_hunk = True
            continue
        if not in_hunk:
            continue  # the "diff --git" / "index" / "--- a/…" / "+++ b/…" preamble, not a hunk
        if line.startswith("-"):
            removed.append((old_line, line[1:]))
            old_line += 1
        elif line.startswith("+"):
            added.append((new_line, line[1:]))
            new_line += 1
    flush()
    return hunks


def check_hangul(base: str, files: list[str]) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """Per-file (removed, added) Hangul-line counts, plus lang's allowlist verdict and dropped comments."""
    start = merge_base_or(base)
    ledger: dict[str, tuple[int, int]] = {}
    for path in files:
        removed = hangul_changed_lines(start, path, "-")
        added = hangul_changed_lines(start, path, "+")
        if removed or added:
            ledger[path] = (removed, added)

    lang_check = HERE / "checks" / "lang"
    done = subprocess.run(["sh", str(lang_check), "--range", base], capture_output=True, text=True)
    failures: list[str] = []
    if done.returncode != 0:
        for line in done.stderr.splitlines():
            if ":" in line and line.split(":", 1)[0] in ledger:
                failures.append(line)

    for path in files:
        before = invariants.blob(start, path)
        after = invariants.blob("HEAD", path)
        if before is None or after is None:
            continue
        kind = invariants.kind_of(path, before)
        if not kind or kind == "markdown":
            continue
        for old_start in dropped_comment_hunks(start, path, kind, before, after):
            failures.append(f"{path}:{old_start}: a comment was dropped, not carried across")
    return ledger, failures


# ---------- (d) docker compose config ----------


def render_compose(rev: str, workdir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-f", str(workdir / "stack" / "docker-compose.yml"), "config"],
        capture_output=True,
        text=True,
        cwd=str(workdir),
    )


def check_compose(base: str, changed: list[str]) -> tuple[str, str]:
    """('ok'|'differs'|'skipped'|'error', a note).

    Both sides erroring alike (an unset `COSMAI_*` on a clean host) says nothing about the translation
    -- only an asymmetric result (one side renders, the other doesn't, or they render differently) is
    this check's business (#234 review, fix-when-touched).
    """
    if not any(f.startswith("stack/") for f in changed):
        return "ok", "no stack/ file changed"
    if shutil.which("docker") is None:
        return "skipped", "docker is not installed on this host"

    with tempfile.TemporaryDirectory(prefix="cosmai-translation-compose-") as tmp:
        results: dict[str, subprocess.CompletedProcess] = {}
        for rev, name in ((merge_base_or(base), "base"), ("HEAD", "head")):
            wt = Path(tmp) / name
            git("worktree", "add", "--detach", str(wt), rev)
            try:
                results[name] = render_compose(rev, wt)
            finally:
                git("worktree", "remove", "--force", str(wt))
        base_done, head_done = results["base"], results["head"]
        base_ok, head_ok = base_done.returncode == 0, head_done.returncode == 0
        if base_ok and head_ok:
            if base_done.stdout == head_done.stdout:
                return "ok", "byte-identical"
            return "differs", "docker compose config differs between base and HEAD"
        if not base_ok and not head_ok and base_done.stderr == head_done.stderr:
            first_line = base_done.stderr.strip().splitlines()[0] if base_done.stderr.strip() else ""
            return "skipped", f"errors identically at base and HEAD -- {first_line}"
        return "error", (
            f"asymmetric: base {'ok' if base_ok else 'error'}, head {'ok' if head_ok else 'error'}"
        )


# ---------- report ----------


def undeclared_anchors(rev: str) -> list[str]:
    """Every undeclared `§` anchor anywhere in the tree at `rev` -- an audit item, not this exit
    status: pre-existing gaps in section-names.md are not this branch's fault (#234 review, blocker 1).
    """
    _, failures = check_anchors(rev, ls_tree(rev, *SCOPE_PATHS))
    return failures


def main(argv: list[str]) -> int:
    if "--undeclared" in argv:
        for line in undeclared_anchors("HEAD"):
            print(line)
        return 0
    if not argv:
        print("usage: translation.py <base> | translation.py --undeclared", file=sys.stderr)
        return 2
    base = argv[0]
    start = merge_base_or(base)
    changed = [f for f in git("diff", "--name-only", "--no-renames", start, "HEAD").split("\n") if f]

    inv_failures, translated_counts = check_invariants(base, changed)
    anchor_count, anchor_failures = check_anchors("HEAD", changed)
    hangul_ledger, hangul_failures = check_hangul(base, changed)
    compose_status, compose_note = check_compose(base, changed)

    print(f"tool/checks/translation against {base} ({len(changed)} file(s) changed)")
    print()
    print(f"invariants: {len(changed)} file(s), {len(inv_failures)} differ")
    for line in inv_failures:
        print(f"  {line}")
    for path, count in sorted(translated_counts.items()):
        print(f"  {path}: {count} literal(s) translated")
    print()
    print(f"anchors: {anchor_count} §token(s) judged, {len(anchor_failures)} unresolved")
    for line in anchor_failures:
        print(f"  {line}")
    print()
    print(
        f"hangul: {len(hangul_ledger)} file(s) with a Hangul-line change, "
        f"{len(hangul_failures)} outside the allowlist"
    )
    for path, (removed, added) in sorted(hangul_ledger.items()):
        print(f"  {path}: -{removed} +{added}")
    for line in hangul_failures:
        print(f"  {line}")
    print()
    print(f"compose: {compose_status} ({compose_note})")

    bad = (
        bool(inv_failures)
        or bool(anchor_failures)
        or bool(hangul_failures)
        or compose_status in ("differs", "error")
    )
    print()
    print(f"translation: {'FAIL' if bad else 'PASS'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
