"""tool/checks/translation <base>: the translation reviewer's mechanical checks, one exit status (#234).

Every 2026-09-04 translation wave's first review round re-derived the same three checks by hand --
did any code move, does every `§` anchor resolve, did a comment get dropped instead of carried across.
This script is those checks, kept, so a reviewer's first round starts from its report instead of
re-running them.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK = REPO_ROOT / "tool" / "checks" / "translation"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "translation_check"

CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

ANCHOR_SCRIPT = (
    "import sys; sys.path.insert(0, {tool!r}); import translation;"
    "judged, failures = translation.check_anchors({rev!r}, {paths!r});"
    "print(judged);"
    "[print(f) for f in failures]"
)


def check_anchors_direct(repo: Path, rev: str, paths: list[str]) -> subprocess.CompletedProcess:
    """`check_anchors` in isolation -- no invariants/hangul/compose noise from an unrelated content
    change in the fixture (#234 review round 2: two new out-of-scope rules)."""
    code = ANCHOR_SCRIPT.format(tool=str(REPO_ROOT / "tool"), rev=rev, paths=paths)
    return subprocess.run(
        ["python3", "-c", code], cwd=str(repo), capture_output=True, text=True, env=CLEAN_ENV
    )


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo, isolated from THIS checkout (#60 GIT_DIR)."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, env=CLEAN_ENV)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "t@example.com"], check=True, env=CLEAN_ENV
    )
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True, env=CLEAN_ENV)
    return root


def write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=CLEAN_ENV)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--no-verify", "-m", f"chore: {message}"],
        check=True,
        env=CLEAN_ENV,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=CLEAN_ENV,
    ).stdout.strip()


def translation(repo: Path, base: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(CHECK), base],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=CLEAN_ENV,
    )


def base_tree(repo: Path) -> str:
    """A minimal contracts/ tree the fixtures build a translation wave against."""
    write(repo, "contracts/section-names.md", fixture("section_names.md"))
    write(
        repo,
        "contracts/interfaces.md",
        "# Interfaces\n\n## Ingredients (fork #6)\n\nSee §Ingredients for the rule.\n",
    )
    write(repo, "tool/greet.py", fixture("greet_before.py"))
    write(repo, "db/note.sql", fixture("note_before.sql"))
    return commit(repo, "base tree")


def test_exit_0_on_comment_only_translation_with_resolvable_anchor(repo: Path) -> None:
    base_tree(repo)
    write(
        repo,
        "tool/greet.py",
        '"""Returns the greeting."""\n\n\ndef greet() -> str:\n'
        '    return "hello"  # the contract\'s §Ingredients\n',
    )
    write(repo, "db/note.sql", "-- the rule\nSELECT 1;\n")
    commit(repo, "translate comments")

    result = translation(repo, "HEAD~1")
    assert result.returncode == 0, result.stdout + result.stderr


def test_exit_1_on_invented_anchor(repo: Path) -> None:
    base_tree(repo)
    write(
        repo,
        "contracts/interfaces.md",
        "# Interfaces\n\n## Ingredients (fork #6)\n\nSee §NoSuchSection for the rule.\n",
    )
    commit(repo, "invent an anchor")

    result = translation(repo, "HEAD~1")
    assert result.returncode == 1
    assert "interfaces.md" in result.stdout


