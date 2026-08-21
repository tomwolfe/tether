import json
import subprocess
import sys

import pytest

from tether.audit import AuditTrail, find_session_dir
from tether.config import resolve_config
from tether.git_safety import (
    changed_files_since,
    create_checkpoint,
    head_sha,
    is_dirty,
    list_checkpoint_refs,
    rollback,
)
from tether.mission import MissionError, load_mission
from tether.models import TetherConfig
from tether.orchestrator import Orchestrator
from tether.adapters import resolve_adapter
from tether.verification import run_verification, summarize


def py_cmd(code: str) -> str:
    """Cross-platform shell-free python command string for verification."""
    return f"{sys.executable} -c '{code}'"


PASS_CMD = py_cmd("import sys; sys.exit(0)")
FAIL_CMD = py_cmd("import sys; sys.exit(1)")


@pytest.fixture
def project_dir(tmp_path):
    return tmp_path


def _write_mission(tmp_path, body):
    p = tmp_path / "mission.yaml"
    p.write_text(body, encoding="utf-8")
    return load_mission(p)


MISSION = f"""\
mission:
  name: test-mission
  goal: do a thing
verification:
  commands:
    - {PASS_CMD}
recovery:
  max_attempts: 3
adapter: mock
"""


def test_mission_validation_ok(tmp_path):
    m = _write_mission(tmp_path, MISSION)
    assert m.name == "test-mission"
    assert m.verification.commands == [PASS_CMD]


@pytest.mark.parametrize("bad", [
    "mission:\n  name: ''\n  goal: x\n",
    "mission:\n  name: x\n  goal: ''\n",
    "goal: no mission block\n",
    "mission:\n  name: x\n  goal: y\nverification:\n  commands: not-a-list\n",
])
def test_mission_validation_fails(tmp_path, bad):
    with pytest.raises(MissionError):
        _write_mission(tmp_path, bad)


def test_config_defaults_and_precedence(project_dir):
    cfg = resolve_config(project_dir)
    assert isinstance(cfg, TetherConfig)
    assert cfg.default_adapter == "mock"
    (project_dir / "tether.yaml").write_text("default_adapter: command\nmax_attempts: 5\n")
    cfg = resolve_config(project_dir)
    assert cfg.default_adapter == "command"
    assert cfg.max_attempts == 5
    # CLI overrides beat project config
    cfg = resolve_config(project_dir, cli_overrides={"max_attempts": 2})
    assert cfg.max_attempts == 2


def test_config_rejects_unknown_keys(project_dir):
    (project_dir / "tether.yaml").write_text("bogus_key: 1\n")
    with pytest.raises(ValueError):
        resolve_config(project_dir)


def _orchestra(tmp_path, scenario, commands=None, max_attempts=3):
    adapter = resolve_adapter("mock", {"mock": {"scenario": scenario}})
    cfg = TetherConfig(audit_dir=".tether/sessions", max_attempts=max_attempts)
    commands = [PASS_CMD] if commands is None else list(commands)
    mission_text = f"""\
mission:
  name: m
  goal: g
verification:
  commands: {json.dumps(list(commands))}
recovery:
  max_attempts: {max_attempts}
adapter: mock
"""
    mp = tmp_path / "m.yaml"
    mp.write_text(mission_text)
    mission = load_mission(mp)
    return Orchestrator(adapter, cfg, tmp_path).run(mission)


def test_mock_success_path(tmp_path):
    report = _orchestra(tmp_path, "success")
    assert report["status"] == "success"
    assert report["recovery_attempts"] == []
    session = find_session_dir(tmp_path, ".tether/sessions", report["session_id"])
    assert session is not None
    assert (session / "report.json").exists()
    assert (session / "events.jsonl").exists()
    assert list((session / "prompts").glob("*.txt"))
    saved = json.loads((session / "report.json").read_text())
    assert saved["status"] == "success"


def test_mock_always_fails_max_attempts(tmp_path):
    report = _orchestra(tmp_path, "always_fail", max_attempts=2)
    assert report["status"] == "failed"
    assert len(report["recovery_attempts"]) == 1  # attempts 1..2 -> one recovery between


def test_recovery_succeeds_after_retry(tmp_path):
    report = _orchestra(tmp_path, "fail_then_succeed")
    assert report["status"] == "success"
    assert len(report["recovery_attempts"]) == 1


def test_verification_failure_detected(tmp_path):
    results = run_verification([FAIL_CMD], tmp_path)
    passed, out = summarize(results)
    assert not passed
    assert "exit code 1" in out
    ok, _ = summarize(run_verification([PASS_CMD], tmp_path))
    assert ok


