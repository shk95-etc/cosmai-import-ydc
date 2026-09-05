"""#214: three suites fit in this host's memory and a fourth kills one of them (#212, #204).

`tool/checks/suite-lock` is the fragment `tool/checks/test` sources before it starts a container.
It counts the running `cosmai-test-postgres-*` containers AND the lock directories -- a suite that
is still migrating has no container yet -- and waits while the larger number is at the limit.

No Docker here: `docker` is a fake first on PATH that answers each call from a plan file, and the
poll interval comes from COSMAI_SUITE_WAIT_SECONDS so the wait is exercised without sleeping 15 s.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PORT = "55123"

# One line of the plan per `docker ps` call: how many suite containers are running right now.
# An exhausted plan means none, so a test cannot hang on a plan that ran out.
FAKE_DOCKER = """#!/bin/sh
plan="$FAKE_DOCKER_PLAN"
running=$(head -n 1 "$plan" 2>/dev/null)
[ -n "$running" ] || running=0
tail -n +2 "$plan" > "$plan.rest" 2>/dev/null || true
mv "$plan.rest" "$plan" 2>/dev/null || true
i=1
while [ "$i" -le "$running" ]; do
    printf 'cosmai-test-postgres-%s\\n' "$i"
    i=$((i + 1))
done
exit 0
"""


@pytest.fixture
def acquire(tmp_path: Path):
    """Sources the fragment and takes a slot, with a fake `docker` and a throwaway lock root."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    lock_root = tmp_path / "locks"

    def _acquire(
        running: list[int],
        interval: str = "0",
        timeout: float | None = 20.0,
        **overrides: str,
    ):
        plan = tmp_path / "plan"
        plan.write_text("".join(f"{n}\n" for n in running), encoding="utf-8")
        return subprocess.run(
            ["sh", "-c", f'. tool/checks/suite-lock; suite_lock_acquire {PORT}; printf "acquired\\n"'],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env={
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "FAKE_DOCKER_PLAN": str(plan),
                "COSMAI_SUITE_LOCK_DIR": str(lock_root),
                "COSMAI_SUITE_WAIT_SECONDS": interval,
                **overrides,
            },
        )

    _acquire.lock_root = lock_root  # type: ignore[attr-defined]
    return _acquire


def waiting_lines(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith("waiting: ")]


def waited_once(stdout: str) -> str:
    lines = waiting_lines(stdout)
    assert len(lines) == 1, stdout
    return lines[0]


def test_a_free_host_starts_at_once(acquire):
    done = acquire([0])
    assert done.returncode == 0, done.stderr
    assert "acquired" in done.stdout
    assert waiting_lines(done.stdout) == []


def test_the_slot_is_taken_as_a_directory_named_for_the_port(acquire):
    done = acquire([0])
    assert done.returncode == 0, done.stderr
    assert (acquire.lock_root / PORT).is_dir(), "no lock directory, so a starting suite is invisible"


def test_two_running_suites_are_under_the_limit(acquire):
    done = acquire([2])
    assert waiting_lines(done.stdout) == [], done.stdout
    assert "acquired" in done.stdout


def test_a_fourth_suite_waits_until_a_slot_frees(acquire):
    done = acquire([3, 3, 0])
    assert done.returncode == 0, done.stderr
    line = waited_once(done.stdout)
    assert line.startswith("waiting: 3 suites running (limit 3, slots in "), line
    assert str(acquire.lock_root) in line, "the waiting line must say where the slots are"
    assert "acquired" in done.stdout


def test_the_waiting_line_is_not_repeated_on_every_poll(acquire):
    # A twenty-minute wait must not scroll the terminal: one line a minute, not one a poll.
    done = acquire([3, 3, 3, 3, 3, 3, 0])
    assert len(waiting_lines(done.stdout)) == 1, done.stdout


