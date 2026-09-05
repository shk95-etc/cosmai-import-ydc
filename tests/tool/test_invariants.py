"""#215: `tool/checks/invariants <base>` is the evidence behind class C.

The translation wave asked the same question of every file it touched -- "is this only text?" -- and
answered it with three throwaway scripts. Here they are one command with a repository behind them:
Python compared as an AST with its docstrings and string constants blanked, SQL compared with its
comments stripped, shell and JS compared line by line, Markdown compared by the anchors and literals
a translation must carry across. Every check fails closed: a file it cannot read, cannot parse or has
no rule for is reported as differing, because "I could not tell" and "nothing changed" must never
produce the same exit status.

Fixture repositories, not this checkout: the check reads git history, and the history here is not a
fixture anybody can pin.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK = REPO_ROOT / "tool" / "checks" / "invariants"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo, isolated from THIS checkout (#60 GIT_DIR)."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
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


def invariants(repo: Path, base: str, *paths: str, blanked: bool = False) -> subprocess.CompletedProcess:
    flags = ["--strings-blanked"] if blanked else []
    return subprocess.run(
        ["sh", str(CHECK), *flags, base, *paths],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    )


def rewrite(repo: Path, path: str, before: str, after: str, blanked: bool = False):
    """Commits `before`, replaces it with `after`, and asks whether the change moved any code."""
    write(repo, path, before)
    base = commit(repo, "before")
    write(repo, path, after)
    commit(repo, "after")
    return invariants(repo, base, blanked=blanked)


PY_BEFORE = '''"""One sentence of prose."""
LIMIT = 3


def run(rows):
    # the old wording of why
    print(f"{len(rows)} rows, limit {LIMIT}")
    return rows[:LIMIT]
'''

PY_TRANSLATED = '''"""What this module is for."""
LIMIT = 3


def run(rows):
    # one line of why
    print(f"limit {LIMIT} over {len(rows)} rows")
    return rows[:LIMIT]
'''


def test_a_python_file_whose_comments_and_docstrings_changed_is_invariant(repo: Path):
    retold = PY_BEFORE.replace('"""One sentence of prose."""', '"""The same, said better."""').replace(
        "# the old wording of why", "# a better why"
    )
    done = rewrite(repo, "analysis/thing.py", PY_BEFORE, retold)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "thing.py" not in done.stdout, done.stdout


def test_a_translated_message_is_code_until_the_reviewer_says_otherwise(repo: Path):
    # #215 review C1: a string constant carries a SQL predicate, a regex, an env-var name, a model
    # default. The gate cannot tell those from a message, so by default none of them are text.
    done = rewrite(repo, "analysis/thing.py", PY_BEFORE, PY_TRANSLATED)
    assert done.returncode == 1, done.stdout
    assert "analysis/thing.py" in done.stdout, done.stdout


def test_the_strings_blanked_mode_is_where_a_translation_reads_as_unchanged(repo: Path):
    # The translation reviewer's question -- "is this the same code with different words?" -- is a
    # smaller one than the gate's, so it lives behind a flag the gate never passes.
    done = rewrite(repo, "analysis/thing.py", PY_BEFORE, PY_TRANSLATED, blanked=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_changed_constant_in_python_is_not_invariant(repo: Path):
    done = rewrite(repo, "analysis/thing.py", PY_BEFORE, PY_BEFORE.replace("LIMIT = 3", "LIMIT = 4"))
    assert done.returncode == 1, done.stdout
    assert "analysis/thing.py" in done.stdout, done.stdout


def test_a_reordered_f_string_is_text_only_under_strings_blanked(repo: Path):
    # Korean and English put the same values in a different order, so under the reviewer's mode the
    # order inside one message is text while the set of values interpolated is code.
    same = rewrite(repo, "a.py", 'print(f"{a} then {b}")\n', 'print(f"{b} first, {a}")\n', blanked=True)
    assert same.returncode == 0, same.stdout

    other = repo / "b.py"
    other.write_text('print(f"{a} then {b}")\n', encoding="utf-8")
    base = commit(repo, "two")
    other.write_text('print(f"{a} then {c}")\n', encoding="utf-8")
    commit(repo, "three")
    done = invariants(repo, base, blanked=True)
    assert done.returncode == 1, done.stdout
    assert "b.py" in done.stdout, done.stdout


def test_a_rounding_change_inside_an_f_string_is_never_invariant(repo: Path):
    # #215 review, minor 6: format_spec and conversion decide what the value prints as, so they are
    # part of the code in both modes.
    for blanked in (False, True):
        fresh = repo / f"round{int(blanked)}.py"
        fresh.write_text('print(f"{total:.2f}")\n', encoding="utf-8")
        base = commit(repo, "before")
        fresh.write_text('print(f"{total:.0f}")\n', encoding="utf-8")
        commit(repo, "after")
        done = invariants(repo, base, blanked=blanked)
        assert done.returncode == 1, (blanked, done.stdout)


ADVERSARIAL = {
    "analysis/polarity/q.py": (
        'QUERY = "SELECT id FROM app.need WHERE tenant = %s"\n',
        'QUERY = "SELECT id FROM app.need WHERE 1=1"\n',
    ),
    "analysis/polarity/model.py": ('DEFAULT_MODEL = "gpt-4o-mini"\n', 'DEFAULT_MODEL = "gpt-3.5-turbo"\n'),
    "tests/conftest.py": ('TEST_DB_URL_ENV = "TEST_POSTGRES_URL"\n', 'TEST_DB_URL_ENV = "TEST_PG_URL"\n'),
    "analysis/extractor/rules.py": ('PATTERN = r"^\\d{4}-\\d{2}$"\n', 'PATTERN = r"^\\d{4}-\\d{1}$"\n'),
}


@pytest.mark.parametrize("path", sorted(ADVERSARIAL))
def test_behaviour_that_lives_in_a_string_is_not_invariant(repo: Path, path: str):
    # The four the review measured: a SQL predicate, a model default, the env var the whole test
    # harness is keyed on, and a regex. Each one is a behaviour change wearing quotation marks.
    before, after = ADVERSARIAL[path]
    done = rewrite(repo, path, before, after)
    assert done.returncode == 1, done.stdout
    assert path in done.stdout, done.stdout


def test_unparsable_python_is_reported_rather_than_waved_through(repo: Path):
    done = rewrite(repo, "a.py", "x = 1\n", "x = (1\n")
    assert done.returncode == 1, done.stdout
    assert "a.py" in done.stdout, done.stdout


def test_an_extensionless_python_tool_is_compared_as_python(repo: Path):
    before = "#!/usr/bin/env python3\n# the old wording\nPORT = 5432\n"
    done = rewrite(repo, "tool/measure-thing", before, before.replace("PORT = 5432", "PORT = 5433"))
    assert done.returncode == 1, done.stdout
    assert "tool/measure-thing" in done.stdout, done.stdout


SQL_BEFORE = """-- the old wording
CREATE TABLE app.need (
    id bigserial PRIMARY KEY,  -- an id
    body text NOT NULL
);
"""


def test_sql_keeps_its_meaning_when_only_the_comments_change(repo: Path):
    after = SQL_BEFORE.replace("-- the old wording", "-- what the table holds").replace(
        "-- an id", "-- the identifier"
    )
    done = rewrite(repo, "contracts/ddl/needs/001_need.sql", SQL_BEFORE, after)
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_changed_sql_column_is_not_invariant(repo: Path):
    after = SQL_BEFORE.replace("body text NOT NULL", "body text")
    done = rewrite(repo, "contracts/ddl/needs/001_need.sql", SQL_BEFORE, after)
    assert done.returncode == 1, done.stdout
    assert "001_need.sql" in done.stdout, done.stdout


def test_a_double_dash_inside_a_sql_string_is_data_not_a_comment(repo: Path):
    before = "INSERT INTO t VALUES ('a -- b');\n"
    done = rewrite(repo, "db/seed.sql", before, "INSERT INTO t VALUES ('a -- c');\n")
    assert done.returncode == 1, done.stdout


SH_BEFORE = """#!/bin/sh
# the old wording of why
set -e
exec psql -q -f "$1"
"""


def test_a_shell_script_whose_comment_lines_changed_is_invariant(repo: Path):
    done = rewrite(
        repo, "db/migrate.sh", SH_BEFORE, SH_BEFORE.replace("# the old wording of why", "# why, said better")
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_shell_script_whose_command_changed_is_not(repo: Path):
    done = rewrite(repo, "db/migrate.sh", SH_BEFORE, SH_BEFORE.replace("psql -q", "psql -v"))
    assert done.returncode == 1, done.stdout
    assert "db/migrate.sh" in done.stdout, done.stdout


JS_BEFORE = """// the old wording
export function render(rows) {
  return rows.length;
}
"""


def test_a_js_comment_change_is_invariant_and_a_code_change_is_not(repo: Path):
    same = rewrite(
        repo, "portal/public/render.js", JS_BEFORE, JS_BEFORE.replace("// the old wording", "// draws")
    )
    assert same.returncode == 0, same.stdout + same.stderr

    other = repo / "portal" / "public" / "query.js"
    other.write_text(JS_BEFORE, encoding="utf-8")
    base = commit(repo, "query")
    other.write_text(JS_BEFORE.replace("rows.length", "rows.size"), encoding="utf-8")
    commit(repo, "changed")
    done = invariants(repo, base)
    assert done.returncode == 1, done.stdout
    assert "query.js" in done.stdout, done.stdout


MD_BEFORE = "# Heading\n\nRead STATE.md §2. `tool/checks/test` arrived in #214.\n"


def test_markdown_that_carries_its_anchors_across_is_invariant(repo: Path):
    after = "# A better heading\n\nSee STATE.md §2. `tool/checks/test` came from #214.\n"
    done = rewrite(repo, "AGENTS.md", MD_BEFORE, after)
    assert done.returncode == 0, done.stdout + done.stderr


def test_markdown_that_drops_an_anchor_is_reported(repo: Path):
    after = "# A better heading\n\nSee STATE.md §3. `tool/checks/test` came from #214.\n"
    done = rewrite(repo, "AGENTS.md", MD_BEFORE, after)
    assert done.returncode == 1, done.stdout
    assert "AGENTS.md" in done.stdout, done.stdout


def test_a_new_file_is_not_invariant(repo: Path):
    write(repo, "a.py", "x = 1\n")
    base = commit(repo, "one")
    write(repo, "b.py", "y = 2\n")
    commit(repo, "two")
    done = invariants(repo, base)
    assert done.returncode == 1, done.stdout
    assert "b.py" in done.stdout, "a file with no `before` cannot be proved unchanged"


def test_a_file_type_with_no_rule_is_reported_rather_than_assumed(repo: Path):
    done = rewrite(repo, "pyproject.toml", "[project]\nname = 'a'\n", "[project]\nname = 'b'\n")
    assert done.returncode == 1, done.stdout
    assert "pyproject.toml" in done.stdout, done.stdout


def test_nothing_changed_is_a_pass(repo: Path):
    write(repo, "a.py", "x = 1\n")
    base = commit(repo, "one")
    done = invariants(repo, base)
    assert done.returncode == 0, done.stdout + done.stderr


def test_only_the_named_paths_are_examined(repo: Path):
    write(repo, "a.py", "x = 1\n")
    write(repo, "notes.md", "one\n")
    base = commit(repo, "one")
    write(repo, "a.py", "x = 2\n")
    write(repo, "notes.md", "two\n")
    commit(repo, "two")
    assert invariants(repo, base).returncode == 1
    assert invariants(repo, base, "notes.md").returncode == 0, "a.py was examined despite the pathspec"


def test_the_summary_says_how_many_files_were_compared(repo: Path):
    done = rewrite(repo, "a.py", "x = 1\n", "# why\nx = 1\n")
    assert "1 file" in done.stdout, done.stdout


def test_a_swapped_shebang_is_not_a_comment(repo: Path):
    # #215 review, minor 7: `#!` is read by the kernel, not by a person -- it decides what runs.
    before = '#!/bin/sh\n# why\nexec psql -f "$1"\n'
    done = rewrite(repo, "db/run.sh", before, before.replace("#!/bin/sh", "#!/bin/bash"))
    assert done.returncode == 1, done.stdout


def test_a_js_generator_method_is_not_a_block_comment_line(repo: Path):
    before = "class Q {\n  /* how this walks\n   * one row at a time\n   */\n  *rows() {}\n}\n"
    changed = before.replace("*rows() {}", "*items() {}")
    assert rewrite(repo, "portal/public/gen.js", before, changed).returncode == 1

    other = repo / "portal" / "public" / "two.js"
    other.write_text(before, encoding="utf-8")
    base = commit(repo, "two")
    other.write_text(before.replace("* one row at a time", "* a row at a time"), encoding="utf-8")
    commit(repo, "reworded")
    assert invariants(repo, base).returncode == 0, "a block-comment line stopped counting as prose"


YAML_BEFORE = "# the old wording\nservices:\n  app:\n    image: cosmai:latest\n"


def test_a_yaml_comment_only_change_is_invariant(repo: Path):
    done = rewrite(
        repo, "stack/docker-compose.yml", YAML_BEFORE, YAML_BEFORE.replace("# the old wording", "# what runs")
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_yaml_value_change_is_not_invariant(repo: Path):
    done = rewrite(repo, "stack/docker-compose.yml", YAML_BEFORE, YAML_BEFORE.replace("latest", "v2"))
    assert done.returncode == 1, done.stdout
    assert "docker-compose.yml" in done.stdout, done.stdout


TOML_BEFORE = '# the old wording\n[project]\nname = "cosmai"\n'


def test_a_toml_comment_only_change_is_invariant(repo: Path):
    done = rewrite(
        repo, "app.toml", TOML_BEFORE, TOML_BEFORE.replace("# the old wording", "# what this pins")
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_toml_value_change_is_not_invariant(repo: Path):
    done = rewrite(repo, "app.toml", TOML_BEFORE, TOML_BEFORE.replace('"cosmai"', '"cosmai2"'))
    assert done.returncode == 1, done.stdout


DOCKER_BEFORE = "# the old wording\nFROM python:3.13-slim\n"


def test_a_dockerfile_comment_only_change_is_invariant(repo: Path):
    done = rewrite(
        repo, "stack/Dockerfile", DOCKER_BEFORE, DOCKER_BEFORE.replace("# the old wording", "# base image")
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_dockerfile_instruction_change_is_not_invariant(repo: Path):
    done = rewrite(repo, "stack/Dockerfile", DOCKER_BEFORE, DOCKER_BEFORE.replace("3.13", "3.12"))
    assert done.returncode == 1, done.stdout


def test_a_dockerfile_cron_comment_only_change_is_invariant(repo: Path):
    # #231 Work 7c: any basename starting with "Dockerfile" routes to the same rule, not only the
    # bare name and the *.Dockerfile suffix.
    done = rewrite(
        repo, "stack/Dockerfile.cron", DOCKER_BEFORE, DOCKER_BEFORE.replace("# the old wording", "# base")
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_suffixed_dockerfile_comment_only_change_is_invariant(repo: Path):
    done = rewrite(
        repo, "stack/worker.Dockerfile", DOCKER_BEFORE, DOCKER_BEFORE.replace("# the old wording", "# base")
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_dockerfile_syntax_directive_is_not_a_comment(repo: Path):
    # #231 Work 7e: `# syntax=` is a parser directive Docker itself reads before the first
    # instruction, so changing it changes what the build does even though it looks like a comment.
    before = "# syntax=docker/dockerfile:1\nFROM python:3.13-slim\n"
    done = rewrite(repo, "stack/Dockerfile", before, before.replace("dockerfile:1", "dockerfile:1.7"))
    assert done.returncode == 1, done.stdout


def test_a_dockerfile_escape_directive_is_not_a_comment(repo: Path):
    before = "# escape=a\nFROM python:3.13-slim\n"
    done = rewrite(repo, "stack/Dockerfile", before, before.replace("escape=a", "escape=b"))
    assert done.returncode == 1, done.stdout


CRON_BEFORE = "# the old wording\n0 * * * * /app/run.sh\n"


def test_a_crontab_comment_only_change_is_invariant(repo: Path):
    done = rewrite(
        repo, "stack/crontab.d/analyze", CRON_BEFORE, CRON_BEFORE.replace("# the old wording", "# hourly")
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_crontab_schedule_change_is_not_invariant(repo: Path):
    done = rewrite(
        repo, "stack/crontab.d/analyze", CRON_BEFORE, CRON_BEFORE.replace("0 * * * *", "0 */2 * * *")
    )
    assert done.returncode == 1, done.stdout


def test_a_gitignore_comment_only_change_is_invariant(repo: Path):
    done = rewrite(repo, ".gitignore", "# the old wording\n*.pyc\n", "# build output\n*.pyc\n")
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_gitignore_pattern_change_is_not_invariant(repo: Path):
    done = rewrite(repo, ".gitignore", "# the old wording\n*.pyc\n", "# the old wording\n*.pyo\n")
    assert done.returncode == 1, done.stdout


def test_an_env_example_comment_only_change_is_invariant(repo: Path):
    done = rewrite(
        repo, "stack/.env.example", "# the old wording\nPORT=5432\n", "# default port\nPORT=5432\n"
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_an_env_example_value_change_is_not_invariant(repo: Path):
    done = rewrite(
        repo, "stack/.env.example", "# the old wording\nPORT=5432\n", "# the old wording\nPORT=5433\n"
    )
    assert done.returncode == 1, done.stdout


def test_a_hash_not_at_line_start_is_not_a_comment_marker(repo: Path):
    # A `#` inside a value is part of the value, not a comment: only a line whose first character is
    # `#` reads as commented-out.
    done = rewrite(repo, "stack/.env.example", 'TOKEN="abc#def"\n', 'TOKEN="abc#xyz"\n')
    assert done.returncode == 1, done.stdout


def test_whitespace_inside_a_sql_literal_is_the_value(repo: Path):
    # #215 review, minor 8: normalizing the whole file made 'a  b' and 'a b' the same row.
    done = rewrite(
        repo, "db/seed2.sql", "INSERT INTO t VALUES ('a  b');\n", "INSERT INTO t VALUES ('a b');\n"
    )
    assert done.returncode == 1, done.stdout
