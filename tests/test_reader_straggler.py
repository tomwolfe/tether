"""Streaming-straggler acceptance tests (dogfood-37).

Proves the dogfood-33/34 trade-off is bounded and acceptable: a descendant
that survives its parent while inheriting the adapter's stdout/stderr
pipe write-ends cannot hang send(), cannot grow threads/fds across sends,
and does not weaken the normal completion path.

Everything runs through real subprocesses: stub agents are plain .py files
executed via sys.executable, and every Popen uses the default shell=False.
The only sleeps are the stragglers' own lifetimes; every other wait polls
with generous deadlines. Stragglers sleep long and are SIGKILLed (or
taskkilled) by the test, so cleanup never depends on timing.
"""
import os
import shlex
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from tether.adapters.command import READER_JOIN_GRACE_SECONDS, CommandAdapter

# Acceptance bound from the mission: send() latency with a straggler must
# stay under 5 seconds. The mechanism guarantees ~READER_JOIN_GRACE_SECONDS
# plus process startup, so this is generous by design.
ACCEPTANCE_LATENCY_SECONDS = 5.0


def _stub(tmp_path, name, body):
    path = tmp_path / (name + ".py")
    path.write_text(textwrap.dedent(body))
    return str(path)


def _wait_until(condition, timeout=20.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


def _adapter(stub, *args):
    return CommandAdapter({
        "command": [sys.executable, stub, *args],
        "timeout_seconds": 60,
    })


def _open_fd_count() -> int:
    """Best-effort open-fd count for this process (-1 when unknowable)."""
    for fd_dir in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(fd_dir))
        except OSError:
            continue
    return -1


def _kill_pid(pid: int) -> None:
    """Hard-kill a stray test process on any platform."""
    sigkill = getattr(signal, "SIGKILL", None)
    if sigkill is not None:
        try:
            os.kill(pid, sigkill)
        except OSError:
            pass
        return
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/F"],
        capture_output=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _read_pids(pid_file) -> list[int]:
    text = pid_file.read_text(encoding="utf-8").strip()
    return [int(part) for part in text.split()]


def _reap_and_settle(pids: list[int], baseline_threads: int) -> None:
    """Kill the stragglers, then require the daemon readers to have exited.

    Once every write-end dies the blocked read() returns EOF, each reader
    closes its stream and finishes, so the thread count must drop back to
    the pre-send level (+1 slack for ambient noise).
    """
    for pid in pids:
        _kill_pid(pid)
    assert _wait_until(
        lambda: threading.active_count() <= baseline_threads + 1,
        timeout=15.0), (
        f"reader threads linger after straggler exit: "
        f"{threading.active_count()} > {baseline_threads + 1}")


# Spawns one descendant that inherits BOTH pipe write-ends, emits a chunk
# of its own (typically just after the direct child exits, always well
# within the 2.0s join grace), then holds the pipes open indefinitely.
_LATE_CHUNK_STUB = """\
    import subprocess, sys
    kid = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time\\n"
         "sys.stdout.write('STRAGGLER-LATE-CHUNK\\\\n')\\n"
         "sys.stdout.flush()\\n"
         "time.sleep(120)\\n"])
    with open(sys.argv[1], "w") as f:
        f.write(str(kid.pid))
    print("agent-done", flush=True)
    """


def test_single_straggler_returns_promptly_and_late_chunk_survives(tmp_path):
    # (a) One surviving descendant holding stdout/stderr: send() must
    # return promptly, still deliver output that arrives inside the grace
    # window, and never hang.
    pid_file = tmp_path / "single.pid"
    stub = _stub(tmp_path, "late-chunk-agent", _LATE_CHUNK_STUB)
    adapter = _adapter(stub, str(pid_file))
    chunks: list[str] = []
    adapter.stream_callback = chunks.append
    session = adapter.start_session(str(tmp_path), "s")
    before_threads = threading.active_count()
    started = time.monotonic()
    state = adapter.send("p", session)
    elapsed = time.monotonic() - started
    pids = _read_pids(pid_file) if pid_file.exists() else []
    try:
        assert state.status == "completed", state.error
        assert len(pids) == 1, "stub failed to leave exactly one straggler"
        assert "agent-done" in state.logs
        # Output written to the held-open pipe inside the grace window is
        # still captured -- truncation only happens after the grace.
        assert "STRAGGLER-LATE-CHUNK" in state.logs
        assert "STRAGGLER-LATE-CHUNK" in "".join(chunks)
        assert elapsed < ACCEPTANCE_LATENCY_SECONDS, (
            f"send() took {elapsed:.2f}s with one straggler holding pipes")
    finally:
        _reap_and_settle(pids, before_threads)