def test_lock_directories_count_as_suites_that_have_no_container_yet(acquire):
    # A suite between `docker run` and its first test has a lock directory and, for a moment, no
    # container `docker ps` will name; counting only containers would let a fourth one start.
    for port in ("55001", "55002", "55003"):
        (acquire.lock_root / port).mkdir(parents=True)
    with pytest.raises(subprocess.TimeoutExpired) as waited:
        acquire([0, 0, 0, 0], interval="1", timeout=2.5)
    stdout = (waited.value.stdout or b"").decode()
    assert "waiting: 3 suites running (limit 3" in stdout, stdout
    assert "acquired" not in stdout, stdout


def age(directory: Path, minutes: int) -> None:
    old = time.time() - minutes * 60
    os.utime(directory, (old, old))


def test_a_slot_older_than_the_longest_run_stops_counting(acquire):
    # SIGKILL runs no trap, so a leaked slot is inevitable; three of them would otherwise put every
    # suite on this host into a wait nothing ever ends -- the failure this feature exists to avoid.
    for port in ("55001", "55002", "55003"):
        stale = acquire.lock_root / port
        stale.mkdir(parents=True)
        age(stale, 60 * 4)
    done = acquire([0])
    assert done.returncode == 0, done.stderr
    assert waiting_lines(done.stdout) == [], done.stdout
    assert "acquired" in done.stdout
    assert sorted(p.name for p in acquire.lock_root.iterdir()) == [PORT]


def test_dropping_a_stale_slot_is_said_out_loud(acquire):
    stale = acquire.lock_root / "55001"
    stale.mkdir(parents=True)
    age(stale, 60 * 4)
    done = acquire([0])
    assert "suite-lock: dropped a slot older than" in done.stdout, done.stdout


def test_a_slot_younger_than_the_limit_still_counts(acquire):
    for port in ("55001", "55002", "55003"):
        (acquire.lock_root / port).mkdir(parents=True)
    with pytest.raises(subprocess.TimeoutExpired):
        acquire([0, 0, 0, 0], interval="1", timeout=2.5)
    assert (acquire.lock_root / "55001").is_dir(), "a fresh slot was expired"


def test_the_wait_gives_up_and_says_where_to_look(acquire):
    # An indefinite hang is the one failure nobody can diagnose from outside the process.
    done = acquire([3, 3, 3, 3, 3, 3], interval="1", COSMAI_SUITE_WAIT_TIMEOUT_SECONDS="2", timeout=20.0)
    assert done.returncode == 1, done.stdout
    assert "gave up after" in done.stderr, done.stderr
    assert str(acquire.lock_root) in done.stderr, done.stderr
    assert "3 containers running" in done.stderr, done.stderr
    assert "acquired" not in done.stdout, done.stdout


# The cleanup handler of tool/checks/test is lifted out of the real script rather than restated, so
# a change there cannot pass here. /bin/sh is dash on this host: killed by a signal it has not
# trapped, it runs no EXIT trap at all, so a plain `kill`, a timeout wrapper or a supervisor would
# leak the slot and the container. dash also runs a trap only between commands, which is why the
# driver below waits in `wait` rather than inside a foreground command.
CLEANUP_BLOCK = ["sed", "-n", "/^cleanup() {/,/^trap cleanup EXIT$/p"]


