"""Clean-room verification (dogfood-23): false-green closure end-to-end,
materializer semantics, fail-closed orchestration, and contract validation."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import tether.orchestrator as orch_module
from tether.adapters.base import AgentAdapter, SessionInfo
from tether.audit import find_session_dir
from tether.cleanroom import CleanRoomError, materialize_clean_room
from tether.mission import MissionError, load_mission
from tether.models import AgentState, TetherConfig
from tether.orchestrator import Orchestrator
from tether.verification import run_mutation_testing

PYTEST_CMD = f"{sys.executable} -m pytest -q -p no:cacheprovider"

# Planted gitignored helper that games the failing test in-tree.
# conftest.py stands in for this whole class of plants (sitecustomize.py,
# tox.ini overrides, ...): it is what verification tools actually auto-load
# from an untracked project root under `python -m pytest`.
CONFTEST = "import app\napp.value = lambda: 2\n"


def py_pass() -> str:
    return f"{sys.executable} -c 'import sys; sys.exit(0)'"


# ------------------------------------------------------------- fixtures


def _git_repo(project: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"],
                   cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "t"],
                   cwd=project, check=True)
    (project / "app.py").write_text("def value():\n    return 1\n",
                                    encoding="utf-8")
    (project / "test_app.py").write_text(
        "from app import value\n\ndef test_value():\n    assert value() == 2\n",
        encoding="utf-8")
    (project / ".gitignore").write_text(
        "conftest.py\n.tether/\n__pycache__/\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=project, check=True)


class Planter(AgentAdapter):
    """Completed sends that drop files into the project (no commits)."""

    name = "planter"
    verified = True

    def __init__(self, per_send):
        super().__init__({})
        self.per_send = per_send
        self.send_count = 0

    def is_available(self):
        return True, ""

    def start_session(self, project_dir, session_id):
        return SessionInfo(session_id=session_id, project_dir=project_dir)

    def send(self, prompt, session):
        self.send_count += 1
        for rel, content in self.per_send(self.send_count).items():
            path = Path(session.project_dir) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return AgentState(status="completed", logs="out")

    def cancel(self, session):
        pass


def _always(files):
    return lambda n: files


FIXED_APP = "def value():\n    return 2\n"


def _mission_text(extra="", max_attempts=1):
    return (
        "mission:\n  name: m\n  goal: g\n"
        "verification:\n"
        f"  commands:\n    - {PYTEST_CMD}\n"
        f"{extra}"
        f"recovery:\n  max_attempts: {max_attempts}\nadapter: mock\n"
    )


def _commit_mission(project: Path, name: str, text: str) -> None:
    (project / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", name], cwd=project, check=True)


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


# ------------------------------------- task 4: false-green closed end-to-end


def test_in_tree_green_but_clean_room_fails_false_green_closed(tmp_path):
    _git_repo(tmp_path)
    _commit_mission(tmp_path, "intree.yaml", _mission_text())
    _commit_mission(tmp_path, "clean.yaml",
                    _mission_text("  clean_room: true\n"))
    adapter = Planter(_always({"conftest.py": CONFTEST}))
    # Unset (default OFF): the planted gitignored helper games pytest.
    report = _run(tmp_path, adapter, "intree.yaml")
    assert report["status"] == "success", report["next_steps"]
    # clean_room: true: verification runs where the helper does not exist.
    report = _run(tmp_path, adapter, "clean.yaml")
    assert report["status"] == "failed"
    assert report["verification_results"][0]["exit_code"] != 0


def test_default_off_leaves_no_clean_room_traces(tmp_path):
    _git_repo(tmp_path)
    _commit_mission(tmp_path, "intree.yaml", _mission_text())
    report = _run(tmp_path, Planter(_always({})), "intree.yaml")
    assert report["status"] == "failed"  # base world genuinely fails
    assert all(not str(e.get("kind", "")).startswith("clean_room")
               for e in _events(tmp_path, report))
    assert not any(k.startswith("clean_room") for k in report)


def test_recovery_round_sees_refreshed_change_in_fresh_room(tmp_path):
    _git_repo(tmp_path)
    _commit_mission(tmp_path, "clean.yaml",
                    _mission_text("  clean_room: true\n", max_attempts=2))

    def per_send(n):
        if n <= 2:      # plan + execute: only the gitignored helper
            return {"conftest.py": CONFTEST}
        # repair round: a REAL tracked fix lands after attempt 1
        return {"conftest.py": CONFTEST, "app.py": FIXED_APP}

    report = _run(tmp_path, Planter(per_send), "clean.yaml")
    # Attempt 2 re-materialized the room from the refreshed patch.diff, so
    # the real fix is what passed -- not a stale checkout, not the tree.
    assert report["status"] == "success", report["next_steps"]
    assert len(report["recovery_attempts"]) == 1


def test_mutation_battery_runs_against_the_clean_room(tmp_path, monkeypatch):
    _git_repo(tmp_path)
    _commit_mission(tmp_path, "clean.yaml", _mission_text(
        "  clean_room: true\n"
        "  mutation:\n    enabled: true\n    fail_below: 0.5\n"))
    # Tracked fix + gitignored helper: if the suite ran in-tree, the helper
    # would mask every mutant (kill_rate 0); in the room mutants die.
    adapter = Planter(_always(
        {"conftest.py": CONFTEST, "app.py": FIXED_APP}))
    captured: dict = {}

    def spy(spec, changed_files, project_dir, run_suite, **kwargs):
        captured["project_dir"] = project_dir
        return run_mutation_testing(
            spec, changed_files, project_dir, run_suite, **kwargs)

    monkeypatch.setattr(orch_module, "run_mutation_testing", spy)
    # Leftover detection must key on the dirs THIS run created: the shared
    # system tempdir can hold live tether-cleanroom-* directories from other
    # tether processes (e.g. an outer tether verifying this very suite from
    # inside its own clean room), which are not ours to clean or assert on.
    real_mkdtemp = tempfile.mkdtemp
    staged: list[Path] = []

    def tracking_mkdtemp(*args, **kwargs):
        path = Path(real_mkdtemp(*args, **kwargs))
        staged.append(path)
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", tracking_mkdtemp)
    report = _run(tmp_path, adapter, "clean.yaml")
    assert report["status"] == "success", report["next_steps"]
    assert report["mutation"]["killed"] >= 1
    assert report["mutation"]["kill_rate"] == 1.0
    assert captured["project_dir"] != tmp_path
    assert str(captured["project_dir"]).startswith(tempfile.gettempdir())
    # The throwaway staging directory is always cleaned up.
    assert staged
    assert all(not p.exists() for p in staged)


# ------------------------------------------- task 3: fail-closed orchestration


def test_materialization_failure_fails_mission_closed(tmp_path, monkeypatch):
    _git_repo(tmp_path)
    _commit_mission(tmp_path, "clean.yaml",
                    _mission_text("  clean_room: true\n"))

    def boom(*args, **kwargs):
        raise CleanRoomError("simulated materialization failure")

    monkeypatch.setattr(orch_module, "materialize_clean_room", boom)
    report = _run(tmp_path, Planter(_always({"conftest.py": CONFTEST})),
                  "clean.yaml")
    assert report["status"] == "failed"
    assert report["verification_results"] == []  # no in-tree fallback ever
    assert any("simulated materialization failure" in s
               for s in report["next_steps"])
    events = _events(tmp_path, report)
    hits = [e for e in events if e.get("kind") == "clean_room_error"]
    assert hits and "simulated materialization failure" in hits[-1]["error"]


def test_non_git_project_with_clean_room_fails_closed(tmp_path):
    # No git repo => no checkpoint ref can exist; refuse rather than verify
    # in-tree.
    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    mp = tmp_path / "m.yaml"
    mp.write_text(_mission_text("  clean_room: true\n").replace(
        PYTEST_CMD, py_pass()), encoding="utf-8")
    config = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(Planter(_always({})), config, tmp_path).run(
        load_mission(mp))
    assert report["status"] == "failed"
    assert report["verification_results"] == []
    assert any("checkpoint" in s for s in report["next_steps"])
    assert any(e.get("kind") == "clean_room_error"
               for e in _events(tmp_path, report))


def test_dry_run_records_clean_room_as_skipped(tmp_path):
    _git_repo(tmp_path)
    _commit_mission(tmp_path, "clean.yaml",
                    _mission_text("  clean_room: true\n"))
    report = _run(tmp_path, Planter(_always({"conftest.py": CONFTEST})),
                  "clean.yaml", dry_run=True)
    assert report["status"] == "success"
    skipped = [e for e in _events(tmp_path, report)
               if e.get("kind") == "clean_room"]
    assert skipped and skipped[-1]["status"] == "skipped"
    assert skipped[-1]["reason"] == "dry-run"


# --------------------------------------- task 2: materializer semantics


def _capture_artifacts(project: Path, session: Path) -> None:
    """Simulate the orchestrator's forensic capture, INCLUDING gitignored
    paths in untracked.txt (tampered/stale listing) to prove exclusion."""
    session.mkdir(parents=True, exist_ok=True)
    patch = subprocess.run(["git", "diff", "--no-color"], cwd=project,
                           capture_output=True, check=True).stdout
    (session / "patch.diff").write_bytes(patch)
    others = subprocess.run(["git", "ls-files", "--others"], cwd=project,
                            capture_output=True, text=True, check=True).stdout
    (session / "untracked.txt").write_text(others, encoding="utf-8")


def test_patch_applies_untracked_carries_ignored_helper_excluded(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _git_repo(project)
    # Agent state: tracked fix + legitimate untracked note + gitignored plant.
    (project / "app.py").write_text(FIXED_APP, encoding="utf-8")
    (project / "notes.txt").write_text("carried\n", encoding="utf-8")
    (project / "conftest.py").write_text("# planted helper\n",
                                         encoding="utf-8")
    session = tmp_path / "session"
    _capture_artifacts(project, session)
    listing = (session / "untracked.txt").read_text("utf-8")
    assert "conftest.py" in listing          # tampered listing includes it
    dest = tmp_path / "room"
    materialize_clean_room(project, "HEAD", session, [], dest)
    assert "return 2" in (dest / "app.py").read_text(encoding="utf-8")
    assert (dest / "notes.txt").read_text(encoding="utf-8") == "carried\n"
    assert not (dest / "conftest.py").exists()


def test_clean_room_copy_brings_dirs_missing_entries_skipped(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _git_repo(project)
    (project / ".venv" / "lib").mkdir(parents=True)
    (project / ".venv" / "lib" / "marker.txt").write_text("x",
                                                          encoding="utf-8")
    session = tmp_path / "session"
    _capture_artifacts(project, session)
    dest = tmp_path / "room"
    materialize_clean_room(project, "HEAD", session,
                           [".venv", "does-not-exist"], dest)
    assert (dest / ".venv" / "lib" / "marker.txt").is_file()
    assert not (dest / "does-not-exist").exists()


def test_corrupt_or_missing_patch_diff_fails_closed(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _git_repo(project)
    session = tmp_path / "session"
    session.mkdir()
    (session / "patch.diff").write_text("*** total garbage ***\n",
                                        encoding="utf-8")
    with pytest.raises(CleanRoomError):
        materialize_clean_room(project, "HEAD", session, [],
                               tmp_path / "room")
    empty = tmp_path / "empty-session"
    empty.mkdir()
    with pytest.raises(CleanRoomError):  # missing artifact
        materialize_clean_room(project, "HEAD", empty, [],
                               tmp_path / "room")


def test_bad_checkpoint_ref_fails_closed(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _git_repo(project)
    session = tmp_path / "session"
    _capture_artifacts(project, session)
    with pytest.raises(CleanRoomError):
        materialize_clean_room(project, "0123456789abcdef0123456789abcdef"
                               "01234567", session, [], tmp_path / "room")


def test_copy_entries_reject_absolute_escape_and_protected_paths(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _git_repo(project)
    session = tmp_path / "session"
    _capture_artifacts(project, session)
    for bad in ("/etc", "../outside", ".git", ".tether"):
        with pytest.raises(CleanRoomError):
            materialize_clean_room(project, "HEAD", session, [bad],
                                   tmp_path / "room")


def test_untracked_listing_escapes_are_skipped_silently(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _git_repo(project)
    (tmp_path / "evil.txt").write_text("outside\n", encoding="utf-8")
    session = tmp_path / "session"
    _capture_artifacts(project, session)
    (session / "untracked.txt").write_text("../evil.txt\n",
                                            encoding="utf-8")
    dest = tmp_path / "room"
    materialize_clean_room(project, "HEAD", session, [], dest)
    assert not (dest / "evil.txt").exists()


# --------------------------------------------- task 1: contract validation


def test_clean_room_contract_parses(tmp_path):
    mp = tmp_path / "m.yaml"
    mp.write_text(_mission_text("  clean_room: true\n"
                                "  clean_room_copy:\n"
                                "    - .venv\n"
                                "    - tools/cache\n"), encoding="utf-8")
    m = load_mission(mp)
    assert m.verification.clean_room is True
    assert m.verification.clean_room_copy == [".venv", "tools/cache"]


def test_absent_clean_room_defaults_to_none(tmp_path):
    mp = tmp_path / "m.yaml"
    mp.write_text(_mission_text(), encoding="utf-8")
    m = load_mission(mp)
    assert m.verification.clean_room is None
    assert m.verification.clean_room_copy is None


@pytest.mark.parametrize("block", [
    "  clean_room: definitely\n",            # not a boolean
    "  clean_room: 1\n",                     # int is not a boolean
    "  clean_room_copy: .venv\n",            # not a list
    "  clean_room_copy: ['/abs/path']\n",    # absolute path
    "  clean_room_copy: ['../escape']\n",    # '..' component
    "  clean_room_copy: ['ok', 42]\n",       # non-string entry
    "  clean_room_copy: ['']\n",             # empty entry
])
def test_invalid_clean_room_contract_raises(tmp_path, block):
    mp = tmp_path / "m.yaml"
    mp.write_text(_mission_text(block), encoding="utf-8")
    with pytest.raises(MissionError):
        load_mission(mp)
