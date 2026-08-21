import json
import logging
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from tether.adapters import check_adapter_settings, resolve_adapter
from tether.adapters.command import CommandAdapter
from tether.adapters.experimental import OpencodeAdapter, PiAdapter
from tether.adapters.mock import MockAdapter
from tether.cli import app

runner = CliRunner()


def py_cmd(code: str) -> str:
    return f"{sys.executable} -c '{code}'"


PASS_CMD = py_cmd("import sys; sys.exit(0)")


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
    adapter = CommandAdapter({"command": [sys.executable, "-c", "print('hi {session_id}')"]})
    ok, _ = adapter.is_available()
    assert ok
    session = adapter.start_session(str(tmp_path), "abc123")
    state = adapter.send("ignored", session)
    assert state.status == "completed"
    assert "abc123" in state.logs
    adapter = CommandAdapter({"command": [sys.executable, "-c", "raise SystemExit(3)"]})
    assert adapter.send("x", session).status == "failed"
    bad = CommandAdapter({"command": ["no-such-binary-xyz"]})
    assert bad.is_available()[0] is False
    assert bad.send("x", session).status == "unavailable"


def test_cli_adapters_smoke_mock(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["adapters", "smoke", "mock"])
    assert r.exit_code == 0, r.output
    for field in ("Adapter:", "Availability:", "Prompt:", "Status:",
                  "Exit code:", "Elapsed:", "Output excerpt:"):
        assert field in r.output
    assert "available" in r.output and "completed" in r.output
    assert "[mock:" in r.output  # adapter output excerpt
    assert "Smoke PASSED" in r.output
    # throwaway directory only: the caller's tree is untouched, no audit writes
    assert list(tmp_path.iterdir()) == []


def test_cli_adapters_smoke_command_adapter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tether.yaml").write_text(
        "adapters:\n  command:\n    command:\n"
        f"      - {sys.executable}\n"
        "      - '-c'\n"
        "      - \"import sys; print('P=' + sys.argv[1])\"\n"
        "      - '{prompt}'\n"
    )
    r = runner.invoke(app, ["adapters", "smoke", "command",
                            "--prompt", "custom-prompt-123"])
    assert r.exit_code == 0, r.output
    assert "Availability: available" in r.output
    assert "Status:" in r.output and "completed" in r.output
    assert "Exit code:" in r.output and "0" in r.output
    assert "Elapsed:" in r.output
    assert "P=custom-prompt-123" in r.output
    assert "Smoke PASSED" in r.output


def test_cli_adapters_smoke_command_failure_and_unavailable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tether.yaml").write_text(
        "adapters:\n  command:\n    command:\n"
        f"      - {sys.executable}\n"
        "      - '-c'\n"
        "      - 'raise SystemExit(3)'\n"
    )
    r = runner.invoke(app, ["adapters", "smoke", "command"])
    assert r.exit_code == 1
    assert "Status:" in r.output and "failed" in r.output
    assert "Exit code:" in r.output and "3" in r.output
    assert "Smoke FAILED" in r.output

    (tmp_path / "tether.yaml").write_text(
        "adapters:\n  command:\n    command: ['no-such-binary-xyz']\n"
    )
    r = runner.invoke(app, ["adapters", "smoke", "command"])
    assert r.exit_code == 1
    assert "unavailable" in r.output


def test_cli_adapters_smoke_unknown_adapter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["adapters", "smoke", "no-such-adapter"])
    assert r.exit_code == 1
    assert "Smoke FAILED" in r.output and "no-such-adapter" in r.output


def test_known_settings_attributes():
    assert CommandAdapter.known_settings == frozenset(
        {"command", "timeout_seconds", "prompt_via_stdin", "env"}
    )
    assert MockAdapter.known_settings == frozenset({"scenario"})
    # presets inherit the generic command adapter's settings
    assert OpencodeAdapter.known_settings == CommandAdapter.known_settings
    assert PiAdapter.known_settings == CommandAdapter.known_settings


def test_unknown_adapter_setting_warns_but_still_constructs(caplog):
    with caplog.at_level(logging.WARNING, logger="tether.adapters"):
        adapter = resolve_adapter("mock", {"mock": {"scnario": "success"}})
    assert isinstance(adapter, MockAdapter)  # typo warns, does not abort
    assert any(
        "adapter 'mock': unknown setting 'scnario'" in r.message
        for r in caplog.records
    )


def test_known_adapter_settings_emit_no_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="tether.adapters"):
        resolve_adapter("mock", {"mock": {"scenario": "success"}})
        check_adapter_settings({"mock": {"scenario": "fail_then_succeed"}})
    assert not any("unknown setting" in r.message for r in caplog.records)


def test_check_adapter_settings_strict_raises():
    problems = check_adapter_settings({"mock": {"scnario": "success"}})
    assert problems == ["adapter 'mock': unknown setting 'scnario'"]
    with pytest.raises(ValueError, match="unknown setting 'scnario'"):
        check_adapter_settings({"mock": {"scnario": "success"}}, strict=True)


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
        f"mission:\n  name: x\n  goal: y\nverification:\n  commands:\n    - {PASS_CMD}\n"
    )
    r = runner.invoke(app, ["validate-mission", "m.yaml"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["validate-mission", "missing.yaml"])
    assert r.exit_code == 1


def test_cli_validate_config_unknown_setting_strict(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tether.yaml").write_text(
        "default_adapter: mock\n"
        "adapters:\n  mock:\n    scenario: success\n    promt_via_stdin: true\n"
    )
    r = runner.invoke(app, ["validate-config"])
    assert r.exit_code == 0  # warning only
    r = runner.invoke(app, ["validate-config", "--strict"])
    assert r.exit_code == 1
    assert "unknown setting 'promt_via_stdin'" in r.output
    # known settings stay valid under --strict
    (tmp_path / "tether.yaml").write_text(
        "default_adapter: mock\nadapters:\n  mock:\n    scenario: success\n"
    )
    r = runner.invoke(app, ["validate-config", "--strict"])
    assert r.exit_code == 0


def test_cli_run_strict_rejects_unknown_adapter_setting(tmp_path):
    mission = tmp_path / "m.yaml"
    mission.write_text(
        "mission:\n  name: strict-run\n  goal: g\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\nadapter: mock\n"
        "adapters:\n  mock:\n    scnario: success\n"
    )
    r = runner.invoke(app, ["run", str(mission), "--project-dir", str(tmp_path)])
    assert r.exit_code == 0  # typo warns but the run proceeds
    r = runner.invoke(app, ["run", str(mission), "--project-dir", str(tmp_path),
                            "--strict"])
    assert r.exit_code == 1
    assert "unknown setting 'scnario'" in r.output


def test_cli_run_mock_success_and_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mission = tmp_path / "m.yaml"
    mission.write_text(
        "mission:\n  name: cli-run\n  goal: g\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\nadapter: mock\n"
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
        f"verification:\n  commands:\n    - {PASS_CMD}\nadapter: mock\n"
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
        f"verification:\n  commands:\n    - {PASS_CMD}\nadapter: mock\n"
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