def test_a_terminated_run_frees_its_slot_and_removes_its_container(tmp_path: Path):
    block = subprocess.run(
        [*CLEANUP_BLOCK, str(REPO_ROOT / "tool" / "checks" / "test")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "trap cleanup EXIT" in block, "the cleanup handler moved; this test extracts nothing"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker.calls"
    docker = bin_dir / "docker"
    docker.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\n', encoding="utf-8")
    docker.chmod(0o755)

    lock_dir = tmp_path / "locks" / PORT
    ready = tmp_path / "ready"
    driver = tmp_path / "driver.sh"
    driver.write_text(
        f"set -e\n"
        f". {REPO_ROOT / 'tool' / 'checks' / 'suite-lock'}\n"
        f". {REPO_ROOT / 'tool' / 'checks' / 'tested-tree'}\n"
        f"{block}\n"
        f"container_name=cosmai-test-postgres-{PORT}\n"
        f'suite_lock_dir="{lock_dir}"\n'
        f'mkdir -p "$suite_lock_dir"\n'
        f': > "{ready}"\n'
        # A backgrounded sleep with `wait`: dash runs a trap between commands, and `wait` is the
        # one it can be interrupted in.
        f"sleep 20 &\nwait\n",
        encoding="utf-8",
    )

    running = subprocess.Popen(
        ["sh", str(driver)],
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.05)
        assert ready.exists(), "the driver never started"
        running.terminate()
        running.wait(timeout=10)
    finally:
        if running.poll() is None:  # pragma: no cover - only on a failure path
            running.kill()

    assert not lock_dir.exists(), "SIGTERM left the slot held; the host loses a suite for ever"
    # Exactly once: the signal trap runs cleanup and then exits, which fires the EXIT trap over the
    # same handler. Without its `cleanup_ran` guard everything here would be done twice, and the
    # second `docker rm` would be aimed at whatever took the name in between (#214 review, minor).
    removals = [line for line in calls.read_text(encoding="utf-8").splitlines() if line.startswith("rm ")]
    assert removals == [f"rm -f -v cosmai-test-postgres-{PORT}"], removals


def test_a_non_numeric_wait_timeout_falls_back_instead_of_breaking_the_wait(acquire):
    # `[ 0 -ge abc ]` is an error, not a comparison: the give-up test would fail on every poll and
    # the wait this timeout exists to bound would never end (#214 review, minor).
    done = acquire([3, 3, 0], COSMAI_SUITE_WAIT_TIMEOUT_SECONDS="soon")
    assert done.returncode == 0, done.stderr
    assert "acquired" in done.stdout, done.stdout
    assert "Illegal number" not in done.stderr, done.stderr


def test_a_non_numeric_stale_limit_still_expires_a_leaked_slot(acquire):
    # `find -mmin +abc` fails, and the failure is swallowed: the expiry would stop happening in
    # silence and three leaked slots would deadlock every suite on the host.
    for port in ("55001", "55002", "55003"):
        stale = acquire.lock_root / port
        stale.mkdir(parents=True)
        age(stale, 60 * 4)
    done = acquire([0], COSMAI_SUITE_LOCK_STALE_MINUTES="180m")
    assert done.returncode == 0, done.stderr
    assert "acquired" in done.stdout, done.stdout
    assert sorted(p.name for p in acquire.lock_root.iterdir()) == [PORT]


def spawn_live_pid(tmp_path: Path) -> subprocess.Popen:
    """A process guaranteed to still be running, for a slot's recorded pid to point at."""
    return subprocess.Popen(["sleep", "30"])


def spawn_dead_pid(tmp_path: Path) -> int:
    """A pid guaranteed to be exited, for a slot's recorded (leaked) pid to point at."""
    proc = subprocess.Popen(["sh", "-c", "exit 0"])
    proc.wait(timeout=5)
    return proc.pid


# #240: the second of two overlapping pushes from one worktree adopted the first's still-running
# slot as a leak and pulled its container out from under it (2026-09-05). A slot's recorded pid is
# how the second run tells "still running" from "actually leaked" apart.
def test_a_live_owner_on_the_same_port_is_waited_for(acquire):
    holder = spawn_live_pid(acquire.lock_root)
    try:
        slot = acquire.lock_root / PORT
        slot.mkdir(parents=True)
        (slot / "pid").write_text(f"{holder.pid}\n", encoding="utf-8")
        with pytest.raises(subprocess.TimeoutExpired) as waited:
            acquire([0, 0, 0, 0], interval="1", timeout=2.5)
        stdout = (waited.value.stdout or b"").decode()
        assert "waiting: a suite is already running on this port from this worktree" in stdout, stdout
        assert "acquired" not in stdout, stdout
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_a_dead_owner_older_than_the_limit_is_reclaimed(acquire):
    dead_pid = spawn_dead_pid(acquire.lock_root)
    slot = acquire.lock_root / PORT
    slot.mkdir(parents=True)
    (slot / "pid").write_text(f"{dead_pid}\n", encoding="utf-8")
    age(slot, 60 * 4)
    done = acquire([0])
    assert done.returncode == 0, done.stderr
    assert waiting_lines(done.stdout) == [], done.stdout
    assert "acquired" in done.stdout, done.stdout
    assert (acquire.lock_root / PORT).is_dir()


# The container block of tool/checks/test is lifted out rather than restated, so a change there
# cannot pass here without also being exercised. The range covers the port's holder-refusal, the
# slot acquisition and the leaked-container removal in whichever order the file currently has them.
CONTAINER_BLOCK = ["sed", "-n", '/holder=\\$(docker ps --filter/,/docker rm -f -v "\\$name"/p']


def test_a_second_run_on_the_same_port_does_not_remove_the_first_s_container(tmp_path: Path):
    block = subprocess.run(
        [*CONTAINER_BLOCK, str(REPO_ROOT / "tool" / "checks" / "test")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "suite_lock_acquire" in block and "docker rm -f -v" in block, (
        "the container block moved; this test extracts nothing"
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker.calls"
    name = f"cosmai-test-postgres-{PORT}"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        'if [ "$1" = "ps" ]; then\n'
        f'    printf "%s\\n" "{name}"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    lock_root = tmp_path / "locks"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "COSMAI_SUITE_LOCK_DIR": str(lock_root),
        "COSMAI_SUITE_WAIT_SECONDS": "1",
        "COSMAI_SUITE_WAIT_TIMEOUT_SECONDS": "20",
    }

    def driver(tag: str, hold_seconds: int) -> tuple[Path, Path]:
        ready = tmp_path / f"ready-{tag}"
        script = tmp_path / f"driver-{tag}.sh"
        script.write_text(
            f"set -e\n"
            f". {REPO_ROOT / 'tool' / 'checks' / 'suite-lock'}\n"
            f"port={PORT}\n"
            f"name={name}\n"
            f"{block}\n"
            f': > "{ready}"\n'
            f"sleep {hold_seconds}\n"
            f'rm -rf "$suite_lock_dir"\n',
            encoding="utf-8",
        )
        return script, ready

    script1, ready1 = driver("1", hold_seconds=3)
    proc1 = subprocess.Popen(
        ["sh", str(script1)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        for _ in range(100):
            if ready1.exists():
                break
            time.sleep(0.05)
        assert ready1.exists(), "the first run never reached the container block"

        script2, ready2 = driver("2", hold_seconds=0)
        proc2 = subprocess.Popen(
            ["sh", str(script2)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            time.sleep(1.0)
            assert not ready2.exists(), "the second run proceeded while the first still holds the port"
            rm_calls = [
                line for line in calls.read_text(encoding="utf-8").splitlines() if line.startswith("rm ")
            ]
            assert len(rm_calls) <= 1, "the second run removed a container before the first released"

            out2, err2 = proc2.communicate(timeout=15)
            assert proc2.returncode == 0, err2
            assert "waiting: a suite is already running on this port from this worktree" in out2, out2
        finally:
            if proc2.poll() is None:  # pragma: no cover - only on a failure path
                proc2.kill()

        proc1.wait(timeout=15)
        assert proc1.returncode == 0
    finally:
        if proc1.poll() is None:  # pragma: no cover - only on a failure path
            proc1.kill()

    final_rm_calls = [
        line for line in calls.read_text(encoding="utf-8").splitlines() if line.startswith("rm ")
    ]
    assert final_rm_calls == [f"rm -f -v {name}", f"rm -f -v {name}"], final_rm_calls
