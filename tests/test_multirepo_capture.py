"""Multi-repo change capture + sibling clean-room materialization.

Covers the atomic-workspace branches in ``Orchestrator._gate_and_capture``
(per-repo ``patch_<repo>.diff`` / ``untracked_<repo>.txt``, sibling-prefixed
changed files) and ``cleanroom.materialize_clean_room`` (sibling
``git archive`` + patch + non-gitignored untracked + ``.lake`` build env).
All repos are real ``git init`` trees; no mocks touch the filesystem paths.
"""
import subprocess
from pathlib import Path

from tether.audit import AuditTrail
from tether.cleanroom import materialize_clean_room
from tether.git_safety import create_checkpoint, head_sha
from tether.models import CheckpointInfo, TetherConfig
from tether.orchestrator import Orchestrator
from tether.adapters.mock import MockAdapter


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=str(path),
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(path),
                   check=True, capture_output=True)
    (path / "f.txt").write_text("v1\n")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), check=True,
                   capture_output=True)


def _mission(**kw):
    from tether.models import MissionContract
    base = {"name": "m", "goal": "g", "verification": {"commands": []}}
    base.update(kw)
    return MissionContract(mission={"name": "m", "goal": "g"}, name="m", goal="g",
                           **{k: v for k, v in base.items()
                              if k in ("workspace_repos", "verification",
                                       "allowed_paths", "forbidden_paths")})


def test_gate_captures_sibling_changes_with_prefixed_paths(tmp_path):
    proj = tmp_path / "proj"
    sib = tmp_path / "sib"
    _init_repo(proj)
    _init_repo(sib)
    (sib / "f.txt").write_text("dirty-sib\n")
    (sib / "new.txt").write_text("untracked\n")

    orch = Orchestrator(MockAdapter(), TetherConfig(), proj, session_id="sess-cap1")
    audit = AuditTrail(proj, ".tether/s", "m", "sess-cap1")
    cp = create_checkpoint(proj, "sess-cap1", allow_dirty=True)
    assert isinstance(cp, CheckpointInfo)
    mission = _mission(workspace_repos=["../sib"])
    changed = orch._gate_and_capture(audit, mission, cp, None, dry_run=False)
    assert "sib/f.txt" in changed
    assert "sib/new.txt" in changed
    assert (audit.dir / "patch_sib.diff").is_file()
    assert (audit.dir / "untracked_sib.txt").is_file()
    untracked = (audit.dir / "untracked_sib.txt").read_text()
    assert "new.txt" in untracked


def test_gate_clean_sibling_yields_no_prefixed_changes(tmp_path):
    proj = tmp_path / "proj"
    sib = tmp_path / "sib"
    _init_repo(proj)
    _init_repo(sib)
    orch = Orchestrator(MockAdapter(), TetherConfig(), proj, session_id="sess-cap2")
    audit = AuditTrail(proj, ".tether/s2", "m", "sess-cap2")
    cp = create_checkpoint(proj, "sess-cap2", allow_dirty=True)
    mission = _mission(workspace_repos=["../sib"])
    changed = orch._gate_and_capture(audit, mission, cp, None, dry_run=False)
    assert changed == []
    assert (audit.dir / "patch_sib.diff").is_file()


def test_cleanroom_sibling_archive_plus_patch_and_lake(tmp_path):
    proj = tmp_path / "proj"
    sib = tmp_path / "sib"
    _init_repo(proj)
    _init_repo(sib)
    # Pinned build env on the host sibling.
    lake_build = sib / ".lake" / "build"
    lake_build.mkdir(parents=True)
    (lake_build / "Compartmental.olean").write_bytes(b"OLEAN")
    # Symlink in .lake must stay a symlink (kills symlinks=False flip).
    (lake_build / "Compartmental.olean.link").symlink_to("Compartmental.olean")
    # Gitignored helper must NOT carry over.
    (sib / ".gitignore").write_text("secret.bin\n")
    (sib / "secret.bin").write_text("planted\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=str(sib), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "ignore"], cwd=str(sib), check=True,
                   capture_output=True)
    ref = head_sha(sib)
    assert ref

    session = tmp_path / "sess"
    session.mkdir()
    (session / "patch.diff").write_bytes(b"")
    (session / "untracked.txt").write_text("")
    # Session patch for the sibling: tracked change applied post-archive.
    diff = subprocess.run(
        ["git", "-C", str(sib), "diff", ref], capture_output=True, check=True)
    assert diff.returncode == 0
    (sib / "f.txt").write_text("patched\n")
    diff2 = subprocess.run(["git", "-C", str(sib), "diff", ref],
                           capture_output=True, check=True)
    (session / "patch_sib.diff").write_bytes(diff2.stdout)
    (session / "untracked_sib.txt").write_text("new_untracked.txt\n")
    (sib / "new_untracked.txt").write_text("hello\n")

    (sib / "sub").mkdir()
    (sib / "sub" / "a.txt").write_text("a\n")
    (sib / "sub" / "b.txt").write_text("b\n")
    (sib / "deep").mkdir()
    (sib / "deep" / "nested").mkdir()
    (sib / "deep" / "nested" / "c.txt").write_text("c\n")
    with open(session / "untracked_sib.txt", "a") as fh:
        fh.write("sub/a.txt\nsub/b.txt\ndeep/nested/c.txt\n")

    dest = tmp_path / "clean" / "proj"
    materialize_clean_room(proj, head_sha(proj), session, ["../sib"], dest)
    sib_out = dest.parent / "sib"
    assert (sib_out / "f.txt").read_text() == "patched\n"
    assert (sib_out / "new_untracked.txt").read_text() == "hello\n"
    assert not (sib_out / "secret.bin").exists()
    assert (sib_out / ".lake" / "build" / "Compartmental.olean").read_bytes() == b"OLEAN"
    assert (sib_out / ".lake" / "build" / "Compartmental.olean.link").is_symlink()
    # Two untracked files sharing one subdir: the carry path must create
    # parents with exist_ok (second file reuses the directory).
    assert (sib_out / "sub" / "a.txt").read_text() == "a\n"
    assert (sib_out / "sub" / "b.txt").read_text() == "b\n"
    # Nested untracked path needs recursive parent creation.
    assert (sib_out / "deep" / "nested" / "c.txt").read_text() == "c\n"


