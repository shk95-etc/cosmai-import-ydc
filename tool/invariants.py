"""Did this change move any code? `tool/checks/invariants <base> [paths...]` (#215).

The translation wave of #192 had to answer that about every file it touched and answered it with
three throwaway scripts. This is those three, kept:

  python    the AST with docstrings dropped -- and nothing else dropped. A string constant is code
            here: a SQL literal, a regex, an env-var name and a model default all live in one, and a
            gate that cannot see them is a gate that lets behaviour through as prose (#215 review C1)
  sql       the statements with their comments stripped, whitespace normalized OUTSIDE quotes only
  shell/js  the diff itself: every added and removed line must be a comment line (a shebang is not)
  hash      YAML, TOML, Dockerfile (any basename starting with `Dockerfile`, so `Dockerfile.cron`
            counts too, #231 Work 7c), crontab, .gitignore/.dockerignore, .env* -- the diff itself,
            every added and removed line must start with `#` (#230; no shebang exception, none of
            these run) -- except a Dockerfile's `# syntax=`/`# escape=` line, which Docker itself
            reads as a directive, not prose (#231 Work 7e)
  markdown  the anchors and literals a translation has to carry across (`§2`, `#214`, code spans)

`--strings-blanked` is the translation reviewer's mode, and only theirs: it also blanks every string
constant and collapses each f-string to the interpolations it carries, so a re-worded message reads
as unchanged. The push gate never passes it -- what it answers is "is this the same text?", which is
a smaller question than the one a class decision has to ask.

Every rule fails closed. A file that is new, gone, binary, unparsable or of a type no rule covers is
reported as differing, because "nothing changed" and "I could not tell" must not share an exit status.
"""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
import sys

JS_SUFFIXES = (".js", ".mjs", ".cjs")

# What a translation must carry across a Markdown file unchanged: section anchors, issue numbers and
# anything in backticks (a path, a command, a column name).
MARKDOWN_LITERAL = re.compile(r"§\s?[0-9A-Za-z.\-]+|#\d+|`[^`\n]+`")

COMMENT_PREFIXES = {"shell": ("#",), "js": ("//", "/*", "*/")}

