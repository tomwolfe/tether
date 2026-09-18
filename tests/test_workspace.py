"""Workspace multi-repo tests."""
from pathlib import Path
from tether import git_safety as gs

def test_resolve_workspace_repos(tmp_path):
    base = tmp_path / "proj"; base.mkdir()
    out = gs.resolve_workspace_repos(base, ["../QED", "../VeriTrial"])
    assert len(out) == 2
    assert str(out[0]).endswith("QED")

def test_workspace_helpers_no_repo(tmp_path):
    base = tmp_path / "proj"; base.mkdir()
    assert gs.workspace_is_dirty(base, []) is False
    infos = gs.workspace_create_checkpoint(base, "s1", [], write_ref=False)
    assert str(base) in infos
    ok, msg = gs.workspace_rollback(base, "s1", [])
    assert isinstance(ok, bool)
