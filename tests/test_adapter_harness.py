"""Verified-adapter test harness: CommandAdapter against stub executables.

No real agent binaries or network required. Stubs are plain .py files run via
sys.executable so the suite is portable (including Windows).
"""
import sys
import textwrap


from tether.adapters.command import CommandAdapter


def _stub(tmp_path, name, body):
    path = tmp_path / (name + ".py")
    path.write_text(textwrap.dedent(body))
    return str(path)


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


def test_tether_mission_env_injected_when_known(tmp_path):
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
