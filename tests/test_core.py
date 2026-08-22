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
from tether.models import TetherConfig, VerificationResult
from tether.orchestrator import Orchestrator
from tether.adapters import resolve_adapter
from tether.verification import classify_failure, run_verification, summarize


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


# ------------------------------- dogfood-08: atomic writer lock hardening


def _write_json_lock(tmp_path, session_id, pid, created_at=None):
    import json
    import time as _time
    lock = tmp_path / ".tether" / "tether.lock"
    lock.parent.mkdir(exist_ok=True)
    payload = {"session_id": session_id, "pid": pid,
               "created_at": created_at if created_at is not None else _time.time()}
    lock.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return lock


def test_atomic_lock_contention_fails_second_writer(tmp_path):
    # A live holder (this test process owns the PID) blocks the second writer
    # even with an aggressively short staleness timeout, and its lock file is
    # left byte-for-byte intact.
    import json
    import os
    lock = _write_json_lock(tmp_path, "livesess1111", os.getpid())
    adapter = _RecordingAdapter(["completed"])
    cfg = TetherConfig(audit_dir=".tether/sessions", writer_lock_stale_seconds=1)
    report = Orchestrator(adapter, cfg, tmp_path).run(
        _write_mission(tmp_path, f"mission:\n  name: m\n  goal: g\n"
                                 f"verification:\n  commands:\n    - {PASS_CMD}\n"
                                 f"adapter: mock\n"))
    assert report["status"] == "failed"
    assert adapter.calls == []  # fails fast before any adapter interaction
    data = json.loads(lock.read_text())
    assert data["session_id"] == "livesess1111"  # not clobbered or rewritten


def test_dead_pid_lock_is_taken_over(tmp_path):
    # Lock whose owning process no longer exists is acquired immediately,
    # regardless of how fresh it is or how long the stale timeout is.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()  # ensure it is really gone
    lock = _write_json_lock(tmp_path, "deadsess2222", proc.pid)
    adapter = _RecordingAdapter(["completed"])
    report = _run_scripted(tmp_path, adapter)
    assert report["status"] == "success"
    assert "start_session" in adapter.calls
    assert not lock.exists()  # taken over, then released after the run


def test_lock_released_on_exception(tmp_path):
    # Whatever escapes _run_locked must propagate AND leave no lock behind.
    adapter = _RecordingAdapter(["completed"])
    mp = tmp_path / "m.yaml"
    mp.write_text(f"mission:\n  name: m\n  goal: g\nverification:\n"
                  f"  commands:\n    - {PASS_CMD}\nadapter: mock\n")
    orch = Orchestrator(adapter, TetherConfig(audit_dir=".tether/sessions"), tmp_path)
    original = Orchestrator._run_locked

    def boom(self, mission, allow_dirty, dry_run, started_at):
        raise RuntimeError("exploded inside the locked section")

    Orchestrator._run_locked = boom
    try:
        with pytest.raises(RuntimeError, match="exploded"):
            orch.run(load_mission(mp))
    finally:
        Orchestrator._run_locked = original
    assert not (tmp_path / ".tether" / "tether.lock").exists()


def test_release_never_removes_another_holders_lock(tmp_path):
    import json
    import time as _time

    def boom(self, mission, allow_dirty, dry_run, started_at):
        raise RuntimeError("still exploded")

    mp = tmp_path / "m.yaml"
    mp.write_text(f"mission:\n  name: m\n  goal: g\nverification:\n"
                  f"  commands:\n    - {PASS_CMD}\nadapter: mock\n")
    orch = Orchestrator(_RecordingAdapter(["completed"]),
                        TetherConfig(audit_dir=".tether/sessions"), tmp_path)
    original = Orchestrator._run_locked

    def steal_then_boom(self, *args):
        # Simulate our lock expiring and another session taking it over
        # while we were still inside the locked section.
        lock = tmp_path / ".tether" / "tether.lock"
        lock.write_text(json.dumps({
            "session_id": "newowner9999", "pid": 999999999,
            "created_at": _time.time(),
        }) + "\n", encoding="utf-8")
        raise RuntimeError("boom")

    Orchestrator._run_locked = steal_then_boom
    try:
        with pytest.raises(RuntimeError, match="boom"):
            orch.run(load_mission(mp))
    finally:
        Orchestrator._run_locked = original
    # the new owner's lock file survives our release path
    data = json.loads((tmp_path / ".tether" / "tether.lock").read_text())
    assert data["session_id"] == "newowner9999"


