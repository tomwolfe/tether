"""Git-state guard (dogfood-41): detect agent-driven history/ref surgery.

Session 7f460335 live fire proved the blind spot: the nested agent ran
`git reset` mid-mission, moving the user's branch pointer — invisible to
the path-based write sandbox, corrupting capture bases and post-mission
repo state. These tests pin the contract:

- `mission.git_state_guard: true` makes the post-send gate verify that
  HEAD still equals the checkpointed original_head and that the session's
  checkpoint ref still resolves to it; any drift fails the mission
  immediately and skips verification, exactly like a sandbox violation.
- Default (unset/None) is byte-identical legacy behavior: no checks, no
  new report keys, no new events.

The ON-case tests FAIL against the current code (red acceptance suite);
the OFF/inert cases are regression pins that must stay green.
"""
import json
import subprocess
import sys
from pathlib import Path

from tether.adapters.base import AgentAdapter, SessionInfo
from tether.audit import find_session_dir
from tether.mission import load_mission
from tether.models import AgentState, TetherConfig
from tether.orchestrator import Orchestrator

PY_PASS = f"{sys.executable} -c 'import sys; sys.exit(0)'"


def _git_repo_two_commits(project: Path) -> None:
    """Two commits with IDENTICAL trees: mixed reset moves only the ref."""
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"],
                   cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=project,
                   check=True)
    (project / "app.py").write_text("def value():\n    return 1\n",
                                    encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "c1"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "c2", "--allow-empty"],
                   cwd=project, check=True)


