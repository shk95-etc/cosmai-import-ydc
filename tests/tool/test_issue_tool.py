"""tool/issue against a fake `gh`, so the rules in #60 are checked without the network.

The fake reads the repository name out of the `-f query=` string and answers with a fixture, which
is the whole reason the query names the repository inline instead of passing it as a variable: a
test that cannot tell the two repos apart cannot exercise the cross-repo blockedBy that #55 <- fork#6
actually has.

Isolation here is PATH precedence, not conftest's socket block: tool/issue shells out, and a
subprocess is outside the guard that stops in-process sockets.

#192's migration window closed (#204): the tool reads only the English anchors now, and a Korean
body is content the D12 rule can flag, not an anchor set it understands. The Korean prose those
tests still need comes from tests/tool/fixtures/korean_line.txt rather than being written here:
tool/checks/lang stops a Hangul literal from reaching a .py file.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE = REPO_ROOT / "tool" / "issue"
UPSTREAM = "slopindustries/cosmai"
FORK = "slopindustries/cosmai-import-ydc"
COMMON_LABELS = ["channel", "goal", "decision", "memo", "when-touched", "needs-user"]
# A plain Korean sentence, not an anchor: the D12 rule fires on Korean prose anywhere in a body or
# title, whatever the section headings around it say.
KOREAN_LINE = (
    (Path(__file__).resolve().parent / "fixtures" / "korean_line.txt").read_text(encoding="utf-8").strip()
)
# Escapes, not literals: this pattern is the assertion that the output carries no Korean at all.
HANGUL = re.compile("[\u1100-\u11ff\u3130-\u318f\uac00-\ud7a3]")

NOW = datetime.now(UTC)
BODY = (
    "## Context\nsomething\n\n## Done when\na machine checks it\n\n"
    "## Channel / grade / size\nSize S \u00b7 Resources: none\n"
)
MEMO_BODY = "## Context\nan observation, nothing else\n"
# A when-touched issue is finished by whatever work next opens that file, not by this issue, so it
# carries "When to fix" instead of "Done when" (#137).
WHEN_TOUCHED_BODY = "## Fact\nit does not break today\n\n## When to fix\nwhoever next opens that file\n"
# Every fixture issue is old enough that the Korean-in-a-new-issue rule cannot fire on it, and the
# one test of that rule names its own creation date -- neither depends on today's date.
BEFORE_THE_WINDOW = "2026-01-01T00:00:00Z"
AFTER_THE_WINDOW = "2099-01-01T00:00:00Z"


def stamp(days_ago: float = 0.0) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def issue(
    number: int,
    title: str = "a title",
    *,
    body: str = BODY,
    labels: tuple[str, ...] = (),
    assignees: tuple[str, ...] = (),
    parent: int | None = None,
    subs: tuple[int, ...] = (),
    blocked_by: tuple[tuple[str, int, str], ...] = (),
    updated_days_ago: float = 0.0,
    created_at: str = BEFORE_THE_WINDOW,
) -> dict:
    return {
        "number": number,
        "title": title,
        "updatedAt": stamp(updated_days_ago),
        "createdAt": created_at,
        "body": body,
        "labels": {"nodes": [{"name": name} for name in labels]},
        "assignees": {"nodes": [{"login": login} for login in assignees]},
        "parent": None if parent is None else {"number": parent},
        "subIssues": {"nodes": [{"number": n} for n in subs]},
        "blockedBy": {
            "nodes": [
                {"number": n, "state": state, "repository": {"nameWithOwner": repo}}
                for repo, n, state in blocked_by
            ]
        },
    }


def closed(number: int, title: str, body: str, *, days_ago: float, labels: tuple[str, ...]) -> dict:
    row = issue(number, title, body=body, labels=labels, updated_days_ago=days_ago)
    row["closedAt"] = stamp(days_ago)
    return row


def released(date: str) -> str:
    return BODY + f"\n## Release condition\nafter {date}\n"


def day(days_from_now: float) -> str:
    return (NOW + timedelta(days=days_from_now)).strftime("%Y-%m-%d")


def page(repo: str, issues: list[dict], labels: list[str] | None = None) -> dict:
    return {
        "data": {
            "repository": {
                "nameWithOwner": repo,
                "labels": {"nodes": [{"name": n} for n in (COMMON_LABELS if labels is None else labels)]},
                "issues": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": issues,
                },
            }
        }
    }


def partial_page(repo: str, issues: list[dict], labels: list[str] | None = None) -> dict:
    """The same page after the nested fields timed out: arrays present, contents gone.

    This is what makes the failure quiet. `issues.nodes` is still a list of issues, so the
    repository looks answered; it is `subIssues`, `assignees` and `blockedBy` that came back
    empty, and those are exactly what the queue order, the held-resource summary and the blockers
    are read from. The server says so in `errors` -- the only place the loss is visible.
    """
    hollowed = []
    for source in issues:
        item = dict(source)
        item["assignees"] = {"nodes": []}
        item["subIssues"] = {"nodes": []}
        item["blockedBy"] = {"pageInfo": {"hasNextPage": False}, "nodes": []}
        hollowed.append(item)
    answer = page(repo, hollowed, labels)
    answer["errors"] = [
        {"message": "Something went wrong while executing your query.", "type": "SERVICE_UNAVAILABLE"}
    ]
    return answer


FAKE_GH = """#!/bin/sh
# #232: `ready`'s CI-status lookup and `audit`'s nightly section both call `gh api` outside the
# graphql loop below -- checked first, by substring on the whole argument list, because neither
# carries a `-f query=` arg the loop's own matching depends on.
case "$*" in
  *"/check-runs"*)
    if [ -n "$FAKE_GH_CI_FAIL" ]; then echo "fake gh: the API said no" >&2; exit 1; fi
    # This stands in for `gh api ... --jq '<expr>'` as a whole: the fake prints the expression's
    # own final answer directly, the same string the real --jq (gojq, bundled) would have produced.
    printf '%s\\n' "${FAKE_GH_CI_CONCLUSION:-success}"
    exit 0
    ;;
  *"workflows/suite.yml/runs"*)
    if [ -n "$FAKE_GH_NIGHTLY_FAIL" ]; then echo "fake gh: the API said no" >&2; exit 1; fi
    if [ -n "$FAKE_GH_NIGHTLY_MISSING" ]; then
      echo '{"workflow_runs":[]}'
      exit 0
    fi
    if [ -n "$FAKE_GH_NIGHTLY_CONCLUSION" ]; then
      concl="\\"$FAKE_GH_NIGHTLY_CONCLUSION\\""
    else
      concl=null
    fi
    printf '{"workflow_runs":[{"created_at":"%s","conclusion":%s}]}\\n' \\
      "${FAKE_GH_NIGHTLY_DATE:-2026-09-05T17:00:00Z}" "$concl"
    exit 0
    ;;
esac
# The fork pattern is tested first because "cosmai" is a substring of "cosmai-import-ydc":
# the looser case would answer for both repos and the cross-repo tests would prove nothing.
for arg in "$@"; do
  case "$arg" in
    query=*'name: "cosmai-import-ydc"'*) which=fork ;;
    query=*'name: "cosmai"'*) which=upstream ;;
    *) continue ;;
  esac
  # recheck (e) needs closed issues, which the shared graph does not carry; the fixture is
  # separate so a test can prove the closed page is fetched by recheck and by nothing else.
  case "$arg" in *'states: CLOSED'*) cat "$FIXTURES/$which.closed.json"; exit 0 ;; esac
  if [ "$FAKE_GH_FAIL" = "$which" ]; then echo "fake gh: the API said no" >&2; exit 1; fi
  if [ "$FAKE_GH_ERRORS" = "$which" ]; then
    echo '{"errors":[{"message":"Although you appear to have the correct authorization"}]}'
    exit 0
  fi
  # A partial failure: `data` arrives, but the expensive nested fields timed out and the
  # server said so in `errors`. The nodes are still arrays, so a guard that only asks
  # "did data arrive" reads this as the truth.
  if [ "$FAKE_GH_PARTIAL" = "$which" ]; then
    cat "$FIXTURES/$which.partial.json"
    exit 0
  fi
  cat "$FIXTURES/$which.json"
  exit 0
done
echo "fake gh: no fixture for: $*" >&2
exit 1
"""

# #233: `audit`'s ops section shells out to `docker exec cosmai-postgres psql ...` the same way
# tool/status's `== db ==` block does. Real docker sits further down this host's PATH, so a fake
# docker at the front of bin_dir shadows it completely -- no test here ever reaches a real
# daemon or a real container, empty by default so every test that does not care about ops reads
# a clean "none" rather than silently touching anything real.
FAKE_DOCKER = """#!/bin/sh
if [ "$1" != exec ]; then echo "fake docker: no fixture for: $*" >&2; exit 1; fi
shift
case "$*" in
  *pipeline_health*)
    if [ -n "$FAKE_DOCKER_OPS_FAIL" ]; then echo "fake docker: exec failed" >&2; exit 1; fi
    printf '%s' "$FAKE_DOCKER_OPS_PIPELINE"
    exit 0
    ;;
  *collector_health*)
    if [ -n "$FAKE_DOCKER_OPS_FAIL" ]; then echo "fake docker: exec failed" >&2; exit 1; fi
    printf '%s' "$FAKE_DOCKER_OPS_COLLECTOR"
    exit 0
    ;;