def test_legacy_plain_text_lock_still_recognized(tmp_path):
    # Locks written by older versions contain a bare session id; they must
    # still be honored as live when fresh and taken over when stale.
    lock = _write_lock(tmp_path, "legacysess01")
    report = _run_scripted(tmp_path, _RecordingAdapter(["completed"]))
    assert report["status"] == "failed"
    assert any("legacysess01" in s for s in report["next_steps"])
    import os
    import time as _time
    old = _time.time() - 13 * 3600
    os.utime(lock, (old, old))  # beyond the default 12h staleness timeout
    report2 = _run_scripted(tmp_path, _RecordingAdapter(["completed"]))
    assert report2["status"] == "success"


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


# --------------------------------------- dogfood-10: bounded context files


class _PromptRecorder(AgentAdapter):
    """Records every prompt sent; always completes."""

    name = "recorder"
    verified = True

    def __init__(self):
        super().__init__({})
        self.prompts: list[str] = []

    def is_available(self):
        return True, ""

    def start_session(self, project_dir, session_id):
        return SessionInfo(session_id=session_id, project_dir=project_dir)

    def send(self, prompt, session):
        self.prompts.append(prompt)
        return AgentState(status="completed", logs="ok")

    def cancel(self, session):
        pass


def _ctx_mission(tmp_path, entries):
    listing = "".join(f"  - '{e}'\n" for e in entries)
    p = tmp_path / "m.yaml"
    p.write_text(
        "mission:\n  name: ctx\n  goal: g\n"
        f"context_files:\n{listing}"
        f"verification:\n  commands:\n    - {PASS_CMD}\nadapter: mock\n",
        encoding="utf-8",
    )
    return load_mission(p)


def _run_ctx(tmp_path, mission, adapter=None, **cfg_kwargs):
    cfg = TetherConfig(audit_dir=".tether/sessions", **cfg_kwargs)
    adapter = adapter or _PromptRecorder()
    report = Orchestrator(adapter, cfg, tmp_path).run(mission)
    return report, adapter


