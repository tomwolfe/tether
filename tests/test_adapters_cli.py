import json
import subprocess

import pytest
from typer.testing import CliRunner

from tether.adapters import resolve_adapter
from tether.adapters.command import CommandAdapter
from tether.adapters.experimental import OpencodeAdapter, PiAdapter
from tether.cli import app

runner = CliRunner()


def test_cli_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("init", "validate-config", "validate-mission", "adapters",
                "run", "rollback", "report"):
        assert cmd in result.output


def test_adapters_list():
    result = runner.invoke(app, ["adapters", "list"])
    assert result.exit_code == 0
    assert "mock" in result.output and "verified" in result.output
    assert "opencode" in result.output and "experimental" in result.output


def test_mock_adapter_scenarios(tmp_path):
    s = resolve_adapter("mock", {"mock": {"scenario": "success"}})
    session = s.start_session(str(tmp_path), "sid")
    assert s.send("p", session).status == "completed"
    s = resolve_adapter("mock", {"mock": {"scenario": "fail_then_succeed"}})
    session = s.start_session(str(tmp_path), "sid")
    assert s.send("plan", session).status == "completed"  # plan step always succeeds
    assert s.send("execute", session).status == "failed"
    assert s.send("repair", session).status == "completed"
    with pytest.raises(ValueError):
        resolve_adapter("mock", {"mock": {"scenario": "bogus"}})


def test_command_adapter_runs_real_command(tmp_path):
    adapter = CommandAdapter({"command": ["python3", "-c", "print('hi {session_id}')"]})
    ok, _ = adapter.is_available()
    assert ok
    session = adapter.start_session(str(tmp_path), "abc123")
    state = adapter.send("ignored", session)
    assert state.status == "completed"
    assert "abc123" in state.logs
    adapter = CommandAdapter({"command": ["python3", "-c", "raise SystemExit(3)"]})
    assert adapter.send("x", session).status == "failed"
    bad = CommandAdapter({"command": ["no-such-binary-xyz"]})
    assert bad.is_available()[0] is False
    assert bad.send("x", session).status == "unavailable"


def test_experimental_adapters_are_unverified_presets():
    oc = OpencodeAdapter()
    assert oc.verified is False
    assert oc.command == ["opencode", "run", "{prompt}"]
    ok, _ = oc.is_available()
    assert isinstance(ok, bool)
    pi = PiAdapter()
    assert pi.verified is False
    assert pi.command == ["pi", "--print", "{prompt}"]
    # config override works
    custom = OpencodeAdapter({"command": ["opencode", "--custom"]})
    assert custom.command == ["opencode", "--custom"]


def test_cli_init_and_validate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0 and (tmp_path / "tether.yaml").exists()
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 1  # refuses overwrite
    r = runner.invoke(app, ["validate-config"])
    assert r.exit_code == 0
    (tmp_path / "m.yaml").write_text(
        "mission:\n  name: x\n  goal: y\nverification:\n  commands: ['true']\n"
    )
    r = runner.invoke(app, ["validate-mission", "m.yaml"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["validate-mission", "missing.yaml"])
    assert r.exit_code == 1


def test_cli_run_mock_success_and_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mission = tmp_path / "m.yaml"
    mission.write_text(
        "mission:\n  name: cli-run\n  goal: g\n"
        "verification:\n  commands: ['true']\nadapter: mock\n"
        "adapters:\n  mock:\n    scenario: success\n"
    )
    r = runner.invoke(app, ["run", str(mission), "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "Status: success" in r.output
    session_id = r.output.split("Session: ")[1].split()[0]
    r = runner.invoke(app, ["report", session_id, "--project-dir", str(tmp_path)])
    assert r.exit_code == 0
    report = json.loads(r.output)
    assert report["mission_name"] == "cli-run"
    assert report["checkpoint_info"]["is_git_repo"] is False


def test_cli_run_mock_recovery(tmp_path):
    mission = tmp_path / "r.yaml"
    mission.write_text(
        "mission:\n  name: cli-recovery\n  goal: g\n"
        "verification:\n  commands: ['true']\nadapter: mock\n"
        "adapters:\n  mock:\n    scenario: fail_then_succeed\n"
    )
    r = runner.invoke(app, ["run", str(mission), "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    sid = r.output.split("Session: ")[1].split()[0]
    r = runner.invoke(app, ["report", sid, "--project-dir", str(tmp_path)])
    report = json.loads(r.output)
    assert report["status"] == "success"
    assert len(report["recovery_attempts"]) == 1


def test_cli_run_git_checkpoint_and_rollback(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("v1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)
    mission = tmp_path / "m.yaml"
    mission.write_text(
        "mission:\n  name: gitrun\n  goal: g\n"
        "verification:\n  commands: ['true']\nadapter: mock\n"
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "mission"], check=True)
    r = runner.invoke(app, ["run", str(mission), "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    sid = r.output.split("Session: ")[1].split()[0]
    ref = f"refs/tether/checkpoint/{sid}"
    p = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "--verify", ref],
                       capture_output=True, text=True)
    assert p.returncode == 0
    # dirty tree blocks rollback
    (tmp_path / "a.txt").write_text("v2\n")
    r = runner.invoke(app, ["rollback", sid, "--project-dir", str(tmp_path)])
    assert r.exit_code == 1 and "dirty" in r.output
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "--", "."], check=True)
    r = runner.invoke(app, ["rollback", sid, "--project-dir", str(tmp_path)])
    assert r.exit_code == 0
    assert (tmp_path / "a.txt").read_text() == "v1\n"


def test_cli_version():
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0 and "tether" in r.output
