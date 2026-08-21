"""Verified-adapter test harness: CommandAdapter against stub executables.

No real agent binaries or network required.
"""
import os
import stat
import textwrap

import pytest

from tether.adapters.command import CommandAdapter


def _stub(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_stub_agent_receives_prompt_env_and_session(tmp_path):
    stub = _stub(tmp_path, "fake-agent", """\
        #!/usr/bin/env python3
        import os, sys
        print("PROMPT:" + sys.argv[1])
        print("SESSION:" + os.environ["TETHER_SESSION_ID"])
        print("CUSTOM:" + os.environ["FAKE_TOKEN"])
        """)
    adapter = CommandAdapter({
        "command": [stub, "{prompt}"],
        "env": {"FAKE_TOKEN": "abc123", "TETHER_SESSION_ID": "sess42"},
    })
    ok, reason = adapter.is_available()
    assert ok, reason
    session = adapter.start_session(str(tmp_path), "sess42")
    state = adapter.send("fix the bug", session)
    assert state.status == "completed", state.error
    assert "PROMPT:fix the bug" in state.logs
    assert "SESSION:sess42" in state.logs
    assert "CUSTOM:abc123" in state.logs


def test_stub_agent_timeout(tmp_path):
    stub = _stub(tmp_path, "slow-agent", """\
        #!/usr/bin/env python3
        import time
        time.sleep(30)
        """)
    adapter = CommandAdapter({"command": [stub], "timeout_seconds": 1})
    session = adapter.start_session(str(tmp_path), "s")
    state = adapter.send("p", session)
    assert state.status == "failed"
    assert "timed out" in (state.error or "")


def test_stub_agent_failure_exit_code(tmp_path):
    stub = _stub(tmp_path, "failing-agent", """\
        #!/usr/bin/env python3
        import sys
        print("partial output")
        sys.exit(7)
        """)
    adapter = CommandAdapter({"command": [stub]})
    session = adapter.start_session(str(tmp_path), "s")
    state = adapter.send("p", session)
    assert state.status == "failed"
    assert state.result == {"exit_code": 7}
    assert "partial output" in state.logs


def test_stub_agent_runs_in_project_dir(tmp_path):
    marker = tmp_path / "marker.txt"
    stub = _stub(tmp_path, "cwd-agent", """\
        #!/usr/bin/env python3
        open("marker.txt", "w").write("cwd ok")
        """)
    adapter = CommandAdapter({"command": [stub]})
    session = adapter.start_session(str(tmp_path), "s")
    state = adapter.send("p", session)
    assert state.status == "completed"
    assert marker.exists()