esac
echo "fake docker: no fixture for exec: $*" >&2
exit 1
"""


@pytest.fixture
def run(tmp_path: Path):
    """Runs tool/issue with a fake `gh`/`docker` first on PATH and the fixtures it should answer with."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)

    def _run(
        *args: str,
        upstream: list[dict],
        fork: list[dict] | None = None,
        cwd: Path | None = None,
        **fixture_kwargs,
    ):
        (tmp_path / "upstream.json").write_text(
            json.dumps(page(UPSTREAM, upstream, fixture_kwargs.get("upstream_labels"))), encoding="utf-8"
        )
        (tmp_path / "fork.json").write_text(
            json.dumps(page(FORK, fork or [], fixture_kwargs.get("fork_labels"))), encoding="utf-8"
        )
        (tmp_path / "upstream.partial.json").write_text(
            json.dumps(partial_page(UPSTREAM, upstream, fixture_kwargs.get("upstream_labels"))),
            encoding="utf-8",
        )
        (tmp_path / "fork.partial.json").write_text(
            json.dumps(partial_page(FORK, fork or [], fixture_kwargs.get("fork_labels"))),
            encoding="utf-8",
        )
        (tmp_path / "upstream.closed.json").write_text(
            json.dumps(page(UPSTREAM, fixture_kwargs.get("upstream_closed") or [])), encoding="utf-8"
        )
        (tmp_path / "fork.closed.json").write_text(
            json.dumps(page(FORK, fixture_kwargs.get("fork_closed") or [])), encoding="utf-8"
        )
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FIXTURES": str(tmp_path),
            "COSMAI_ISSUE_REPOS": f"{UPSTREAM} {FORK}",
            "FAKE_GH_FAIL": fixture_kwargs.get("gh_fails_on", ""),
            "FAKE_GH_ERRORS": fixture_kwargs.get("gh_errors_on", ""),
            "FAKE_GH_PARTIAL": fixture_kwargs.get("gh_partial_on", ""),
            "FAKE_GH_CI_FAIL": "1" if fixture_kwargs.get("ci_fails") else "",
            "FAKE_GH_CI_CONCLUSION": fixture_kwargs.get("ci_conclusion", ""),
            "FAKE_GH_NIGHTLY_FAIL": "1" if fixture_kwargs.get("nightly_fails") else "",
            "FAKE_GH_NIGHTLY_MISSING": "1" if fixture_kwargs.get("nightly_missing") else "",
            "FAKE_GH_NIGHTLY_DATE": fixture_kwargs.get("nightly_date", ""),
            "FAKE_GH_NIGHTLY_CONCLUSION": fixture_kwargs.get("nightly_conclusion", ""),
            "FAKE_DOCKER_OPS_FAIL": "1" if fixture_kwargs.get("ops_fails") else "",
            "FAKE_DOCKER_OPS_PIPELINE": fixture_kwargs.get("ops_pipeline", ""),
            "FAKE_DOCKER_OPS_COLLECTOR": fixture_kwargs.get("ops_collector", ""),
        }
        return subprocess.run(
            [str(ISSUE), *args],
            capture_output=True,
            text=True,
            cwd=str(cwd or REPO_ROOT),
            env=env,
            check=False,
        )

    return _run


def epic(number: int, channel: str, subs: tuple[int, ...] = ()) -> dict:
    return issue(number, f"[channel] {channel}", labels=("channel", f"ch:{channel}"), subs=subs)


def test_a_closed_blocker_does_not_hold_an_issue_back(run):
    # blockedBy keeps closed rows, so a tool that reads presence instead of state reports the whole
    # backlog as blocked the moment anything upstream of it is finished.
    done = run(
        "ready",
        "--json",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "closed blocker", labels=("ch:tool",), blocked_by=((UPSTREAM, 9, "CLOSED"),)),
        ],
    )
    assert done.returncode == 0, done.stderr
    item = json.loads(done.stdout)["channels"][0]["items"][0]
    assert item["status"] == "ready", item


def test_a_blocker_in_the_other_repo_blocks(run):
    # cosmai#55 <- cosmai-import-ydc#6 is live today; one repo at a time would call #55 ready.
    done = run(
        "ready",
        "--json",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "cross repo", labels=("ch:tool",), blocked_by=((FORK, 6, "OPEN"),)),
        ],
        fork=[issue(6, "work on the fork", labels=("ch:analysis/retrieval",))],
    )
    assert done.returncode == 0, done.stderr
    item = json.loads(done.stdout)["channels"][0]["items"][0]
    assert item["status"] == "blocked"
    assert item["detail"] == "cosmai-import-ydc#6"
    assert (
        "blocked: cosmai-import-ydc#6"
        in run(
            "ready",
            upstream=[
                epic(10, "tool", subs=(11,)),
                issue(11, "cross repo", labels=("ch:tool",), blocked_by=((FORK, 6, "OPEN"),)),
            ],
            fork=[issue(6, "work on the fork", labels=("ch:analysis/retrieval",))],
        ).stdout
    )


# ---------------------------------------------------------------------------------------------
# #232 Work 3: `ready` names the CI status of each open issue branch's tip.
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def ci_checkout(tmp_path: Path) -> Path:
    """A repo with one remote-tracking branch shaped like a worker's own: `origin/tool/11-thing`."""
    repo = tmp_path / "ci-checkout"
    git, env = _git(repo)
    subprocess.run(
        [*git[:-2], "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True, env=env
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run([*git, "commit", "-qm", "chore: seed"], check=True, capture_output=True, env=env)
    sha = subprocess.run(
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()
    subprocess.run(
        [*git, "update-ref", "refs/remotes/origin/tool/11-thing", sha],
        check=True,
        capture_output=True,
        env=env,
    )
    return repo


def _ready_line_for(stdout: str, key: str) -> str:
    return next(line for line in stdout.splitlines() if key in line)


def test_ready_shows_the_ci_status_of_a_branch_that_earned_it(run, ci_checkout: Path):
    done = run(
        "ready",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",))],
        cwd=ci_checkout,
        ci_conclusion="success",
    )
    assert done.returncode == 0, done.stderr
    assert "ci: success" in _ready_line_for(done.stdout, "cosmai#11"), done.stdout


def test_ready_shows_a_red_ci_run(run, ci_checkout: Path):
    done = run(
        "ready",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",))],
        cwd=ci_checkout,
        ci_conclusion="failure",
    )
    assert "ci: failure" in _ready_line_for(done.stdout, "cosmai#11"), done.stdout


def test_ready_shows_ci_none_for_a_branch_with_no_check_run(run, ci_checkout: Path):
    done = run(
        "ready",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",))],
        cwd=ci_checkout,
        ci_conclusion="none",
    )
    assert "ci: none" in _ready_line_for(done.stdout, "cosmai#11"), done.stdout


def test_ready_reports_ci_unavailable_when_the_api_call_fails(run, ci_checkout: Path):
    done = run(
        "ready",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",))],
        cwd=ci_checkout,
        ci_fails=True,
    )
    assert "ci: unavailable" in _ready_line_for(done.stdout, "cosmai#11"), done.stdout


def test_ready_prints_no_ci_for_an_issue_with_no_branch(run, ci_checkout: Path):
    # #12 has no origin branch in ci_checkout (only #11 does): nothing to look up, nothing printed.
    done = run(
        "ready",
        upstream=[epic(10, "tool", subs=(12,)), issue(12, "unstarted", labels=("ch:tool",))],
        cwd=ci_checkout,
    )
    assert "ci:" not in _ready_line_for(done.stdout, "cosmai#12"), done.stdout


# ---------------------------------------------------------------------------------------------
# #233: `ready` also names whether class A is owed for that branch's diff against origin/main.
# ---------------------------------------------------------------------------------------------

# The real scope.toml, carried so the fixture's "owed" answer comes from the actual gate/trigger
# lists rather than a stand-in that could drift from them.
REAL_SCOPE_TOML = (REPO_ROOT / "tests" / "scope.toml").read_text(encoding="utf-8")
REAL_CHANGE_SCOPE = (REPO_ROOT / "tool" / "change_scope.py").read_text(encoding="utf-8")


@pytest.fixture
def ci_owed_checkout(tmp_path: Path) -> Path:
    """`origin/main` plus two branch tips: one touched `db/views/` (trigger list), one did not."""
    repo = tmp_path / "ci-owed-checkout"
    git, env = _git(repo)
    subprocess.run(
        [*git[:-2], "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True, env=env
    )
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "scope.toml").write_text(REAL_SCOPE_TOML, encoding="utf-8")
    (repo / "tool").mkdir(parents=True, exist_ok=True)
    (repo / "tool" / "change_scope.py").write_text(REAL_CHANGE_SCOPE, encoding="utf-8")
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run([*git, "commit", "-qm", "chore: seed"], check=True, capture_output=True, env=env)
    base_sha = subprocess.run(
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()
    subprocess.run(
        [*git, "update-ref", "refs/remotes/origin/main", base_sha], check=True, capture_output=True, env=env
    )

    def branch(name: str, path: str, text: str) -> None:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        subprocess.run([*git, "add", "-A"], check=True, capture_output=True, env=env)
        subprocess.run([*git, "commit", "-qm", f"chore: {path}"], check=True, capture_output=True, env=env)
        tip = subprocess.run(
            [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True, env=env
        ).stdout.strip()
        subprocess.run(
            [*git, "update-ref", f"refs/remotes/origin/{name}", tip],
            check=True,
            capture_output=True,
            env=env,
        )
        subprocess.run([*git, "reset", "-q", "--hard", base_sha], check=True, capture_output=True, env=env)

    branch("tool/11-owed", "db/views/pipeline_health.sql", "-- a view\n")
    branch("tool/12-clean", "app/thing.py", "print(1)\n")
    return repo


def test_ready_flags_a_owed_when_the_diff_touches_the_trigger_list(run, ci_owed_checkout: Path):
    done = run(
        "ready",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",))],
        cwd=ci_owed_checkout,
        ci_conclusion="success",
    )
    assert done.returncode == 0, done.stderr
    assert "A owed" in _ready_line_for(done.stdout, "cosmai#11"), done.stdout


def test_ready_flags_a_not_owed_when_the_diff_touches_nothing_on_either_list(run, ci_owed_checkout: Path):
    done = run(
        "ready",
        upstream=[epic(10, "tool", subs=(12,)), issue(12, "work", labels=("ch:tool",))],
        cwd=ci_owed_checkout,
        ci_conclusion="success",
    )
    assert done.returncode == 0, done.stderr
    assert "A not owed" in _ready_line_for(done.stdout, "cosmai#12"), done.stdout


def test_ready_leads_with_the_resources_the_running_issues_hold(run):
    # The cap on workers is gone (#185): what a new issue collides with is a resource someone is
    # already holding, and that is only visible if the first line says who holds what.
    resourced = "## Channel / grade / size\nSize M · Resources: ops(stopping the old stack) · sharedDB\n"
    done = run(
        "ready",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "in progress", body=resourced, labels=("ch:tool",), assignees=("shk95",)),
        ],
        fork=[
            issue(
                6, "fork in progress", body=resourced, labels=("ch:analysis/retrieval",), assignees=("shk95",)
            )
        ],
    )
    assert done.returncode == 0, done.stderr
    first = done.stdout.splitlines()[0]
    assert first.startswith("in progress 2 · held:"), done.stdout
    # One row per resource with both repos on it: the collision that matters is the cross-repo one.
    assert "ops cosmai#11, cosmai-import-ydc#6" in first, first
    assert "sharedDB cosmai#11, cosmai-import-ydc#6" in first, first
    assert "WIP" not in done.stdout, done.stdout
    assert "in progress: shk95 since" in done.stdout


def test_a_resource_of_none_is_not_folded_into_the_held_summary(run):
    # "Resources: none" is most issues. Folding it would put a row on the first line that blocks
    # nothing.
    done = run(
        "ready",
        "--json",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "the ledger", labels=("ch:repo",), assignees=("shk95",)),
        ],
    )
    assert done.returncode == 0, done.stderr
    model = json.loads(done.stdout)
    assert model["held"] == {"in_progress": 1, "resources": []}, model["held"]
    # The gate is deleted, not hidden behind a flag: nothing may read wip/limit/gate again.
    assert "wip" not in model and "limit" not in model and "gate" not in model, list(model)


def test_a_capitalised_resource_of_none_is_not_folded_either(run):
    # The first line of `ready` is what a worker checks before starting; a phantom resource there
    # costs a wait on nothing, and a migrated body writing "None" is the likely spelling.
    body = "## Channel / grade / size\nSize S · Resources: None\n"
    done = run(
        "ready",
        "--json",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "the ledger", body=body, labels=("ch:repo",), assignees=("shk95",)),
        ],
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["held"]["resources"] == [], done.stdout


