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
    # The root is taken over the comparable projection, which drops the ledger
    # host's own head -- that field changes whenever the ledger is committed, so
    # including it would make a fresh write disagree with itself forever.
    expected = hashlib.sha256(
        json.dumps(mod._comparable(payload["repos"]), sort_keys=True).encode()
    ).hexdigest()
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


# ------------------- --check: making ledger drift detectable
#
# WHY THIS EXISTS. Writing SYSTEM_STATE.json lives inside the tether checkout,
# so the tether row's `head` can never equal live HEAD -- committing the ledger
# creates the very commit that would have to be recorded. That much is structural
# and the report states it as such.
#
# The defect was what happened NEXT. The write path silently overwrote the file,
# so there was no way to ask "does the committed ledger still describe a live
# tree?" A stale ledger was indistinguishable from a current one: merkle
# 52f21c3c sat in the committed file for several commits after the recorded
# heads had moved on, and nothing failed, because nothing compared.
# `--check` is the missing comparison.


def _write_ledger(mod, ws) -> None:
    (ws / "tether" / "SYSTEM_STATE.json").write_text(
        json.dumps(mod._compute(), indent=2))


def test_check_passes_on_a_freshly_written_ledger(tmp_path, monkeypatch):
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    _write_ledger(mod, ws)
    assert mod.verify() == 0


def test_check_is_read_only(tmp_path, monkeypatch):
    """A verification must never rewrite the ledger it is verifying.

    Otherwise running the check would itself dirty the tree and change the very
    state being checked, which is how the write-dirty-the-tree problem that
    made this awkward in the first place would come straight back.
    """
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    _write_ledger(mod, ws)
    before = (ws / "tether" / "SYSTEM_STATE.json").read_bytes()
    mod.verify()
    assert (ws / "tether" / "SYSTEM_STATE.json").read_bytes() == before


def test_check_fails_when_a_sibling_head_moves(tmp_path, monkeypatch):
    """The regression this closes: a moved head used to be undetectable."""
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    _write_ledger(mod, ws)
    assert mod.verify() == 0

    qed = ws / "QED"
    (qed / "f.txt").write_text("v2\n")
    _git(qed, "add", ".")
    _git(qed, "commit", "-m", "move QED head")

    assert mod.verify() == 1


def test_check_fails_when_lean_content_changes_without_a_commit(tmp_path, monkeypatch):
    """An uncommitted .lean edit must still move the verdict.

    This is the specific hole the ledger's own history documents: merkle_root
    once hashed only commit shas and booleans, so it could not move when a byte
    of Lean changed. If --check inherited that blindness it would be worse than
    no check at all, because it would report "OK" with authority.
    """
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    (ws / "QED" / "T.lean").write_text("theorem t : True := trivial\n")
    monkeypatch.setattr(mod, "ROOT", ws)
    _write_ledger(mod, ws)
    assert mod.verify() == 0

    (ws / "QED" / "T.lean").write_text(
        "theorem t : True := by sorry\n")  # sorry, and a different digest
    assert mod.verify() == 1


def test_check_fails_on_a_tampered_merkle_root(tmp_path, monkeypatch):
    """A hand-edited root must not be able to pass as a real measurement."""
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    _write_ledger(mod, ws)
    path = ws / "tether" / "SYSTEM_STATE.json"
    payload = json.loads(path.read_text())
    payload["merkle_root"] = "0" * 64
    path.write_text(json.dumps(payload, indent=2))
    assert mod.verify() == 1


def test_missing_ledger_fails_loudly(tmp_path, monkeypatch):
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    assert mod.verify() == 1


def test_malformed_ledger_fails_loudly(tmp_path, monkeypatch):
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    (ws / "tether" / "SYSTEM_STATE.json").write_text("{not json")
    assert mod.verify() == 1


def test_ledger_host_row_is_exempt_from_head_comparison(tmp_path, monkeypatch):
    """The self-reference is exempt -- but ONLY the head, never the rest.

    The tether row's head can never equal live HEAD, so a verifier that
    demanded it would be permanently red and therefore permanently ignored.
    But that exemption must not extend to dirty, sorry_free or lean_files: if
    it did, the one row able to hide real drift would be the one row not
    checked.
    """
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    _write_ledger(mod, ws)

    # Advance tether's head past what the ledger recorded.
    tether = ws / "tether"
    (tether / "f.txt").write_text("v2\n")
    _git(tether, "add", ".")
    _git(tether, "commit", "-m", "move tether head")
    assert mod.verify() == 0, "head self-reference must be exempt"

    # But dirty state on that same row is NOT exempt.
    (tether / "f.txt").write_text("v3-uncommitted\n")
    assert mod.verify() == 1, "tether dirty must still be caught"


