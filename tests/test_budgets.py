"""Budget guardrails (dogfood-21): model, cumulative usage, enforcement."""
import json
import sys
import time

import pytest

from tether.adapters.base import AgentAdapter, SessionInfo
from tether.audit import find_session_dir
from tether.mission import MissionError, load_mission
from tether.models import AgentState, TetherConfig
from tether.orchestrator import Orchestrator


def py_cmd(code: str) -> str:
    """Cross-platform shell-free python command string for verification."""
    return f"{sys.executable} -c '{code}'"


PASS_CMD = py_cmd("import sys; sys.exit(0)")
FAIL_CMD = py_cmd("import sys; sys.exit(1)")


def _write_mission(tmp_path, body):
    p = tmp_path / "m.yaml"
    p.write_text(body, encoding="utf-8")
    return load_mission(p)


# ------------------------------------------------ task 1: budget model


def test_absent_budget_defaults_to_none(tmp_path):
    m = _write_mission(tmp_path, "mission:\n  name: m\n  goal: g\n")
    assert m.budget is None


def test_full_budget_parses(tmp_path):
    m = _write_mission(tmp_path, (
        "mission:\n  name: m\n  goal: g\n"
        "budget:\n"
        "  max_wall_seconds: 600\n"
        "  max_sends: 4\n"
        "  max_usage:\n"
        "    tokens: 200000\n"
        "    cost: 10.5\n"
    ))
    assert m.budget is not None
    assert m.budget.max_wall_seconds == 600
    assert m.budget.max_sends == 4
    assert m.budget.max_usage == {"tokens": 200000.0, "cost": 10.5}


@pytest.mark.parametrize("body", [
    "budget: oops\n",                              # not a mapping
    "budget:\n  max_wall_seconds: 0\n",            # non-positive wall
    "budget:\n  max_sends: -1\n",                  # negative sends
    "budget:\n  max_wall_seconds: true\n",         # bool is not a valid int
    "budget:\n  max_sends: many\n",                # non-int sends
    "budget:\n  max_usage: 5\n",                   # not a mapping
    "budget:\n  max_usage:\n    tokens: -1\n",     # negative ceiling
    "budget:\n  max_usage:\n    tokens: lots\n",   # non-numeric ceiling
    "budget:\n  max_wal_seconds: 60\n",            # typo'd key must not no-op
])
def test_invalid_budget_raises_mission_error(tmp_path, body):
    text = "mission:\n  name: m\n  goal: g\n" + body
    with pytest.raises(MissionError):
        _write_mission(tmp_path, text)


# ------------------------- tasks 2+3: tracking + runtime enforcement


class _ScriptedAdapter(AgentAdapter):
    """Scripted sends: dicts of {status, usage, sleep}; counts real sends."""

    name = "scripted"
    verified = True

    def __init__(self, sends):
        super().__init__({})
        self.sends = [dict(s) for s in sends]
        self.sent = 0

    def is_available(self):
        return True, ""

    def start_session(self, project_dir, session_id):
        return SessionInfo(session_id=session_id, project_dir=project_dir)

    def send(self, prompt, session):
        self.sent += 1
        spec = self.sends.pop(0) if self.sends else {}
        if spec.get("sleep"):
            time.sleep(spec["sleep"])
        return AgentState(status=spec.get("status", "completed"),
                          logs="out", usage=spec.get("usage"))

    def cancel(self, session):
        pass


def _run_scripted(tmp_path, adapter, budget_yaml="", commands=None,
                  max_attempts=3):
    commands = PASS_CMD if commands is None else commands
    budget_block = f"budget:\n  {budget_yaml}\n" if budget_yaml else ""
    mission_text = (
        "mission:\n  name: m\n  goal: g\n"
        + budget_block
        + f"verification:\n  commands:\n    - {commands}\n"
        + f"recovery:\n  max_attempts: {max_attempts}\n"
        + "adapter: mock\n"
    )
    mp = tmp_path / "m.yaml"
    mp.write_text(mission_text)
    cfg = TetherConfig(audit_dir=".tether/sessions")
    return Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))