def _commit_mission(project: Path, name: str, text: str) -> None:
    (project / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", name], cwd=project, check=True)


class Surgeon(AgentAdapter):
    """Completed sends that run scripted git commands in the project."""

    name = "surgeon"
    verified = True

    def __init__(self, argv_per_send):
        super().__init__({})
        self.argv_per_send = argv_per_send
        self.send_count = 0

    def is_available(self):
        return True, ""

    def start_session(self, project_dir, session_id):
        return SessionInfo(session_id=session_id, project_dir=project_dir)

    def send(self, prompt, session):
        self.send_count += 1
        for argv in self.argv_per_send(self.send_count):
            subprocess.run(argv, cwd=str(session.project_dir),
                           capture_output=True, check=False, shell=False)
        return AgentState(status="completed", logs="out")

    def cancel(self, session):
        pass


def _always(*argvs):
    return lambda n: list(argvs)


def _mission_text(extra="", max_attempts=1):
    return (
        "mission:\n  name: gsg\n  goal: g\n"
        "verification:\n"
        f"  commands:\n    - {PY_PASS}\n"
        f"{extra}"
        f"recovery:\n  max_attempts: {max_attempts}\nadapter: mock\n"
    )


def _run(project: Path, adapter: AgentAdapter, mission: str, **cfg):
    config = TetherConfig(audit_dir=".tether/sessions", **cfg)
    return Orchestrator(adapter, config, project).run(
        load_mission(project / mission))


def _events(project: Path, report: dict) -> list[dict]:
    session = find_session_dir(
        project, ".tether/sessions", report["session_id"])
    return [json.loads(line) for line in
            (session / "events.jsonl").read_text("utf-8").splitlines()
            if line.strip()]


# ------------------------------------------------- default OFF (legacy pin)


def test_default_off_head_reset_stays_legacy_behavior(tmp_path):
    # Unset guard: a mixed reset (identical tree, moved branch) is invisible
    # to every existing mechanism and the mission succeeds exactly as
    # before — no new keys, no new events. Byte-identical contract.
    _git_repo_two_commits(tmp_path)
    _commit_mission(tmp_path, "m.yaml", _mission_text())
    surgeon = Surgeon(_always(["git", "reset", "-q", "HEAD~1"]))
    report = _run(tmp_path, surgeon, "m.yaml")
    assert report["status"] == "success", report["next_steps"]
    assert "git_state_violations" not in report
    assert all(e.get("kind") != "git_state_violations"
               for e in _events(tmp_path, report))


# --------------------------------------- enabled: fail closed on surgery


def test_enabled_head_move_fails_mission_and_skips_verification(tmp_path):
    _git_repo_two_commits(tmp_path)
    _commit_mission(tmp_path, "m.yaml",
                    _mission_text("git_state_guard: true\n"))
    surgeon = Surgeon(_always(["git", "reset", "-q", "HEAD~1"]))
    report = _run(tmp_path, surgeon, "m.yaml")
    assert report["status"] == "failed"
    assert report["verification_results"] == []  # skipped, never trusted
    violations = report["git_state_violations"]
    assert violations and any("HEAD" in v for v in violations)
    events = [e for e in _events(tmp_path, report)
              if e.get("kind") == "git_state_violations"]
    assert events and events[-1]["violations"]
    assert any("tether rollback" in s for s in report["next_steps"])


def test_enabled_checkpoint_ref_deletion_fails_closed(tmp_path):
    _git_repo_two_commits(tmp_path)
    _commit_mission(tmp_path, "m.yaml",
                    _mission_text("git_state_guard: true\n"))

    def surgery(n):
        # Delete every tether checkpoint ref regardless of session id.
        refs = subprocess.run(
            ["git", "for-each-ref", "refs/tether/checkpoint/",
             "--format=%(refname)"],
            cwd=tmp_path, capture_output=True, text=True, check=True).stdout
        out = []
        for ref in refs.splitlines():
            ref = ref.strip()
            if ref:
                out.append(["git", "update-ref", "-d", ref])
        return out

    report = _run(tmp_path, Surgeon(surgery), "m.yaml")
    assert report["status"] == "failed"
    assert report["verification_results"] == []
    violations = report["git_state_violations"]
    assert violations and any("checkpoint" in v.lower() for v in violations)


# --------------------------------------------- no false positives


def test_enabled_innocent_agent_still_succeeds(tmp_path):
    # Guard must never punish legitimate work: writes land normally.
    _git_repo_two_commits(tmp_path)
    _commit_mission(tmp_path, "m.yaml",
                    _mission_text("git_state_guard: true\n"))
    planter = Surgeon(lambda n: [])
    report = _run(tmp_path, planter, "m.yaml")
    assert report["status"] == "success", report["next_steps"]
    assert "git_state_violations" not in report


def test_enabled_survives_reset_to_checkpoint_recovery_strategy(tmp_path):
    # reset_to_checkpoint legitimately restores the checkpoint state before
    # every repair send; the guard must see HEAD == original_head afterwards
    # and never trip. Real two-round flow: verification fails on attempt 1
    # (marker absent), the repair send creates the marker, attempt 2 green.
    _git_repo_two_commits(tmp_path)
    flip = (f"{sys.executable} -c \"import pathlib,sys; "
            "sys.exit(0 if pathlib.Path('fixed.marker').exists() else 3)\"")
    text = (
        "mission:\n  name: gsg\n  goal: g\n"
        "verification:\n"
        f"  commands:\n    - {flip}\n"
        "git_state_guard: true\n"
        "recovery:\n"
        "  max_attempts: 2\n"
        "  strategy: reset_to_checkpoint\nadapter: mock\n")
    _commit_mission(tmp_path, "m.yaml", text)

    class MarkerWriter(Surgeon):
        def send(self, prompt, session):
            self.send_count += 1
            if self.send_count >= 3:  # 1=planning, 2=execution, 3+=repair
                (Path(session.project_dir) / "fixed.marker").write_text(
                    "x", encoding="utf-8")
            return AgentState(status="completed", logs="out")

    report = _run(tmp_path, MarkerWriter(_always()), "m.yaml")
    assert report["status"] == "success", report["next_steps"]
    assert len(report["recovery_attempts"]) == 1
    assert all(e.get("kind") != "git_state_violations"
               for e in _events(tmp_path, report))


def test_enabled_non_git_project_is_inert(tmp_path):
    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    mp = tmp_path / "m.yaml"
    mp.write_text(_mission_text("git_state_guard: true\n"),
                  encoding="utf-8")
    config = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(Surgeon(lambda n: []), config, tmp_path).run(
        load_mission(mp))
    # Plain missions succeed on non-git projects (tar backup path); the
    # guard must stay completely inert there.
    assert report["status"] == "success", report["next_steps"]
    assert "git_state_violations" not in report


# ------------------------------------------------------------- model truth


def test_mission_contract_field_defaults_to_none():
    from tether.models import MissionContract
    field = MissionContract.model_fields["git_state_guard"]
    assert field.default is None
