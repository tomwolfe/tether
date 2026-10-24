"""Tests for scripts/audit_system_state.py (tri-repo ledger).

Loads the script by path (it lives outside the package) and exercises it
against real throwaway repos: workspace-root discovery, sorry auditing with
Lean comment stripping (line + block comments), and Merkle determinism.
"""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

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
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
        (ws / r).mkdir(parents=True)
    nested = ws / "tether" / "scripts" / "audit_system_state.py"
    assert nested.parent in list(nested.resolve().parents)
    # Simulate: root is found by walking up while all the repos exist.
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
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
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
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
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
    """The ledger must cover the Lean bytes, not just the commits.

    Regression test for a real ledger bug: ``_lean_files`` existed but was
    consumed ONLY by the ``sorry`` scan, so ``merkle_root`` hashed just
    head/dirty/sorry_free and could not move when a theorem file changed --
    while the gate's mission file claimed this ledger "hashes the host's Fin
    14 file into SYSTEM_STATE.json".
    """
    import hashlib

    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
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
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
        _init_repo(ws / r)
    (ws / "QED" / "VeriTrialExport.lean").write_text("theorem t : True := trivial\n")
    monkeypatch.setattr(mod, "ROOT", ws)
    rec = mod.audit_repo("VeriTrial")
    assert "QED/VeriTrialExport.lean" in rec["lean_files"]


def test_lean_free_repo_records_empty_digests_not_a_missing_key(tmp_path, monkeypatch):
    """A repo shipping no Lean gets ``{}``, so the key is always present."""
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    rec = mod.audit_repo("tether")
    assert rec["lean_files"] == {}
    assert rec["sorry_free"] == "n/a"


def test_lean_digests_skip_vendored_trees(tmp_path, monkeypatch):
    """.lake / .venv / output Lean must not enter the verified surface."""
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
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
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
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
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
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
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
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
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
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
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
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
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
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
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    assert mod.verify() == 1


def test_malformed_ledger_fails_loudly(tmp_path, monkeypatch):
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
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
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
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
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
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
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
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
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
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
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
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
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
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
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
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
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
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


def test_extra_row_for_a_nonexistent_repo_is_drift(tmp_path, monkeypatch):
    """The committed ledger must not be able to ADD rows that live state lacks.

    _diff originally walked only the live rows, so it reported what was missing
    from the committed file and said nothing about what the committed file had
    added. Appending a well-formed row for a repo that does not exist therefore
    passed as "ledger OK" -- the one direction a tampered ledger actually needs.
    The comparison is now bidirectional.
    """
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    _write_ledger(mod, ws)
    path = ws / "tether" / "SYSTEM_STATE.json"
    payload = json.loads(path.read_text())
    payload["repos"].append({"repo": "ghost", "head": "f" * 40, "dirty": False,
                             "sorry_free": True, "lean_files": {}})
    path.write_text(json.dumps(payload, indent=2))
    assert mod.verify() == 1


def test_ledger_host_cannot_be_planted_on_a_sibling_to_exempt_its_head(
        tmp_path, monkeypatch):
    """Marking a sibling ``ledger_host`` must not buy it the head exemption.

    That flag is what disables the head comparison. If a committed file could set
    it on QED or VeriTrial, their heads would stop being checked -- so the two
    repos whose heads are genuinely certified would become unauditable by
    editing the ledger.
    """
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    _write_ledger(mod, ws)
    path = ws / "tether" / "SYSTEM_STATE.json"
    payload = json.loads(path.read_text())
    for row in payload["repos"]:
        if row["repo"] == "QED":
            row["ledger_host"] = True
    path.write_text(json.dumps(payload, indent=2))
    assert mod.verify() == 1, "a sibling must not be able to claim the exemption"


def test_duplicate_row_is_drift(tmp_path, monkeypatch):
    """A repeated repo must not collapse silently in the row lookup.

    Both sides are indexed by repo name, so a duplicate is invisible to a
    per-name comparison -- two rows for one repo, one of them fabricated.
    """
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    _write_ledger(mod, ws)
    path = ws / "tether" / "SYSTEM_STATE.json"
    payload = json.loads(path.read_text())
    payload["repos"].append(json.loads(json.dumps(payload["repos"][0])))
    path.write_text(json.dumps(payload, indent=2))
    assert mod.verify() == 1


