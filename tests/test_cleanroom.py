"""Clean-room verification (dogfood-23): false-green closure end-to-end,
materializer semantics, fail-closed orchestration, and contract validation."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import tether.orchestrator as orch_module
from tether.adapters.base import AgentAdapter, SessionInfo
from tether.audit import find_session_dir
import tether.cleanroom as cleanroom
from tether.cleanroom import (
    CleanRoomError,
    _carry_git_metadata,
    _contained,
    _gitignored_paths,
    _is_relative,
    materialize_clean_room,
)
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
    for bad in ("/etc", "../../outside", ".git", ".tether"):
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
    "  clean_room_copy: ['../../escape']\n",    # '..' escape beyond parent
    "  clean_room_copy: ['ok', 42]\n",       # non-string entry
    "  clean_room_copy: ['']\n",             # empty entry
])
def test_invalid_clean_room_contract_raises(tmp_path, block):
    mp = tmp_path / "m.yaml"
    mp.write_text(_mission_text(block), encoding="utf-8")
    with pytest.raises(MissionError):
        load_mission(mp)


# --------------------------- task 5: mutation-survivor probes (dogfood-40)
#
# Every probe in this section exists to kill a specific mutant that survived
# the full cleanroom.py mutation battery (tools/mutation_killrate.py,
# baseline kill rate 0.76 < gate 0.8). Four of the twelve survivors are
# equivalent by construction and are documented, not probed:
#   - 74:8 / 76:8 break_return turn `return None` into itself;
#   - 83:8 / 85:8 turn `return False` into `return None`, indistinguishable
#     at every call site because _is_relative is only used for truthiness.


def test_gitignored_paths_empty_listing_is_provably_not_ignored(tmp_path):
    # Kills 66:8 (break_return on `return set()` -> None): an empty listing
    # means gitignore status was never queried, i.e. PROVABLY none ignored —
    # distinct from None ("status unknown => callers must skip").
    assert _gitignored_paths(tmp_path, []) == set()
    assert _gitignored_paths(tmp_path, []) is not None
    # The two outcomes are observably different: outside any git repository
    # check-ignore cannot decide, and the contract demands None (skip), not
    # set() (allow), so callers fail closed on undeterminable status.
    outside_any_repo = tmp_path / "not-a-repo"
    outside_any_repo.mkdir()
    assert _gitignored_paths(outside_any_repo, ["x"]) is None


def test_contained_treats_root_itself_as_inside_boundary(tmp_path):
    # Kills 91:11 (negate_compare `resolved == root` -> !=): the containment
    # contract guarding every copy path is inclusive at the root boundary.
    root = tmp_path.resolve()
    assert _contained(root, root) is True
    assert _contained(root / "sub" / "leaf.txt", root) is True
    sibling = tmp_path.parent / (tmp_path.name + "-sibling")
    assert _contained(sibling, root) is False


def _materialize_project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _git_repo(project)
    session = tmp_path / "session"
    _capture_artifacts(project, session)
    return project, session


def test_absolute_copy_entry_rejected_with_exact_relative_path_error(
        tmp_path):
    # Kills 83:15 (flip_bool False->True): under the mutant the absolute
    # entry slips past the relative-path contract and dies later with the
    # DIFFERENT "escapes the project dir" message; pin the real message.
    project, session = _materialize_project(tmp_path)
    with pytest.raises(CleanRoomError,
                       match="must be a relative path: '/etc'"):
        materialize_clean_room(project, "HEAD", session, ["/etc"],
                               tmp_path / "room")


def test_materialize_creates_missing_dest_parents(tmp_path):
    # Kills 122:27 (flip_bool parents=True -> False): the contract is
    # "dest (which is created)" — any not-yet-existing destination path must
    # work, not just destinations whose parent already exists.
    project, session = _materialize_project(tmp_path)
    deep = tmp_path / "deep" / "nest" / "room"
    materialize_clean_room(project, "HEAD", session, [], deep)
    assert (deep / "app.py").is_file()


def test_rematerialize_into_existing_dest_is_idempotent(tmp_path):
    # Kills 122:42 (flip_bool exist_ok=True -> False): retrying a run into a
    # reused staging directory (e.g. after a partial failure) must succeed.
    project, session = _materialize_project(tmp_path)
    dest = tmp_path / "room"
    materialize_clean_room(project, "HEAD", session, [], dest)
    materialize_clean_room(project, "HEAD", session, [], dest)
    assert (dest / "app.py").read_text(encoding="utf-8") == (
        "def value():\n    return 1\n")


def test_dir_copy_preserves_symlinks_instead_of_dereferencing(tmp_path):
    # Kills 191:54 (flip_bool symlinks=True -> False): copied trees keep
    # symlinks AS symlinks; dereferencing would duplicate or break them.
    project, session = _materialize_project(tmp_path)
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / ".venv" / "bin" / "real.txt").write_text("r",
                                                        encoding="utf-8")
    os.symlink("real.txt", project / ".venv" / "bin" / "link.txt")
    dest = tmp_path / "room"
    materialize_clean_room(project, "HEAD", session, [".venv"], dest)
    link = dest / ".venv" / "bin" / "link.txt"
    assert link.is_symlink()
    assert os.readlink(link) == "real.txt"


def test_file_copy_entry_creates_missing_target_dirs(tmp_path):
    # Kills 194:44 (flip_bool parents=True -> False): a file entry nested
    # TWO levels below dest (both levels absent from the checkpoint archive)
    # must be created on demand; parents=False cannot create intermediates.
    # freshdir is gitignored so the step-3 untracked carry cannot pre-create
    # it — only this branch's own mkdir can.
    project, session = _materialize_project(tmp_path)
    (project / "freshdir" / "nested").mkdir(parents=True)
    (project / "freshdir" / "nested" / "tool.conf").write_text(
        "cfg\n", encoding="utf-8")
    with open(project / ".gitignore", "a", encoding="utf-8") as fh:
        fh.write("freshdir/\n")
    dest = tmp_path / "room"
    materialize_clean_room(project, "HEAD", session,
                           ["freshdir/nested/tool.conf"], dest)
    assert (dest / "freshdir" / "nested" / "tool.conf").read_text(
        encoding="utf-8") == "cfg\n"


def test_plain_file_copy_entry_overwrites_archive_bytes(tmp_path):
    # Kills 194:59 (flip_bool exist_ok=True -> False): the target's parent
    # (the extracted dest root) ALWAYS pre-exists for file entries, so the
    # mkdir must tolerate it; also pins that the explicit copy wins over
    # the pristine archive content.
    project, session = _materialize_project(tmp_path)
    (project / "app.py").write_text(FIXED_APP, encoding="utf-8")
    dest = tmp_path / "room"
    materialize_clean_room(project, "HEAD", session, ["app.py"], dest)
    assert (dest / "app.py").read_text(encoding="utf-8") == FIXED_APP


# ------------------- sibling-repo materialization + .git carry: mutation teeth
#
# dogfood-43..46 run mutation testing of cleanroom.py against THIS suite at
# --min-kill-rate 0.8. The cohorts above pin the project-tree contract, but
# the sibling-repo (../<name>) branch and the .git-carrying fallback were
# unexercised, and a regression there is SILENT: the room still materializes,
# it just quietly stops being a git repo -- which is exactly what the tri-repo
# gate's `git rev-parse HEAD` and sibling-state checks depend on.


def _sibling_repo(path):
    """A real git repo one level above the project, plus a .lake build env."""
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "s@example.com"],
                   cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "s"], cwd=path, check=True)
    (path / "sib.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (path / ".lake" / "packages").mkdir(parents=True)
    (path / ".lake" / "packages" / "marker.txt").write_text("deps\n",
                                                           encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "sib"], cwd=path, check=True)
    return path


def _room_sibling_scratch(tmp_path):
    """project + session for a project living beside a `../SIB` repo.

    `dest` is nested one level deeper so `dest.parent` is NOT the directory
    holding the source sibling: the materializer rmtree's the destination
    sibling before copying, and pointing it at the source would delete the
    very tree it is reading.
    """
    project, session = _materialize_project(tmp_path)
    _sibling_repo(tmp_path / "SIB")
    return project, session, tmp_path / "room" / "sub"


def _block_full_git_copy(monkeypatch, git_dst, record=None, partial=True):
    """Make the full `.git` copytree fail *after* creating its destination,
    leaving the minimal fallback with a pre-existing git_dst to tolerate.

    The partial creation is the realistic shape of a copytree that dies
    mid-transfer, and it is the only way to observe that the fallback's
    mkdir must pass exist_ok=True.
    """
    real = shutil.copytree

    def blocked(*a, **kw):
        if record is not None:
            record(*a, **kw)
        if Path(a[1]) == git_dst:
            if partial:
                Path(a[1]).mkdir(parents=True, exist_ok=True)
            raise OSError("partial copy")
        return real(*a, **kw)

    monkeypatch.setattr(shutil, "copytree", blocked)
    return real


@pytest.mark.parametrize("rel,expected", [
    ("src/app.py", True),          # plain in-tree path
    ("../QED", True),              # sibling: exactly one level, name only
    ("..", False),                 # bare parent dir: len(parts) < 2
    ("a/../b", False),             # embedded .. whose head is not ".."
    ("/etc/passwd", False),        # absolute
    ("", False),                   # empty: no parts
])
def test_is_relative_separates_sibling_paths_from_escapes(rel, expected):
    # Kills 87:8 (break_return), 87:15 (len(parts) >= 2 negated), 87:45
    # (parts[0] == ".." negated) and 82:8 (break_return on `return False`).
    # The sibling allowance is exactly `../<name>`; a negated length or head
    # comparison flips `../QED` to False, which is the difference between a
    # working tri-repo gate and a sibling that silently refuses to land.
    assert _is_relative(rel) is expected


def test_carry_git_metadata_copies_dot_git_so_head_resolves(tmp_path):
    # Kills 102:14 / 103:14 (arithmetic on the .git source and destination):
    # if either side is mutated the metadata lands in the wrong place and
    # `git rev-parse HEAD` inside the clean room fails.
    src = _sibling_repo(tmp_path / "SIB")
    target = tmp_path / "room" / "SIB"
    target.mkdir(parents=True)
    _carry_git_metadata(src, target)
    assert (target / ".git" / "HEAD").is_file()
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=target,
                          capture_output=True, text=True)
    assert proc.returncode == 0


def test_carry_git_metadata_never_clobbers_an_existing_dot_git(tmp_path):
    # Kills 104:28 (flip_bool `not git_dst.exists()`). With a pre-existing
    # .git the ORIGINAL code skips the branch entirely and leaves the
    # existing metadata alone; the mutant would recopy over it.
    src = _sibling_repo(tmp_path / "SIB")
    target = tmp_path / "room" / "SIB"
    (target / ".git").mkdir(parents=True)
    (target / ".git" / "SENTINEL").write_text("keep\n", encoding="utf-8")
    _carry_git_metadata(src, target)
    assert (target / ".git" / "SENTINEL").read_text(encoding="utf-8") == (
        "keep\n")
    assert not (target / ".git" / "HEAD").exists()


def test_carry_git_metadata_fallback_recovers_head_refs_and_config(
        tmp_path, monkeypatch):
    # Kills 113:24 / 114:24 (arithmetic on the fallback's source and
    # destination paths), 115:39 (`not d.exists()` negated on the file
    # branch), 117:40 (same negation on the directory branch), 110:53
    # (`exist_ok` negated -- the destination .git already exists here, so a
    # bare mkdir would raise and be swallowed) and 118:55 (`symlinks`
    # negated). The full copy is unavailable, so the room must still end up
    # with enough git metadata for `git rev-parse HEAD`.
    src = _sibling_repo(tmp_path / "SIB")
    target = tmp_path / "room" / "SIB"
    target.mkdir(parents=True)
    seen = []
    _block_full_git_copy(monkeypatch, target / ".git",
                         record=lambda *a, **kw: seen.append(
                             (Path(a[1]), kw.get("symlinks"))))
    _carry_git_metadata(src, target)
    assert (target / ".git" / "HEAD").is_file()
    assert (target / ".git" / "config").is_file()
    assert (target / ".git" / "refs").is_dir()
    # Kills 118:55: the fallback's directory copy must also preserve symlinks.
    fallback_dir_copies = [v for d, v in seen
                           if d == target / ".git" / "refs"]
    assert fallback_dir_copies == [True]
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=target,
                          capture_output=True, text=True)
    assert proc.returncode == 0


def test_carry_git_metadata_fallback_creates_missing_parents(
        tmp_path, monkeypatch):
    # Kills 110:38 (flip_bool parents=True -> False). The fallback's mkdir
    # runs against a destination whose parent chain does not exist yet, so
    # without parents=True it raises FileNotFoundError, is swallowed by the
    # bare `except OSError: pass`, and .git/HEAD is never written.
    src = _sibling_repo(tmp_path / "SIB")
    target = tmp_path / "deep" / "nested" / "SIB"
    _block_full_git_copy(monkeypatch, target / ".git", partial=False)
    _carry_git_metadata(src, target)
    assert (target / ".git" / "HEAD").is_file()


def test_carry_git_metadata_copies_with_symlinks_preserved(
        tmp_path, monkeypatch):
    # Kills 106:55 and 118:55 (flip_bool symlinks=True -> False on the
    # copytree calls). A false `symlinks` dereferences links, silently
    # rewriting the pinned worktree/.lake layout it is meant to reproduce.
    src = _sibling_repo(tmp_path / "SIB")
    target = tmp_path / "room" / "SIB"
    target.mkdir(parents=True)
    seen = []
    real = shutil.copytree

    def record(*a, **kw):
        seen.append((Path(a[1]), kw.get("symlinks")))
        return real(*a, **kw)

    monkeypatch.setattr(shutil, "copytree", record)
    _carry_git_metadata(src, target)
    # Kills 106:55: the full .git copy must preserve symlinks.
    assert (target / ".git", True) in seen


def test_carry_git_metadata_handles_worktree_gitlink_file(tmp_path):
    # Kills 121:31 (flip_bool `not git_dst.exists()` on the worktree branch):
    # a worktree stores .git as a FILE pointing at the real gitdir, and the
    # mutant would leave the clean room with no git metadata at all.
    src = tmp_path / "W"
    src.mkdir()
    pointer = "gitdir: /elsewhere/.git/worktrees/W\n"
    (src / ".git").write_text(pointer, encoding="utf-8")
    target = tmp_path / "room" / "W"
    target.mkdir(parents=True)
    _carry_git_metadata(src, target)
    assert (target / ".git").is_file()
    assert (target / ".git").read_text(encoding="utf-8") == pointer


def test_sibling_room_is_materialized_from_archive_patch_and_untracked(
        tmp_path):
    # The core tri-repo contract: `../SIB` is rebuilt from its OWN checkpoint
    # (never copytree'd from the dirty host tree), its captured patch applied,
    # its non-gitignored untracked files carried, and its pinned .lake
    # environment preserved.
    # Kills 223:22 / 223:48 (both negations of the is_sibling predicate: a
    # negated `parts[0] == ".."` or `len(parts) == 2` demotes a legitimate
    # sibling to an escaping path and aborts the mission), 231:21, 259:33,
    # 260:48 (`st_size > 0` negated skips a real sibling patch), 262:31,
    # 270:27 (flip_bool on the gitignore exclusion admits a planted ignored
    # file into the room), 272:29, 275:29, 285:32, 287:36, 293:75.
    project, session, dest = _room_sibling_scratch(tmp_path)
    sib = tmp_path / "SIB"
    (sib / "sib.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    (sib / "note.txt").write_text("carried\n", encoding="utf-8")
    (sib / "d").mkdir()
    (sib / "d" / "x.txt").write_text("x\n", encoding="utf-8")
    (sib / "d" / "y.txt").write_text("y\n", encoding="utf-8")
    # Two levels deep, so its parent chain is genuinely absent: this is what
    # makes `parents=True` on the untracked-copy mkdir observable at all.
    (sib / "d" / "e").mkdir()
    (sib / "d" / "e" / "z.txt").write_text("z\n", encoding="utf-8")
    (sib / ".lake" / "keep.txt").write_text("k\n", encoding="utf-8")
    (sib / "planted.txt").write_text("nope\n", encoding="utf-8")
    (sib / ".gitignore").write_text("planted.txt\n", encoding="utf-8")
    patch = subprocess.run(["git", "diff", "--no-color"], cwd=sib,
                           capture_output=True, check=True).stdout
    (session / "patch_SIB.diff").write_bytes(patch)
    (session / "untracked_SIB.txt").write_text(
        # d/e/z.txt is listed FIRST: its whole parent chain is absent, which
        # is the only way `parents=True` on this mkdir is observable at all
        # (once d/x.txt lands, room/d exists and parents=False would do).
        "d/e/z.txt\nnote.txt\nplanted.txt\nd/x.txt\nd/y.txt\n.lake\n",
        encoding="utf-8")

    materialize_clean_room(project, "HEAD", session, ["../SIB"], dest)

    room = tmp_path / "room" / "SIB"
    assert room.is_dir(), "sibling must land beside the clean room, not in it"
    # the captured sibling change is applied
    assert "return 2" in (room / "sib.py").read_text(encoding="utf-8")
    # non-gitignored untracked files are carried byte-for-byte
    assert (room / "note.txt").read_text(encoding="utf-8") == "carried\n"
    # Kills 276:63 (exist_ok negated): "d/y.txt" is the SECOND file under an
    # already-created "d", so the parent mkdir must tolerate it.
    assert (room / "d" / "x.txt").read_text(encoding="utf-8") == "x\n"
    assert (room / "d" / "y.txt").read_text(encoding="utf-8") == "y\n"
    # Kills 276:48 (flip_bool parents=True -> False): "d/e" does not exist,
    # so without parents=True this mkdir raises and the untracked carry fails
    # closed for every legitimate nested file.
    assert (room / "d" / "e" / "z.txt").read_text(encoding="utf-8") == "z\n"
    # a gitignored plant is excluded even though the listing is tampered
    assert not (room / "planted.txt").exists()
    # Kills 273:56 (flip_bool `not _s.is_file()`): a DIRECTORY entry (".lake")
    # must be skipped by the untracked copy loop, not read_bytes()'d.
    # the pinned build environment is preserved
    assert (room / ".lake" / "packages" / "marker.txt").is_file()
    assert (room / ".lake" / "keep.txt").is_file()
    # git metadata is carried so sibling-state checks function
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=room,
                          capture_output=True, text=True)
    assert proc.returncode == 0


def test_sibling_deep_path_is_rejected_as_an_escape(tmp_path):
    # Kills 223:22 / 223:48 from the other direction: `../SIB/nested` is NOT
    # a sibling (it is deeper than one level) and must fail closed rather
    # than being copied from outside the project root.
    project, session, dest = _room_sibling_scratch(tmp_path)
    (tmp_path / "SIB" / "nested").mkdir()
    with pytest.raises(CleanRoomError, match="escapes the project dir"):
        materialize_clean_room(project, "HEAD", session,
                               ["../SIB/nested"], dest)


def test_sibling_without_resolvable_head_fails_closed(tmp_path):
    # Kills 246:23 (negate_compare returncode != 0): a sibling that is not a
    # git repository cannot have its checkpoint resolved, and the gate must
    # abort instead of copying a dirty directory across.
    project, session = _materialize_project(tmp_path)
    (tmp_path / "SIB").mkdir()
    with pytest.raises(CleanRoomError, match="cannot resolve HEAD"):
        materialize_clean_room(project, "HEAD", session, ["../SIB"],
                               tmp_path / "room" / "sub")


def test_sibling_archive_failure_fails_closed(tmp_path, monkeypatch):
    # Kills 252:23 (negate_compare on the sibling `git archive` result): a
    # sibling whose archive cannot be produced must fail closed rather than
    # proceed to patch an empty tree.
    project, session, dest = _room_sibling_scratch(tmp_path)
    sib = tmp_path / "SIB"
    real_git = cleanroom._git

    def fake(project_dir, *args):
        if Path(project_dir) == sib and args and args[0] == "archive":
            return subprocess.CompletedProcess(args, 1, b"", b"archive boom")
        return real_git(project_dir, *args)

    monkeypatch.setattr(cleanroom, "_git", fake)
    with pytest.raises(CleanRoomError, match="git archive failed for sibling"):
        materialize_clean_room(project, "HEAD", session, ["../SIB"], dest)


def test_sibling_materialization_tolerates_pre_existing_target(tmp_path):
    # Kills 254:41 / 254:56 (flip_bool on the target mkdir): the destination
    # sibling directory can pre-exist (a resumed or repeated
    # materialization), so the mkdir must tolerate it and the archive must
    # still land.
    project, session, dest = _room_sibling_scratch(tmp_path)
    (tmp_path / "room" / "SIB").mkdir(parents=True)
    materialize_clean_room(project, "HEAD", session, ["../SIB"], dest)
    assert (tmp_path / "room" / "SIB" / "sib.py").is_file()


def test_sibling_untracked_entry_pointing_outside_is_skipped(tmp_path):
    # Kills 273:27 (flip_bool `not _contained(_s, _root)`). The containment
    # check is the only thing stopping an untracked listing entry from
    # writing *through* the clean room into the host filesystem; negating it
    # lets "../evil.txt" escape. It must be skipped, silently and safely.
    project, session, dest = _room_sibling_scratch(tmp_path)
    (tmp_path / "evil.txt").write_text("evil\n", encoding="utf-8")
    (session / "untracked_SIB.txt").write_text("../evil.txt\n", encoding="utf-8")
    materialize_clean_room(project, "HEAD", session, ["../SIB"], dest)
    assert not (tmp_path / "room" / "evil.txt").exists()
    assert not (tmp_path / "room" / "sub" / "evil.txt").exists()


def test_sibling_lake_environment_is_copied_with_symlinks_preserved(
        tmp_path, monkeypatch):
    # Kills 293:75 (flip_bool symlinks=True -> False) and 287:36. The pinned
    # `.lake` build environment is what lets the formal gate compile without
    # network access; dereferencing its symlinks would silently rewrite the
    # layout the mission depends on.
    project, session, dest = _room_sibling_scratch(tmp_path)
    lake = tmp_path / "SIB" / ".lake"
    (lake / "linked").mkdir(parents=True)
    (lake / "target.txt").write_text("t\n", encoding="utf-8")
    try:
        (lake / "link.txt").symlink_to(lake / "target.txt")
    except (OSError, NotImplementedError):  # pragma: no cover
        pytest.skip("symlinks unavailable on this platform")
    seen = []
    real = shutil.copytree

    def record(*a, **kw):
        seen.append((Path(a[1]), kw.get("symlinks")))
        return real(*a, **kw)

    monkeypatch.setattr(shutil, "copytree", record)
    materialize_clean_room(project, "HEAD", session, ["../SIB"], dest)
    carried = tmp_path / "room" / "SIB" / ".lake"
    assert (carried / "target.txt").is_file()
    assert seen and (carried, True) in seen
    assert (carried / "link.txt").is_symlink()