def test_summarize_bounds_repair_prompt_but_audit_keeps_full(tmp_path):
    from tether.verification import clip_output
    results = run_verification(
        [py_cmd('import sys; print("A"*100000); sys.exit(1)')], tmp_path)
    assert not results[0].passed
    # audit record (VerificationResult) keeps full output
    assert len(results[0].stdout) >= 100_000
    passed, out = summarize(results)
    assert not passed
    assert len(out) < 20_000
    assert "truncated" in out
    assert clip_output("short") == "short"


def test_verification_timeout_and_missing_binary(tmp_path):
    r = run_verification([py_cmd("import time; time.sleep(5)")], tmp_path, timeout_seconds=1)[0]
    assert r.timed_out
    r = run_verification(["definitely-not-a-binary-xyz"], tmp_path)[0]
    assert not r.passed and "not found" in r.stderr


def test_dry_run_skips_execution(tmp_path):
    results = run_verification([FAIL_CMD], tmp_path, dry_run=True)
    assert results[0].skipped_dry_run and results[0].passed


def _git_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)


def test_git_checkpoint_and_rollback(tmp_path):
    _git_repo(tmp_path)
    base = head_sha(tmp_path)
    info = create_checkpoint(tmp_path, "sess1234")
    assert info.created and info.ref.endswith("sess1234")
    (tmp_path / "f.txt").write_text("changed\n")
    (tmp_path / "new.txt").write_text("new\n")
    assert is_dirty(tmp_path)
    assert changed_files_since(tmp_path, base) == ["f.txt", "new.txt"]
    # dirty tree blocks destructive rollback
    ok, msg = rollback(tmp_path, "sess1234")
    assert not ok and "dirty" in msg
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "--", "."], check=True)
    (tmp_path / "new.txt").unlink()
    ok, msg = rollback(tmp_path, "sess1234")
    assert ok
    assert head_sha(tmp_path) == base
    assert (tmp_path / "f.txt").read_text() == "hello\n"


def test_git_dirty_requires_allow_dirty(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / "f.txt").write_text("dirty\n")
    info = create_checkpoint(tmp_path, "s1", allow_dirty=False)
    assert not info.created and "dirty" in info.warning
    info = create_checkpoint(tmp_path, "s2", allow_dirty=True)
    assert info.created and info.dirty


def test_non_git_project_warns_and_backs_up(tmp_path):
    info = create_checkpoint(tmp_path, "s3")
    assert not info.is_git_repo and info.warning
    from tether.git_safety import make_file_backup
    (tmp_path / "data.txt").write_text("keep me")
    dest = make_file_backup(tmp_path, tmp_path / ".tether/backups", "s3")
    assert dest and (tmp_path / ".tether/backups/s3.tar.gz").exists()


def test_audit_trail_events(tmp_path):
    audit = AuditTrail(tmp_path, ".tether/sessions", "my/mission:name", "abcdef123456")
    audit.log_event("x", {"a": 1})
    audit.save_prompt("plan", "hello")
    assert (audit.dir / "events.jsonl").exists()
    assert find_session_dir(tmp_path, ".tether/sessions", "abcdef1234") == audit.dir


# ------------------------------------------------- non-completed states (B1)


from tether.adapters.base import AgentAdapter, SessionInfo  # noqa: E402
from tether.models import AgentState  # noqa: E402


class _ScriptedAdapter(AgentAdapter):
    """Returns scripted AgentStates per send; records availability."""

    name = "scripted"
    verified = True

    def __init__(self, statuses, available=True):
        super().__init__({})
        self.statuses = list(statuses)
        self.available = available

    def is_available(self):
        return self.available, "" if self.available else "binary missing"

    def start_session(self, project_dir, session_id):
        return SessionInfo(session_id=session_id, project_dir=project_dir)

    def send(self, prompt, session):
        status = self.statuses.pop(0) if self.statuses else "completed"
        return AgentState(status=status, logs=f"[{status}]")

    def cancel(self, session):
        pass


def _run_scripted(tmp_path, adapter):
    mission_text = f"""\
mission:
  name: m
  goal: g
verification:
  commands:
    - {PASS_CMD}
adapter: mock
"""
    mp = tmp_path / "m.yaml"
    mp.write_text(mission_text)
    cfg = TetherConfig(audit_dir=".tether/sessions")
    return Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))


@pytest.mark.parametrize("status", [
    "unavailable", "cancelled", "needs_input", "running",
])
def test_non_completed_state_never_reports_success(tmp_path, status):
    report = _run_scripted(tmp_path, _ScriptedAdapter([status, status]))
    assert report["status"] == "failed"


def test_unavailable_adapter_fails_fast_even_with_passing_verification(tmp_path):
    # verification would trivially pass; agent never ran -> must NOT be success
    adapter = _ScriptedAdapter(["unavailable"])
    report = _run_scripted(tmp_path, adapter)
    assert report["status"] == "failed"
    assert any("unavailable" in s.lower() for s in report["next_steps"])