def test_a_channel_issue_without_a_parent_is_reported_at_the_end_of_its_channel(run):
    done = run(
        "ready",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "in place", labels=("ch:tool",)),
            issue(12, "stray", labels=("ch:tool",)),
        ],
    )
    assert done.returncode == 0, done.stderr
    lines = [line for line in done.stdout.splitlines() if "cosmai#1" in line]
    assert "(no parent)" in lines[-1] and "cosmai#12" in lines[-1], done.stdout
    assert "(no parent)" not in lines[0]


def test_being_blocked_by_a_memo_is_a_lint_error(run):
    # A memo has no completion criterion, so an issue blocked by one can never become ready.
    done = run(
        "lint",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(
                11, "blocked on a memo", labels=("ch:tool",), parent=10, blocked_by=((UPSTREAM, 20, "OPEN"),)
            ),
            issue(20, "an observation", body=MEMO_BODY, labels=("memo",)),
        ],
    )
    assert done.returncode == 1
    lines = done.stdout.splitlines()
    assert any(line.startswith("cosmai#11:") and "memo" in line for line in lines), done.stdout


def test_a_blockedby_cycle_is_found_across_the_repos(run):
    done = run(
        "lint",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "one", labels=("ch:tool",), parent=10, blocked_by=((FORK, 6, "OPEN"),)),
        ],
        fork=[
            epic(5, "analysis/retrieval", subs=(6,)),
            issue(
                6,
                "two",
                labels=("ch:analysis/retrieval",),
                parent=5,
                blocked_by=((UPSTREAM, 11, "OPEN"),),
            ),
        ],
    )
    assert done.returncode == 1
    cycles = [line for line in done.stdout.splitlines() if "cycle" in line]
    assert len(cycles) == 1, done.stdout
    assert "cosmai#11" in cycles[0] and "cosmai-import-ydc#6" in cycles[0]


def test_a_deferred_issue_with_a_release_condition_section_passes(run):
    # #60 allows either shape, so a deferred issue whose condition is an observation and not an
    # issue must not be reported -- otherwise `lint` is noise and stops being run.
    body = BODY + "\n## Release condition\nwhen a retrieval consumer exists\n"
    done = run(
        "lint",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "deferred", body=body, labels=("ch:tool", "deferred"), parent=10),
        ],
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() == ""


def test_a_deferred_issue_with_neither_condition_is_a_lint_error(run):
    done = run(
        "lint",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "deferred", labels=("ch:tool", "deferred"), parent=10),
        ],
    )
    assert done.returncode == 1
    assert "Release condition" in done.stdout


def test_lint_reports_the_rest_of_the_registration_rules(run):
    done = run(
        "lint",
        upstream=[
            issue(11, "two channels", labels=("ch:tool", "ch:repo"), parent=10),
            issue(12, "no done-when", body="## Context\nnothing\n", labels=("ch:tool",), parent=10),
            issue(13, "machine path", body=BODY + "\n/ho" + "me/user1/x\n", labels=("ch:tool",), parent=10),
            issue(20, "a memo with a channel", body=MEMO_BODY, labels=("memo", "ch:tool"), parent=10),
        ],
    )
    assert done.returncode == 1
    reasons = done.stdout
    assert "cosmai#11:" in reasons and "ch:*" in reasons
    assert "cosmai#12:" in reasons and "Done when" in reasons
    assert "cosmai#13:" in reasons and "machine path" in reasons
    assert "cosmai#20:" in reasons


def test_a_body_with_only_english_anchors_lints_clean(run):
    # #192 D11: the anchors move to English, and an issue that already uses them must not be read as
    # an issue with no completion criterion at all.
    done = run(
        "lint",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "english anchors", body=BODY, labels=("ch:tool",), parent=10),
        ],
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() == ""


def test_a_body_with_only_korean_anchors_now_lints_red(run):
    # #204: the window closed and the Korean anchor pair is gone, so a body whose completion
    # heading is Korean has no "Done when" section as far as the tool is concerned.
    body = f"## Context\nsomething\n\n## {KOREAN_LINE}\na machine checks it\n"
    done = run(
        "lint",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "korean anchor", body=body, labels=("ch:tool",), parent=10),
        ],
    )
    assert done.returncode == 1, done.stdout + done.stderr
    assert "body has no Done when section" in done.stdout, done.stdout


def test_the_user_queue_is_ordered_by_how_much_it_unblocks(run):
    done = run(
        "ready",
        "--user",
        "--json",
        upstream=[
            epic(10, "tool", subs=(11, 12)),
            issue(11, "awaiting a decision", labels=("ch:tool", "needs-user"), parent=10),
            issue(12, "another decision", labels=("ch:tool", "needs-user"), parent=10),
            issue(13, "one", labels=("ch:tool",), parent=10, blocked_by=((UPSTREAM, 12, "OPEN"),)),
            issue(14, "two", labels=("ch:tool",), parent=10, blocked_by=((UPSTREAM, 12, "OPEN"),)),
            issue(20, "an old memo", body=MEMO_BODY, labels=("memo",), updated_days_ago=20),
            issue(21, "a new memo", body=MEMO_BODY, labels=("memo",), updated_days_ago=1),
        ],
    )
    assert done.returncode == 0, done.stderr
    queue = json.loads(done.stdout)["user_queue"]
    assert [row["key"] for row in queue] == ["cosmai#12", "cosmai#11", "cosmai#20"], queue
    assert queue[0]["unblocks"] == 2


