"""Tests for scripts/audit_system_state.py (tri-repo ledger).

Loads the script by path (it lives outside the package) and exercises it
against real throwaway repos: workspace-root discovery, sorry auditing with
Lean comment stripping (line + block comments), and Merkle determinism.
"""
import importlib.util
import json
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_system_state.py"


def _load():
    spec = importlib.util.spec_from_file_location("audit_system_state", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(dir: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(dir), *args], check=True, capture_output=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "t@t.t")
    _git(path, "config", "user.name", "t")
    (path / "f.txt").write_text("v1\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init")


def test_find_workspace_root_from_nested_script(tmp_path):
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        (ws / r).mkdir(parents=True)
    nested = ws / "tether" / "scripts" / "audit_system_state.py"
    assert nested.parent in list(nested.resolve().parents)
    # Simulate: root is found by walking up while all three repos exist.
    found = None
    for parent in nested.resolve().parents:
        if all((parent / r).is_dir() for r in mod.REPOS):
            found = parent
            break
    assert found == ws


def test_sorry_audit_ignores_comments(tmp_path):
    mod = _load()
    repo = tmp_path / "QED"
    repo.mkdir()
    (repo / "A.lean").write_text(
        "/- No `sorry` or `sorryAx` is used anywhere. -/\n"
        "-- sorry-free by construction\n"
        "def x : Nat := 1\n")
    (repo / "lakefile.lean").write_text("package QED\n")
    assert mod._check_sorry(repo) is True
    (repo / "B.lean").write_text("theorem t : True := by sorry\n")
    assert mod._check_sorry(repo) is False


def test_module_root_resolves_real_workspace():
    mod = _load()
    assert all((mod.ROOT / r).is_dir() for r in mod.REPOS)


def test_find_workspace_root_fail_closed(tmp_path):
    mod = _load()
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    import pytest
    with pytest.raises(mod.WorkspaceNotFoundError):
        mod._find_workspace_root(start=lonely / "deeper" / "x.py")


def test_run_oserror_returns_empty(monkeypatch):
    mod = _load()
    import subprocess as _sp

    def _boom(*a, **k):
        raise OSError("no git")
    monkeypatch.setattr(_sp, "run", _boom)
    assert mod._run(__import__("pathlib").Path("/nowhere"), "HEAD") == ""


def test_sorry_check_oserror_is_false(monkeypatch, tmp_path):
    mod = _load()
    (tmp_path / "A.lean").write_text("def x : Nat := 1\n")
    monkeypatch.setattr(
        mod.Path, "read_text",
        lambda *a, **k: (_ for _ in ()).throw(OSError("unreadable")))
    assert mod._check_sorry(tmp_path) is False


def test_non_qed_sorry_free_is_na_when_no_lean(tmp_path):
    mod = _load()
    _init_repo(tmp_path / "plain")
    monkeypatch_root = tmp_path
    orig_root = mod.ROOT
    mod.ROOT = monkeypatch_root
    try:
        rec = mod.audit_repo("plain")
    finally:
        mod.ROOT = orig_root
    assert rec["sorry_free"] == "n/a"
    assert len(rec["head"]) == 40


def test_main_end_to_end_writes_sorted_merkle(tmp_path, monkeypatch, capsys):
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    (ws / "QED" / "A.lean").write_text("def x : Nat := 1\n")
    monkeypatch.setattr(mod, "ROOT", ws)
    assert mod.main() == 0
    payload = json.loads((ws / "tether" / "SYSTEM_STATE.json").read_text())
    assert set(payload) == {"repos", "merkle_root"}
    import hashlib
    expected = hashlib.sha256(
        json.dumps(payload["repos"], sort_keys=True).encode()).hexdigest()
    assert payload["merkle_root"] == expected
    out = capsys.readouterr().out
    assert expected in out


def test_audit_repo_records_head_and_merkle_determinism(tmp_path, monkeypatch):
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    first = [mod.audit_repo(r) for r in mod.REPOS]
    second = [mod.audit_repo(r) for r in mod.REPOS]
    assert first == second
    for rec in first:
        assert len(rec["head"]) == 40
    blob = json.dumps(first, sort_keys=True).encode()
    import hashlib
    assert hashlib.sha256(blob).hexdigest() == hashlib.sha256(
        json.dumps(second, sort_keys=True).encode()).hexdigest()