def test_cleanroom_sibling_preserves_git_metadata(tmp_path):
    """Sibling .git metadata (HEAD + refs + symlinks) survives materialization.

    Kills mutants around sibling git preservation (cleanroom.py:269-288):
    any dropped copy, flipped existence check, or lost symlink breaks
    `git rev-parse HEAD` inside the room.
    """
    proj = tmp_path / "proj"
    sib = tmp_path / "sib"
    _init_repo(proj)
    _init_repo(sib)
    # A symlink inside .git must stay a symlink (kills symlinks=False flip).
    (sib / ".git" / "HEAD.link").symlink_to("HEAD")
    session = tmp_path / "sess"
    session.mkdir()
    (session / "patch.diff").write_bytes(b"")
    (session / "untracked.txt").write_text("")
    (session / "patch_sib.diff").write_bytes(b"")
    (session / "untracked_sib.txt").write_text("")
    dest = tmp_path / "clean" / "proj"
    materialize_clean_room(proj, head_sha(proj), session, ["../sib"], dest)
    sib_out = dest.parent / "sib"
    assert (sib_out / ".git" / "HEAD").read_bytes() == (sib / ".git" / "HEAD").read_bytes()
    assert (sib_out / ".git" / "HEAD.link").is_symlink()
    rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(sib_out),
                         capture_output=True, check=True, text=True)
    assert rev.stdout.strip() == head_sha(sib)


def test_cleanroom_sibling_git_fallback_preserves_head(tmp_path, monkeypatch):
    """Forced copytree failure exercises the minimal HEAD/refs fallback.

    Kills mutants in the OSError fallback (cleanroom.py:276-287): the room
    must still contain HEAD and refs so rev-parse functions.
    """
    import shutil
    import tether.cleanroom as cr
    proj = tmp_path / "proj"
    sib = tmp_path / "sib"
    _init_repo(proj)
    _init_repo(sib)
    real_copytree = shutil.copytree

    def _fail_git_copytree(*args, **kw):
        if args and Path(args[0]) == sib / ".git":
            raise OSError("injected git copy failure")
        return real_copytree(*args, **kw)

    monkeypatch.setattr(shutil, "copytree", _fail_git_copytree)
    monkeypatch.setattr(cr.shutil, "copytree", _fail_git_copytree)
    session = tmp_path / "sess"
    session.mkdir()
    (session / "patch.diff").write_bytes(b"")
    (session / "untracked.txt").write_text("")
    (session / "patch_sib.diff").write_bytes(b"")
    (session / "untracked_sib.txt").write_text("")
    dest = tmp_path / "clean" / "proj"
    materialize_clean_room(proj, head_sha(proj), session, ["../sib"], dest)
    sib_out = dest.parent / "sib"
    assert (sib_out / ".git" / "HEAD").read_bytes() == (sib / ".git" / "HEAD").read_bytes()
    assert (sib_out / ".git" / "refs").is_dir()


def test_cleanroom_sibling_gitfile_pointer_copied(tmp_path):
    """Worktree-style .git pointer file is copied into the room.

    Kills mutants on the gitlink branch (cleanroom.py:288-293).
    """
    proj = tmp_path / "proj"
    _init_repo(proj)
    sib = tmp_path / "sib"
    session = tmp_path / "sess"
    session.mkdir()
    (session / "patch.diff").write_bytes(b"")
    (session / "untracked.txt").write_text("")
    (session / "patch_sib.diff").write_bytes(b"")
    (session / "untracked_sib.txt").write_text("")
    # Real linked worktree: .git is a pointer file, archive works.
    _init_repo(sib)
    linked = tmp_path / "linked"
    subprocess.run(["git", "worktree", "add", str(linked)], cwd=str(sib),
                   check=True, capture_output=True)
    assert (linked / ".git").is_file()
    dest = tmp_path / "clean" / "proj"
    materialize_clean_room(proj, head_sha(proj), session, ["../linked"], dest)
    linked_out = dest.parent / "linked"
    assert (linked_out / ".git").is_file()
    assert (linked_out / ".git").read_bytes() == (linked / ".git").read_bytes()


def test_cleanroom_sibling_without_git_fails_closed(tmp_path):
    import pytest
    from tether.cleanroom import CleanRoomError
    proj = tmp_path / "proj"
    _init_repo(proj)
    sib = tmp_path / "sib"
    sib.mkdir()  # exists on disk but is not a git repository
    (sib / "f.txt").write_text("v1\n")
    session = tmp_path / "sess"
    session.mkdir()
    (session / "patch.diff").write_bytes(b"")
    (session / "untracked.txt").write_text("")
    (session / "patch_sib.diff").write_bytes(b"")
    (session / "untracked_sib.txt").write_text("")
    with pytest.raises(CleanRoomError):
        materialize_clean_room(proj, head_sha(proj), session,
                               ["../sib"], tmp_path / "clean" / "proj")