def test_needs_user_is_out_of_the_default_ready_listing(run):
    fixture = [
        epic(10, "tool", subs=(11,)),
        issue(11, "awaiting a decision", labels=("ch:tool", "needs-user"), parent=10),
    ]
    assert "cosmai#11" not in run("ready", upstream=fixture).stdout
    assert "cosmai#11" in run("ready", "--user", upstream=fixture).stdout


def test_audit_reports_drift_without_failing(run):
    done = run(
        "audit",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "idle", labels=("ch:tool",), parent=10, assignees=("shk95",), updated_days_ago=3),
            issue(20, "an old memo", body=MEMO_BODY, labels=("memo",), updated_days_ago=30),
        ],
        fork_labels=["channel"],
    )
    assert done.returncode == 0, done.stderr
    assert "cosmai#11" in done.stdout
    assert "memo older than 14 days (1)" in done.stdout, done.stdout
    # The fork fixture is missing five of the six shared labels, which is what makes a rule
    # unenforceable in one repo while reading as enforced in the other.
    assert "when-touched" in done.stdout


# ---------------------------------------------------------------------------------------------
# #232 Work 3: `audit`'s first section is the last nightly (scheduled) run of suite.yml.
# ---------------------------------------------------------------------------------------------


def _nightly_block(stdout: str) -> str:
    return stdout.split("nightly")[1].split("\n\n")[0]


def test_audit_reports_a_green_nightly_first(run):
    done = run(
        "audit",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",))],
        nightly_date="2026-09-05T17:00:00Z",
        nightly_conclusion="success",
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().startswith("nightly"), done.stdout
    block = _nightly_block(done.stdout)
    assert "2026-09-05T17:00:00Z" in block and "success" in block, done.stdout
    assert "⚠" not in block, done.stdout


def test_audit_flags_a_red_nightly(run):
    done = run(
        "audit",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",))],
        nightly_date="2026-09-05T17:00:00Z",
        nightly_conclusion="failure",
    )
    block = _nightly_block(done.stdout)
    assert "failure" in block, done.stdout
    assert "⚠" in block, done.stdout


def test_audit_flags_a_missing_nightly(run):
    done = run(
        "audit",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",))],
        nightly_missing=True,
    )
    block = _nightly_block(done.stdout)
    assert "no scheduled run" in block, done.stdout
    assert "⚠" in block, done.stdout


def test_audit_reports_nightly_unavailable_when_the_api_call_fails(run):
    done = run(
        "audit",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",))],
        nightly_fails=True,
    )
    assert done.returncode == 0, done.stderr
    block = _nightly_block(done.stdout)
    assert "(unavailable)" in block, done.stdout


# ---------------------------------------------------------------------------------------------
# #233 Work 3: an `ops` section, after nightly, from pipeline_health/collector_health.
# ---------------------------------------------------------------------------------------------


def _ops_block(stdout: str) -> str:
    return stdout.split("ops")[1].split("\n\n")[0]


def test_audit_reports_a_late_pipeline_stage(run):
    done = run(
        "audit",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",))],
        ops_pipeline="commerce:oliveyoung: late\n",
    )
    assert done.returncode == 0, done.stderr
    assert "commerce:oliveyoung: late" in _ops_block(done.stdout), done.stdout


def test_audit_reports_a_failed_collector_run(run):
    done = run(
        "audit",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",))],
        ops_collector="naver:blog failed (run 42)\n",
    )
    assert done.returncode == 0, done.stderr
    assert "naver:blog failed (run 42)" in _ops_block(done.stdout), done.stdout


def test_audit_reports_ops_none_when_both_queries_come_back_clean(run):
    done = run(
        "audit",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",))],
    )
    assert "none" in _ops_block(done.stdout), done.stdout


def test_audit_reports_ops_unavailable_when_the_db_query_fails(run):
    done = run(
        "audit",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",))],
        ops_fails=True,
    )
    assert done.returncode == 0, done.stderr
    assert "(unavailable)" in _ops_block(done.stdout), done.stdout


def test_the_tool_says_it_is_unverified_when_gh_is_missing(tmp_path: Path):
    # Exit 69 is prerequisite's "unknown". Reporting a missing gh as a rule violation would teach
    # people that lint's exit 1 means nothing.
    empty = tmp_path / "empty"
    empty.mkdir()
    done = subprocess.run(
        [str(ISSUE), "ready"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PATH": str(empty)},
        check=False,
    )
    assert done.returncode == 69, (done.returncode, done.stdout, done.stderr)
    assert "gh" in done.stderr


def test_the_resource_is_read_from_the_scale_section_only(run):
    # #61's own body quotes the word "Resources:" while describing this rule; a whole-body search
    # printed that sentence as the issue's resource.
    body = (
        "## Todo\nput a `Resources:` value on every row\n\n"
        "## Channel / grade / size\nSize M · Resources: shared DB read\n"
    )
    done = run(
        "ready",
        "--json",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "scale section", body=body, labels=("ch:tool",))],
    )
    assert json.loads(done.stdout)["channels"][0]["items"][0]["resource"] == "shared DB read"


def test_a_rule_quoting_home_is_not_a_machine_path(run):
    # #60 and #61 both write the guarded prefix inside backticks to state the guard itself. Flagging that
    # makes lint cry wolf on the two issues that define the rule.
    done = run(
        "lint",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(
                11,
                "quoting the rule",
                body=BODY + "\n- the body contains `/ho" + "me/`.\n",
                labels=("ch:tool",),
                parent=10,
            ),
        ],
    )
    assert done.returncode == 0, done.stdout


def test_audit_counts_the_default_ready_queue_and_needs_user_apart(run):
    # #60 Phase 4.1 wants the two to agree. The needs-user issue is the only thing that can make
    # them disagree, because it is the one issue the default listing drops.
    fixture = [
        epic(10, "tool", subs=(11, 13)),
        issue(11, "in place", labels=("ch:tool",), parent=10),
        issue(13, "awaiting a decision", labels=("ch:tool", "needs-user"), parent=10),
        issue(12, "stray", labels=("ch:tool",)),
    ]
    audit = run("audit", upstream=fixture).stdout
    assert "ch:tool (cosmai#10) · 2 open" in audit, audit
    assert "needs-user 1" in audit, audit
    ready = json.loads(run("ready", "--json", upstream=fixture).stdout)["channels"][0]["items"]
    assert [row["key"] for row in ready] == ["cosmai#11", "cosmai#12"]


def test_the_user_listing_marks_which_items_are_waiting_on_the_user(run):
    # --user folds two lists into one; without a marker the reader cannot tell which rows are
    # startable work and which are sitting in their own queue.
    fixture = [
        epic(10, "tool", subs=(11, 12)),
        issue(11, "awaiting a decision", labels=("ch:tool", "needs-user"), parent=10),
        issue(12, "plain work", labels=("ch:tool",), parent=10),
    ]
    rows = json.loads(run("ready", "--user", "--json", upstream=fixture).stdout)["channels"][0]["items"]
    assert [row["needs_user"] for row in rows] == [True, False]
    assert [row["status"] for row in rows] == ["ready", "ready"]
    text = run("ready", "--user", upstream=fixture).stdout
    assert "needs-user" in [line for line in text.splitlines() if "cosmai#11" in line][0]
    assert "needs-user" not in [line for line in text.splitlines() if "cosmai#12" in line][0]


def test_a_closed_memo_blocker_does_not_keep_lint_red(run):
    # Promotion closes the memo and re-issues it. A rule that reads the edge without its state
    # would keep the promoted issue red forever, which is how a check stops being run.
    done = run(
        "lint",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(
                11, "after promotion", labels=("ch:tool",), parent=10, blocked_by=((UPSTREAM, 20, "CLOSED"),)
            ),
            issue(20, "a closed memo", body=MEMO_BODY, labels=("memo",)),
        ],
    )
    assert done.returncode == 0, done.stdout


def test_a_repo_that_fails_to_fetch_stops_the_command(run):
    # A half graph is worse than no graph: the fork holds the blocker for cosmai#55, so a swallowed
    # fork fetch would report a blocked issue as ready.
    done = run(
        "ready",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",), parent=10)],
        gh_fails_on="fork",
    )
    assert done.returncode != 0, done.stdout
    assert done.stdout.strip() == "", done.stdout
    assert FORK in done.stderr, done.stderr


def test_an_errors_response_is_not_read_as_an_empty_repo(run):
    # GraphQL answers a partial failure with HTTP 200 and no `data`, which reads as "this repo has
    # no open issues" to anything that only checks gh's exit code.
    done = run(
        "lint",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",), parent=10)],
        gh_errors_on="upstream",
    )
    assert done.returncode != 0, done.stdout
    assert done.stdout.strip() == ""
    assert UPSTREAM in done.stderr, done.stderr