def test_orchestrator_checks_availability_before_running(tmp_path):
    adapter = _ScriptedAdapter([], available=False)
    mission_text = 'mission:\n  name: m\n  goal: g\nadapter: mock\n'
    mp = tmp_path / "m.yaml"
    mp.write_text(mission_text)
    cfg = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"
    assert any("unavailable" in s.lower() for s in report["next_steps"])


# ------------------------------------------------------- plan failure (B2)


def test_plan_failure_does_not_proceed_to_success(tmp_path):
    class PlanFailAdapter(_ScriptedAdapter):
        def send(self, prompt, session):
            if not getattr(self, "_planned", False):
                self._planned = True
                return AgentState(status="failed", error="plan exploded")
            return super().send(prompt, session)

    adapter = PlanFailAdapter(["completed"])
    report = _run_scripted(tmp_path, adapter)
    assert report["status"] == "failed"
    assert any("Planning step" in s for s in report["next_steps"])


def test_adapter_reported_changed_files_and_usage_surfaced(tmp_path):
    class RichAdapter(_ScriptedAdapter):
        def send(self, prompt, session):
            if not getattr(self, "_planned", False):
                self._planned = True
                return AgentState(status="completed", logs="plan")
            return AgentState(status="completed", logs="done",
                              changed_files=["agent-made.txt"],
                              usage={"tokens": 42})

    report = _run_scripted(tmp_path, RichAdapter([]))
    assert report["status"] == "success"
    assert "agent-made.txt" in report["changed_files"]
    assert report["usage"] == {"tokens": 42}


# ------------------------------------------------ graceful interrupt (B3)


class _InterruptAdapter(_ScriptedAdapter):
    """Raises KeyboardInterrupt from the first send(); records cancel calls."""

    def __init__(self):
        super().__init__([])
        self.cancel_calls = 0

    def send(self, prompt, session):
        raise KeyboardInterrupt()

    def cancel(self, session):
        self.cancel_calls += 1