@pytest.mark.parametrize("raw", [
    '{"repos": ["not-a-dict"]}',
    '{"repos": {"a": 1}}',
    "[1, 2, 3]",
    '"a bare string"',
    "",
    "null",
])
def test_malformed_ledger_fails_closed_without_crashing(tmp_path, monkeypatch, raw):
    """A verifier must not die on malformed input.

    It must fail closed AND say so. An uncaught AttributeError still exits
    non-zero, so a caller checking only the exit code reads a crash as a verdict
    -- indistinguishable from a genuine drift report, and it prints no reason.
    """
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    (ws / "tether" / "SYSTEM_STATE.json").write_text(raw)
    assert mod.verify() == 1


def test_valid_ledger_still_passes_after_the_hardening(tmp_path, monkeypatch):
    """The guard must not reject well-formed ledgers.

    Added because the hardening above rejects anything _rows() cannot parse, and
    a validator that fails closed on valid input is just as broken as one that
    fails open.
    """
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
        _init_repo(ws / r)
    (ws / "QED" / "T.lean").write_text("theorem t : True := trivial\n")
    monkeypatch.setattr(mod, "ROOT", ws)
    _write_ledger(mod, ws)
    assert mod.verify() == 0


def test_check_detects_a_caller_that_perturbs_the_tree_mid_check(
        tmp_path, monkeypatch):
    """Non-interference, and the regression that proves it is load-bearing.

    A caller once wrapped --check in a `git checkout --` / stash dance to
    normalise the room's tree. That produced a green check AND reverted
    QED/VeriTrialExport.lean on the way out, so the commands after it in the
    same ladder certified Fin 14 while the gate believed it certified Fin 6.
    Both suites exit 0: a green gate, a wrong answer.

    Drift detection cannot see this -- after the revert the tree just looks
    clean. So the check asks a second, separate question: did the tree move
    while I was running?
    """

    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    _write_ledger(mod, ws)

    target = ws / "tether" / "NOTES.md"
    target.write_bytes(b"original\n")
    original = target.read_bytes()
    _git(ws / "tether", "add", "NOTES.md")
    _git(ws / "tether", "commit", "-m", "notes")
    _write_ledger(mod, ws)
    # Also stage a tracked file in EACH repo. Perturbing only tether left three
    # mutants alive: a fingerprint that ignored file CONTENT, one that walked
    # only REPOS[:1], and one that compared before/after backwards. All three
    # are invisible when only the first repo is disturbed.
    siblings = {}
    for repo_name in ("QED", "VeriTrial"):
        p = ws / repo_name / "NOTES.md"
        p.write_bytes(b"original\n")
        siblings[repo_name] = (p, p.read_bytes())
        _git(ws / repo_name, "add", "NOTES.md")
        _git(ws / repo_name, "commit", "-m", "notes")
    _write_ledger(mod, ws)

    # Perturb a TRACKED file that the LEDGER never compares. The ledger looks
    # only at head/dirty/sorry_free/lean_files, so editing a Markdown file
    # moves the fingerprint without moving any ledger field.
    #
    # Two earlier attempts failed here and both are worth recording. A
    # gitignored file is invisible to the fingerprint (it uses
    # --exclude-standard), so nothing fired at all. A tracked file that was
    # merely MODIFIED flipped `tether.dirty`, so the drift branch returned 1 on
    # its own and the test passed with the guard disabled -- all four guard
    # mutants survived. This one is committed first, so editing its content
    # leaves `dirty` false: only the non-interference guard can see it.
    real_fp = mod._tree_fingerprint
    calls = {"n": 0}

    def counting_fp():
        calls["n"] += 1
        if calls["n"] >= 2:
            target.write_bytes(original + b"<!-- touched -->\n")
            for name, (p, orig) in siblings.items():
                p.write_bytes(orig + b"<!-- touched -->\n")
        return real_fp()

    monkeypatch.setattr(mod, "_tree_fingerprint", counting_fp)
    # Precondition BEFORE perturbing: the ledger must be clean, so that drift
    # cannot be what makes verify() fail. Checked after the perturbation it
    # would always be dirty and the assertion would be meaningless.
    committed = json.loads((ws / "tether" / "SYSTEM_STATE.json").read_text())
    assert not mod._diff(mod._compute(), committed), \
        "precondition: the ledger must already be fresh"
    try:
        rc = mod.verify()
        assert rc == 1, "a check whose tree moved cannot report OK"
        # The report must name the SIBLING repos too, not just the host.
        found = mod._tree_perturbations()
        for name in ("QED", "VeriTrial"):
            assert any(name in f for f in found), \
                f"{name} must appear in the perturbation report: {found}"
    finally:
        target.write_bytes(original)
        for p, orig in siblings.values():
            p.write_bytes(orig)


