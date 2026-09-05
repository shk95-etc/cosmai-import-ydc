"""tool/status against fake `docker`/`nvidia-smi`, so #62's six sections are checked offline.

Isolation here is PATH precedence, not conftest's socket block: tool/status shells out to
`docker`/`nvidia-smi`, and a subprocess is outside the guard that stops in-process sockets.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STATUS = REPO_ROOT / "tool" / "status"

SECTIONS = ["containers", "images", "db", "cron", "gpu", "test-leftovers"]

# Answers "docker ps -a --format ...", "docker image inspect ...", "docker run ... sh -c ...",
# and "docker exec cosmai-postgres psql ..." with fixed text so the test never touches a real
# daemon; DOCKER_PS_FAIL turns the containers section into a forced failure to prove the other
# five sections still print when one breaks.
FAKE_DOCKER = """#!/bin/sh
case "$1" in
  ps)
    if [ -n "$DOCKER_PS_FAIL" ]; then echo "fake docker: daemon unreachable" >&2; exit 1; fi
    echo "cosmai-portal-1	Up 2 hours"
    echo "shared-postgres	Up 3 days"
    echo "cosmai-test-postgres-55432	Up 4 minutes"
    exit 0
    ;;
  image)
    echo "2026-08-24T00:00:00Z"
    exit 0
    ;;
  run)
    echo "PRETTY_NAME=\\"Debian GNU/Linux 13 (trixie)\\""
    echo "OpenSSL 3.5.0"
    exit 0
    ;;
  exec)
    echo "020_retrieval_chunk 2026-08-24 23:47:02"
    exit 0
    ;;
  *)
    echo "fake docker: no fixture for: $*" >&2
    exit 1
    ;;
esac
"""

FAKE_NVIDIA_SMI = """#!/bin/sh
echo "512 MiB, 3 %"
"""

# #232: the `== ci ==` section shells out to `gh api .../actions/workflows/suite.yml/runs`. A real
# `gh` on this host's PATH would otherwise reach the network, so this fake stands in front of it
# the same way FAKE_DOCKER does for docker.
FAKE_GH = """#!/bin/sh
case "$*" in
  *"workflows/suite.yml/runs"*)
    if [ -n "$FAKE_GH_CI_FAIL" ]; then echo "fake gh: the API said no" >&2; exit 1; fi
    if [ -n "$FAKE_GH_CI_MISSING" ]; then exit 0; fi
    printf '%s %s\\n' "$FAKE_GH_CI_DATE" "$FAKE_GH_CI_CONCLUSION"
    exit 0
    ;;
esac
echo "fake gh: no fixture for: $*" >&2
exit 1
"""


@pytest.fixture
def run(tmp_path: Path):
    """Runs tool/status with fake docker/nvidia-smi/gh first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    nvidia_smi = bin_dir / "nvidia-smi"
    nvidia_smi.write_text(FAKE_NVIDIA_SMI, encoding="utf-8")
    nvidia_smi.chmod(0o755)
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)

    def _run(*args: str, docker_ps_fails: bool = False, env_extra: dict | None = None):
        import os

        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "DOCKER_PS_FAIL": "1" if docker_ps_fails else "",
            "FAKE_GH_CI_FAIL": "",
            "FAKE_GH_CI_MISSING": "",
            "FAKE_GH_CI_DATE": "2026-09-05T17:00:00Z",
            "FAKE_GH_CI_CONCLUSION": "success",
            **(env_extra or {}),
        }
        return subprocess.run(
            [str(STATUS), *args],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=env,
            check=False,
        )

    return _run


def header_of(name: str) -> str:
    # The bare section name is not a safe search key: the image "cosmai-needs-cron:local" itself
    # contains the substring "cron", so only the full header line pins down where a section starts.
    return f"== {name} =="


def test_all_six_headers_print_in_order(run):
    done = run()
    assert done.returncode == 0, done.stderr
    positions = [done.stdout.index(header_of(name)) for name in SECTIONS]
    assert positions == sorted(positions), done.stdout


def test_exit_is_zero_even_when_a_section_fails(run):
    # containers depends on `docker ps`; forcing that to fail must not stop the other five.
    done = run(docker_ps_fails=True)
    assert done.returncode == 0, done.stderr
    for name in SECTIONS:
        assert header_of(name) in done.stdout, done.stdout