def test_a_partial_response_is_not_read_as_the_whole_graph(run):
    # HTTP 200 with `data` AND `errors`: the nodes are arrays, so "did data arrive" says yes.
    # What is missing is the nesting, and the queue is built out of the nesting.
    done = run(
        "audit",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",), parent=10)],
        gh_partial_on="upstream",
    )
    assert done.returncode != 0, done.stdout
    assert done.stdout.strip() == "", done.stdout
    assert UPSTREAM in done.stderr, done.stderr


def test_a_partial_response_does_not_empty_the_held_summary(run):
    # The sharpest loss: `assignees` comes back empty, so two issues someone is already working
    # read as startable and the resources they hold vanish. Dying is the only safe answer.
    done = run(
        "ready",
        upstream=[
            epic(10, "tool", subs=(11, 12)),
            issue(11, "one", labels=("ch:tool",), parent=10, assignees=("shk95",)),
            issue(12, "two", labels=("ch:tool",), parent=10, assignees=("shk95",)),
        ],
        gh_partial_on="upstream",
    )
    assert done.returncode != 0, done.stdout
    assert "in progress" not in done.stdout, done.stdout


def test_a_partial_response_on_the_fork_names_the_fork(run):
    # Two repos share one graph; the message has to say which half was lost.
    done = run(
        "lint",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",), parent=10)],
        fork=[
            epic(20, "population", subs=(21,)),
            issue(21, "fork work", labels=("ch:population",), parent=20),
        ],
        gh_partial_on="fork",
    )
    assert done.returncode != 0, done.stdout
    assert FORK in done.stderr, done.stderr


def test_a_when_touched_issue_needs_no_completion_criteria(run):
    # This kind is finished by other work. Demanding a completion criterion contradicts the label
    # and did in fact keep lint red on issues whose bodies were right (fork #43, #44).
    done = run(
        "lint",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "when touched", body=WHEN_TOUCHED_BODY, labels=("ch:tool", "when-touched"), parent=10),
        ],
    )
    assert done.returncode == 0, done.stdout
    assert done.stdout.strip() == "", done.stdout


def test_a_when_touched_issue_without_when_to_fix_is_a_lint_error(run):
    # The exemption is not an absence of rules -- when it gets fixed still has to be written down.
    done = run(
        "lint",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "when touched", body=BODY, labels=("ch:tool", "when-touched"), parent=10),
        ],
    )
    assert done.returncode != 0, done.stdout
    assert "When to fix" in done.stdout, done.stdout


def test_korean_in_an_issue_opened_after_the_effective_date_is_a_lint_error(run):
    # #192 D12: after the migration date a new issue written in Korean is a rule violation, not a
    # body left over from before. The date is a constant in the tool, so this fixture names a
    # creation date far past it rather than leaning on today's. The body keeps the English Done
    # when heading so the only finding this proves is the D12 one, not a missing section.
    done = run(
        "lint",
        upstream=[
            epic(10, "tool", subs=(11, 12)),
            issue(
                11,
                "korean body",
                body=BODY + f"\n{KOREAN_LINE}\n",
                labels=("ch:tool",),
                parent=10,
                created_at=AFTER_THE_WINDOW,
            ),
            issue(
                12, f"[{KOREAN_LINE}]", body=BODY, labels=("ch:tool",), parent=10, created_at=AFTER_THE_WINDOW
            ),
        ],
    )
    assert done.returncode == 1, done.stdout
    flagged = [line for line in done.stdout.splitlines() if "Korean" in line]
    assert len(flagged) == 2, done.stdout
    assert any(line.startswith("cosmai#11:") for line in flagged), done.stdout
    assert any(line.startswith("cosmai#12:") for line in flagged), done.stdout


def test_halfwidth_jamo_is_korean_to_the_lint_rule_too(run):
    # One rule, three enforcement sites: tool/checks/lang and the commit-msg hook both count these
    # codepoints, so a lint that did not would make a green check stop being evidence.
    jamo = (
        (Path(__file__).resolve().parent / "fixtures" / "halfwidth_jamo.txt")
        .read_text(encoding="utf-8")
        .strip()
    )
    done = run(
        "lint",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(
                11,
                "halfwidth " + jamo,
                body=BODY,
                labels=("ch:tool",),
                parent=10,
                created_at=AFTER_THE_WINDOW,
            ),
        ],
    )
    assert done.returncode == 1, done.stdout
    assert "Korean" in done.stdout, done.stdout


def test_korean_in_an_issue_opened_before_the_effective_date_is_not_a_lint_error(run):
    # Otherwise the rule reports every issue in both repos on the day it lands, which is the same
    # as having no rule. Uses `issue`'s BEFORE_THE_WINDOW default; the English Done when heading
    # keeps this isolated to the D12 date check.
    done = run(
        "lint",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "korean body", body=BODY + f"\n{KOREAN_LINE}\n", labels=("ch:tool",), parent=10),
        ],
    )
    assert done.returncode == 0, done.stdout
    assert done.stdout.strip() == "", done.stdout