def test_perturbation_is_reported_even_when_the_ledger_is_also_stale(
        tmp_path, monkeypatch):
    """The guard must fire on the DRIFT path too.

    This is the bug this test was written after. The non-interference check was
    originally nested inside `if not diffs`, on the reasoning that it only
    mattered for a clean verdict. That is backwards: a perturbed tree almost
    always also shows drift, so the guard was skipped in exactly the case it
    exists for. Measured -- verify() returned 0 while a file was being rewritten
    underneath it, because the drift branch returned before the guard ran.

    Perturbation and staleness are independent faults and both must be named.
    """
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    _write_ledger(mod, ws)
    # Make the ledger genuinely stale at the same time: QED's head moves.
    qed = ws / "QED"
    (qed / "f.txt").write_text("v2\n")
    _git(qed, "add", ".")
    _git(qed, "commit", "-m", "advance QED")

    committed = json.loads((ws / "tether" / "SYSTEM_STATE.json").read_text())
    assert mod._diff(mod._compute(), committed), \
        "precondition: the ledger must be stale for this test to mean anything"

    # Perturb on the second fingerprint, exactly as the test above does, so the
    # tree changes WHILE verify() runs. Calling _tree_perturbations() directly
    # -- as the first version of this test did -- cannot catch the ordering bug,
    # because the bug lives in verify()'s control flow, not in the helper.
    target = ws / "tether" / "NOTES.md"
    target.write_bytes(b"original\n")
    _git(qed.parent / "tether", "add", "NOTES.md")
    _git(ws / "tether", "commit", "-m", "notes")

    real_fp = mod._tree_fingerprint
    calls = {"n": 0}

    def counting_fp():
        calls["n"] += 1
        if calls["n"] >= 2:
            target.write_bytes(target.read_bytes() + b"<!-- touched -->\n")
        return real_fp()

    monkeypatch.setattr(mod, "_tree_fingerprint", counting_fp)
    # Assert WHICH fault was reported, not just the exit code. With the guard
    # moved back inside `if not diffs`, this test still exited 1 -- via the
    # DRIFT branch -- so the mutant survived while the suite stayed green.
    # Drift is expected here, so exit 1 proves nothing on its own; the
    # perturbation message must be the one that appears.
    import io
    import contextlib
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = mod.verify()
    assert rc == 1
    assert "PERTURBED" in err.getvalue(), (
        "the guard must report the perturbation, not merely the pre-existing "
        f"drift; stderr was: {err.getvalue()[:300]}")
    assert "STALE" not in err.getvalue(), \
        "the perturbation must be reported BEFORE the drift report, so the " \
        "untrustworthy verdict is the one the reader sees first"


def test_clean_run_reports_no_perturbation(tmp_path, monkeypatch):
    """The guard must not fire on an untouched tree.

    Without this, a guard that always trips is indistinguishable from one that
    works, and the obvious 'fix' is to delete it.
    """
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
        _init_repo(ws / r)
    monkeypatch.setattr(mod, "ROOT", ws)
    _write_ledger(mod, ws)
    assert mod.verify() == 0


def test_write_then_check_agrees_with_the_committed_file(tmp_path, monkeypatch):
    """Round-trip: what --write produces, --check must accept.

    Guards against the two paths computing the ledger differently -- the exact
    class of bug where the writer and the verifier each trust their own
    serialisation and quietly disagree about what the ledger means.
    """
    mod = _load()
    ws = tmp_path / "ws"
    for r in ("tether", "QED", "VeriTrial", "DRIFT"):
        _init_repo(ws / r)
    (ws / "QED" / "T.lean").write_text("theorem t : True := trivial\n")
    monkeypatch.setattr(mod, "ROOT", ws)
    assert mod.main(["--write"]) == 0
    assert mod.verify() == 0
