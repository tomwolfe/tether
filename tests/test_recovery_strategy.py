"""dogfood-24: nonlinear recovery (strategy + oscillation) and meta-trust.

Covers:
1. Configurable recovery strategy: cumulative (default) vs
   reset_to_checkpoint.
2. Oscillation detection over repeated failure signatures with automatic
   reset escalation and early abort.
(Reviewer credibility probing lives in test_review_gate.py.)
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tether.adapters.base import AgentAdapter, SessionInfo
from tether.audit import find_session_dir
from tether.mission import MissionError, load_mission
from tether.models import AgentState, RecoverySpec, TetherConfig
from tether.orchestrator import (
    Orchestrator,
    _OscillationDetector,
    _failure_signature,
)

PASS_CMD = f"{sys.executable} -c 'import sys; sys.exit(0)'"
FAIL_CMD = f"{sys.executable} -c 'import sys; sys.exit(1)'"

RESET_STRATEGY_YAML = (
    "recovery:\n"
    "  strategy: reset_to_checkpoint\n"
)


# ------------------------------- step 1: config plumbing


def test_recovery_spec_defaults_to_cumulative_and_accepts_both_values():
    assert RecoverySpec().strategy == "cumulative"
    assert RecoverySpec(strategy="cumulative").strategy == "cumulative"
    assert RecoverySpec(
        strategy="reset_to_checkpoint").strategy == "reset_to_checkpoint"


def test_recovery_spec_rejects_unknown_strategy():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        RecoverySpec(strategy="bogus")


def test_load_mission_parses_reset_to_checkpoint_strategy(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text(
        "mission:\n  name: m\n  goal: g\n"
        "recovery:\n  max_attempts: 2\n"
        "  strategy: reset_to_checkpoint\n"
    )
    spec = load_mission(p).recovery
    assert spec.strategy == "reset_to_checkpoint"
    assert spec.max_attempts == 2


def test_load_mission_defaults_strategy_to_cumulative(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("mission:\n  name: m\n  goal: g\n")
    assert load_mission(p).recovery.strategy == "cumulative"


def test_load_mission_rejects_unknown_strategy(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text(
        "mission:\n  name: m\n  goal: g\n"
        "recovery:\n  strategy: teleport\n"
    )
    with pytest.raises(MissionError):
        load_mission(p)


# ------------------------------- scripted adapter


class _TreeProbeAdapter(AgentAdapter):
    """Scripted adapter recording a probe file's content at every send.

    The execute send writes ``execute_files``; each repair send records the
    probe file's text as seen BEFORE writing ``repair_files`` (a dict for
    one-shot repairs or a list cycled per round), so tests can pin exactly
    what the tree looked like when the repair prompt was sent.
    """

    name = "tree-probe"
    verified = True

    def __init__(self, execute_files, repair_files, probe_file="f.txt"):
        super().__init__({})
        self.execute_files = dict(execute_files)
        if isinstance(repair_files, list):
            self.repair_sequence = list(repair_files)
        else:
            self.repair_sequence = [dict(repair_files)]
        self.probe_file = probe_file
        self.repair_prompts: list[str] = []
        self.repair_tree_states: list[str] = []
        self._planned = False
        self._executed = False

    def is_available(self):
        return True, ""

    def start_session(self, project_dir, session_id):
        self.project_dir = project_dir
        return SessionInfo(session_id=session_id, project_dir=project_dir)

    def _write(self, files):
        for rel, content in files.items():
            target = Path(self.project_dir) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    def send(self, prompt, session):
        if not self._planned:
            self._planned = True
            return AgentState(status="completed", logs="plan")
        if not self._executed:
            self._executed = True
            self._write(self.execute_files)
            return AgentState(status="completed", logs="done")
        probe = Path(self.project_dir) / self.probe_file
        try:
            self.repair_tree_states.append(probe.read_text())
        except OSError:
            self.repair_tree_states.append("<missing>")
        self.repair_prompts.append(prompt)
        self._write(self.repair_sequence[
            min(len(self.repair_prompts) - 1,
                len(self.repair_sequence) - 1)])
        return AgentState(status="completed", logs="repaired")

    def cancel(self, session):
        pass


def _git_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"],
                   cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"],
                   check=True)


def _committed_mission(tmp_path, body, name="m.yaml"):
    mp = tmp_path / name
    mp.write_text(body)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "mission"],
                   check=True)
    return mp


def _session_events(tmp_path, report):
    d = find_session_dir(tmp_path, ".tether/sessions", report["session_id"])
    return [json.loads(ln) for ln in
            (d / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if ln.strip()]


FAILING_MISSION = (
    "mission:\n  name: rec-strat\n  goal: g\n{extra}"
    f"verification:\n  commands:\n    - {FAIL_CMD}\nadapter: mock\n"
)


# ------------------------------- step 2: reset_to_checkpoint behavior


def test_reset_restores_checkpoint_tree_before_each_repair_send(tmp_path):
    _git_repo(tmp_path)
    mp = _committed_mission(
        tmp_path, FAILING_MISSION.format(extra="\n" + RESET_STRATEGY_YAML))
    adapter = _TreeProbeAdapter(
        execute_files={"f.txt": "changed by agent\n"},
        repair_files={"f.txt": "junk\n"})
    cfg = TetherConfig(audit_dir=".tether/sessions", max_attempts=3)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"
    # verification never passes; two repair rounds ran under max_attempts=3
    assert len(adapter.repair_prompts) == 2
    # before EACH repair send the tracked file was back at checkpoint state
    assert adapter.repair_tree_states == ["hello\n", "hello\n"]
    # forensic context reflects the clean post-reset tree
    assert "- (none)" in adapter.repair_prompts[0]
    assert "diff --git" not in adapter.repair_prompts[0]
    events = _session_events(tmp_path, report)
    resets = [e for e in events if e.get("kind") == "recovery_reset"]
    assert len(resets) == 2
    assert all(e["ok"] is True for e in resets)


def test_default_cumulative_keeps_dirty_state_and_never_resets(tmp_path):
    _git_repo(tmp_path)
    mp = _committed_mission(tmp_path, FAILING_MISSION.format(extra=""))
    # distinct repair artifacts -> distinct failure signatures -> the
    # oscillation guard never fires, pinning the pure cumulative default
    adapter = _TreeProbeAdapter(
        execute_files={"f.txt": "changed by agent\n"},
        repair_files=[{"fix-1.txt": "one\n"}, {"fix-2.txt": "two\n"}])
    cfg = TetherConfig(audit_dir=".tether/sessions", max_attempts=3)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"
    assert len(adapter.repair_prompts) == 2
    # byte-for-byte default guarantee: intermediate damage is preserved
    assert adapter.repair_tree_states == \
        ["changed by agent\n", "changed by agent\n"]
    assert adapter.repair_prompts[0] != adapter.repair_prompts[1]
    events = _session_events(tmp_path, report)
    assert not any(e.get("kind") == "recovery_reset" for e in events)


def test_non_git_reset_restores_from_backup_before_repair_send(tmp_path):
    mp = tmp_path / "m.yaml"
    mp.write_text(FAILING_MISSION.format(extra="\n" + RESET_STRATEGY_YAML))
    (tmp_path / "seed.txt").write_text("v1")
    adapter = _TreeProbeAdapter(
        execute_files={"seed.txt": "v2"},
        repair_files={"fix.txt": "attempted fix\n"},
        probe_file="seed.txt")
    cfg = TetherConfig(audit_dir=".tether/sessions",
                       backup_dir=".tether/backups", max_attempts=2)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"
    assert len(adapter.repair_prompts) == 1
    # the backup restore wiped the execute send's intermediate state
    assert adapter.repair_tree_states == ["v1"]
    events = _session_events(tmp_path, report)
    resets = [e for e in events if e.get("kind") == "recovery_reset"]
    assert len(resets) == 1
    assert resets[0]["method"] == "backup_restore"


def test_reset_rollback_failure_is_tolerated_and_recorded(
        tmp_path, monkeypatch):
    _git_repo(tmp_path)
    mp = _committed_mission(
        tmp_path, FAILING_MISSION.format(extra="\n" + RESET_STRATEGY_YAML))
    monkeypatch.setattr(
        "tether.orchestrator.git_rollback",
        lambda *args, **kwargs: (False, "boom: rollback refused"))
    adapter = _TreeProbeAdapter(
        execute_files={"f.txt": "changed by agent\n"},
        repair_files={"f.txt": "junk\n"})
    cfg = TetherConfig(audit_dir=".tether/sessions", max_attempts=2)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"
    # best-effort posture: the mission continues after a failed reset
    assert len(adapter.repair_prompts) == 1
    entry = report["recovery_attempts"][0]
    assert "rollback refused" in entry["reset_error"]
    events = _session_events(tmp_path, report)
    resets = [e for e in events if e.get("kind") == "recovery_reset"]
    assert resets and resets[0]["ok"] is False


def test_dry_run_never_resets_even_with_reset_strategy(tmp_path):
    _git_repo(tmp_path)
    mp = _committed_mission(
        tmp_path, FAILING_MISSION.format(extra="\n" + RESET_STRATEGY_YAML))
    adapter = _TreeProbeAdapter(execute_files={}, repair_files={})
    cfg = TetherConfig(audit_dir=".tether/sessions", max_attempts=2)
    report = Orchestrator(adapter, cfg, tmp_path).run(
        load_mission(mp), dry_run=True)
    assert report["status"] == "success"
    events = _session_events(tmp_path, report)
    assert not any(e.get("kind") == "recovery_reset" for e in events)


# ------------------------------- step 3: oscillation detection


def test_detector_triggers_only_on_repeated_signatures():
    d = _OscillationDetector()
    assert d.record("sig-a") is False
    assert d.record("sig-b") is False
    assert d.record("sig-a") is True   # repeat -> oscillation
    assert d.record("sig-b") is True   # its own repeat
    assert d.record("sig-c") is False


def test_detector_distinct_signatures_never_trigger():
    d = _OscillationDetector()
    for i in range(6):
        assert d.record(f"unique-{i}") is False


def test_detector_memory_is_bounded_by_distinct_signatures():
    d = _OscillationDetector()
    for i in range(50):
        d.record(f"s{i % 3}")
    assert len(d.counts) == 3


def test_failure_signature_normalizes_whitespace_but_not_content():
    assert _failure_signature("a\nb", ["x"]) == \
        _failure_signature(" a \n b \n", ["x"])
    assert _failure_signature("a", ["x"]) != _failure_signature("b", ["x"])
    assert _failure_signature("a", ["x"]) != _failure_signature("a", ["y"])
    assert _failure_signature("a", ["y", "x"]) == \
        _failure_signature("a", ["x", "y"])


def _always_failing_adapter():
    return _TreeProbeAdapter(
        execute_files={"f.txt": "changed by agent\n"},
        repair_files={"f.txt": "junk\n"})


def test_oscillation_auto_switches_cumulative_to_reset_then_aborts(tmp_path):
    _git_repo(tmp_path)
    mp = _committed_mission(tmp_path, FAILING_MISSION.format(extra=""))
    adapter = _always_failing_adapter()
    cfg = TetherConfig(audit_dir=".tether/sessions", max_attempts=4)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"
    events = _session_events(tmp_path, report)
    osc = [e for e in events if e.get("kind") == "oscillation_detected"]
    # detected on the repeat, then escalated once reset failed to break it
    assert len(osc) >= 2
    assert osc[0]["occurrences"] == 2 and osc[0]["escalated"] is False
    assert osc[-1]["occurrences"] == 3 and osc[-1]["escalated"] is True
    # the round right after detection performed a real reset despite the
    # cumulative config; round 1 ran cumulative as configured
    assert any(e.get("kind") == "recovery_reset" for e in events)
    assert adapter.repair_tree_states == ["changed by agent\n", "hello\n"]
    # ...and the second recurrence aborted early instead of burning budget
    assert len(adapter.repair_prompts) == 2
    last = report["recovery_attempts"][-1]
    assert last["failure_class"] == "oscillation_detected"
    assert any("oscillation" in s.lower() and "rollback" in s.lower()
               for s in report["next_steps"])


def test_configured_reset_mode_aborts_after_second_recurrence(tmp_path):
    _git_repo(tmp_path)
    mp = _committed_mission(
        tmp_path, FAILING_MISSION.format(extra="\n" + RESET_STRATEGY_YAML))
    adapter = _always_failing_adapter()
    cfg = TetherConfig(audit_dir=".tether/sessions", max_attempts=5)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"
    # every repair send started from the restored checkpoint tree
    assert adapter.repair_tree_states == ["hello\n", "hello\n"]
    # early abort: far fewer sends than the configured budget allows
    assert len(adapter.repair_prompts) < 4
    last = report["recovery_attempts"][-1]
    assert last["failure_class"] == "oscillation_detected"
    events = _session_events(tmp_path, report)
    assert any(
        e.get("kind") == "oscillation_detected" and e["escalated"] is True
        for e in events)
    assert any("oscillation" in s.lower() and "rollback" in s.lower()
               for s in report["next_steps"])


def test_alternating_failures_never_trigger_oscillation(tmp_path):
    _git_repo(tmp_path)
    mp = _committed_mission(tmp_path, FAILING_MISSION.format(extra=""))
    adapter = _TreeProbeAdapter(
        execute_files={"f.txt": "changed by agent\n"},
        repair_files=[
            {"fix-1.txt": "one\n"},
            {"fix-2.txt": "two\n"},
            {"fix-3.txt": "three\n"},
        ])
    cfg = TetherConfig(audit_dir=".tether/sessions", max_attempts=4)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"
    # all three recovery rounds ran to the normal attempt exhaustion
    assert len(adapter.repair_prompts) == 3
    events = _session_events(tmp_path, report)
    assert not any(e.get("kind") == "oscillation_detected" for e in events)
    assert not any(e.get("kind") == "recovery_reset" for e in events)
    assert report["recovery_attempts"][-1]["failure_class"] != \
        "oscillation_detected"