def test_a_heading_in_another_document_is_out_of_scope(repo: Path) -> None:
    """`snippets/AGENTS.template.md` §How we work (4c92310) names another file's own heading."""
    write(
        repo,
        "contracts/interfaces.md",
        "# Interfaces\n\nOne paragraph (`snippets/AGENTS.template.md` §How we work).\n",
    )
    commit(repo, "cite another document's own section")

    result = check_anchors_direct(repo, "HEAD", ["contracts/interfaces.md"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[0] == "0"


def test_an_issue_anchor_wrapped_onto_the_next_line_is_out_of_scope(repo: Path) -> None:
    """`#10\\n§A-2` (entrypoints.md, ydc-import wave) still points into issue #10, not this ledger."""
    write(
        repo,
        "contracts/interfaces.md",
        "# Interfaces\n\nSee fork #10\n§A-2's ruling for the rest of it.\n",
    )
    commit(repo, "wrap a GitHub issue anchor across the line break")

    result = check_anchors_direct(repo, "HEAD", ["contracts/interfaces.md"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[0] == "0"


def test_a_tool_or_tests_comment_about_anchors_is_never_judged(repo: Path) -> None:
    """A comment/docstring under `tool/` or `tests/` TALKS ABOUT the `§` syntax -- it never cites one,
    no matter what example string it quotes (#234 review round 2, blocker 1 was still reachable)."""
    write(
        repo,
        "tool/example.py",
        '"""Describes the pattern, does not cite it: a wrapped line reads "...#10\\n§A-2".\n\n'
        'A test fixture\'s own invented anchor, §NoSuchSection, is a placeholder too.\n"""\n',
    )
    commit(repo, "add a module whose docstring quotes anchor-shaped examples")

    result = check_anchors_direct(repo, "HEAD", ["tool/example.py"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[0] == "0"

    # The same invented shape, in a changed contracts/*.md PROSE line, still fails -- this rule
    # narrows WHERE anchors are judged, it does not widen what counts as a resolved one.
    write(
        repo,
        "contracts/x.md",
        '# X\n\nA wrapped line reads "...#10\\n§A-2". §NoSuchSection is invented too.\n',
    )
    commit(repo, "the same shapes, but as real contracts prose")
    result = check_anchors_direct(repo, "HEAD", ["contracts/x.md"])
    assert result.returncode == 0  # check_anchors itself always exits 0; failures are in its stdout
    assert "NoSuchSection" in result.stdout


def test_exit_0_on_a_single_in_place_translation_and_it_is_counted(repo: Path) -> None:
    write(repo, "tool/messages.py", fixture("single_translate_before.py"))
    commit(repo, "base tree")
    write(repo, "tool/messages.py", fixture("single_translate_after.py"))
    commit(repo, "translate the one literal")

    result = translation(repo, "HEAD~1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 literal(s) translated" in result.stdout


def test_exit_1_on_a_pure_swap_of_two_literals(repo: Path) -> None:
    """A moved literal: the value at one changed position reappears at another changed position."""
    write(repo, "tool/messages.py", fixture("swap_before.py"))
    commit(repo, "base tree")
    write(repo, "tool/messages.py", fixture("swap_after.py"))
    commit(repo, "quietly swap the two return values")

    result = translation(repo, "HEAD~1")
    assert result.returncode == 1
    assert "messages.py:5" in result.stdout
    assert "messages.py:9" in result.stdout


def test_string_literal_anchor_is_not_judged(repo: Path) -> None:
    """A `§`-shaped value inside a string literal is a fixture's placeholder, not a real anchor --
    even though the file carrying it is itself part of the changed diff (its docstring is translated)."""
    write(repo, "tool/placeholder.py", fixture("anchor_placeholder_before.py"))
    commit(repo, "base tree")
    write(repo, "tool/placeholder.py", fixture("anchor_placeholder_after.py"))
    commit(repo, "translate the docstring; the placeholder literal is untouched")

    result = translation(repo, "HEAD~1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "NoSuchSection" not in result.stdout


def test_undeclared_mode_lists_tree_wide_gaps_without_failing(repo: Path) -> None:
    base_tree(repo)
    write(
        repo,
        "contracts/interfaces.md",
        "# Interfaces\n\n## Ingredients (fork #6)\n\nSee §Undeclared for the rule.\n",
    )
    commit(repo, "an old, never-declared anchor already in the tree")

    result = subprocess.run(
        ["python3", str(REPO_ROOT / "tool" / "translation.py"), "--undeclared"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=CLEAN_ENV,
    )
    assert result.returncode == 0
    assert "interfaces.md:5" in result.stdout


def test_exit_1_on_dropped_comment_block(repo: Path) -> None:
    """Dropped = a comment line removed with no comment line added in the same hunk."""
    base_tree(repo)
    write(repo, "db/note.sql", "SELECT 1;\n")
    commit(repo, "drop the comment instead of translating it")

    result = translation(repo, "HEAD~1")
    assert result.returncode == 1
    assert "note.sql" in result.stdout


def test_report_names_the_failing_file_and_line(repo: Path) -> None:
    base_tree(repo)
    write(
        repo,
        "contracts/interfaces.md",
        "# Interfaces\n\n## Ingredients (fork #6)\n\nSee §NoSuchSection for the rule.\n",
    )
    commit(repo, "invent an anchor")

    result = translation(repo, "HEAD~1")
    assert "contracts/interfaces.md:5" in result.stdout