def test_ledger_host_marker_is_recorded_for_the_host_only(tmp_path, monkeypatch):
    """The exemption is data in the schema, not prose a reader must trust."""
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    rows = {r["repo"]: r for r in mod._compute()["repos"]}
    assert rows["tether"].get("ledger_host") is True
    assert "ledger_host" not in rows["QED"]
    assert "ledger_host" not in rows["VeriTrial"]


def test_merkle_root_excludes_the_self_referential_head_only(tmp_path, monkeypatch):
    """The projection drops exactly one field -- and only from the host row.

    Too narrow and the root stays self-invalidating; too wide and it stops
    detecting real drift. This pins both halves: the ledger host's head is
    excluded, while every other head, flag and digest is still inside the hash.
    """
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    (ws / "QED" / "T.lean").write_text("theorem t : True := trivial\n")
    monkeypatch.setattr(mod, "ROOT", ws)
    rows = mod._compute()["repos"]
    projected = mod._comparable(rows)
    assert "head" not in projected[0], "host head must be excluded"
    assert "head" in projected[1], "QED head must remain"
    assert "head" in projected[2], "VeriTrial head must remain"
    # Everything except the one field survives the projection.
    assert set(projected[0]) == set(rows[0]) - {"head"}


def test_merkle_root_still_moves_when_a_sibling_head_moves(tmp_path, monkeypatch):
    """Excluding the host head must not blunt the root's sensitivity."""
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    before = mod._compute()["merkle_root"]

    qed = ws / "QED"
    (qed / "f.txt").write_text("v2\n")
    _git(qed, "add", ".")
    _git(qed, "commit", "--allow-empty", "-m", "advance QED")

    assert mod._compute()["merkle_root"] != before


def test_diff_reports_tether_dirty_even_though_its_head_is_exempt(tmp_path, monkeypatch):
    """Pin the field-level exemption, not just the exit code.

    WHY THIS TEST IS NEEDED. A narrower mutant -- one where _diff skips the
    host row's `dirty` as well as its `head` -- SURVIVED the first version of
    this suite. verify() still exited 1, so the mutant looked dead, but only
    because merkle_root independently moved. The check failed for the wrong
    reason and the narrowing went unnoticed: exactly the "the gate ran
    something, so it reported covered" trap, one level down.

    So this asserts on _diff's output directly. merkle_root is a redundant
    signal here, and relying on it is what hid the defect.
    """
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    _write_ledger(mod, ws)
    committed = json.loads((ws / "tether" / "SYSTEM_STATE.json").read_text())

    (ws / "tether" / "f.txt").write_text("uncommitted\n")
    diffs = mod._diff(mod._compute(), committed)

    assert any("tether.dirty" in d for d in diffs), diffs
    assert not any("tether.head" in d for d in diffs), diffs


def test_check_catches_an_uncommitted_edit_in_a_sibling_repo(tmp_path, monkeypatch):
    """A dirty sibling is drift, and nothing else would catch it.

    The host's `dirty` is exempted from nothing, but the sibling rows' dirty
    flags had no direct coverage at all: the surviving mutant that made
    `_is_dirty` always return False for the host passed every other test in
    this file. Sibling dirty state is now pinned from both directions -- clean
    is accepted, uncommitted is rejected.
    """
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    _write_ledger(mod, ws)
    assert mod.verify() == 0, "all clean is the passing baseline"

    (ws / "VeriTrial" / "f.txt").write_text("uncommitted\n")
    assert mod.verify() == 1, "uncommitted sibling work must be drift"


def test_diff_reports_sibling_dirty_by_name(tmp_path, monkeypatch):
    """Same pin as the host case, for a sibling row."""
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    _write_ledger(mod, ws)
    committed = json.loads((ws / "tether" / "SYSTEM_STATE.json").read_text())

    (ws / "QED" / "f.txt").write_text("uncommitted\n")
    diffs = mod._diff(mod._compute(), committed)
    assert any("QED.dirty" in d for d in diffs), diffs


def test_ledger_only_modification_does_not_count_as_dirty(tmp_path, monkeypatch):
    """Regression: the exemption actually exempts.

    SHIPPED BUG, caught by running the script rather than only its tests. The
    parser sliced ``entry[3:]`` off ``_run(...)`` output, but ``_run`` applies
    ``.strip()``, which eats the leading space of ``" M SYSTEM_STATE.json"`` and
    yields ``"M SYSTEM_STATE.json"``. The slice then cut into the filename and
    produced ``"YSTEM_STATE.json"``, so the ledger's own file never matched and
    the exemption never fired.

    Every existing test still passed, because they only ever assert dirty=True
    when OTHER files are dirty -- a broken exemption is invisible to them. This
    pins the negative direction, which is the only one that can catch it.
    """
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    _write_ledger(mod, ws)

    assert not mod._is_dirty("tether"), \
        "the ledger's own file must not count as dirty"

    # Any OTHER uncommitted change must still register.
    (ws / "tether" / "f.txt").write_text("real change\n")
    assert mod._is_dirty("tether"), "genuine uncommitted work must count"