def test_korean_inside_a_code_fence_is_not_a_lint_error(run):
    # A fenced block is quoted evidence -- a log line, a seed row, a body being migrated. Rewriting
    # it would falsify the quote.
    body = BODY + "\n```\n" + KOREAN_LINE + "\n```\n"
    done = run(
        "lint",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(
                11, "quoted korean", body=body, labels=("ch:tool",), parent=10, created_at=AFTER_THE_WINDOW
            ),
        ],
    )
    assert done.returncode == 0, done.stdout
    assert done.stdout.strip() == "", done.stdout


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A repo whose main and working tree disagree about markers in both directions.

    git grep skips untracked files, so the working tree has to differ in tracked ones: main's
    marker is edited away, and a marker main never had is added.
    """
    repo = tmp_path / "checkout"
    repo.mkdir()
    git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(repo)]
    # Hooks export GIT_DIR; without stripping it these commands would act on the enclosing checkout.
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(
        [*git[:-2], "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True, env=clean
    )
    (repo / "kept.py").write_text("# TO" + "DO(#7) fix it while you are in here\n", encoding="utf-8")
    (repo / "scratch.py").write_text("# no marker\n", encoding="utf-8")
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True, env=clean)
    subprocess.run([*git, "commit", "-qm", "chore: seed"], check=True, capture_output=True, env=clean)
    (repo / "kept.py").write_text("# the marker was removed mid-work\n", encoding="utf-8")
    (repo / "scratch.py").write_text("# TO" + "DO(#8) an uncommitted marker\n", encoding="utf-8")
    return repo


def test_the_todo_survey_reads_main_not_the_working_tree(run, checkout: Path):
    # Worktrees share the ref, so whichever checkout runs audit sees a different working tree and a
    # different answer. main is the one tree every worker agrees on.
    done = run(
        "audit",
        upstream=[
            epic(10, "tool", subs=(7, 9)),
            issue(7, "has a marker", labels=("ch:tool", "when-touched"), parent=10),
            issue(9, "has no marker", labels=("ch:tool", "when-touched"), parent=10),
        ],
        cwd=checkout,
    )
    assert done.returncode == 0, done.stderr
    assert "TO" + "DO(#8)" not in done.stdout, done.stdout
    missing = done.stdout.split("open when-touched issue with no marker in the code")[1]
    assert "cosmai#9" in missing and "cosmai#7" not in missing, done.stdout


def _git(repo: Path) -> tuple[list[str], dict[str, str]]:
    """A git command prefix scoped to `repo`, with GIT_* stripped so a nested checkout is not fooled
    into acting on the enclosing worktree."""
    return ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(repo)], {
        k: v for k, v in os.environ.items() if not k.startswith("GIT_")
    }


@pytest.fixture
def wave_checkout(tmp_path: Path) -> Path:
    """A repo on main with a merged `wave/tool` branch and no issue branch left open.

    This is the exact shape AGENTS.md says must not happen: the wave rule deletes `wave/<channel>`
    locally and on origin once it merges, and nothing enforced that until #196.
    """
    repo = tmp_path / "wave-checkout"
    git, env = _git(repo)
    subprocess.run(
        [*git[:-2], "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True, env=env
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run([*git, "commit", "-qm", "chore: seed"], check=True, capture_output=True, env=env)
    subprocess.run([*git, "branch", "wave/tool"], check=True, capture_output=True, env=env)
    return repo


def test_audit_reports_a_merged_wave_branch_left_on_a_local_ref(run, wave_checkout: Path):
    # #196: `wave/tool` is an ancestor of main (merged) and no `tool/<n>-*` branch is left open, so
    # this is the exact leftover the wave rule says must not survive the wave.
    done = run(
        "audit",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",), parent=10)],
        cwd=wave_checkout,
    )
    assert done.returncode == 0, done.stderr
    block = done.stdout.split("Merged wave branch left behind")[1].split("\n\n")[0]
    assert "wave/tool" in block, done.stdout


def test_audit_reports_a_merged_wave_branch_left_on_a_remote_tracking_ref(run, wave_checkout: Path):
    # The wave rule deletes the branch on origin too; a checkout that only fetched (not pruned) sees
    # it as `origin/wave/<channel>` instead of a local branch, and that has to be caught the same way.
    git, env = _git(wave_checkout)
    sha = subprocess.run(
        [*git, "rev-parse", "wave/tool"], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()
    subprocess.run([*git, "branch", "-D", "wave/tool"], check=True, capture_output=True, env=env)
    subprocess.run(
        [*git, "update-ref", "refs/remotes/origin/wave/tool", sha], check=True, capture_output=True, env=env
    )
    done = run(
        "audit",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",), parent=10)],
        cwd=wave_checkout,
    )
    assert done.returncode == 0, done.stderr
    block = done.stdout.split("Merged wave branch left behind")[1].split("\n\n")[0]
    assert "wave/tool" in block, done.stdout


def test_audit_stays_silent_on_an_open_wave_with_an_unmerged_issue_branch(run, wave_checkout: Path):
    # The guard: a fresh wave branch that has not diverged from main is trivially "merged" too, and
    # without this guard every open wave would be misreported as a leftover the moment it is cut.
    git, env = _git(wave_checkout)
    subprocess.run(
        [*git, "checkout", "-qb", "tool/117-carry", "main"], check=True, capture_output=True, env=env
    )
    (wave_checkout / "carry.txt").write_text("still open\n", encoding="utf-8")
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run([*git, "commit", "-qm", "chore: carry"], check=True, capture_output=True, env=env)
    subprocess.run([*git, "checkout", "-q", "main"], check=True, capture_output=True, env=env)
    done = run(
        "audit",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",), parent=10)],
        cwd=wave_checkout,
    )
    assert done.returncode == 0, done.stderr
    block = done.stdout.split("Merged wave branch left behind")[1].split("\n\n")[0]
    assert "none" in block, done.stdout


def test_audit_stays_silent_with_no_wave_branch(run, checkout: Path):
    # The common case -- no channel is mid-wave -- must not print anything under the new item.
    done = run(
        "audit",
        upstream=[epic(10, "tool", subs=(7,)), issue(7, "work", labels=("ch:tool",), parent=10)],
        cwd=checkout,
    )
    assert done.returncode == 0, done.stderr
    block = done.stdout.split("Merged wave branch left behind")[1].split("\n\n")[0]
    assert "none" in block, done.stdout


@pytest.mark.parametrize("origin_is_fork", [False, True], ids=["upstream-checkout", "fork-checkout"])
def test_recheck_names_each_reason_with_the_checklist_items_to_walk(
    run, monkeypatch, fork_origin_checkout: Path, origin_is_fork: bool
):
    # AGENTS.md's recheck rule is five questions (premise, blockedBy, release condition, grade,
    # duplicate). A bare list of issue numbers would leave the reader to guess which of the five
    # this row is about.
    # #199: primary must come from this override, not from `git remote get-url origin` of whichever
    # checkout the suite happens to run in -- parametrized over a fork-origin checkout to prove it.
    monkeypatch.setenv("COSMAI_ISSUE_PRIMARY", UPSTREAM)
    cwd = fork_origin_checkout if origin_is_fork else None
    quoted = BODY + "\n`tool/issue` is here and `tool/nowhere.py:12` is not\n"
    done = run(
        "recheck",
        "--json",
        upstream=[
            epic(10, "tool", subs=(11, 12, 13, 14, 15)),
            issue(11, "untouched", labels=("ch:tool",), parent=10, updated_days_ago=20),
            issue(12, "date passed", body=released(day(-2)), labels=("ch:tool", "deferred"), parent=10),
            issue(
                13,
                "prose condition",
                body=BODY + "\n## Release condition\nwhen a consumer exists\n",
                labels=("ch:tool", "deferred"),
                parent=10,
            ),
            issue(14, "a path that moved", body=quoted, labels=("ch:tool",), parent=10),
            # updated before the closing it is pointed at by, so (e) still has something to say
            # about it (C-3: an issue touched after the closing goes quiet instead).
            issue(15, "its goal closed", labels=("ch:tool",), parent=10, updated_days_ago=5),
        ],
        upstream_closed=[
            closed(30, "[goal] a finished goal", "leads to #15\n", days_ago=2, labels=("goal",)),
            closed(31, "[decision] an old decision", "leads to #11\n", days_ago=30, labels=("decision",)),
        ],
        cwd=cwd,
    )
    assert done.returncode == 1, done.stdout + done.stderr
    by_key = {row["key"]: row for row in json.loads(done.stdout)}
    # cosmai#13 is not in the set at all: a "Release condition" section with no date in it is (C-2)
    # no longer treated as "no release condition" -- only (a)'s 14-day cycle watches it, and it has
    # not been 14 days.
    assert set(by_key) == {f"cosmai#{n}" for n in (11, 12, 14, 15)}, sorted(by_key)
    assert {r["code"] for r in by_key["cosmai#11"]["reasons"]} == {"a"}
    assert by_key["cosmai#11"]["reasons"][0]["checks"] == [
        "premise",
        "blockedBy",
        "release condition",
        "grade",
        "duplicate",
    ]
    assert {r["code"] for r in by_key["cosmai#12"]["reasons"]} == {"b"}
    assert by_key["cosmai#12"]["reasons"][0]["checks"] == ["release condition"]
    assert day(-2) in by_key["cosmai#12"]["reasons"][0]["why"]
    assert {r["code"] for r in by_key["cosmai#14"]["reasons"]} == {"d"}
    assert "tool/nowhere.py" in by_key["cosmai#14"]["reasons"][0]["why"]
    # The path that is there must not be reported, or the reason becomes noise nobody reads.
    assert "tool/issue" not in by_key["cosmai#14"]["reasons"][0]["why"]
    assert {r["code"] for r in by_key["cosmai#15"]["reasons"]} == {"e"}
    assert "cosmai#30" in by_key["cosmai#15"]["reasons"][0]["why"]
    # A decision closed 30 days ago is taken as already absorbed -- otherwise the list grows forever.
    assert not any(r["code"] == "e" for r in by_key["cosmai#11"]["reasons"])


def test_recheck_renders_the_reason_and_the_checklist(run):
    done = run(
        "recheck",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "date passed", body=released(day(-2)), labels=("ch:tool", "deferred"), parent=10),
        ],
    )
    assert done.returncode == 1, done.stdout
    assert "recheck 1" in done.stdout, done.stdout
    rows = [line for line in done.stdout.splitlines() if line.startswith("    ")]
    assert rows == ["    release condition " + day(-2) + " has passed → check: release condition"], (
        done.stdout
    )
    assert "  cosmai#11 · date passed" in done.stdout, done.stdout


def test_recheck_leaves_alone_what_is_still_waiting_for_its_condition(run):
    # #86 (date not yet reached) and #183 (deferred behind an open blocker) are the two shapes that
    # must stay quiet, or boot starts with a list of issues nobody can act on.
    done = run(
        "recheck",
        upstream=[
            epic(10, "tool", subs=(11, 12)),
            issue(11, "too early", body=released(day(5)), labels=("ch:tool", "deferred"), parent=10),
            issue(
                12,
                "blocked",
                body=BODY + "\n## Release condition\nafter the naver work\n",
                labels=("ch:tool", "deferred"),
                parent=10,
                blocked_by=((UPSTREAM, 11, "OPEN"),),
            ),
        ],
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "nothing to recheck" in done.stdout, done.stdout
    assert "cosmai#11" not in done.stdout and "cosmai#12" not in done.stdout, done.stdout


def test_recheck_does_not_read_a_repo_name_or_a_label_as_a_path(run):
    # Bodies are full of `owner/repo` and `ch:collectors/youtube`. Reported as missing files they
    # would put every issue on the list, which is the same as having no list.
    body = BODY + "\n`shk95-1/cosmai` and `ch:collectors/youtube` and `tool/checks/paths`\n"
    done = run(
        "recheck",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "quoting", body=body, labels=("ch:tool",), parent=10),
        ],
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "cosmai#11" not in done.stdout, done.stdout


def test_recheck_skips_memos(run):
    # A memo has its own 14-day track in audit and the user queue; listing it twice teaches the
    # reader that recheck is mostly memos.
    done = run(
        "recheck",
        upstream=[issue(20, "an old memo", body=MEMO_BODY, labels=("memo",), updated_days_ago=40)],
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "cosmai#20" not in done.stdout, done.stdout


def test_recheck_reads_the_last_date_in_the_release_section_not_any_date(run):
    # #86 writes a past date and a future one into the same release-condition section ("rebuild
    # 2026-08-25 + 14 days; after 2026-09-08"). A rule that fires on any past date puts an issue
    # that is still early on every boot's list, while #18 has two past dates and must be caught.
    # Only the latest date gets both cases right.
    mixed = "## Release condition\nseven quiet days after the cutover (%s) and after %s\n"
    done = run(
        "recheck",
        "--json",
        upstream=[
            epic(10, "tool", subs=(11, 12)),
            issue(11, "too early", body=BODY + mixed % (day(-9), day(5)), labels=("ch:tool",), parent=10),
            issue(12, "passed", body=BODY + mixed % (day(-9), day(-2)), labels=("ch:tool",), parent=10),
        ],
    )
    assert done.returncode == 1, done.stdout + done.stderr
    rows = json.loads(done.stdout)
    assert [row["key"] for row in rows] == ["cosmai#12"], rows
    assert [r["code"] for r in rows[0]["reasons"]] == ["b"], rows
    assert day(-2) in rows[0]["reasons"][0]["why"], rows
    assert day(-9) not in rows[0]["reasons"][0]["why"], rows


@pytest.fixture
def checkout_with_marker(tmp_path: Path) -> Path:
    """A checkout whose only in-code marker points at #43, an issue that only the fork has."""
    repo = tmp_path / "checkout-primary"
    repo.mkdir()
    git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(repo)]
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(
        [*git[:-2], "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True, env=clean
    )
    (repo / "pipeline.py").write_text("# TO" + "DO(#43) fix it while you are in here\n", encoding="utf-8")
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True, env=clean)
    subprocess.run([*git, "commit", "-qm", "chore: seed"], check=True, capture_output=True, env=clean)
    return repo


