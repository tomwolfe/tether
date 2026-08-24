"""Transient-failure tolerance (dogfood-31): classifier + bounded retries."""
import json
import sys
from pathlib import Path

import pytest

import tether.reliability as reliability
from tether.adapters.base import AgentAdapter, SessionInfo
from tether.audit import find_session_dir
from tether.mission import load_mission
from tether.models import AgentState, RetriesSpec, TetherConfig
from tether.orchestrator import Orchestrator
from tether.reliability import (
    is_transient_failure,
    send_with_transient_retry,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def py_cmd(code: str) -> str:
    """Cross-platform shell-free python command string for verification."""
    return f"{sys.executable} -c '{code}'"


PASS_CMD = py_cmd("import sys; sys.exit(0)")
FAIL_CMD = py_cmd("import sys; sys.exit(1)")


# --------------------------------------------- task 1: transient classifier


@pytest.mark.parametrize("text", [
    "error: network_error while streaming",
    "Provider finish_reason: Network_Error",       # case-insensitive
    "Rate_Limit exceeded for org",
    "upstream OVERLOADED, try again later",
    "Connection Reset by peer",
])
def test_infrastructure_signatures_are_transient(text):
    assert is_transient_failure(AgentState(status="failed", error=text))


@pytest.mark.parametrize("text", [
    "ConnectionError: connection reset by peer",
    "assert resp.status_code == 200, got 503",
    "rate limit warning printed by the code under test",
])
def test_outage_words_in_agent_logs_are_not_transient(text):
    """Review finding (dogfood-31): GENUINE crashed runs whose captured
    agent/test output merely mentions outage words must never retry."""
    assert not is_transient_failure(
        AgentState(status="failed", logs=text, error="exit code 1"))


@pytest.mark.parametrize("code", ["502", "503", "504"])
def test_gateway_status_codes_are_transient(code):
    assert is_transient_failure(
        AgentState(status="failed", error=f"gateway returned HTTP {code}"))


@pytest.mark.parametrize("text", [
    "line 15023 mismatch; 15024 expected",
    "got exit code 15034, wanted 0",
])
def test_near_miss_numbers_never_match_status_codes(text):
    assert not is_transient_failure(AgentState(status="failed", error=text))


@pytest.mark.parametrize("text", [
    "endpoint is unavailable",
    "Error: Endpoint Is Unavailable, retry later",
    "ENDPOINT IS UNAVAILABLE",
    "upstream request failed",
    "Upstream Request Failed with status 500",
])
def test_dogfood34_provider_signatures_are_transient(text):
    """dogfood-34: real provider outage messages classify as TRANSIENT,
    including mixed-case variants."""
    assert is_transient_failure(AgentState(status="failed", error=text))


def test_dogfood34_new_signatures_respect_completed_and_logs_contract():
    """The new dogfood-34 signatures keep the classifier contract: only
    non-completed states' adapter-authored error is scanned."""
    assert not is_transient_failure(AgentState(
        status="completed", logs="endpoint is unavailable"))
    assert not is_transient_failure(
        AgentState(status="failed", logs="upstream request failed"))


def test_network_timeout_forms_in_error_are_transient():
    assert is_transient_failure(AgentState(
        status="unavailable", error="connection ETIMEDOUT"))
    assert is_transient_failure(AgentState(
        status="failed", error="socket.timeout during send"))
    assert is_transient_failure(AgentState(
        status="failed", error="httpx.ReadTimeoutError from provider"))
    assert is_transient_failure(AgentState(
        status="failed", error="provider request timeout after 30s"))


def test_tether_command_timeout_is_genuine_not_transient():
    """Review finding (dogfood-31): Tether's OWN genuine agent-timeout
    failure (src/tether/adapters/command.py) keeps its prior semantics —
    it drives recovery exactly once per send, never retried."""
    genuine = AgentState(
        status="failed",
        logs="$ agent-cli --do-work\n...partial output...",
        error=f"command timed out after {1800}s: agent-cli")
    assert not is_transient_failure(genuine)
    audit = _RecordingAudit()
    final, physical = send_with_transient_retry(
        lambda p, s: genuine, "p", "sess", step="plan", audit=audit,
        max_transient_retries=5, transient_backoff_seconds=10)
    assert len(physical) == 1
    assert audit.events == []


def test_generic_timeout_wording_in_error_is_not_transient():
    """Generic timeout wording collides with Tether's genuine agent
    timeouts, so only clearly network-level forms are transient."""
    assert not is_transient_failure(AgentState(
        status="failed", error="subprocess timed out"))
    assert not is_transient_failure(AgentState(
        status="failed", error="operation timeout"))


def test_completed_state_is_never_transient():
    assert not is_transient_failure(
        AgentState(status="completed", logs="network_error 503 overload"))


def test_plain_non_completed_states_are_not_transient():
    assert not is_transient_failure(AgentState(status="needs_input",
                                               logs="please answer"))
    assert not is_transient_failure(AgentState())
    assert not is_transient_failure(None)


# --------------------------- task 2: bounded retry wrapper (unit level)


class _RecordingAudit:
    def __init__(self):
        self.events = []

    def log_event(self, kind, data):
        self.events.append((kind, data))


def _ok(_prompt, _session):
    return AgentState(status="completed", logs="done")


def test_wrapper_retries_then_completes(monkeypatch):
    sleeps = []
    monkeypatch.setattr(reliability, "sleep", sleeps.append)
    audit = _RecordingAudit()
    states = iter([
        AgentState(status="failed", error="finish_reason: network_error"),
        AgentState(status="failed", error="HTTP 503 overloaded"),
        _ok(None, None),
    ])

    def send_fn(prompt, session):
        calls.append(prompt)
        return next(states)

    calls = []
    final, physical = send_with_transient_retry(
        send_fn, "p", "sess", step="plan", audit=audit,
        max_transient_retries=2, transient_backoff_seconds=10)
    assert final.status == "completed"
    assert len(calls) == 3                      # 1 + 2 retries
    assert [s.status for s in physical] == ["failed", "failed", "completed"]
    assert sleeps == [10.0, 10.0]
    kinds = [(k, d["step"], d["attempt"]) for k, d in audit.events]
    assert kinds == [("transient_retry", "plan", 1),
                     ("transient_retry", "plan", 2)]
    # reason is recorded and bounded
    assert "network_error" in audit.events[0][1]["reason"]
    assert len(audit.events[0][1]["reason"]) <= 200


def test_wrapper_exhaustion_returns_last_failed_state(monkeypatch):
    monkeypatch.setattr(reliability, "sleep", lambda _s: None)
    flaky = AgentState(status="failed", error="rate_limit again")
    final, physical = send_with_transient_retry(
        lambda p, s: flaky.model_copy(), "p", "sess", step="execute",
        audit=_RecordingAudit(), max_transient_retries=2,
        transient_backoff_seconds=0)
    assert final.status == "failed"
    assert len(physical) == 3


def test_genuine_failure_is_sent_exactly_once():
    genuine = AgentState(status="failed", logs="syntax error in patch")
    audit = _RecordingAudit()
    final, physical = send_with_transient_retry(
        lambda p, s: genuine, "p", "sess", step="plan", audit=audit,
        max_transient_retries=5, transient_backoff_seconds=10)
    assert final is genuine
    assert len(physical) == 1
    assert audit.events == []


def test_before_retry_gate_fires_before_sleep(monkeypatch):
    order = []
    monkeypatch.setattr(reliability, "sleep",
                        lambda _s: order.append("sleep"))

    def breach():
        order.append("breach")
        raise RuntimeError("budget exceeded")

    with pytest.raises(RuntimeError, match="budget exceeded"):
        send_with_transient_retry(
            lambda p, s: AgentState(status="failed", error="overloaded"),
            "p", "sess", step="plan", audit=_RecordingAudit(),
            max_transient_retries=2, transient_backoff_seconds=10,
            before_retry=breach)
    assert order == ["breach"]  # budget gate runs before any backoff sleep


def test_on_result_fires_per_physical_send_before_retry_gate(monkeypatch):
    """Review finding (dogfood-31): usage from a physical send must merge
    BEFORE the between-retry budget gate, never only after all retries."""
    monkeypatch.setattr(reliability, "sleep",
                        lambda _s: order.append("sleep"))
    order = []
    states = iter([
        AgentState(status="failed", error="overloaded"),
        AgentState(status="completed", logs="done"),
    ])

    def send_fn(_prompt, _session):
        return next(states)

    final, _physical = send_with_transient_retry(
        send_fn, "p", "sess", step="plan", audit=_RecordingAudit(),
        max_transient_retries=2, transient_backoff_seconds=10,
        on_result=lambda s: order.append(f"usage:{s.status}"))
    assert final.status == "completed"
    # first send -> usage merged; THEN retry gate + backoff; second send
    # merges too — totals are always fresh when the gate runs.
    assert order == ["usage:failed", "sleep", "usage:completed"]


# ------------------------- task 3: orchestrator-level integration


class _ScriptedAdapter(AgentAdapter):
    """Scripted sends: dicts of {status, logs, error, usage}; counts sends."""

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
        return AgentState(status=spec.get("status", "completed"),
                          logs=spec.get("logs", ""),
                          error=spec.get("error"),
                          usage=spec.get("usage"))

    def cancel(self, session):
        pass


FLAKY = {"status": "failed",
         "logs": "error: Provider finish_reason: network_error",
         "error": "Provider finish_reason: network_error"}


def _run(tmp_path, adapter, retries=None, commands=None, mission_extra=""):
    commands = PASS_CMD if commands is None else commands
    mission_text = (
        "mission:\n  name: m\n  goal: g\n"
        f"{mission_extra}"
        f"verification:\n  commands:\n    - {commands}\n"
        "adapter: mock\n"
    )
    mp = tmp_path / "m.yaml"
    mp.write_text(mission_text)
    cfg_kwargs = {"audit_dir": ".tether/sessions"}
    if retries is not None:
        cfg_kwargs["retries"] = RetriesSpec(**retries)
    cfg = TetherConfig(**cfg_kwargs)
    return Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))