def test_keyboard_interrupt_cancels_gracefully(tmp_path):
    adapter = _InterruptAdapter()
    report = _run_scripted(tmp_path, adapter)
    assert report["status"] == "cancelled"
    assert adapter.cancel_calls == 1
    assert any("rollback" in s.lower() for s in report["next_steps"])
    session = find_session_dir(tmp_path, ".tether/sessions", report["session_id"])
    assert session is not None
    assert (session / "report.json").exists()
    saved = json.loads((session / "report.json").read_text())
    assert saved["status"] == "cancelled"
    events = [
        json.loads(line)
        for line in (session / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(e.get("kind") == "cancelled" for e in events)


def test_keyboard_interrupt_swallows_cancel_errors(tmp_path):
    class _BadCancelAdapter(_InterruptAdapter):
        def cancel(self, session):
            self.cancel_calls += 1
            raise RuntimeError("cancel exploded")

    adapter = _BadCancelAdapter()
    report = _run_scripted(tmp_path, adapter)
    assert report["status"] == "cancelled"
    assert adapter.cancel_calls == 1


# -------------------------------------------------- single-writer lock (B4)


class _RecordingAdapter(_ScriptedAdapter):
    """Records every adapter interaction (for proving non-invocation)."""

    def __init__(self, statuses=None):
        super().__init__(statuses if statuses is not None else ["completed"])
        self.calls: list[str] = []

    def is_available(self):
        self.calls.append("is_available")
        return super().is_available()

    def start_session(self, project_dir, session_id):
        self.calls.append("start_session")
        return super().start_session(project_dir, session_id)

    def send(self, prompt, session):
        self.calls.append("send")
        return super().send(prompt, session)


def _write_lock(tmp_path, holder="holdersess1234"):
    lock = tmp_path / ".tether" / "tether.lock"
    lock.parent.mkdir(exist_ok=True)
    lock.write_text(holder + "\n", encoding="utf-8")
    return lock


def test_lock_blocks_second_run_without_adapter_interaction(tmp_path):
    _write_lock(tmp_path, "holdersess1234")
    adapter = _RecordingAdapter(["completed"])
    report = _run_scripted(tmp_path, adapter)
    assert report["status"] == "failed"
    # fails fast: no checkpoint, no backup, no adapter interaction at all
    assert adapter.calls == []
    assert list_checkpoint_refs(tmp_path) == []
    assert not (tmp_path / ".tether/backups").exists()
    assert any("holdersess1234" in s for s in report["next_steps"])
    assert any(
        "stale" in s.lower() and "remove" in s.lower()
        for s in report["next_steps"]
    )
    # the contending run must not clobber or steal the held lock
    assert (tmp_path / ".tether" / "tether.lock").read_text().strip() == \
        "holdersess1234"


def test_stale_lock_is_taken_over(tmp_path):
    import os
    import time as _time
    lock = _write_lock(tmp_path, "oldsess0000")
    old = _time.time() - 13 * 3600
    os.utime(lock, (old, old))
    adapter = _RecordingAdapter(["completed"])
    report = _run_scripted(tmp_path, adapter)
    assert report["status"] == "success"
    assert not lock.exists()  # released again after the run


def test_lock_released_after_normal_run(tmp_path):
    adapter = _RecordingAdapter(["completed"])
    report = _run_scripted(tmp_path, adapter)
    assert report["status"] == "success"
    assert not (tmp_path / ".tether" / "tether.lock").exists()


def test_lock_released_after_cancelled_run(tmp_path):
    adapter = _InterruptAdapter()
    report = _run_scripted(tmp_path, adapter)
    assert report["status"] == "cancelled"
    assert not (tmp_path / ".tether" / "tether.lock").exists()


# --------------------------------------------- plan feeds execution (B5)


def test_execute_prompt_includes_plan_text(tmp_path):
    PLAN = "1. read code\n2. edit code\n3. run tests"

    class PlanRecordingAdapter(_ScriptedAdapter):
        def __init__(self):
            super().__init__([])
            self.prompts: list[str] = []

        def send(self, prompt, session):
            self.prompts.append(prompt)
            if len(self.prompts) == 1:  # planning step
                return AgentState(status="completed", logs=PLAN)
            return AgentState(status="completed", logs="done")

    adapter = PlanRecordingAdapter()
    report = _run_scripted(tmp_path, adapter)
    assert report["status"] == "success"
    assert len(adapter.prompts) == 2
    assert PLAN not in adapter.prompts[0]
    assert PLAN in adapter.prompts[1]


# --------------------------------------------- tamper-evident audit (B6)


def _canonical(obj):
    import json as _json
    return _json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _run_and_get_events(tmp_path):
    report = _orchestra(tmp_path, "success")
    assert report["status"] == "success"
    session = find_session_dir(tmp_path, ".tether/sessions",
                               report["session_id"])
    events = session / "events.jsonl"
    lines = events.read_text(encoding="utf-8").splitlines()
    return report, events, lines


def test_events_chain_valid_after_run(tmp_path):
    import hashlib
    from tether.audit import verify_event_chain
    _, _, lines = _run_and_get_events(tmp_path)
    assert len(lines) >= 2
    parsed = [json.loads(line) for line in lines]
    # first event anchors the chain with an empty prev
    assert parsed[0]["prev"] == ""
    for prev_event, event in zip(parsed, parsed[1:]):
        expected = hashlib.sha256(_canonical(prev_event).encode()).hexdigest()
        assert event["prev"] == expected
    ok, msg = verify_event_chain(lines)
    assert ok, msg


def test_logs_verify_cli_intact_then_tampered(tmp_path):
    from typer.testing import CliRunner
    from tether.cli import app
    runner = CliRunner()
    report, events, lines = _run_and_get_events(tmp_path)
    sid = report["session_id"]

    r = runner.invoke(app, ["logs", sid, "--verify",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "OK" in r.output and "intact" in r.output

    # tamper with an event in the middle of the file
    tampered = list(lines)
    victim = json.loads(tampered[1])
    victim["kind"] = "forged"
    tampered[1] = json.dumps(victim, default=str)
    events.write_text("\n".join(tampered) + "\n", encoding="utf-8")
    r = runner.invoke(app, ["logs", sid, "--verify",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 1
    assert "BROKEN" in r.output
    assert f"event {3}" in r.output  # first break is the next link


def test_verify_event_chain_reports_first_break(tmp_path):
    from tether.audit import verify_event_chain

    def make_chain(n=4):
        out, prev_hash = [], ""
        for i in range(n):
            ev = {"ts": f"2026-01-01T00:00:0{i}+00:00", "kind": f"k{i}",
                  "prev": prev_hash}
            prev_hash = hashlib.sha256(_canonical(ev).encode()).hexdigest()
            out.append(json.dumps(ev))
        return out

    import hashlib

    lines = make_chain()
    ok, msg = verify_event_chain(lines)
    assert ok, msg

    # editing any interior event breaks the chain at its successor
    forged = list(lines)
    ev = json.loads(forged[1])
    ev["kind"] = "forged-kind"
    forged[1] = json.dumps(ev)
    ok, msg = verify_event_chain(forged)
    assert not ok
    assert "event 3" in msg

    # deleting an event breaks the chain where the gap appears
    gap = [lines[0], lines[2]]
    ok, msg = verify_event_chain(gap)
    assert not ok and "event 2" in msg

    # corrupted JSON is reported by line position
    broken_json = lines[:2] + ["{not json"] + lines[3:]
    ok, msg = verify_event_chain(broken_json)
    assert not ok and "event 3" in msg and "invalid JSON" in msg