@pytest.fixture
def fork_origin_checkout(tmp_path: Path) -> Path:
    """A checkout whose origin is the fork, carrying `tool/issue` tracked -- proves recheck's (d)
    depends only on COSMAI_ISSUE_PRIMARY, never on which repo the checkout's origin names (#199).
    """
    repo = tmp_path / "checkout-fork-origin"
    (repo / "tool").mkdir(parents=True)
    (repo / "tool" / "issue").write_text("# stand-in for the real file\n", encoding="utf-8")
    git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(repo)]
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(
        [*git[:-2], "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True, env=clean
    )
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True, env=clean)
    subprocess.run([*git, "commit", "-qm", "chore: seed"], check=True, capture_output=True, env=clean)
    subprocess.run(
        [*git, "remote", "add", "origin", "https://github.com/shk95/cosmai-import-ydc"],
        check=True,
        capture_output=True,
        env=clean,
    )
    return repo


def test_a_fork_checkout_matches_todo_markers_against_the_forks_own_issues(
    run, monkeypatch, checkout_with_marker: Path
):
    # #174: primary used to be hardcoded to the first of REPOS (upstream), so a fork checkout could
    # never match its own TODO(#n) markers and audit stayed red on issues that were open and fine.
    monkeypatch.setenv("COSMAI_ISSUE_PRIMARY", FORK)
    done = run(
        "audit",
        upstream=[
            epic(10, "tool", subs=(9,)),
            issue(9, "upstream has no marker", labels=("ch:tool", "when-touched"), parent=10),
        ],
        fork=[
            epic(20, "population", subs=(43,)),
            issue(43, "the fork's marker", labels=("ch:tool", "when-touched"), parent=20),
        ],
        cwd=checkout_with_marker,
    )
    assert done.returncode == 0, done.stderr
    assert "TO" + "DO(#43)" not in done.stdout, done.stdout
    missing = done.stdout.split("open when-touched issue with no marker in the code")[1]
    assert "cosmai-import-ydc#43" not in missing.split("\n\n")[0], done.stdout


def test_an_upstream_checkout_keeps_matching_todo_markers_against_upstream(
    run, monkeypatch, checkout_with_marker: Path
):
    # Same graph and checkout as above, but the override now names upstream: this is the behavior
    # #174 must leave unchanged, so #43 (a fork-only issue) is still an unmatched marker and
    # upstream's own #9 is still a marker-less when-touched issue -- the asymmetry the bug relied on.
    monkeypatch.setenv("COSMAI_ISSUE_PRIMARY", UPSTREAM)
    done = run(
        "audit",
        upstream=[
            epic(10, "tool", subs=(9,)),
            issue(9, "upstream has no marker", labels=("ch:tool", "when-touched"), parent=10),
        ],
        fork=[
            epic(20, "population", subs=(43,)),
            issue(43, "the fork's marker", labels=("ch:tool", "when-touched"), parent=20),
        ],
        cwd=checkout_with_marker,
    )
    assert done.returncode == 0, done.stderr
    assert "TO" + "DO(#43)" in done.stdout, done.stdout
    missing = done.stdout.split("open when-touched issue with no marker in the code")[1].split("\n\n")[0]
    assert "cosmai#9" in missing, done.stdout


def test_the_primary_repo_is_matched_by_name_not_owner(run, monkeypatch, checkout_with_marker: Path):
    # Origin's owner and REPOS' owner can each be renamed independently (real case: origin is
    # shk95-1/cosmai and shk95/cosmai-import-ydc while REPOS still says slopindustries/*) -- matching
    # the full owner/repo would then pick primary for no checkout at all, silently falling back to
    # upstream and reviving the #174 bug on every fork checkout.
    monkeypatch.setenv("COSMAI_ISSUE_PRIMARY", "some-other-owner/cosmai-import-ydc")
    done = run(
        "audit",
        upstream=[
            epic(10, "tool", subs=(9,)),
            issue(9, "upstream has no marker", labels=("ch:tool", "when-touched"), parent=10),
        ],
        fork=[
            epic(20, "population", subs=(43,)),
            issue(43, "the fork's marker", labels=("ch:tool", "when-touched"), parent=20),
        ],
        cwd=checkout_with_marker,
    )
    assert done.returncode == 0, done.stderr
    assert "matches no REPOS entry" not in done.stderr, done.stderr
    assert "TO" + "DO(#43)" not in done.stdout, done.stdout
    missing = done.stdout.split("open when-touched issue with no marker in the code")[1].split("\n\n")[0]
    assert "cosmai-import-ydc#43" not in missing, done.stdout


@pytest.fixture
def checkout_with_upstream_remote(tmp_path: Path) -> Path:
    """A fork-shaped checkout: a plain git repo with an `upstream` remote configured."""
    repo = tmp_path / "checkout-upstream-remote"
    repo.mkdir()
    git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(repo)]
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(
        [*git[:-2], "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True, env=clean
    )
    (repo / "seed.py").write_text("# seed\n", encoding="utf-8")
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True, env=clean)
    subprocess.run([*git, "commit", "-qm", "chore: seed"], check=True, capture_output=True, env=clean)
    subprocess.run(
        [*git, "remote", "add", "upstream", "https://example.invalid/upstream/repo.git"],
        check=True,
        capture_output=True,
        env=clean,
    )
    return repo


@pytest.fixture
def fork_behind_upstream(tmp_path: Path) -> Path:
    """A fork checkout whose `upstream/main` carries commits it does not have.

    One of them touches a shared surface (db/) and one does not (portal/), which is the whole
    judgment the audit item makes. No network: `upstream` is a sibling directory.
    """
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def run_git(cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(cwd), *args],
            check=True,
            capture_output=True,
            env=clean,
        )

    origin = tmp_path / "upstream-repo"
    origin.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(origin)], check=True, capture_output=True, env=clean
    )
    (origin / "seed.py").write_text("# seed\n", encoding="utf-8")
    run_git(origin, "add", "-A")
    run_git(origin, "commit", "-qm", "chore: seed")

    fork = tmp_path / "fork-repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(fork)], check=True, capture_output=True, env=clean)
    run_git(fork, "remote", "add", "upstream", str(origin))

    (origin / "db").mkdir()
    (origin / "db" / "bootstrap.sql").write_text("-- shared\n", encoding="utf-8")
    run_git(origin, "add", "-A")
    run_git(origin, "commit", "-qm", "feat(db): a shared surface")
    (origin / "portal").mkdir()
    (origin / "portal" / "app.js").write_text("// not shared\n", encoding="utf-8")
    run_git(origin, "add", "-A")
    run_git(origin, "commit", "-qm", "feat(portal): not a shared surface")

    run_git(fork, "fetch", "-q", "upstream")
    return fork


FAKE_FOREIGN_CLOSES = """#!/bin/sh
printf '%s\\n' "#43 · closed by a commit from the other repo · abc1234"
exit 1
"""