# ------------------------------------------- task 2: cumulative usage


def test_cumulative_usage_accumulates_across_sends(tmp_path):
    adapter = _ScriptedAdapter([
        {"usage": {"tokens": 10}},
        {"usage": {"tokens": 15}},
    ])
    report = _run_scripted(tmp_path, adapter)
    assert report["status"] == "success"
    cum = report["cumulative_usage"]
    assert cum["tokens"] == 25.0
    assert cum["send_count"] == 2
    assert cum["wall_seconds"] >= 0
    # last-send report["usage"] semantics unchanged
    assert report["usage"] == {"tokens": 15}


def test_cumulative_usage_merges_recovery_and_ignores_non_numeric(tmp_path):
    adapter = _ScriptedAdapter([
        {"usage": {"tokens": 10, "note": "hi"}},
        {"usage": {"cost": 1.5}},
        {"usage": {"tokens": 7, "cost": 0.5}},   # recovery send
    ])
    report = _run_scripted(tmp_path, adapter, commands=FAIL_CMD)
    assert report["status"] == "failed"
    cum = report["cumulative_usage"]
    assert cum["tokens"] == 17.0
    assert cum["cost"] == 2.0
    assert "note" not in cum


# ------------------------------------------ task 3: runtime enforcement


def test_max_sends_blocks_first_recovery_send(tmp_path):
    adapter = _ScriptedAdapter([{}, {}, {}])
    report = _run_scripted(
        tmp_path, adapter, budget_yaml="max_sends: 2", commands=FAIL_CMD)
    assert report["status"] == "failed"
    assert adapter.sent == 2  # the recovery send never happened
    assert report["budget_exceeded"] == {
        "limit": "max_sends", "threshold": 2, "observed": 2}
    assert any("max_sends" in s for s in report["next_steps"])
    session = find_session_dir(
        tmp_path, ".tether/sessions", report["session_id"])
    events = [json.loads(line) for line in
              (session / "events.jsonl").read_text(encoding="utf-8")
              .splitlines()]
    hits = [e for e in events if e.get("kind") == "budget_exceeded"]
    assert hits and hits[0]["limit"] == "max_sends"


def test_max_sends_allows_exact_budget_success(tmp_path):
    adapter = _ScriptedAdapter([{}, {}])
    report = _run_scripted(tmp_path, adapter, budget_yaml="max_sends: 2")
    assert report["status"] == "success"
    assert "budget_exceeded" not in report


def test_max_usage_breach_skips_verification_after_send(tmp_path):
    adapter = _ScriptedAdapter([
        {"usage": {"tokens": 60}},
        {"usage": {"tokens": 60}},
        {"usage": {"tokens": 60}},
    ])
    report = _run_scripted(
        tmp_path, adapter, budget_yaml="max_usage:\n    tokens: 150",
        commands=FAIL_CMD)
    assert report["status"] == "failed"
    breach = report["budget_exceeded"]
    assert breach["limit"] == "max_usage[tokens]"
    assert breach["threshold"] == 150.0
    assert breach["observed"] == 180.0
    assert any("tokens" in s for s in report["next_steps"])
    assert report["cumulative_usage"]["send_count"] == 3


def test_configured_but_unreported_metric_never_fires(tmp_path):
    adapter = _ScriptedAdapter([
        {"usage": {"tokens": 999}},
        {"usage": {"tokens": 999}},
    ])
    report = _run_scripted(
        tmp_path, adapter, budget_yaml="max_usage:\n    cost_usd: 1")
    assert report["status"] == "success"
    assert "budget_exceeded" not in report