# Extension-less paths compared under the "hash" rule (#230): a crontab entry has no suffix a name
# check could catch, so its directory decides.
HASH_PATH_PREFIXES = ("stack/crontab.d/",)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def blob(rev: str, path: str) -> str | None:
    """The file's content at a revision; None when it is absent there or is not text."""
    done = subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True)
    if done.returncode != 0:
        return None
    try:
        return done.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def kind_of(path: str, text: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    if name.endswith(".py"):
        return "python"
    if name.endswith(".sql"):
        return "sql"
    if name.endswith(".md"):
        return "markdown"
    if name.endswith(JS_SUFFIXES):
        return "js"
    if name.endswith(".sh"):
        return "shell"
    if name.endswith((".yml", ".yaml", ".toml")):
        return "hash"
    # Any basename starting with "Dockerfile" -- "Dockerfile", "Dockerfile.cron", and a
    # "*.Dockerfile" suffix all name the same kind of file (#231 Work 7c).
    if name.startswith("dockerfile") or name.endswith(".dockerfile"):
        return "dockerfile"
    if path.startswith(HASH_PATH_PREFIXES):
        return "hash"
    if name in (".gitignore", ".dockerignore"):
        return "hash"
    if name.startswith(".env"):
        return "hash"
    if "." not in name:
        # tool/ and .githooks/ hold python and shell as extension-less executables, so the shebang is
        # the only thing that says which is which.
        first = text.split("\n", 1)[0]
        if first.startswith("#!") and "python" in first:
            return "python"
        if first.startswith("#!") and "sh" in first:
            return "shell"
    return ""


class Blind(ast.NodeTransformer):
    """Every string constant becomes the same constant, so only the code around them is compared."""

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        # Read before recursing: a format_spec is itself a JoinedStr, and visiting it first would
        # blank `.2f` and `.0f` into the same thing -- a rounding change is behaviour, not wording
        # (#215 review, minor 6).
        specs = [
            (v.conversion, ast.dump(v.format_spec) if v.format_spec else "")
            for v in node.values
            if isinstance(v, ast.FormattedValue)
        ]
        self.generic_visit(node)
        values = [v for v in node.values if isinstance(v, ast.FormattedValue)]
        parts = sorted(
            f"{ast.dump(v.value)}!{conversion}:{spec}"
            for v, (conversion, spec) in zip(values, specs, strict=True)
        )
        return ast.Constant(value="<fstr:" + "|".join(parts) + ">")

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        return ast.Constant(value="<str>") if isinstance(node.value, str) else node


def strip_docstrings(tree: ast.AST) -> None:
    """Blanking a docstring is not enough: a module that GAINED one would still read as changed code."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        first = node.body[0] if node.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                node.body = node.body[1:] or [ast.Pass()]


def python_fingerprint(text: str, blank_strings: bool = False) -> str | None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    strip_docstrings(tree)
    if blank_strings:
        tree = Blind().visit(tree)
    return ast.dump(tree, include_attributes=False)


def sql_fingerprint(text: str) -> str:
    kept: list[str] = []
    code: list[str] = []
    i, end = 0, len(text)

    def flush() -> None:
        # Whitespace between tokens is layout; whitespace inside a literal is the value, so the two
        # are normalized apart -- `'a  b'` and `'a b'` are different rows (#215 review, minor 8).
        kept.append(" ".join("".join(code).split()))
        code.clear()

    while i < end:
        if text[i] == "'":
            # A quoted literal is data: a `--` inside it is part of the value, not a comment.
            j = i + 1
            while j < end:
                if text[j] == "'":
                    if text[j + 1 : j + 2] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            flush()
            kept.append(text[i:j])
            i = j
        elif text.startswith("--", i):
            newline = text.find("\n", i)
            i = end if newline < 0 else newline
            code.append(" ")
        elif text.startswith("/*", i):
            close = text.find("*/", i + 2)
            i = end if close < 0 else close + 2
            code.append(" ")
        else:
            code.append(text[i])
            i += 1
    flush()
    return hashlib.md5(" ".join(kept).encode("utf-8")).hexdigest()


def markdown_literals(text: str) -> list[str]:
    return sorted(match.group(0).replace(" ", "") for match in MARKDOWN_LITERAL.finditer(text))


def changed_lines(base: str, head: str, path: str) -> list[str]:
    diff = git("diff", "-U0", "--no-color", base, head, "--", path)
    out: list[str] = []
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line[:1] in ("+", "-"):
            out.append(line[1:].strip())
    return out


def is_comment_line(line: str, kind: str) -> bool:
    if not line:
        return True
    if kind == "shell":
        # `#!` is not a comment to the kernel: swapping the interpreter changes what runs.
        return line.startswith("#") and not line.startswith("#!")
    if kind == "hash":
        # Only a line whose diff text starts with `#` counts -- a `#` in the middle (a quoted value,
        # an inline comment beside a real change) never makes the line itself a comment (#230).
        return line.startswith("#")
    if kind == "dockerfile":
        if not line.startswith("#"):
            return False
        # `# syntax=` and `# escape=` are parser directives Docker itself reads before the first
        # instruction -- changing one changes what the build does, so it is not a comment (#231
        # Work 7e). Docker only honours them written this way (a leading "#" then no space).
        directive = line[1:].lstrip().lower()
        return not (directive.startswith("syntax=") or directive.startswith("escape="))
    # A block-comment continuation is `*` alone or `* something`; `*next() {}` is a generator method.
    return line == "*" or line.startswith("* ") or line.startswith(COMMENT_PREFIXES["js"])


def comment_only(base: str, head: str, path: str, kind: str) -> bool:
    return all(is_comment_line(line, kind) for line in changed_lines(base, head, path))


def differs(base: str, head: str, path: str, blank_strings: bool = False) -> str | None:
    """The reason this file is not provably unchanged, or None when it is."""
    before = blob(base, path)
    after = blob(head, path)
    if before is None:
        return "added, binary or unreadable at the base: there is nothing to compare it against"
    if after is None:
        return "removed, binary or unreadable at the head"
    kind = kind_of(path, before)
    if not kind:
        return "no invariant covers this file type"
    if kind == "python":
        one = python_fingerprint(before, blank_strings)
        two = python_fingerprint(after, blank_strings)
        if one is None or two is None:
            return "python that does not parse"
        return None if one == two else "the code differs (AST)"
    if kind == "sql":
        return None if sql_fingerprint(before) == sql_fingerprint(after) else "the statements differ"
    if kind == "markdown":
        lost = sorted(set(markdown_literals(before)) - set(markdown_literals(after)))
        gained = sorted(set(markdown_literals(after)) - set(markdown_literals(before)))
        if lost or gained:
            return f"anchors and literals changed (lost {lost}, gained {gained})"
        return None
    return None if comment_only(base, head, path, kind) else "a line that is not a comment changed"


def main(argv: list[str]) -> int:
    blank_strings = "--strings-blanked" in argv
    argv = [a for a in argv if a != "--strings-blanked"]
    if not argv:
        print("usage: invariants.py [--strings-blanked] <base> [paths...]", file=sys.stderr)
        return 2
    base, paths = argv[0], argv[1:]
    head = "HEAD"
    # The classifier asks about `<base>...HEAD`, so the comparison starts where the branch did:
    # against the tip, every commit main gained meanwhile would read as this branch's change.
    merge_base = subprocess.run(
        ["git", "merge-base", base, head], capture_output=True, text=True, check=False
    ).stdout.strip()
    start = merge_base or base
    # --no-renames: a rename reaches this as a delete plus an add, and both fail closed. Detected as
    # one move it would arrive as a single new path whose old content this cannot find.
    files = [
        f for f in git("diff", "--name-only", "--no-renames", start, head, "--", *paths).split("\n") if f
    ]
    bad = 0
    for path in files:
        reason = differs(start, head, path, blank_strings)
        if reason:
            print(f"{path}: {reason}")
            bad += 1
    print(f"invariants: {len(files)} file(s) compared against {base}, {bad} differ")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