def test_a_failed_section_still_reports_unavailable(run):
    done = run(docker_ps_fails=True)
    lines = done.stdout.splitlines()
    containers_start = next(i for i, line in enumerate(lines) if line == header_of("containers"))
    images_start = next(i for i, line in enumerate(lines) if line == header_of("images"))
    body = "\n".join(lines[containers_start + 1 : images_start])
    assert "(unavailable)" in body, done.stdout


def test_no_docker_at_all_still_prints_every_header(tmp_path: Path):
    # Isolation is PATH precedence: an empty bin dir with no `docker`/`nvidia-smi` at all still
    # has to answer every section, just all "(unavailable)".
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    import os

    # Keep only coreutils-bearing dirs plus the empty one, so docker/nvidia-smi truly vanish.
    minimal_path = os.pathsep.join([str(empty_bin), "/usr/bin", "/bin"])
    env = {**os.environ, "PATH": minimal_path}
    done = subprocess.run(
        [str(STATUS)], capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, check=False
    )
    assert done.returncode == 0, done.stderr
    for name in SECTIONS:
        assert header_of(name) in done.stdout, done.stdout
    assert "(unavailable)" in done.stdout


def test_takes_no_arguments_and_still_exits_zero(run):
    done = run()
    assert done.returncode == 0


# ---------------------------------------------------------------------------------------------
# #232 Work 3: `== ci ==` names the last nightly run, the same line `tool/issue audit` prints.
# ---------------------------------------------------------------------------------------------


def test_ci_section_prints_the_last_nightly_run(run):
    done = run(env_extra={"FAKE_GH_CI_DATE": "2026-09-05T17:00:00Z", "FAKE_GH_CI_CONCLUSION": "success"})
    assert done.returncode == 0, done.stderr
    body = done.stdout.split(header_of("ci"))[1]
    assert "2026-09-05T17:00:00Z" in body, done.stdout
    assert "success" in body, done.stdout


def test_ci_section_reports_a_red_nightly(run):
    done = run(env_extra={"FAKE_GH_CI_CONCLUSION": "failure"})
    body = done.stdout.split(header_of("ci"))[1]
    assert "failure" in body, done.stdout


def test_ci_section_reports_no_scheduled_run(run):
    done = run(env_extra={"FAKE_GH_CI_MISSING": "1"})
    body = done.stdout.split(header_of("ci"))[1]
    assert "no scheduled run" in body, done.stdout


def test_ci_section_is_unavailable_when_gh_fails(run):
    done = run(env_extra={"FAKE_GH_CI_FAIL": "1"})
    body = done.stdout.split(header_of("ci"))[1]
    assert "(unavailable)" in body, done.stdout


def test_ci_section_is_unavailable_without_gh(tmp_path: Path):
    # /usr/bin and /bin are not gh-free on every host (this one has both a real, authenticated gh
    # and a real docker daemon with a real cosmai-postgres container) -- a PATH that keeps those
    # dirs wholesale would let this test read the real docker/production state, not just find a
    # real gh. So this hides docker/nvidia-smi/gh from a mirror of the real PATH (the technique
    # tests/tool/test_pre_push_hook.py uses for docker/uv/pg_isready) and puts fakes for the first
    # two in front, the same fakes the `run` fixture's other tests use -- never gh.
    import os

    own_bin = tmp_path / "own-bin"
    own_bin.mkdir()
    (own_bin / "docker").write_text(FAKE_DOCKER, encoding="utf-8")
    (own_bin / "docker").chmod(0o755)
    (own_bin / "nvidia-smi").write_text(FAKE_NVIDIA_SMI, encoding="utf-8")
    (own_bin / "nvidia-smi").chmod(0o755)

    hide = {"gh", "docker", "nvidia-smi"}
    shadow_root = tmp_path / "no-gh-path"
    shadow_dirs = [str(own_bin)]
    for index, directory in enumerate(os.environ.get("PATH", "").split(os.pathsep)):
        if not directory or not os.path.isdir(directory):
            continue
        shadow = shadow_root / f"d{index}"
        shadow.mkdir(parents=True)
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        for name in entries:
            if name in hide:
                continue
            try:
                (shadow / name).symlink_to(os.path.join(directory, name))
            except OSError:
                continue
        shadow_dirs.append(str(shadow))
    env = {**os.environ, "PATH": os.pathsep.join(shadow_dirs), "DOCKER_PS_FAIL": ""}
    done = subprocess.run(
        [str(STATUS)], capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, check=False
    )
    body = done.stdout.split(header_of("ci"))[1]
    assert "(unavailable)" in body, done.stdout