def test_audit_carries_the_foreign_closes_output_verbatim(
    run, monkeypatch, tmp_path: Path, checkout_with_upstream_remote: Path
):
    # #174 (B): audit calls tool/checks/foreign-closes rather than reimplementing its judgment --
    # this proves the call happens and the block is exactly what the script printed.
    fake = tmp_path / "fake-foreign-closes"
    fake.write_text(FAKE_FOREIGN_CLOSES, encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("COSMAI_FOREIGN_CLOSES", str(fake))
    done = run(
        "audit",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",), parent=10)],
        cwd=checkout_with_upstream_remote,
    )
    assert done.returncode == 0, done.stderr
    assert "#43 · closed by a commit from the other repo · abc1234" in done.stdout, done.stdout


def test_audit_skips_foreign_closes_without_an_upstream_remote(run, checkout: Path):
    # An upstream checkout has no `upstream` remote to compare against (there is nothing foreign
    # relative to itself), so the section says it was skipped instead of erroring.
    done = run(
        "audit",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",), parent=10)],
        cwd=checkout,
    )
    assert done.returncode == 0, done.stderr
    # Three fork items share the note: foreign closes, fork behind, ownership.
    assert done.stdout.count("(no upstream remote — skipped)") == 3, done.stdout


FAKE_OWNERSHIP = """#!/bin/sh
printf '%s\\n' "db/migrate.sh" "STATE.md"
exit 1
"""


def test_audit_carries_the_ownership_check_output_verbatim(
    run, monkeypatch, tmp_path: Path, fork_behind_upstream: Path
):
    # #212: the boundary check is one script (tool/checks/ownership) and audit calls it rather than
    # reimplementing the list, one line per file so the reader has the path and not just a count.
    quiet = tmp_path / "fake-foreign-closes"
    quiet.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    quiet.chmod(0o755)
    monkeypatch.setenv("COSMAI_FOREIGN_CLOSES", str(quiet))
    fake = tmp_path / "fake-ownership"
    fake.write_text(FAKE_OWNERSHIP, encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("COSMAI_OWNERSHIP_CHECK", str(fake))
    monkeypatch.setenv("COSMAI_ISSUE_PRIMARY", FORK)
    done = run(
        "audit",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",), parent=10)],
        cwd=fork_behind_upstream,
    )
    assert done.returncode == 0, done.stderr
    block = done.stdout.split("fork changed a file upstream owns")[1].split("\n\n")[0]
    listed = [line.strip() for line in block.splitlines() if line.strip()]
    assert listed == ["db/migrate.sh", "STATE.md"], done.stdout


def test_audit_skips_the_ownership_check_without_an_upstream_remote(run, checkout: Path):
    # Same reason as foreign-closes: an upstream checkout owns everything, so there is no boundary
    # to test and "none" would read as a verdict the check never made.
    done = run(
        "audit",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",), parent=10)],
        cwd=checkout,
    )
    assert done.returncode == 0, done.stderr
    block = done.stdout.split("fork changed a file upstream owns")[1].split("\n\n")[0]
    assert "(no upstream remote — skipped)" in block, done.stdout


def test_audit_lists_upstream_commits_on_shared_surfaces_the_fork_lacks(
    run, monkeypatch, tmp_path: Path, fork_behind_upstream: Path
):
    # #192 methodology row 1: the fork drifting behind on db/, tool/checks/, contracts/ddl/ and
    # AGENTS.md is the gap the migration keeps reopening, so boot has to say it out loud.
    fake = tmp_path / "fake-foreign-closes"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("COSMAI_FOREIGN_CLOSES", str(fake))
    monkeypatch.setenv("COSMAI_ISSUE_PRIMARY", FORK)
    done = run(
        "audit",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",), parent=10)],
        cwd=fork_behind_upstream,
    )
    assert done.returncode == 0, done.stderr
    block = done.stdout.split("fork behind on shared surfaces")[1].split("\n\n")[0]
    listed = [line.strip() for line in block.splitlines() if line.strip()]
    # The db/ commit, and only it: the portal/ one is not a surface the two repos share.
    assert len(listed) == 1, done.stdout
    shas = subprocess.run(
        ["git", "-C", str(fork_behind_upstream), "log", "--format=%h", "upstream/main", "--not", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert listed[0] == shas[1], (listed, shas)


def test_audit_skips_fork_behind_without_an_upstream_remote(run, checkout: Path):
    done = run(
        "audit",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",), parent=10)],
        cwd=checkout,
    )
    assert done.returncode == 0, done.stderr
    block = done.stdout.split("fork behind on shared surfaces")[1]
    assert "(no upstream remote — skipped)" in block.split("\n\n")[0], done.stdout


def test_audit_says_so_when_the_upstream_ref_was_never_fetched(run, checkout_with_upstream_remote: Path):
    # The remote exists but nothing was fetched, so there is no upstream/main to compare against.
    # Printing "none" there would read as "the fork is up to date", which is not what is known.
    done = run(
        "audit",
        upstream=[epic(10, "tool", subs=(11,)), issue(11, "work", labels=("ch:tool",), parent=10)],
        cwd=checkout_with_upstream_remote,
    )
    assert done.returncode == 0, done.stderr
    block = done.stdout.split("fork behind on shared surfaces")[1].split("\n\n")[0]
    assert "no upstream/main" in block, done.stdout


def test_recheck_d_skips_a_path_with_a_parenthetical_note_right_after(run):
    # #174 (C-1): a path the body already marks as not-yet-there or moved should not also be
    # reported as "missing from the checkout" -- that would just restate the same note as a defect.
    body = BODY + "\n`tool/nowhere.py`(planned) is not here yet\n"
    done = run(
        "recheck",
        "--json",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "an annotated path", body=body, labels=("ch:tool",), parent=10),
        ],
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() == "[]", done.stdout


def test_recheck_d_skips_a_path_with_a_parenthetical_note_after_a_space(run):
    # The space-before-paren shape ("`path` (fork)") is the other form #174 (C-1) names.
    body = BODY + "\n`tool/nowhere.py` (fork) is where it went\n"
    done = run(
        "recheck",
        "--json",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "an annotated path", body=body, labels=("ch:tool",), parent=10),
        ],
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() == "[]", done.stdout


@pytest.mark.parametrize("origin_is_fork", [False, True], ids=["upstream-checkout", "fork-checkout"])
def test_recheck_d_still_flags_a_path_with_no_note(
    run, monkeypatch, fork_origin_checkout: Path, origin_is_fork: bool
):
    # #199: same origin-independence as the test above, for the plain case with no note.
    monkeypatch.setenv("COSMAI_ISSUE_PRIMARY", UPSTREAM)
    cwd = fork_origin_checkout if origin_is_fork else None
    body = BODY + "\n`tool/nowhere.py` is not here yet\n"
    done = run(
        "recheck",
        "--json",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "a plain path", body=body, labels=("ch:tool",), parent=10),
        ],
        cwd=cwd,
    )
    assert done.returncode == 1, done.stdout + done.stderr
    rows = json.loads(done.stdout)
    assert [r["code"] for r in rows[0]["reasons"]] == ["d"], rows
    assert "tool/nowhere.py" in rows[0]["reasons"][0]["why"], rows


def test_recheck_c_is_quiet_when_a_release_condition_section_exists_without_a_date(run):
    # #174 (C-2): a "Release condition" section with prose instead of a date already has (a)'s
    # 14-day cycle watching it -- firing (c) too on every boot is what killed the signal.
    body = BODY + "\n## Release condition\nwhen a consumer exists\n"
    done = run(
        "recheck",
        "--json",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "prose condition", body=body, labels=("ch:tool", "deferred"), parent=10),
        ],
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() == "[]", done.stdout


def test_recheck_c_still_fires_without_a_release_condition_section_at_all(run):
    done = run(
        "recheck",
        "--json",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "no condition", labels=("ch:tool", "deferred"), parent=10),
        ],
    )
    assert done.returncode == 1, done.stdout + done.stderr
    rows = json.loads(done.stdout)
    assert [r["code"] for r in rows[0]["reasons"]] == ["c"], rows


def test_recheck_e_is_quiet_once_the_pointed_at_issue_was_touched_after_the_closing(run):
    # #174 (C-3): a recheck comment on the issue is an update to it, so it should turn (e) off for
    # the rest of the 7-day window instead of repeating every boot (measured: #73, #89, #112, #124).
    done = run(
        "recheck",
        "--json",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "commented on", labels=("ch:tool",), parent=10, updated_days_ago=1),
        ],
        upstream_closed=[
            closed(30, "[goal] a finished goal", "leads to #11\n", days_ago=3, labels=("goal",))
        ],
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() == "[]", done.stdout


def test_recheck_e_still_fires_when_updated_before_the_closing(run):
    done = run(
        "recheck",
        "--json",
        upstream=[
            epic(10, "tool", subs=(11,)),
            issue(11, "not touched", labels=("ch:tool",), parent=10, updated_days_ago=5),
        ],
        upstream_closed=[
            closed(30, "[goal] a finished goal", "leads to #11\n", days_ago=3, labels=("goal",))
        ],
    )
    assert done.returncode == 1, done.stdout + done.stderr
    rows = json.loads(done.stdout)
    assert [r["code"] for r in rows[0]["reasons"]] == ["e"], rows
    assert "cosmai#30" in rows[0]["reasons"][0]["why"], rows


def test_no_subcommand_prints_korean(run, monkeypatch, tmp_path: Path, checkout_with_upstream_remote: Path):
    # #192 D10's completion criterion, checked rather than eyeballed: the four subcommands are what
    # a boot reads, and a single leftover Korean heading is what makes the migration look done.
    fake = tmp_path / "fake-foreign-closes"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("COSMAI_FOREIGN_CLOSES", str(fake))
    fixture = [
        epic(10, "tool", subs=(11, 13)),
        issue(11, "idle", labels=("ch:tool",), parent=10, assignees=("shk95",), updated_days_ago=3),
        issue(12, "two channels", labels=("ch:tool", "ch:repo")),
        issue(13, "awaiting a decision", labels=("ch:tool", "needs-user"), parent=10),
        issue(14, "deferred", labels=("ch:tool", "deferred"), parent=10, updated_days_ago=30),
        issue(20, "an old memo", body=MEMO_BODY, labels=("memo",), updated_days_ago=30),
    ]
    for args in (("ready", "--user"), ("recheck",), ("lint",), ("audit",)):
        done = run(*args, upstream=fixture, cwd=checkout_with_upstream_remote, fork_labels=["channel"])
        assert not HANGUL.search(done.stdout), (args, done.stdout)
        assert not HANGUL.search(done.stderr), (args, done.stderr)