def test_status_parser_survives_the_strip_that_broke_it(tmp_path, monkeypatch):
    """Pin the porcelain prefix handling directly.

    The bug above was a whitespace interaction between ``_run``'s ``.strip()``
    and a fixed-offset slice. Asserting on parsed paths rather than on the
    end-to-end boolean keeps the failure legible if anyone routes this through
    a stripping helper again.
    """
    mod = _load()
    assert mod._status_path(" M SYSTEM_STATE.json") == "SYSTEM_STATE.json"
    assert mod._status_path("M  SYSTEM_STATE.json") == "SYSTEM_STATE.json"
    assert mod._status_path("?? SYSTEM_STATE.json") == "SYSTEM_STATE.json"
    # Renames carry "orig -> dest"; only the destination is the live path.
    assert mod._status_path("R  old.json -> SYSTEM_STATE.json") \
        == "SYSTEM_STATE.json"


def test_status_entries_do_not_go_through_run(tmp_path, monkeypatch):
    """Pin the ACTUAL original bug: ``_run``'s strip eating the XY prefix.

    Reintroducing a ``[3:]`` slice does NOT test this, because on a clean
    ``" M SYSTEM_STATE.json"`` line both ``[2:]`` and ``[3:]`` yield the right
    path -- verified, and the mutant survives for that reason rather than
    because the suite is blind.

    The shipped defect was the *interaction*: routing porcelain through a helper
    that calls ``.strip()``. That turns ``" M SYSTEM_STATE.json"`` into
    ``"M SYSTEM_STATE.json"``, and THEN any fixed offset slices into the
    filename. So this pins the helper's own contract -- the prefix must survive
    -- rather than a slice arithmetic that happens to be equivalent.
    """
    mod = _load()
    ws = tmp_path / "ws"
    _init_repo(ws / "tether")
    (ws / "tether" / "SYSTEM_STATE.json").write_text("{}\n")
    entries = mod._status_entries(ws / "tether")
    assert entries, "the modified ledger must appear in the status"
    # The status prefix must still be present, i.e. not stripped away.
    assert all(len(e) >= 3 and e[:2].strip() for e in entries), entries
    # Precondition, stated precisely: _run's .strip() removes the leading space
    # that _status_entries preserves. Commit the file first so the porcelain
    # entry is the " M" (unstaged modification) form that actually has a leading
    # space -- "??" (untracked) and "A " (staged add) do not exercise this.
    _git(ws / "tether", "add", "SYSTEM_STATE.json")
    _git(ws / "tether", "commit", "-m", "track the ledger")
    (ws / "tether" / "SYSTEM_STATE.json").write_text('{"edited": true}\n')
    raw = mod._run(ws / "tether", "status", "--porcelain")
    assert raw.startswith("M"), f"_run stripped the XY prefix: {raw!r}"
    kept = mod._status_entries(ws / "tether")
    assert kept and kept[0].startswith(" M"), kept


def test_status_entries_fail_closed_when_git_is_unavailable(tmp_path, monkeypatch):
    """An unreadable status must read as dirty, not as clean.

    Reporting "clean" on a git failure is a soft pass of exactly the kind this
    ledger already had one of: coverage silently degrading to nothing while the
    output still looked healthy.
    """
    mod = _load()
    ws = tmp_path / "ws"
    _init_repo(ws / "tether")
    import subprocess as _sp

    def _boom(*a, **k):
        raise OSError("no git")

    monkeypatch.setattr(_sp, "run", _boom)
    entries = mod._status_entries(ws / "tether")
    assert entries, "must not report an empty (i.e. clean) status"
    assert mod._is_ledger_host("tether")


def test_write_then_check_agrees_with_the_committed_file(tmp_path, monkeypatch):
    """Round-trip: what --write produces, --check must accept.

    Guards against the two paths computing the ledger differently -- the exact
    class of bug where the writer and the verifier each trust their own
    serialisation and quietly disagree about what the ledger means.
    """
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial"):
        _init_repo(ws / r)
    (ws / "QED" / "T.lean").write_text("theorem t : True := trivial\n")
    monkeypatch.setattr(mod, "ROOT", ws)
    assert mod.main(["--write"]) == 0
    assert mod.verify() == 0
