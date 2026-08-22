"""Verified-adapter test harness: CommandAdapter against stub executables.

No real agent binaries or network required. Stubs are plain .py files run via
sys.executable so the suite is portable (including Windows).
"""
import sys
import textwrap
import threading
import time


from tether.adapters.command import CommandAdapter


def _stub(tmp_path, name, body):
    path = tmp_path / (name + ".py")
    path.write_text(textwrap.dedent(body))
    return str(path)


def _wait_until(condition, timeout=15.0, interval=0.05):
    """Poll ``condition`` until true; return True when satisfied."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


def _wait_until_quiet(path, stable_seconds=1.5, deadline=20.0):
    """Wait until the heartbeat file stops growing (writer process died).

    A live heartbeating child appends every 0.1s, so quietness for
    ``stable_seconds`` is proof the tree member is gone.
    """
    start = time.monotonic()
    last_size, last_change = -1, time.monotonic()
    while time.monotonic() - start < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        now = time.monotonic()
        if size != last_size:
            last_size, last_change = size, now
        elif now - last_change >= stable_seconds:
            return True
        time.sleep(0.05)
    return False


# Spawns a heartbeating grandchild and records its pid, then sleeps. Used to
# prove that timeout/cancel terminate the whole process tree, not just the
# immediate child (the direct agent process).
_TREE_STUB = """\
    import subprocess, sys, time
    hb_path, pid_path = sys.argv[1], sys.argv[2]
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time\\n"
         "while True:\\n"
         "    with open(sys.argv[1], 'a') as f:\\n"
         "        f.write('tick\\\\n')\\n"
         "    time.sleep(0.1)\\n",
         hb_path],
    )
    with open(pid_path, "w") as f:
        f.write(str(child.pid))
    time.sleep(60)
    """


def test_stub_agent_receives_prompt_env_and_session(tmp_path):
    stub = _stub(tmp_path, "fake-agent", """\
        import os, sys
        print("PROMPT:" + sys.argv[1])
        print("SESSION:" + os.environ["TETHER_SESSION_ID"])
        print("PROJECT:" + os.environ["TETHER_PROJECT_DIR"])
        print("CUSTOM:" + os.environ["FAKE_TOKEN"])
        """)
    adapter = CommandAdapter({
        "command": [sys.executable, stub, "{prompt}"],
        "env": {"FAKE_TOKEN": "abc123"},
    })
    ok, reason = adapter.is_available()
    assert ok, reason
    session = adapter.start_session(str(tmp_path), "sess42")
    state = adapter.send("fix the bug", session)
    assert state.status == "completed", state.error
    assert "PROMPT:fix the bug" in state.logs
    # standard Tether env vars are injected without manual configuration
    assert "SESSION:sess42" in state.logs
    assert f"PROJECT:{tmp_path}" in state.logs
    assert "CUSTOM:abc123" in state.logs


def test_user_env_overrides_tether_vars(tmp_path):
    stub = _stub(tmp_path, "override-agent", """\
        import os
        print("SESSION:" + os.environ["TETHER_SESSION_ID"])
        """)
    adapter = CommandAdapter({
        "command": [sys.executable, stub],
        "env": {"TETHER_SESSION_ID": "user-wins"},
    })
    session = adapter.start_session(str(tmp_path), "sess43")
    state = adapter.send("p", session)
    assert state.status == "completed"
    assert "SESSION:user-wins" in state.logs


def test_tether_mission_env_injected_when_known(tmp_path, monkeypatch):
    # Hermetic under dogfooding: an outer Tether run may export TETHER_MISSION
    # into this process; the stub must see it unset until metadata provides one.
    monkeypatch.delenv("TETHER_MISSION", raising=False)
    stub = _stub(tmp_path, "mission-agent", """\
        import os
        print("MISSION:" + os.environ.get("TETHER_MISSION", "<unset>"))
        """)
    adapter = CommandAdapter({"command": [sys.executable, stub]})
    session = adapter.start_session(str(tmp_path), "s")
    state = adapter.send("p", session)
    assert "MISSION:<unset>" in state.logs
    session.metadata["mission_name"] = "my-mission"
    state = adapter.send("p", session)
    assert "MISSION:my-mission" in state.logs


def test_stub_agent_timeout(tmp_path):
    stub = _stub(tmp_path, "slow-agent", """\
        import time
        time.sleep(30)
        """)
    adapter = CommandAdapter({"command": [sys.executable, stub], "timeout_seconds": 1})
    session = adapter.start_session(str(tmp_path), "s")
    state = adapter.send("p", session)
    assert state.status == "failed"
    assert "timed out" in (state.error or "")


def test_stub_agent_failure_exit_code(tmp_path):
    stub = _stub(tmp_path, "failing-agent", """\
        import sys
        print("partial output")
        sys.exit(7)
        """)
    adapter = CommandAdapter({"command": [sys.executable, stub]})
    session = adapter.start_session(str(tmp_path), "s")
    state = adapter.send("p", session)
    assert state.status == "failed"
    assert state.result == {"exit_code": 7}
    assert "partial output" in state.logs


def test_stub_agent_runs_in_project_dir(tmp_path):
    marker = tmp_path / "marker.txt"
    stub = _stub(tmp_path, "cwd-agent", """\
        open("marker.txt", "w").write("cwd ok")
        """)
    adapter = CommandAdapter({"command": [sys.executable, stub]})
    session = adapter.start_session(str(tmp_path), "s")
    state = adapter.send("p", session)
    assert state.status == "completed"
    assert marker.exists()


# --------------------------------------- process tree containment (dogfood-07)


def test_timeout_kills_whole_process_tree(tmp_path):
    heartbeat = tmp_path / "heartbeat.log"
    pid_file = tmp_path / "grandchild.pid"
    stub = _stub(tmp_path, "tree-agent", _TREE_STUB)
    adapter = CommandAdapter({
        "command": [sys.executable, stub, str(heartbeat), str(pid_file)],
        "timeout_seconds": 1,
    })
    session = adapter.start_session(str(tmp_path), "s")
    state = adapter.send("p", session)
    # timeout semantics preserved: status failed with a timeout error
    assert state.status == "failed"
    assert "timed out" in (state.error or "")
    # the grandchild was heartbeating before the kill
    assert _wait_until(lambda: heartbeat.exists() and heartbeat.stat().st_size > 0)
    assert pid_file.exists()  # grandchild was actually spawned
    # ...and the whole tree is dead afterwards: the heartbeat goes quiet.
    assert _wait_until_quiet(heartbeat), (
        "grandchild still alive after command timeout")


def test_cancel_terminates_active_command_tree(tmp_path):
    heartbeat = tmp_path / "cancel-heartbeat.log"
    pid_file = tmp_path / "cancel-grandchild.pid"
    stub = _stub(tmp_path, "long-agent", _TREE_STUB)
    adapter = CommandAdapter({
        "command": [sys.executable, stub, str(heartbeat), str(pid_file)],
        "timeout_seconds": 60,
    })
    session = adapter.start_session(str(tmp_path), "sess-cancel")
    result: dict = {}

    def run():
        result["state"] = adapter.send("p", session)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        # Wait until the grandchild is demonstrably running; by then the
        # parent process is registered in the adapter's active set.
        assert _wait_until(
            lambda: heartbeat.exists() and heartbeat.stat().st_size > 0), \
            "grandchild never started"
        assert pid_file.exists()  # grandchild was actually spawned
        adapter.cancel(session)
        worker.join(timeout=30)
        assert not worker.is_alive(), "send() did not return after cancel()"
        state = result.get("state")
        assert state is not None and state.status == "failed"
    finally:
        if worker.is_alive():
            adapter.cancel(session)
            worker.join(timeout=30)
    # whole tree dead: the grandchild heartbeat stops growing
    assert _wait_until_quiet(heartbeat), (
        "grandchild still alive after cancel()")


def test_cancel_without_active_command_is_noop(tmp_path):
    adapter = CommandAdapter({"command": [sys.executable, "-c", "pass"]})
    session = adapter.start_session(str(tmp_path), "s")
    adapter.cancel(session)  # nothing running: must not raise
    assert adapter.send("p", session).status == "completed"
