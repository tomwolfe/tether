"""Workspace multi-repo tests."""
import subprocess
from pathlib import Path
from tether import git_safety as gs


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(path), check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=str(path),
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(path),
                   check=True, capture_output=True)
    (path / "f.txt").write_text("v1\n")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), check=True,
                   capture_output=True)


def test_resolve_workspace_repos(tmp_path):
    base = tmp_path / "proj"
    base.mkdir()
    out = gs.resolve_workspace_repos(base, ["../QED", "../VeriTrial"])
    assert len(out) == 2
    assert str(out[0]).endswith("QED")


def test_workspace_helpers_no_repo(tmp_path):
    base = tmp_path / "proj"
    base.mkdir()
    assert gs.workspace_is_dirty(base, []) is False
    infos = gs.workspace_create_checkpoint(base, "s1", [], write_ref=False)
    assert str(base) in infos
    ok, msg = gs.workspace_rollback(base, "s1", [])
    assert isinstance(ok, bool)


def test_dirty_sibling_detected(tmp_path):
    proj = tmp_path / "proj"
    sib = tmp_path / "sib"
    _init_repo(proj)
    _init_repo(sib)
    assert gs.workspace_is_dirty(proj, ["../sib"]) is False
    (sib / "f.txt").write_text("dirty\n")
    assert gs.workspace_is_dirty(proj, ["../sib"]) is True
    # allow_dirty=False checkpoint path still records dirty state
    infos = gs.workspace_create_checkpoint(
        proj, "sess-dirty", ["../sib"], allow_dirty=False, write_ref=False)
    assert infos[str(proj)].dirty is False
    assert infos[str(sib.resolve())].dirty is True


def test_workspace_rollback_reverts_all(tmp_path):
    proj = tmp_path / "proj"
    sib = tmp_path / "sib"
    _init_repo(proj)
    _init_repo(sib)
    gs.workspace_create_checkpoint(
        proj, "sess-rb", ["../sib"], allow_dirty=True, write_ref=True)
    (proj / "f.txt").write_text("changed-proj\n")
    (sib / "f.txt").write_text("changed-sib\n")
    assert gs.workspace_is_dirty(proj, ["../sib"]) is True
    ok, _msg = gs.workspace_rollback(
        proj, "sess-rb", ["../sib"], clean=True)
    assert ok is True
    assert (proj / "f.txt").read_text() == "v1\n"
    assert (sib / "f.txt").read_text() == "v1\n"
    assert gs.workspace_is_dirty(proj, ["../sib"]) is False


def test_orchestrator_dirty_sibling_fails(tmp_path):
    from tether.models import MissionContract, TetherConfig
    from tether.orchestrator import Orchestrator
    from tether.adapters.mock import MockAdapter
    proj = tmp_path / "proj"
    sib = tmp_path / "sib"
    _init_repo(proj)
    _init_repo(sib)
    (sib / "f.txt").write_text("dirty\n")
    mission = MissionContract(
        mission={"name": "m", "goal": "g"}, name="m", goal="g",
        workspace_repos=["../sib"],
        verification={"commands": []},
    )
    config = TetherConfig()
    orch = Orchestrator(MockAdapter(), config, proj, session_id="sess-orch1")
    report = orch.run(mission, allow_dirty=False, dry_run=True)
    assert report["status"] == "failed"
    assert "dirty" in " ".join(report.get("next_steps", [])).lower()
