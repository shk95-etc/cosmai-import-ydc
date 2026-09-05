"""#232: CI, not a worker's own push, runs class A -- one workflow triggered on every push and PR
plus a nightly. This is a wiring test, not a runner: it reads the workflow file as text (PyYAML is
not a project dependency, and this file's shape does not need a real parser to check).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "suite.yml"
PYTHON_VERSION_PIN = REPO_ROOT / ".python-version"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_the_workflow_file_exists():
    assert WORKFLOW.is_file(), "no .github/workflows/suite.yml"


def test_it_triggers_on_push_pull_request_schedule_and_dispatch():
    body = text()
    assert "push:" in body, body
    assert "pull_request:" in body, body
    assert "schedule:" in body, body
    assert "workflow_dispatch:" in body, body


def test_the_nightly_cron_is_once_a_day():
    body = text()
    assert "cron:" in body, body
    # One line per Work 1: nightly at 0 17 * * * UTC.
    assert "0 17 * * *" in body, body


def test_the_class_a_job_runs_the_whole_suite():
    body = text()
    assert "class-a" in body, body
    assert "tool/checks/test" in body, body
    # No --changed: that is what makes this run class A, not the computed set (#232 Work 1).
    assert "tool/checks/test --changed" not in body, body


def test_the_job_has_an_hour_timeout():
    assert "timeout-minutes: 60" in text(), text()


def test_concurrency_never_cancels_main_or_a_wave_branch():
    body = text()
    assert "concurrency:" in body, body
    assert "cancel-in-progress" in body, body
    assert "main" in body, body
    assert "wave/" in body, body


def test_postgres_client_and_uv_are_installed():
    body = text()
    assert "postgresql-client" in body, body
    assert "setup-uv" in body, body


def test_the_pytest_summary_reaches_the_job_summary():
    assert "GITHUB_STEP_SUMMARY" in text(), text()


def test_the_workflow_explains_why_ci_owns_class_a():
    # Work 5: one paragraph at the top saying why -- not left implicit in a commit message only.
    body = text().splitlines()
    header = "\n".join(body[:20])
    assert "class A" in header or "class-a" in header.lower(), header


def test_a_workflow_change_is_mapped_to_its_own_wiring_test():
    scope = REPO_ROOT / "tests" / "scope.toml"
    body = scope.read_text(encoding="utf-8")
    assert '".github/workflows/*"' in body, body
    assert "tests/tool/test_ci_workflow.py" in body, body


def test_the_python_version_pin_matches_requires_python():
    # #232 review: run 33954240253 failed because nothing pinned the interpreter -- the runner's
    # system CPython 3.12 wraps argparse's usage text differently from the 3.13 the help snapshot
    # was recorded under. This is the reader tests/scope.toml gives `.python-version` (a trigger
    # file with no test naming it has no computed set to owe locally, per review 2026-09-05).
    assert PYTHON_VERSION_PIN.is_file(), "no .python-version pin"
    pin = PYTHON_VERSION_PIN.read_text(encoding="utf-8").strip()
    major, minor = (int(part) for part in pin.split(".")[:2])
    match = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)"', PYPROJECT.read_text(encoding="utf-8"))
    assert match, "pyproject.toml has no requires-python floor to compare against"
    floor = (int(match.group(1)), int(match.group(2)))
    assert (major, minor) >= floor, (pin, floor)


def test_a_python_version_change_is_mapped_to_its_own_wiring_test():
    body = (REPO_ROOT / "tests" / "scope.toml").read_text(encoding="utf-8")
    assert '".python-version"' in body, body
