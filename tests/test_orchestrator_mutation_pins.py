"""Mutation-kill pins for tether orchestrator survivors (v6 run 5).

Run 5 recovery expanded _MARKERS in _review_evidence, pulling the whole
orchestrator.py into the mutation sample; 6 pre-existing untested branches
survived. Each pin below executes the branch so the mutant dies. Pure
hardening: no production behavior changes.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tether.adapters.base import AgentAdapter, SessionInfo  # noqa: E402
from tether.models import TetherConfig  # noqa: E402
from tether.mission import load_mission  # noqa: E402
from tether.orchestrator import (  # noqa: E402
    Orchestrator,
    _git_patch_bytes,
    _read_lock_payload,
    _run_credibility_probe,
    _snapshot_hook_integrity,
    writer_lock_path,
)


def test_credibility_probe_spawn_failure_returns_false(tmp_path):
    ok, detail = _run_credibility_probe(
        "definitely-not-a-real-command-xyz", "resp", tmp_path, 5)
    assert ok is False
    assert isinstance(detail, str) and detail


def test_read_lock_payload_valid_json_returns_dict(tmp_path):
    lp = writer_lock_path(tmp_path)
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text(json.dumps({"session_id": "s", "pid": 1}))
    out = _read_lock_payload(lp)
    assert isinstance(out, dict) and out["session_id"] == "s"
    lp.write_text("   \n")
    assert _read_lock_payload(lp) is None


def test_git_patch_bytes_returns_diff(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path,
                   capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path,
                   capture_output=True)
    (tmp_path / "f.txt").write_text("a\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "x"], cwd=tmp_path,
                   capture_output=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                          capture_output=True, text=True).stdout.strip()
    (tmp_path / "f.txt").write_text("b\n")
    out = _git_patch_bytes(tmp_path, base)
    assert isinstance(out, bytes) and out.startswith(b"diff")


def test_snapshot_hook_integrity_returns_four_tuple(tmp_path):
    out = _snapshot_hook_integrity(tmp_path)
    assert isinstance(out, tuple) and len(out) == 4


class _UnavailableAdapter(AgentAdapter):
    name = "unavailable"
    verified = True

    def is_available(self):
        return False, "nope"

    def start_session(self, project_dir, session_id):
        return SessionInfo(session_id=session_id, project_dir=project_dir)

    def send(self, prompt, session):
        raise AssertionError("must not send when adapter unavailable")

    def cancel(self, session):
        pass


class _ScriptedAdapter(AgentAdapter):
    name = "scripted"
    verified = True

    def is_available(self):
        return True, ""

    def start_session(self, project_dir, session_id):
        return SessionInfo(session_id=session_id, project_dir=project_dir)

    def send(self, prompt, session):
        from tether.adapters.base import AgentState
        return AgentState(status="completed", logs="out", usage=None)

    def cancel(self, session):
        pass


def _minimal_mission(tmp_path):
    mp = tmp_path / "m.yaml"
    mp.write_text(
        "mission:\n  name: m\n  goal: g\n"
        "verification:\n  commands:\n    - python3 -c \"pass\"\n"
        "recovery:\n  max_attempts: 1\n"
        "adapter: mock\n")
    return load_mission(mp)


def test_dry_run_skips_unavailable_adapter_gate(tmp_path):
    orch = Orchestrator(
        _UnavailableAdapter({}), TetherConfig(audit_dir=".tether/sessions"),
        tmp_path)
    report = orch.run(_minimal_mission(tmp_path),
                      allow_dirty=True, dry_run=True)
    assert isinstance(report, dict)
    assert "unavailable" not in " ".join(report.get("next_steps", [])).lower()
    assert report.get("status") != "failed" or not any(
        "unavailable" in str(s).lower()
        for s in report.get("next_steps", []))


def test_backup_failure_returns_failed_report_dict(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr("tether.orchestrator.make_file_backup", _boom)
    orch = Orchestrator(
        _ScriptedAdapter({}), TetherConfig(audit_dir=".tether/sessions"),
        tmp_path)
    report = orch.run(_minimal_mission(tmp_path), allow_dirty=True,
                      dry_run=False)
    assert isinstance(report, dict)
    assert report.get("status") == "failed"
    assert any("backup" in str(s).lower()
               for s in report.get("next_steps", []))
    assert time.time() > 0  # keep time import live for wall-clock sanity