# Three descendants, each holding its own inherited copy of both pipe
# write-ends and each emitting one marker line.
_MULTI_STUB = """\
    import subprocess, sys
    kids = []
    for i in range(3):
        kids.append(subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time\\n"
             "sys.stdout.write('MULTI-' + sys.argv[1] + '\\\\n')\\n"
             "sys.stdout.flush()\\n"
             "time.sleep(120)\\n",
             str(i)]))
    with open(sys.argv[1], "w") as f:
        f.write(" ".join(str(k.pid) for k in kids))
    print("agent-done", flush=True)
    """


def test_multiple_stragglers_bounded_join_no_deadlock(tmp_path):
    # (b) >=3 surviving descendants each hold duplicated pipe fds: the join
    # stays bounded, every marker emitted within the grace window lands,
    # and nothing deadlocks.
    pid_file = tmp_path / "multi.pid"
    stub = _stub(tmp_path, "multi-straggler-agent", _MULTI_STUB)
    adapter = _adapter(stub, str(pid_file))
    session = adapter.start_session(str(tmp_path), "s")
    before_threads = threading.active_count()
    started = time.monotonic()
    state = adapter.send("p", session)
    elapsed = time.monotonic() - started
    pids = _read_pids(pid_file) if pid_file.exists() else []
    try:
        assert len(pids) >= 3, "stub failed to leave >=3 stragglers"
        assert state.status == "completed", state.error
        for marker in ("MULTI-0", "MULTI-1", "MULTI-2"):
            assert marker in state.logs, marker
        assert elapsed < ACCEPTANCE_LATENCY_SECONDS, (
            f"send() took {elapsed:.2f}s with three stragglers")
    finally:
        _reap_and_settle(pids, before_threads)


# Leaves ONE silent straggler holding both pipes; used repeatedly to show
# fd/thread counts stay flat across sequential agent runs.
_LEAK_STUB = """\
    import subprocess, sys
    kid = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"])
    with open(sys.argv[1], "w") as f:
        f.write(str(kid.pid))
    print("agent-done", flush=True)
    """


def test_repeated_straggler_sends_do_not_leak_threads_or_fds(tmp_path):
    # (c) Several sequential agent runs in one process, each leaving a
    # straggler behind: mid-run growth is capped at two daemon readers per
    # pending send, and once the stragglers' pipes close both the thread
    # count and the open-fd count return to their baselines.
    if os.name == "nt":
        pytest.skip("open-fd accounting is POSIX-only")
    pid_file = tmp_path / "leak.pid"
    stub = _stub(tmp_path, "leaky-agent", _LEAK_STUB)
    adapter = _adapter(stub, str(pid_file))
    baseline_threads = threading.active_count()
    baseline_fds = _open_fd_count()
    all_pids: list[int] = []
    try:
        loop_start_threads = baseline_threads
        for i in range(4):
            session = adapter.start_session(str(tmp_path), f"leak-{i}")
            state = adapter.send("p", session)
            assert state.status == "completed", state.error
            all_pids.extend(_read_pids(pid_file))
            ceiling = loop_start_threads + 2 * (i + 1) + 2
            assert threading.active_count() <= ceiling, (
                f"thread count grew beyond 2 readers per pending send: "
                f"{threading.active_count()} > {ceiling}")
        _reap_and_settle(all_pids, loop_start_threads)
        if baseline_fds >= 0:
            fds_now = _open_fd_count()
            assert fds_now <= baseline_fds + 3, (
                f"fds leaked: {fds_now} now vs {baseline_fds} baseline")
    finally:
        for pid in all_pids:
            _kill_pid(pid)