def test_lean_files_are_hashed_into_the_merkle(tmp_path, monkeypatch):
    """The ledger must cover the Lean bytes, not just three commits.

    Regression test for a real ledger bug: ``_lean_files`` existed but was
    consumed ONLY by the ``sorry`` scan, so ``merkle_root`` hashed just
    head/dirty/sorry_free and could not move when a theorem file changed --
    while the gate's mission file claimed this ledger "hashes the host's Fin
    14 file into SYSTEM_STATE.json".
    """
    import hashlib

    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    lean = ws / "QED" / "VeriTrialExport.lean"
    lean.write_text("theorem t : True := trivial\n")
    monkeypatch.setattr(mod, "ROOT", ws)

    rec = mod.audit_repo("QED")
    assert rec["lean_files"]["VeriTrialExport.lean"] == hashlib.sha256(
        lean.read_bytes()).hexdigest()

    before = mod.audit_repo("QED")["lean_files"]["VeriTrialExport.lean"]
    lean.write_text("theorem t : True := trivial\n-- changed\n")
    after = mod.audit_repo("QED")["lean_files"]["VeriTrialExport.lean"]
    assert before != after, "merkle input must be sensitive to Lean content"


def test_veritrial_sibling_lean_export_is_keyed_as_borrowed(tmp_path, monkeypatch):
    """VeriTrial ships no Lean of its own but borrows QED's export.

    The sibling artifact must appear, keyed so a reader can tell it is not
    VeriTrial's own file -- this is the file the host/clean-room duality is
    about.
    """
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    (ws / "QED" / "VeriTrialExport.lean").write_text("theorem t : True := trivial\n")
    monkeypatch.setattr(mod, "ROOT", ws)
    rec = mod.audit_repo("VeriTrial")
    assert "QED/VeriTrialExport.lean" in rec["lean_files"]


def test_lean_free_repo_records_empty_digests_not_a_missing_key(tmp_path, monkeypatch):
    """A repo shipping no Lean gets ``{}``, so the key is always present."""
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    rec = mod.audit_repo("tether")
    assert rec["lean_files"] == {}
    assert rec["sorry_free"] == "n/a"


def test_lean_digests_skip_vendored_trees(tmp_path, monkeypatch):
    """.lake / .venv / output Lean must not enter the verified surface."""
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    for vendored in (".lake/packages/mathlib/X.lean", ".venv/Y.lean",
                     "output/Z.lean"):
        p = ws / "QED" / vendored
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("theorem v : True := trivial\n")
    (ws / "QED" / "Real.lean").write_text("theorem r : True := trivial\n")
    monkeypatch.setattr(mod, "ROOT", ws)
    digests = mod.audit_repo("QED")["lean_files"]
    assert list(digests) == ["Real.lean"]


def test_unreadable_lean_file_is_recorded_not_silently_dropped(tmp_path, monkeypatch):
    """A found-but-unreadable Lean file must not vanish from the ledger.

    Regression test for the soft pass the 2026-10-04 gate reviewer flagged:
    the digest loop used to be wrapped in ``except OSError: return {}``, which
    is indistinguishable from "this repo ships no Lean". A permissions or I/O
    fault would shrink coverage to nothing while the merkle_root still looked
    healthy. It is now recorded as an explicit ``<unreadable: ...>`` marker.
    """
    import hashlib

    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    good = ws / "QED" / "Good.lean"
    good.write_text("theorem g : True := trivial\n")
    bad = ws / "QED" / "Bad.lean"
    bad.write_text("theorem b : True := trivial\n")

    real_read_bytes = Path.read_bytes

    def flaky(self, *a, **kw):
        if self.name == "Bad.lean":
            raise OSError(13, "Permission denied")
        return real_read_bytes(self, *a, **kw)

    monkeypatch.setattr(Path, "read_bytes", flaky)
    monkeypatch.setattr(mod, "ROOT", ws)
    digests = mod.audit_repo("QED")["lean_files"]

    assert digests["Good.lean"] == hashlib.sha256(
        real_read_bytes(good)).hexdigest()
    assert digests["Bad.lean"].startswith("<unreadable:")
    # The ledger must not have degraded to an empty coverage claim.
    assert len(digests) == 2