def test_max_wall_seconds_aborts_before_next_send(tmp_path):
    adapter = _ScriptedAdapter([
        {"sleep": 1.25},
        {},
    ])
    report = _run_scripted(tmp_path, adapter, budget_yaml="max_wall_seconds: 1")
    assert report["status"] == "failed"
    breach = report["budget_exceeded"]
    assert breach["limit"] == "max_wall_seconds"
    assert breach["threshold"] == 1
    assert breach["observed"] >= 1
    assert adapter.sent == 1  # execution send skipped after the deadline


def test_no_budget_report_has_cumulative_but_never_exceeded(tmp_path):
    report = _run_scripted(tmp_path, _ScriptedAdapter([{}]))
    assert report["status"] == "success"
    assert "budget_exceeded" not in report
    assert report["cumulative_usage"]["send_count"] == 2


# --------------------------------------------- task 4: exit code


def test_cli_budget_exceeded_exit_code(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from tether.cli import EXIT_BUDGET_EXCEEDED, app
    runner = CliRunner()
    monkeypatch.setattr(
        "tether.adapters.resolve_adapter",
        lambda name, settings, default_timeout=None: _ScriptedAdapter([{}, {}]),
    )
    mission = tmp_path / "b.yaml"
    mission.write_text(
        "mission:\n  name: breach\n  goal: g\nbudget:\n  max_sends: 1\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\nadapter: mock\n",
        encoding="utf-8")
    r = runner.invoke(app, ["run", str(mission),
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == EXIT_BUDGET_EXCEEDED == 5, r.output
    assert "Status: failed" in r.output
    assert "max_sends" in r.output


# -------------------------------------- task 5: sessions stats telemetry


def _usage_project(tmp_path, budget_yaml=""):
    """Command-adapter project whose sends report `tokens` via usage_patterns."""
    config = {
        "default_adapter": "command",
        "adapters": {"command": {
            "command": [sys.executable, "-c", 'print("tokens: 123")'],
            "usage_patterns": [
                {"metric": "tokens", "regex": r"tokens:\s*(\d+)"},
            ],
        }},
    }
    (tmp_path / "tether.json").write_text(json.dumps(config))
    budget_block = f"budget:\n  {budget_yaml}\n" if budget_yaml else ""
    mission = tmp_path / "m.yaml"
    mission.write_text(
        "mission:\n  name: telemetry\n  goal: g\n"
        + budget_block
        + "adapter: command\n")
    return mission


def test_sessions_stats_counts_budget_breaches(tmp_path):
    from typer.testing import CliRunner

    from tether.cli import app
    runner = CliRunner()
    mission = _usage_project(tmp_path, budget_yaml="max_usage:\n    tokens: 1")
    for _ in range(2):
        r = runner.invoke(app, ["run", str(mission),
                                "--project-dir", str(tmp_path)])
        assert r.exit_code == 5, r.output

    rj = runner.invoke(app, ["sessions", "stats", "--json",
                             "--project-dir", str(tmp_path)])
    assert rj.exit_code == 0, rj.output
    data = json.loads(rj.output)
    assert data["budgets"]["sessions_exceeded"] == 2

    rh = runner.invoke(app, ["sessions", "stats",
                             "--project-dir", str(tmp_path)])
    assert rh.exit_code == 0, rh.output
    assert "2 session(s) exceeded a mission budget" in rh.output


def test_sessions_stats_zero_breaches_human_output_unchanged(tmp_path):
    from typer.testing import CliRunner

    from tether.cli import app
    runner = CliRunner()
    mission = _usage_project(tmp_path)
    r = runner.invoke(app, ["run", str(mission),
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output

    rj = runner.invoke(app, ["sessions", "stats", "--json",
                             "--project-dir", str(tmp_path)])
    assert rj.exit_code == 0, rj.output
    data = json.loads(rj.output)
    assert data["budgets"]["sessions_exceeded"] == 0

    rh = runner.invoke(app, ["sessions", "stats",
                             "--project-dir", str(tmp_path)])
    assert rh.exit_code == 0, rh.output
    assert "budget" not in rh.output.lower()
