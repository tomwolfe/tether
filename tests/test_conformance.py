"""Capability metadata + conformance harness tests (dogfood-09)."""
import sys

from typer.testing import CliRunner

from tether.adapters.base import AgentAdapter, SessionInfo
from tether.adapters.command import CommandAdapter
from tether.adapters.experimental import OpencodeAdapter, PiAdapter
from tether.adapters.mock import MockAdapter
from tether.cli import app
from tether.conformance import run_conformance, capability_flags
from tether.models import AgentState

runner = CliRunner()


def _by_name(report):
    return {c.name: c for c in report.checks}


def _assert_ok(report):
    failures = [(c.name, c.detail) for c in report.checks if c.status == "failed"]
    assert report.ok, failures


# ------------------------------------------------------------ capabilities


def test_capability_defaults_on_base_are_safe():
    assert AgentAdapter.supports_cancel is False
    assert AgentAdapter.supports_process_tree_kill is False
    assert AgentAdapter.supports_usage is False
    assert AgentAdapter.supports_streaming is False
    assert AgentAdapter.one_shot is True


def test_command_adapter_capabilities():
    cmd = CommandAdapter({"command": [sys.executable, "-c", "pass"]})
    assert cmd.supports_cancel is True
    assert cmd.supports_process_tree_kill is True
    assert cmd.one_shot is True
    # honest negatives: no usage parsing, no streaming today
    assert cmd.supports_usage is False
    assert cmd.supports_streaming is False


def test_mock_adapter_capabilities_minimal_and_honest():
    mock = MockAdapter()
    assert mock.supports_cancel is False  # cancel() only latches a flag
    assert mock.supports_process_tree_kill is False
    assert mock.supports_usage is False
    assert mock.supports_streaming is False
    assert mock.one_shot is True


def test_experimental_presets_inherit_command_capabilities():
    for preset in (OpencodeAdapter(), PiAdapter()):
        assert preset.supports_cancel is True
        assert preset.supports_process_tree_kill is True
        assert preset.one_shot is True
        assert preset.supports_usage is False
        assert preset.supports_streaming is False


def test_capability_flags_string():
    assert capability_flags(MockAdapter()) == "one-shot"
    cmd = CommandAdapter({"command": [sys.executable, "-c", "pass"]})
    assert capability_flags(cmd) == "cancel,tree-kill,one-shot"
    result = runner.invoke(app, ["adapters", "list"])
    assert result.exit_code == 0
    assert "CAPABILITIES" in result.output
    assert "cancel,tree-kill,one-shot" in result.output  # command row


# ---------------------------------------------------------------- harness


def test_conformance_mock_passes_out_of_the_box():
    report = run_conformance(MockAdapter())
    _assert_ok(report)
    checks = _by_name(report)
    for name in ("availability", "success_completes", "logs_capture_output",
                 "failure_maps_failed"):
        assert checks[name].status == "passed", name
    # honestly skipped: no processes, no cancellation capability
    for name in ("timeout_fails_and_terminates_tree",
                 "cancel_terminates_active", "spawn_failure_unavailable",
                 "runs_in_project_dir"):
        assert checks[name].status == "skipped", name
    assert "supports_cancel=False" in checks["cancel_terminates_active"].detail
    assert "Verdict: PASS" in report.verdict_line()


def test_conformance_command_full_battery_with_stubs(tmp_path):
    stub = tmp_path / "agent.py"
    stub.write_text("print('stub agent ready')\n")
    adapter = CommandAdapter({"command": [sys.executable, str(stub)]})
    report = run_conformance(adapter)
    _assert_ok(report)
    # every check applies to the command family: nothing skipped
    assert all(c.status == "passed" for c in report.checks), \
        report.summary()
    names = {c.name for c in report.checks}
    assert {"availability", "success_completes", "logs_capture_output",
            "failure_maps_failed", "timeout_fails_and_terminates_tree",
            "cancel_terminates_active", "spawn_failure_unavailable",
            "runs_in_project_dir"} <= names


def test_conformance_command_without_config_certifies_via_stubs(tmp_path):
    adapter = CommandAdapter({"timeout_seconds": 30})  # no command configured
    report = run_conformance(adapter)
    _assert_ok(report)
    checks = _by_name(report)
    assert checks["availability"].passed is True
    assert "stub executable" in checks["availability"].detail


def test_conformance_detects_broken_command(tmp_path):
    adapter = CommandAdapter(
        {"command": [sys.executable, "-c", "raise SystemExit(5)"]})
    report = run_conformance(adapter)
    assert not report.ok
    checks = _by_name(report)
    assert checks["success_completes"].status == "failed"


def test_conformance_detects_unavailable_configured_command():
    adapter = CommandAdapter({"command": ["tether-no-such-binary-xyz"]})
    report = run_conformance(adapter)
    assert not report.ok
    checks = _by_name(report)
    assert checks["availability"].status == "failed"
    assert checks["success_completes"].status == "skipped"


class _MinimalAdapter(AgentAdapter):
    """A third-party-style adapter exercising the generic harness path."""

    name = "minimal"

    def __init__(self) -> None:
        super().__init__({})

    def is_available(self) -> tuple[bool, str]:
        return True, ""

    def start_session(self, project_dir: str, session_id: str) -> SessionInfo:
        return SessionInfo(session_id=session_id, project_dir=project_dir)

    def send(self, prompt: str, session: SessionInfo) -> AgentState:
        return AgentState(status="completed", logs=f"done: {prompt[:20]}")

    def cancel(self, session: SessionInfo) -> None:
        return None


def test_conformance_works_against_any_adapter_instance():
    report = run_conformance(_MinimalAdapter())
    _assert_ok(report)
    checks = _by_name(report)
    for name in ("availability", "success_completes", "logs_capture_output"):
        assert checks[name].status == "passed", name
    # fault-injection checks are skipped, not failed, for unknown classes
    for name in ("failure_maps_failed", "timeout_fails_and_terminates_tree",
                 "cancel_terminates_active", "spawn_failure_unavailable",
                 "runs_in_project_dir"):
        assert checks[name].status == "skipped", name


# --------------------------------------------------------------------- CLI


def test_cli_conformance_mock_passes_exit_zero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["adapters", "conformance", "mock"])
    assert r.exit_code == 0, r.output
    assert "[PASS]" in r.output and "Verdict: PASS" in r.output
    assert "[FAIL]" not in r.output


def test_cli_conformance_command_passes_exit_zero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no tether.yaml: stub-driven certification
    r = runner.invoke(app, ["adapters", "conformance", "command"])
    assert r.exit_code == 0, r.output
    assert "Verdict: PASS" in r.output


def test_cli_conformance_failure_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tether.yaml").write_text(
        "adapters:\n  command:\n    command: ['tether-no-such-binary-xyz']\n")
    r = runner.invoke(app, ["adapters", "conformance", "command"])
    assert r.exit_code != 0
    assert "[FAIL] availability" in r.output
    assert "Verdict: FAIL" in r.output


def test_cli_conformance_unknown_adapter_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["adapters", "conformance", "no-such-adapter"])
    assert r.exit_code != 0
    assert "no-such-adapter" in r.output