def _events(tmp_path, report):
    session = find_session_dir(
        tmp_path, ".tether/sessions", report["session_id"])
    return [json.loads(line) for line in
            (session / "events.jsonl").read_text(encoding="utf-8")
            .splitlines()]


def test_planning_transient_failures_recover(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(reliability, "sleep", sleeps.append)
    adapter = _ScriptedAdapter([FLAKY, FLAKY, {}, {}])  # plan x3, execute
    report = _run(tmp_path, adapter)
    assert report["status"] == "success"
    assert adapter.sent == 4                    # 3 planning physical + exec
    assert sleeps == [10.0, 10.0]               # default backoff
    # one LOGICAL send per step despite the extra physical attempts
    assert report["cumulative_usage"]["send_count"] == 2
    retry_events = [(e["step"], e["attempt"])
                    for e in _events(tmp_path, report)
                    if e["kind"] == "transient_retry"]
    assert retry_events == [("plan", 1), ("plan", 2)]


def test_usage_merged_across_all_physical_sends(tmp_path, monkeypatch):
    monkeypatch.setattr(reliability, "sleep", lambda _s: None)
    adapter = _ScriptedAdapter([
        dict(FLAKY, usage={"tokens": 10}),
        dict(FLAKY, usage={"tokens": 10}),
        {"usage": {"tokens": 10}},              # plan succeeds on attempt 3
        {"usage": {"tokens": 15}},              # execution
    ])
    report = _run(tmp_path, adapter)
    assert report["status"] == "success"
    assert report["cumulative_usage"]["tokens"] == 45.0  # all 4 physical
    assert report["cumulative_usage"]["send_count"] == 2  # logical sends only
    assert report["usage"] == {"tokens": 15}    # last-send semantics intact


def test_between_retry_budget_check_sees_fresh_usage(tmp_path, monkeypatch):
    """Review finding (dogfood-31): usage from already-made physical sends
    must count toward the budget gate BETWEEN retries, not only after all
    retries finish. Ceiling 15: after the second physical send cumulative
    usage is 20, so the second retry's gate aborts with a budget breach
    instead of a third send."""
    monkeypatch.setattr(reliability, "sleep", lambda _s: None)
    adapter = _ScriptedAdapter([
        dict(FLAKY, usage={"tokens": 10}),
        dict(FLAKY, usage={"tokens": 10}),
        dict(FLAKY, usage={"tokens": 10}),      # never reached: gate fires
    ])
    report = _run(
        tmp_path, adapter,
        mission_extra="budget:\n  max_usage:\n    tokens: 15\n")
    assert report["status"] == "failed"
    assert adapter.sent == 2                    # third send never happens
    assert report["budget_exceeded"] == {
        "limit": "max_usage[tokens]", "threshold": 15, "observed": 20.0}
    breaches = [e for e in _events(tmp_path, report)
                if e["kind"] == "budget_exceeded"]
    assert breaches and breaches[0]["observed"] == 20.0


def test_planning_exhaustion_keeps_legacy_failure_message(
        tmp_path, monkeypatch):
    monkeypatch.setattr(reliability, "sleep", lambda _s: None)
    adapter = _ScriptedAdapter([dict(FLAKY) for _ in range(3)])
    report = _run(tmp_path, adapter)
    assert report["status"] == "failed"
    assert adapter.sent == 3                    # default: 2 retries => 3 total
    assert report["next_steps"][-1] == (
        "Planning step ended with status 'failed'; "
        "mission aborted before execution.")
    assert report["recovery_attempts"] == []
    assert [(e["step"], e["attempt"]) for e in _events(tmp_path, report)
            if e["kind"] == "transient_retry"] \
        == [("plan", 1), ("plan", 2)]


def test_custom_backoff_and_retry_count_from_config(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(reliability, "sleep", sleeps.append)
    adapter = _ScriptedAdapter([FLAKY, {}])     # plan recovers after 1 retry
    report = _run(tmp_path, adapter,
                  retries={"max_transient_retries": 1,
                           "transient_backoff_seconds": 2.5})
    assert report["status"] == "success"
    assert adapter.sent == 3
    assert sleeps == [2.5]


def test_genuine_planning_failure_never_retried(tmp_path):
    adapter = _ScriptedAdapter([
        {"status": "failed", "logs": "syntax error in generated code"},
    ])
    report = _run(tmp_path, adapter)
    assert report["status"] == "failed"
    assert adapter.sent == 1                    # zero retries
    assert report["next_steps"][-1] == (
        "Planning step ended with status 'failed'; "
        "mission aborted before execution.")


def test_repair_transient_retry_recovers_within_one_attempt(
        tmp_path, monkeypatch):
    monkeypatch.setattr(reliability, "sleep", lambda _s: None)
    adapter = _ScriptedAdapter([
        {},                                     # planning completed
        {"status": "failed", "logs": "tests failed"},   # genuine exec failure
        FLAKY,                                  # repair send flakes once...
        {},                                     # ...and completes on retry
    ])
    report = _run(tmp_path, adapter)
    assert report["status"] == "success"
    assert adapter.sent == 4
    # exactly ONE recovery attempt was consumed despite the flaky repair send
    assert len(report["recovery_attempts"]) == 1
    retry_events = [(e["step"], e["attempt"])
                    for e in _events(tmp_path, report)
                    if e["kind"] == "transient_retry"]
    assert retry_events == [("repair-1", 1)]


# --------------------------------------- dogfood-34 gap 2: review-gate send


REVIEW_APPROVED = "REVIEW: APPROVE\nthe change accomplishes the goal"


def test_review_send_transient_retries_recover(tmp_path, monkeypatch):
    """dogfood-34: the review-gate send retries TRANSIENT failures like
    every other agent send: two flaky attempts then an approval completes
    the review, with one transient_retry audit event per retry."""
    monkeypatch.setattr(reliability, "sleep", lambda _s: None)
    adapter = _ScriptedAdapter([
        {},                          # planning
        {},                          # execution
        FLAKY,                       # review attempt 1: transient...
        FLAKY,                       # ...attempt 2: transient again...
        {"logs": REVIEW_APPROVED},   # ...attempt 3 approves
    ])
    report = _run(tmp_path, adapter,
                  mission_extra="review:\n  enabled: true\n")
    assert report["status"] == "success"
    assert report["review"]["verdict"] == "approve"
    assert adapter.sent == 5                 # 3 physical review sends
    retry_events = [(e["step"], e["attempt"])
                    for e in _events(tmp_path, report)
                    if e["kind"] == "transient_retry"]
    assert retry_events == [("review", 1), ("review", 2)]


def test_review_genuine_failure_still_rejects_without_retries(tmp_path):
    """Non-transient review failure keeps byte-for-byte prior semantics:
    immediate request_changes, zero retries, zero retry events."""
    adapter = _ScriptedAdapter([
        {},
        {},
        {"status": "failed", "logs": "no verdict here",
         "error": "exit code 1"},
    ])
    report = _run(tmp_path, adapter,
                  mission_extra="review:\n  enabled: true\n")
    assert report["status"] == "failed"
    assert report["review"]["verdict"] == "request_changes"
    assert adapter.sent == 3                 # plan + execute + one review send
    assert not [e for e in _events(tmp_path, report)
                if e["kind"] == "transient_retry"]


def test_review_transient_exhaustion_keeps_fail_safe_semantics(
        tmp_path, monkeypatch):
    monkeypatch.setattr(reliability, "sleep", lambda _s: None)
    adapter = _ScriptedAdapter([{}, {}] + [dict(FLAKY) for _ in range(3)])
    report = _run(tmp_path, adapter,
                  mission_extra="review:\n  enabled: true\n")
    # Default retry budget exhausted (2 retries => 3 physical sends); the
    # gate falls into its existing fail-safe request_changes path.
    assert report["status"] == "failed"
    assert adapter.sent == 5
    assert report["review"]["verdict"] == "request_changes"
    assert [(e["step"], e["attempt"]) for e in _events(tmp_path, report)
            if e["kind"] == "transient_retry"] \
        == [("review", 1), ("review", 2)]


def test_dry_run_still_makes_no_adapter_calls(tmp_path):
    mp = tmp_path / "m.yaml"
    mp.write_text(f"mission:\n  name: m\n  goal: g\n"
                  f"verification:\n  commands:\n    - {PASS_CMD}\n")
    adapter = _ScriptedAdapter([])
    cfg = TetherConfig(audit_dir=".tether/sessions", dry_run=True)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "success"
    assert adapter.sent == 0


# --------------------------------------------- task 4: docs truth


def test_docs_document_transient_retries():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    recovery = readme.split("## Recovery", 1)[1].split("\n## ", 1)[0]
    for needle in ("transient_retry", "max_transient_retries",
                   "transient_backoff_seconds", "network_error"):
        assert needle in recovery, needle
    arch = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    module_map = arch.split("## Module map", 1)[1].split("```", 2)[1]
    assert "reliability.py" in module_map
    core_loop = arch.split("## Core loop", 1)[1].split("## Sandbox modes", 1)[0]
    assert "transient_retry" in core_loop
