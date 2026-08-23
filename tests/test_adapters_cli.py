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
from tether.cli import EXIT_CANCELLED, EXIT_FAILED, EXIT_SANDBOX_VIOLATION, \
    EXIT_SUCCESS, app

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


def test_command_adapter_usage_on_completed_and_failed_sends(tmp_path):
    ok = CommandAdapter({"command": [sys.executable, "-c", "print('hi')"]})
    session = ok.start_session(str(tmp_path), "abc123")
    state = ok.send("ignored", session)
    assert state.status == "completed"
    usage = state.usage
    assert usage is not None
    assert isinstance(usage["elapsed_seconds"], float)
    assert usage["elapsed_seconds"] >= 0
    assert usage["exit_code"] == 0

    fail = CommandAdapter({"command": [sys.executable, "-c", "raise SystemExit(3)"]})
    failed = fail.send("x", session)
    assert failed.status == "failed"
    failed_usage = failed.usage
    assert failed_usage is not None
    assert isinstance(failed_usage["elapsed_seconds"], float)
    assert failed_usage["elapsed_seconds"] >= 0
    assert failed_usage["exit_code"] == 3


def test_run_report_and_sessions_show_carry_usage(tmp_path):
    from tether.audit import find_session_dir

    config = {
        "default_adapter": "command",
        "adapters": {"command": {"command": [sys.executable, "-c", "print('stub ran')"]}},
    }
    (tmp_path / "tether.json").write_text(json.dumps(config))
    mission = tmp_path / "m.yaml"
    mission.write_text("mission:\n  name: usage-run\n  goal: g\nadapter: command\n")
    r = runner.invoke(app, ["run", str(mission), "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    sid = r.output.split("Session: ")[1].split()[0]

    session_dir = find_session_dir(tmp_path, ".tether/sessions", sid)
    assert session_dir is not None
    report = json.loads((session_dir / "report.json").read_text(encoding="utf-8"))
    usage = report.get("usage")
    assert usage is not None
    assert isinstance(usage["elapsed_seconds"], float)
    assert usage["elapsed_seconds"] >= 0
    assert usage["exit_code"] == 0

    r = runner.invoke(app, ["sessions", "show", sid, "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "Usage:" in r.output
    assert "elapsed_seconds" in r.output and "exit_code" in r.output


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
        {"command", "timeout_seconds", "prompt_via_stdin", "env",
         "usage_patterns"}
    )
    assert MockAdapter.known_settings == frozenset({"scenario"})
    # presets inherit the generic command adapter's settings
    assert OpencodeAdapter.known_settings == CommandAdapter.known_settings
    assert PiAdapter.known_settings == CommandAdapter.known_settings


# ------------------------- dogfood-20: usage/cost telemetry


def test_command_adapter_usage_patterns_extract_tokens_and_cost(tmp_path):
    stub = [sys.executable, "-c", 'print("tokens: 123 cost: 0.42")']
    adapter = CommandAdapter({
        "command": stub,
        "usage_patterns": [
            {"metric": "tokens", "regex": r"tokens:\s*(\d+)"},
            {"metric": "cost", "regex": r"cost:\s*([0-9.]+)"},
        ],
    })
    session = adapter.start_session(str(tmp_path), "sid-usage")
    state = adapter.send("ignored", session)
    assert state.status == "completed"
    usage = state.usage
    assert usage is not None
    assert usage["tokens"] == 123.0
    assert usage["cost"] == 0.42
    # existing keys untouched
    assert isinstance(usage["elapsed_seconds"], float)
    assert usage["exit_code"] == 0

    # failed sends extract too
    fail = CommandAdapter({
        "command": [sys.executable, "-c",
                    'print("tokens: 7"); raise SystemExit(2)'],
        "usage_patterns": [{"metric": "tokens", "regex": r"tokens:\s*(\d+)"}],
    })
    failed = fail.send("x", session)
    assert failed.status == "failed"
    assert failed.usage is not None
    assert failed.usage["tokens"] == 7.0
    assert failed.usage["exit_code"] == 2


def test_command_adapter_without_usage_patterns_stays_clean(tmp_path):
    adapter = CommandAdapter({
        "command": [sys.executable, "-c", 'print("tokens: 999")'],
    })
    session = adapter.start_session(str(tmp_path), "sid-clean")
    state = adapter.send("x", session)
    assert state.status == "completed"
    assert set(state.usage or {}) == {"elapsed_seconds", "exit_code"}


def _usage_project(tmp_path):
    config = {
        "default_adapter": "command",
        "adapters": {"command": {
            "command": [sys.executable, "-c",
                        'print("tokens: 123 cost: 0.42")'],
            "usage_patterns": [
                {"metric": "tokens", "regex": r"tokens:\s*(\d+)"},
                {"metric": "cost", "regex": r"cost:\s*([0-9.]+)"},
            ],
        }},
    }
    (tmp_path / "tether.json").write_text(json.dumps(config))
    mission = tmp_path / "m.yaml"
    mission.write_text("mission:\n  name: telemetry\n  goal: g\n"
                       "adapter: command\n")
    return mission


def test_full_run_report_and_sessions_show_carry_extracted_usage(tmp_path):
    from tether.audit import find_session_dir
    mission = _usage_project(tmp_path)
    r = runner.invoke(app, ["run", str(mission), "--project-dir",
                            str(tmp_path)])
    assert r.exit_code == 0, r.output
    sid = r.output.split("Session: ")[1].split()[0]
    session_dir = find_session_dir(tmp_path, ".tether/sessions", sid)
    report = json.loads(
        (session_dir / "report.json").read_text(encoding="utf-8"))
    usage = report.get("usage")
    assert usage is not None
    assert usage["tokens"] == 123.0
    assert usage["cost"] == 0.42

    r = runner.invoke(app, ["sessions", "show", sid,
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "Usage:" in r.output
    assert '"tokens"' in r.output and "123" in r.output
    assert '"cost"' in r.output and "0.42" in r.output


def test_sessions_stats_aggregates_usage_totals(tmp_path):
    _usage_project(tmp_path)
    mission = tmp_path / "m.yaml"
    r = runner.invoke(app, ["run", str(mission), "--project-dir",
                            str(tmp_path)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["run", str(mission), "--project-dir",
                            str(tmp_path)])
    assert r.exit_code == 0, r.output

    rj = runner.invoke(app, ["sessions", "stats", "--json",
                             "--project-dir", str(tmp_path)])
    assert rj.exit_code == 0, rj.output
    data = json.loads(rj.output)
    assert data["usage"]["sessions_reporting"] == 2
    assert data["usage"]["totals"]["tokens"] == 246
    assert data["usage"]["totals"]["cost"] == 0.84

    rh = runner.invoke(app, ["sessions", "stats",
                             "--project-dir", str(tmp_path)])
    assert rh.exit_code == 0, rh.output
    assert "Usage: 2 session(s) reporting;" in rh.output
    assert "tokens total 246" in rh.output
    assert "cost total 0.84" in rh.output


def test_sessions_stats_unchanged_when_no_usage_present(tmp_path):
    config = {
        "default_adapter": "mock",
        "adapters": {"mock": {"scenario": "success"}},
    }
    (tmp_path / "tether.json").write_text(json.dumps(config))
    mission = tmp_path / "m.yaml"
    mission.write_text(
        "mission:\n  name: nousage\n  goal: g\nadapter: mock\n")
    r = runner.invoke(app, ["run", str(mission), "--project-dir",
                            str(tmp_path)])
    assert r.exit_code == 0, r.output

    rj = runner.invoke(app, ["sessions", "stats", "--json",
                             "--project-dir", str(tmp_path)])
    assert rj.exit_code == 0, rj.output
    data = json.loads(rj.output)
    assert "usage" not in data

    rh = runner.invoke(app, ["sessions", "stats",
                             "--project-dir", str(tmp_path)])
    assert rh.exit_code == 0, rh.output
    assert "Usage:" not in rh.output


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
    # Promoted 2026-08-22 (docs/ADAPTERS.md): certified + multiple real
    # missions (dogfood-14/16/17/18).
    assert oc.verified is True
    assert oc.command == [
        "opencode", "run", "-m", "opencode/x-preview-f-free", "{prompt}",
    ]
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


def test_cli_validate_config_strict_bad_config_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tether.yaml").write_text(
        "default_adapter: mock\nadapters:\n  mock:\n    scenarioo: x\n"
    )
    r = runner.invoke(app, ["validate-config"])
    assert r.exit_code == 0  # warning only without --strict
    r = runner.invoke(app, ["validate-config", "--strict"])
    assert r.exit_code != 0
    assert "INVALID:" in r.output and "unknown setting 'scenarioo'" in r.output


def test_unknown_adapter_name_warns_and_fails_strict(caplog):
    with caplog.at_level(logging.WARNING, logger="tether.adapters"):
        problems = check_adapter_settings({"myagent": {"foo": "bar"}})
    assert problems == ["adapter 'myagent': unknown adapter name; cannot validate settings"]
    assert any("unknown adapter name" in r.message for r in caplog.records)
    with pytest.raises(ValueError, match="unknown adapter name"):
        check_adapter_settings({"myagent": {"foo": "bar"}}, strict=True)


def test_cli_validate_config_unregistered_adapter_name(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tether.yaml").write_text(
        "default_adapter: mock\nadapters:\n  myagent:\n    foo: bar\n"
    )
    with caplog.at_level(logging.WARNING, logger="tether.adapters"):
        r = runner.invoke(app, ["validate-config"])
    assert r.exit_code == 0  # warns, does not abort
    assert any(
        "adapter 'myagent': unknown adapter name; cannot validate settings"
        in rec.message
        for rec in caplog.records
    )
    r = runner.invoke(app, ["validate-config", "--strict"])
    assert r.exit_code != 0
    assert "unknown adapter name" in r.output


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


def test_setup_logging_applies_config_level_and_verbose_forces_debug():
    from tether.cli import _setup_logging
    logger = logging.getLogger("tether")
    original = logger.level
    try:
        _setup_logging(False, "WARNING")
        assert logger.level == logging.WARNING
        _setup_logging(False, "debug")  # case-insensitive
        assert logger.level == logging.DEBUG
        _setup_logging(True, "ERROR")  # --verbose always wins
        assert logger.level == logging.DEBUG
        _setup_logging(False, "not-a-level")  # invalid -> INFO fallback
        assert logger.level == logging.INFO
        _setup_logging(False)  # default INFO
        assert logger.level == logging.INFO
    finally:
        logger.setLevel(original)


def test_cli_run_applies_config_log_level_and_verbose_forces_debug(
        tmp_path, caplog):
    (tmp_path / "tether.yaml").write_text("log_level: ERROR\n")
    mission = tmp_path / "m.yaml"
    mission.write_text(
        "mission:\n  name: loglvl\n  goal: g\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\nadapter: mock\n"
        "adapters:\n  mock:\n    scenario: success\n"
    )
    try:
        caplog.clear()
        r = runner.invoke(app, ["run", str(mission),
                                "--project-dir", str(tmp_path)])
        assert r.exit_code == 0, r.output
        # config log_level ERROR suppresses the orchestrator's INFO records
        assert not any(rec.name == "tether" and rec.levelno < logging.ERROR
                       for rec in caplog.records)

        caplog.clear()
        r = runner.invoke(app, ["run", str(mission),
                                "--project-dir", str(tmp_path), "--verbose"])
        assert r.exit_code == 0, r.output
        # --verbose still forces DEBUG despite log_level: ERROR in config,
        # so the orchestrator's INFO records flow again
        assert logging.getLogger("tether").level == logging.DEBUG
        assert any(rec.name == "tether" and rec.levelno == logging.INFO
                   for rec in caplog.records)
    finally:
        logging.getLogger("tether").setLevel(logging.NOTSET)


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


# ------------------------------------------- dogfood-06: granular exit codes


def test_run_exit_code_constants():
    from tether import cli
    assert cli.EXIT_SUCCESS == 0
    assert cli.EXIT_FAILED == 1
    assert cli.EXIT_CANCELLED == 2
    assert cli.EXIT_REJECTED == 3
    assert cli.EXIT_SANDBOX_VIOLATION == 4
    # dogfood-21: mission budget breaches get their own exit code.
    assert cli.EXIT_BUDGET_EXCEEDED == 5


def test_cli_exit_success_and_failed(tmp_path):
    success = tmp_path / "ok.yaml"
    success.write_text(
        "mission:\n  name: ok\n  goal: g\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\nadapter: mock\n"
        "adapters:\n  mock:\n    scenario: success\n"
    )
    r = runner.invoke(app, ["run", str(success), "--project-dir", str(tmp_path)])
    assert r.exit_code == EXIT_SUCCESS, r.output

    failing = tmp_path / "bad.yaml"
    failing.write_text(
        "mission:\n  name: bad\n  goal: g\nadapter: mock\n"
        "recovery:\n  max_attempts: 1\n"
        "adapters:\n  mock:\n    scenario: always_fail\n"
    )
    r = runner.invoke(app, ["run", str(failing), "--project-dir", str(tmp_path)])
    assert r.exit_code == EXIT_FAILED
    assert "Status: failed" in r.output


def test_cli_exit_cancelled(tmp_path, monkeypatch):
    from tether.adapters.base import AgentAdapter, SessionInfo

    class _InterruptAdapter(AgentAdapter):
        name = "interrupt"
        verified = True

        def __init__(self):
            super().__init__({})

        def is_available(self):
            return True, ""

        def start_session(self, project_dir, session_id):
            return SessionInfo(session_id=session_id, project_dir=project_dir)

        def send(self, prompt, session):
            raise KeyboardInterrupt()

        def cancel(self, session):
            pass

    monkeypatch.setattr(
        "tether.adapters.resolve_adapter",
        lambda name, settings, default_timeout=None: _InterruptAdapter(),
    )
    mission = tmp_path / "c.yaml"
    mission.write_text("mission:\n  name: cancel\n  goal: g\n")
    r = runner.invoke(app, ["run", str(mission), "--project-dir", str(tmp_path)])
    assert r.exit_code == EXIT_CANCELLED, r.output
    assert "Status: cancelled" in r.output


def test_cli_exit_sandbox_violation(tmp_path):
    mission = tmp_path / "s.yaml"
    mission.write_text(
        "mission:\n  name: sbx-cli\n  goal: g\n"
        "forbidden_paths:\n  - '*.secret'\n"
        "adapter: command\n"
        "adapters:\n  command:\n    command:\n"
        f"      - {json.dumps(sys.executable)}\n"
        "      - '-c'\n"
        "      - \"open('config.secret', 'w').write('x')\"\n"
    )
    r = runner.invoke(app, ["run", str(mission), "--project-dir", str(tmp_path)])
    assert r.exit_code == EXIT_SANDBOX_VIOLATION, r.output
    sid = r.output.split("Session: ")[1].split()[0]
    report = json.loads(runner.invoke(
        app, ["report", sid, "--project-dir", str(tmp_path)]).output)
    assert report["status"] == "failed"
    assert report["sandbox_violations"] == [
        {"path": "config.secret", "rule": "forbidden_paths: *.secret"}]