def test_context_files_reach_plan_and_execute_prompts(tmp_path):
    (tmp_path / "notes.txt").write_text("CONTEXT-MARKER-ONE\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "guide.md").write_text("GUIDE-MARKER\n", encoding="utf-8")
    report, adapter = _run_ctx(
        tmp_path, _ctx_mission(tmp_path, ["notes.txt", "sub/guide.md"]))
    assert report["status"] == "success"
    assert len(adapter.prompts) == 2  # plan + execute
    for prompt in adapter.prompts:
        assert "Context file: notes.txt" in prompt
        assert "<<<BEGIN notes.txt>>>\nCONTEXT-MARKER-ONE" in prompt
        assert "<<<END notes.txt>>>" in prompt
        assert "GUIDE-MARKER" in prompt
    # contents also reach the stored audit prompts
    session = find_session_dir(tmp_path, ".tether/sessions",
                               report["session_id"])
    stored = "".join(p.read_text(encoding="utf-8")
                     for p in sorted((session / "prompts").glob("*.txt")))
    assert "CONTEXT-MARKER-ONE" in stored


def test_context_files_too_many_fails_before_execution(tmp_path):
    entries = []
    for i in range(33):  # limit is 32
        rel = f"f{i:02d}.txt"
        (tmp_path / rel).write_text("x", encoding="utf-8")
        entries.append(rel)
    report, adapter = _run_ctx(tmp_path, _ctx_mission(tmp_path, entries))
    assert report["status"] == "failed"
    assert adapter.prompts == []  # aborted before any adapter interaction
    assert report["verification_results"] == []
    assert any("limit is 32 files" in s for s in report["next_steps"])


def test_context_files_oversized_file_fails(tmp_path):
    (tmp_path / "big.txt").write_text("x" * (256 * 1024 + 1), encoding="utf-8")
    report, adapter = _run_ctx(tmp_path, _ctx_mission(tmp_path, ["big.txt"]))
    assert report["status"] == "failed"
    assert adapter.prompts == []
    assert any("per-file limit is 262144 bytes" in s
               for s in report["next_steps"])


def test_context_files_total_limit_fails(tmp_path):
    # each file under the per-file cap, together over the 512 KiB total
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text("x" * (200 * 1024), encoding="utf-8")
    report, adapter = _run_ctx(
        tmp_path, _ctx_mission(tmp_path, ["a.txt", "b.txt", "c.txt"]))
    assert report["status"] == "failed"
    assert adapter.prompts == []
    assert any("total 614400 bytes" in s and "limit is 524288 bytes" in s
               for s in report["next_steps"])


def test_context_files_binary_refused(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"A" * 100 + b"\x00" + b"B" * 10)
    report, adapter = _run_ctx(tmp_path, _ctx_mission(tmp_path, ["blob.bin"]))
    assert report["status"] == "failed"
    assert adapter.prompts == []
    assert any("binary" in s for s in report["next_steps"])


@pytest.mark.parametrize("entry,reason", [
    ("../outside.txt", "escapes the project directory"),
    ("/etc/hostname", "must be relative"),
])
def test_context_files_path_escapes_refused(tmp_path, entry, reason):
    # the target exists outside the project dir: rejection must be path policy,
    # not a missing file
    (tmp_path.parent / "outside.txt").write_text("out there", encoding="utf-8")
    report, adapter = _run_ctx(tmp_path, _ctx_mission(tmp_path, [entry]))
    assert report["status"] == "failed"
    assert adapter.prompts == []
    assert any(reason in s for s in report["next_steps"])


def test_context_files_missing_file_is_error_not_warning(tmp_path):
    report, adapter = _run_ctx(tmp_path, _ctx_mission(tmp_path, ["ghost.md"]))
    assert report["status"] == "failed"
    assert adapter.prompts == []
    assert any("context file not found: ghost.md" in s
               for s in report["next_steps"])


def test_validate_mission_checks_structure_only(tmp_path):
    # existence/size/binary are run-time checks against the target project;
    # validate-mission only enforces the list-of-strings structure.
    p = tmp_path / "m.yaml"
    p.write_text("mission:\n  name: x\n  goal: y\n"
                 "context_files:\n  - ghost.md\n")
    m = load_mission(p)
    assert m.context_files == ["ghost.md"]


@pytest.mark.parametrize("bad", [
    "mission:\n  name: x\n  goal: y\ncontext_files: not-a-list\n",
    "mission:\n  name: x\n  goal: y\ncontext_files: ['a', 42]\n",
])
def test_context_files_structural_errors_raise_mission_error(tmp_path, bad):
    p = tmp_path / "bad.yaml"
    p.write_text(bad)
    with pytest.raises(MissionError):
        load_mission(p)


def test_context_files_redacted_when_redact_prompts_enabled(tmp_path):
    # marker sits beyond the 64-char head/tail excerpts kept by redact_body
    body = "F" * 100 + "TOPSECRET-CONTEXT-BODY" + "G" * 100
    (tmp_path / "sec.txt").write_text(body + "\n", encoding="utf-8")
    report, adapter = _run_ctx(tmp_path, _ctx_mission(tmp_path, ["sec.txt"]),
                               redact_prompts=True)
    assert report["status"] == "success"
    # delivered prompts carry the redacted form only
    assert all("TOPSECRET-CONTEXT-BODY" not in p for p in adapter.prompts)
    assert all("[REDACTED sha256=" in p for p in adapter.prompts)
    # audit-stored prompts carry no raw content either
    session = find_session_dir(tmp_path, ".tether/sessions",
                               report["session_id"])
    stored = "".join(p.read_text(encoding="utf-8")
                     for p in sorted((session / "prompts").glob("*.txt")))
    assert "TOPSECRET-CONTEXT-BODY" not in stored
    assert "[REDACTED sha256=" in stored


def test_context_files_audit_event_lists_paths_and_sizes(tmp_path):
    body = "hello context"  # 13 bytes
    (tmp_path / "notes.txt").write_text(body, encoding="utf-8")
    report, _ = _run_ctx(tmp_path, _ctx_mission(tmp_path, ["notes.txt"]))
    assert report["status"] == "success"
    session = find_session_dir(tmp_path, ".tether/sessions",
                               report["session_id"])
    events = [json.loads(line) for line in
              (session / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    ev = next(e for e in events if e["kind"] == "context_files")
    assert ev["files"] == [{"path": "notes.txt", "bytes": len(body)}]
    assert ev["total_bytes"] == len(body)


# ------------------------------------- verification artifacts (dogfood-11)


from tether.verification import check_artifacts, summarize_artifacts  # noqa: E402


def _artifact_mission(tmp_path, artifacts, commands=None, max_attempts=2):
    cmds = [PASS_CMD] if commands is None else list(commands)
    cmd_lines = "".join(f"    - {json.dumps(c)}\n" for c in cmds)
    art_lines = "".join(f"    - '{a}'\n" for a in artifacts)
    p = tmp_path / "m.yaml"
    p.write_text(
        "mission:\n  name: art\n  goal: g\n"
        "verification:\n  commands:\n"
        f"{cmd_lines}"
        f"  artifacts:\n{art_lines}"
        f"recovery:\n  max_attempts: {max_attempts}\n"
        "adapter: mock\n",
        encoding="utf-8",
    )
    return load_mission(p)


def _run_artifacts(tmp_path, mission, scenario="success", dry_run=False):
    adapter = resolve_adapter("mock", {"mock": {"scenario": scenario}})
    cfg = TetherConfig(audit_dir=".tether/sessions",
                       max_attempts=mission.recovery.max_attempts or 3,
                       dry_run=dry_run)
    return Orchestrator(adapter, cfg, tmp_path).run(mission)


def test_mission_artifacts_parse_as_list_of_strings(tmp_path):
    m = _artifact_mission(tmp_path, ["docs/SECURITY.md", "src/**/*.py"])
    assert m.verification.artifacts == ["docs/SECURITY.md", "src/**/*.py"]
    assert m.verification.commands == [PASS_CMD]


@pytest.mark.parametrize("bad", [
    "mission:\n  name: x\n  goal: y\nverification:\n  artifacts: not-a-list\n",
    "mission:\n  name: x\n  goal: y\nverification:\n  artifacts: ['a', 42]\n",
])
def test_artifacts_structural_errors_raise_mission_error(tmp_path, bad):
    p = tmp_path / "bad.yaml"
    p.write_text(bad)
    with pytest.raises(MissionError):
        load_mission(p)


def test_existing_artifact_passes_and_report_includes_entry(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "SECURITY.md").write_text("trust but verify\n")
    report = _run_artifacts(tmp_path,
                            _artifact_mission(tmp_path, ["docs/SECURITY.md"],
                                              max_attempts=3))
    assert report["status"] == "success"
    entries = report["verification_results"]
    # command results and the artifact entry sit alongside each other
    assert any("command" in e for e in entries)
    artifact_entries = [e for e in entries if "pattern" in e]
    assert artifact_entries == [{
        "pattern": "docs/SECURITY.md",
        "matched_files": ["docs/SECURITY.md"],
        "passed": True,
        "detail": "",
    }]
    session = find_session_dir(tmp_path, ".tether/sessions",
                               report["session_id"])
    saved = json.loads(
        (session / "verification" / "attempt-01.json").read_text())
    assert any(e.get("pattern") == "docs/SECURITY.md" for e in saved)


def test_missing_artifact_fails_green_attempt_and_recovery_runs(tmp_path):
    report = _run_artifacts(
        tmp_path,
        _artifact_mission(tmp_path, ["docs/ghost.md"], max_attempts=2),
    )
    assert report["status"] == "failed"
    # attempt 1 was green on commands, so recovery ran over the missing file
    assert len(report["recovery_attempts"]) == 1
    reason = report["recovery_attempts"][0]["failing_output"]
    assert "missing required artifacts" in reason
    assert "docs/ghost.md" in reason
    last_entry = report["verification_results"][-1]
    assert last_entry["pattern"] == "docs/ghost.md"
    assert last_entry["matched_files"] == [] and last_entry["passed"] is False
    assert any("missing required artifacts" in s and "docs/ghost.md" in s
               for s in report["next_steps"])


def test_verification_command_can_satisfy_artifact_during_recovery(tmp_path):
    creator = py_cmd('from pathlib import Path; '
                     'Path("docs").mkdir(exist_ok=True); '
                     'Path("docs/SECURITY.md").write_text("s")')
    report = _run_artifacts(
        tmp_path,
        _artifact_mission(tmp_path, ["docs/SECURITY.md"], commands=[creator],
                          max_attempts=3),
        scenario="fail_then_succeed",
    )
    assert report["status"] == "success"
    assert len(report["recovery_attempts"]) == 1


def test_artifact_glob_patterns_match(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("x")
    (docs / "b.txt").write_text("x")
    report = _run_artifacts(tmp_path,
                            _artifact_mission(tmp_path, ["docs/*.md"]))
    assert report["status"] == "success"
    entry = next(e for e in report["verification_results"] if "pattern" in e)
    assert entry["passed"] is True
    assert entry["matched_files"] == ["docs/a.md"]


def test_failing_commands_skip_the_artifact_gate(tmp_path):
    report = _run_artifacts(
        tmp_path,
        _artifact_mission(tmp_path, ["docs/ghost.md"], commands=[FAIL_CMD],
                          max_attempts=2),
    )
    assert report["status"] == "failed"
    reason = report["recovery_attempts"][0]["failing_output"]
    assert "exit code 1" in reason
    assert "missing required artifacts" not in reason


def test_dry_run_does_not_enforce_artifacts(tmp_path):
    report = _run_artifacts(
        tmp_path, _artifact_mission(tmp_path, ["docs/ghost.md"]), dry_run=True)
    assert report["status"] == "success"
    entry = next(e for e in report["verification_results"] if "pattern" in e)
    assert entry["passed"] is True and "dry-run" in entry["detail"]


def test_check_artifacts_matches_files_and_skips_tether_dir(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x")
    tether_dir = tmp_path / ".tether" / "sessions" / "s"
    tether_dir.mkdir(parents=True)
    (tether_dir / "report.json").write_text("{}")

    results = check_artifacts(["docs/*.md", "*.json", "nope.bin"], tmp_path)
    by_pattern = {r.pattern: r for r in results}
    assert by_pattern["docs/*.md"].passed is True
    assert by_pattern["docs/*.md"].matched_files == ["docs/a.md"]
    # tether's own audit output never satisfies a deliverable
    assert by_pattern["*.json"].passed is False
    assert by_pattern["*.json"].matched_files == []
    assert by_pattern["nope.bin"].passed is False
    ok, message = summarize_artifacts(results)
    assert not ok
    assert "*.json" in message and "nope.bin" in message


# -------------------------------- failure classification (dogfood-14 task 1)


def _res(**kwargs):
    defaults = dict(command="cmd", exit_code=1, stdout="", stderr="",
                    timed_out=False)
    defaults.update(kwargs)
    return VerificationResult(**defaults)


@pytest.mark.parametrize("results,expected", [
    # timeout wins over every other signal
    ([_res(timed_out=True)], "timeout"),
    ([_res(stderr="error: bad"), _res(timed_out=True)], "timeout"),
    # missing binary: "not found" in stderr (exit code may be None)
    ([_res(exit_code=None, stderr="ruff: command not found")],
     "missing_binary"),
    ([_res(stderr="binary not found: nope")], "missing_binary"),
    # compile errors on stderr
    ([_res(stderr="src/x.py:1: error: bad indent")], "compile_error"),
    ([_res(stderr="SyntaxError: invalid syntax")], "compile_error"),
    ([_res(stderr="TypeError: 'int' object is not callable")],
     "compile_error"),
    ([_res(stderr="ImportError: cannot import name x")], "compile_error"),
    ([_res(stderr="fatal error: cannot find module 'x'")], "compile_error"),
    ([_res(stderr="No such file or directory: f.py")], "compile_error"),
    # test failures via stdout/stderr markers
    ([_res(stdout="FAILED tests/test_a.py::test_b")], "test_failure"),
    ([_res(stderr="E       assert 1 == 2")], "test_failure"),
    ([_res(stdout="AssertionError: boom")], "test_failure"),
    ([_res(stdout="test_x.py .")], "test_failure"),
    # nothing matches -> unknown
    ([_res(stdout="boom", stderr="")], "unknown"),
])
def test_classify_failure_classes(results, expected):
    assert classify_failure(results) == expected


def test_classify_failure_all_passing_is_unknown():
    ok = VerificationResult(command="ok", exit_code=0, stdout="", passed=True)
    assert classify_failure([ok]) == "unknown"
    assert classify_failure([]) == "unknown"


def test_classify_failure_priority_order():
    # compile-style output plus a timeout -> timeout wins
    assert classify_failure(
        [_res(stderr="error: syntax"), _res(timed_out=True)]) == "timeout"
    # missing binary beats compile-style stderr seen elsewhere
    assert classify_failure([
        _res(stderr="error: x"),
        _res(exit_code=None, stderr="pytest: command not found"),
    ]) == "missing_binary"
    # compile beats test markers when both appear for the same result
    assert classify_failure(
        [_res(stderr="error: line 3", stdout="FAILED test_z")]
    ) == "compile_error"


# --------------------- cross-session analytics and retention (task 2/3)


def _fabricate_session(root, stamp, sid, status, adapter="mock",
                       mission_name="m", attempts=1, recovery=0,
                       failing_cmds=()):
    d = root / f"{stamp}-{mission_name}-{sid[:8]}"
    (d / "verification").mkdir(parents=True)
    for i in range(attempts):
        (d / "verification" / f"attempt-{i + 1:02d}.json").write_text("[]")
    report = {
        "session_id": sid,
        "mission_name": mission_name,
        "adapter": adapter,
        "status": status,
        "verification_results": [
            {"command": c, "passed": False} for c in failing_cmds
        ],
        "recovery_attempts": [{"attempt": i} for i in range(recovery)],
        "next_steps": [],
    }
    (d / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return d


def _stats_runner(tmp_path):
    from typer.testing import CliRunner
    from tether.cli import app
    return CliRunner(), app


def test_sessions_stats_counts_percentages_and_rates(tmp_path):
    root = tmp_path / ".tether" / "sessions"
    root.mkdir(parents=True)
    _fabricate_session(root, "20260801-000000", "aaaa11111111", "success",
                       attempts=1)
    _fabricate_session(root, "20260802-000000", "bbbb22222222", "failed",
                       attempts=3, recovery=1, failing_cmds=["pytest -q"])
    _fabricate_session(root, "20260803-000000", "cccc33333333", "success",
                       adapter="opencode", attempts=2, recovery=1)
    _fabricate_session(root, "20260804-000000", "dddd44444444", "cancelled",
                       adapter="opencode", attempts=1, recovery=1,
                       failing_cmds=["pytest -q"])
    runner, app = _stats_runner(tmp_path)
    r = runner.invoke(app, ["sessions", "stats",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    out = r.output
    assert "4 total" in out
    assert "success: 2 (50.0%)" in out
    assert "failed: 1 (25.0%)" in out
    assert "cancelled: 1 (25.0%)" in out
    assert "median 1.5, max 3" in out  # attempts: 1,3,2,1
    assert "33.3%" in out  # recovery successes (1) / recovery sessions (3)
    assert out.count("pytest -q") >= 1  # most common failing command listed
    assert "mock: 2 session(s), success rate 50.0%" in out
    assert "opencode: 2 session(s), success rate 50.0%" in out


def test_sessions_stats_json_flag_emits_single_object(tmp_path):
    root = tmp_path / ".tether" / "sessions"
    root.mkdir(parents=True)
    _fabricate_session(root, "20260801-000000", "aaaa11111111", "success")
    _fabricate_session(root, "20260802-000000", "bbbb22222222", "failed",
                       attempts=3, recovery=1, failing_cmds=["mypy src"])
    runner, app = _stats_runner(tmp_path)
    r = runner.invoke(app, ["sessions", "stats", "--json",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)  # single parseable JSON object
    assert data["total_sessions"] == 2
    assert data["statuses"]["success"] == {"count": 1, "pct": 50.0}
    assert data["statuses"]["failed"]["count"] == 1
    assert data["statuses"]["cancelled"] == {"count": 0, "pct": 0.0}
    assert data["attempts"] == {"median": 2.0, "max": 3}
    assert data["recovery"] == {"sessions_with_recovery_attempts": 1,
                                "recoveries_ending_in_success": 0,
                                "success_rate_pct": 0.0}
    assert data["top_failing_commands"] == [{"command": "mypy src",
                                             "count": 1}]
    assert data["adapters"]["mock"] == {"count": 2, "success_rate_pct": 50.0}


def test_sessions_stats_empty_audit_dir(tmp_path):
    runner, app = _stats_runner(tmp_path)
    r = runner.invoke(app, ["sessions", "stats",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "No sessions found." in r.output


def test_parse_older_than_accepts_minutes_hours_days():
    from tether.cli import _parse_older_than
    assert _parse_older_than("15m") == 15 * 60
    assert _parse_older_than("24h") == 24 * 3600
    assert _parse_older_than("30d") == 30 * 86400
    with pytest.raises(ValueError):
        _parse_older_than("1w")
    with pytest.raises(ValueError):
        _parse_older_than("abc")


def test_sessions_clean_requires_confirm_and_deletes_old_only(tmp_path):
    import os
    import time as _time
    root = tmp_path / ".tether" / "sessions"
    old = _fabricate_session(root, "20260101-000000", "aaaa11111111",
                             "failed")
    fresh = _fabricate_session(root, "20260602-000000", "bbbb22222222",
                               "success")
    now = _time.time()
    os.utime(old, (now - 3 * 3600, now - 3 * 3600))  # 3h old
    os.utime(fresh, (now - 60, now - 60))            # 1min old
    runner, app = _stats_runner(tmp_path)

    # Without --confirm nothing is deleted.
    r = runner.invoke(app, ["sessions", "clean", "--older-than", "1h",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert f"Would delete: {old}" in r.output
    assert fresh.name not in r.output.split("Would delete")[1]
    assert "Dry run" in r.output
    assert old.exists() and fresh.exists()

    # With --confirm the old directory is gone; the recent one remains.
    r = runner.invoke(app, ["sessions", "clean", "--older-than", "1h",
                            "--confirm", "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert not old.exists()
    assert fresh.exists()


def test_sessions_clean_falls_back_to_retention_days_config(tmp_path):
    import os
    import time as _time
    (tmp_path / "tether.yaml").write_text("retention_days: 1\n",
                                          encoding="utf-8")
    root = tmp_path / ".tether" / "sessions"
    old = _fabricate_session(root, "20260101-000000", "aaaa11111111",
                             "success")
    fresh = _fabricate_session(root, "20260602-000000", "bbbb22222222",
                               "success")
    now = _time.time()
    os.utime(old, (now - 25 * 3600, now - 25 * 3600))  # beyond 1 day
    os.utime(fresh, (now - 3600, now - 3600))
    runner, app = _stats_runner(tmp_path)
    r = runner.invoke(app, ["sessions", "clean", "--confirm",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert not old.exists() and fresh.exists()


def test_retention_days_config_default_is_none(tmp_path):
    cfg = resolve_config(tmp_path)
    assert cfg.retention_days is None
    cfg = resolve_config(tmp_path, cli_overrides={"retention_days": 7})
    assert cfg.retention_days == 7


def test_sessions_clean_without_any_threshold_errors(tmp_path):
    runner, app = _stats_runner(tmp_path)
    r = runner.invoke(app, ["sessions", "clean",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 1
    assert "retention_days" in r.output