_CLEAN_STUB = """\
    import sys
    sys.stdout.write("CLEAN-OUT\\n")
    sys.stdout.flush()
    sys.stderr.write("CLEAN-ERR\\n")
    sys.stderr.flush()
    """


def test_clean_path_joins_within_grace_and_keeps_full_output(tmp_path):
    # (d) Regression guard for the normal case: a child that exits cleanly
    # has its readers joined inside the grace window (EOF comes instantly)
    # and ALL of its output reaches the logs.
    stub = _stub(tmp_path, "clean-agent", _CLEAN_STUB)
    adapter = _adapter(stub)
    session = adapter.start_session(str(tmp_path), "s")
    started = time.monotonic()
    state = adapter.send("p", session)
    elapsed = time.monotonic() - started
    assert state.status == "completed", state.error
    assert "CLEAN-OUT" in state.logs and "CLEAN-ERR" in state.logs
    assert elapsed < READER_JOIN_GRACE_SECONDS + 3.0, (
        f"clean-path send took {elapsed:.2f}s")


# ------------------- acceptance criteria as tests (dogfood-37 task 3)


def test_acceptance_send_latency_under_5_seconds_with_straggler(tmp_path):
    # Acceptance criterion 1: with a surviving straggler holding both
    # pipes, send() latency stays under 5 seconds.
    pid_file = tmp_path / "accept.pid"
    stub = _stub(tmp_path, "latency-agent", _LEAK_STUB)
    adapter = _adapter(stub, str(pid_file))
    session = adapter.start_session(str(tmp_path), "s")
    before_threads = threading.active_count()
    started = time.monotonic()
    state = adapter.send("p", session)
    elapsed = time.monotonic() - started
    pids = _read_pids(pid_file) if pid_file.exists() else []
    try:
        assert state.status == "completed", state.error
        assert elapsed < ACCEPTANCE_LATENCY_SECONDS, (
            f"ACCEPTANCE FAILED: send() latency {elapsed:.2f}s >= 5s")
    finally:
        _reap_and_settle(pids, before_threads)


def test_acceptance_normal_path_output_is_byte_exact(tmp_path):
    # Acceptance criterion 2: without stragglers the captured output is
    # byte-exact -- audit log AND streamed callback chunks alike.
    out_payload = "OUT-alpha\nOUT-beta\n"
    err_payload = "ERR-gamma\n"
    body = f"""\
        import sys
        sys.stdout.write({out_payload!r})
        sys.stdout.flush()
        sys.stderr.write({err_payload!r})
        sys.stderr.flush()
        """
    stub = _stub(tmp_path, "byte-exact-agent", body)
    adapter = _adapter(stub)
    chunks: list[str] = []
    adapter.stream_callback = chunks.append
    session = adapter.start_session(str(tmp_path), "s")
    state = adapter.send("p", session)
    assert state.status == "completed", state.error
    argv = [sys.executable, stub]
    expected_logs = ("$ " + " ".join(shlex.quote(a) for a in argv) + "\n"
                     + out_payload + err_payload)
    assert state.logs == expected_logs, "audit log not byte-exact"
    streamed = "".join(chunks)
    # The two streams interleave nondeterministically; content must be
    # complete either way.
    assert streamed in (out_payload + err_payload, err_payload + out_payload), \
        "streamed output not byte-exact"


def test_acceptance_thread_count_returns_to_baseline_after_close(tmp_path):
    # Acceptance criterion 3: after several straggler-leaving sends, once
    # the stragglers die their daemon readers exit and the process thread
    # count returns to baseline -- no unbounded growth.
    pid_file = tmp_path / "accept-threads.pid"
    stub = _stub(tmp_path, "thread-baseline-agent", _LEAK_STUB)
    adapter = _adapter(stub, str(pid_file))
    baseline = threading.active_count()
    all_pids: list[int] = []
    try:
        for i in range(3):
            session = adapter.start_session(str(tmp_path), f"acc-{i}")
            state = adapter.send("p", session)
            assert state.status == "completed", state.error
            all_pids.extend(_read_pids(pid_file))
        _reap_and_settle(all_pids, baseline)
    finally:
        for pid in all_pids:
            _kill_pid(pid)
