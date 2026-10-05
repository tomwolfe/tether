#!/usr/bin/env python3
"""Unified ledger: audit tether/QED/VeriTrial repos into SYSTEM_STATE.json.

Two modes:

* ``--write`` (default) recompute the ledger and overwrite it.
* ``--check`` recompute and compare against the committed file WITHOUT writing,
  exiting non-zero on any drift.

WHY ``--check`` EXISTS. Writing the ledger is self-invalidating for the tether
row: the file being written lives inside the tether repo, so committing it moves
the tether ``head`` that the file records. The row therefore always trails live
HEAD by (at least) the ledger's own commit, and that is a *structural* fact
rather than staleness -- see ``_is_ledger_host`` and ``verify``.

The practical consequence was that drift was undetectable. Running the script
silently rewrote the file, so a committed ledger that no longer described any
live tree looked exactly as healthy as a current one: ``52f21c3c`` sat in the
committed file for several commits after both its own repo head and its recorded
tether head had moved on. Nothing failed, because nothing compared.

``--check`` closes that. It is read-only, so it can run in CI or a pre-commit
hook without the write-dirty-the-tree problem that made this awkward before.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPOS = ["tether", "QED", "VeriTrial"]

#: The repo whose checkout contains this script and therefore SYSTEM_STATE.json.
LEDGER_HOST = "tether"


def _is_ledger_host(name: str) -> bool:
    """True for the repo that physically stores the ledger.

    That row's ``head`` can never equal live HEAD: writing the ledger changes
    the tree, and committing it creates a new commit. Rather than leave that as
    a convention a reader has to know about, the row is marked ``ledger_host``
    so the schema itself declares the exemption.
    """
    return name == LEDGER_HOST


def ledger_path() -> Path:
    """Absolute path of the committed ledger file."""
    return ROOT / LEDGER_HOST / "SYSTEM_STATE.json"


def _comparable(rows: list[dict]) -> list[dict]:
    """*rows* with the self-referential field removed.

    This projection is what the merkle root is computed over. It has to be,
    because the ledger host's ``head`` is inside the ledger and therefore
    changes every time the ledger is committed: hashing it would guarantee the
    root differs from its own freshly-written value on every single run, so a
    byte-comparing verifier could never report green.

    ``merkle_root`` consequently means "the state of everything that *can* be
    named" -- every head, dirty flag, sorry_free verdict and Lean digest of all
    three repos, minus the one field that is structurally unnameable. It still
    moves on any real change to any repo; it just stops being self-invalidating.
    """
    out = []
    for row in rows:
        r = {k: v for k, v in row.items()
             if not (k == "head" and row.get("ledger_host"))}
        out.append(r)
    return out


def _compute() -> dict:
    """The ledger as live state actually describes it right now."""
    repos = [audit_repo(r) for r in REPOS]
    blob = json.dumps(_comparable(repos), sort_keys=True).encode()
    return {"repos": repos,
            "merkle_root": hashlib.sha256(blob).hexdigest()}


def _diff(expected: dict, actual: dict) -> list[str]:
    """Human-readable field-level differences, ledger_host rows excluded.

    The ledger_host row is skipped because it is *expected* to differ -- that is
    the self-reference, not drift. Reporting it would train a reader to ignore
    the one line that matters.
    """
    out: list[str] = []
    actual_rows = {r["repo"]: r for r in actual.get("repos", [])}
    for exp in expected["repos"]:
        name = exp["repo"]
        act = actual_rows.get(name)
        if act is None:
            out.append(f"{name}: absent from committed ledger")
            continue
        for key, want in exp.items():
            if key == "head" and _is_ledger_host(name):
                continue
            got = act.get(key)
            if got != want:
                out.append(f"{name}.{key}: committed={got!r} live={want!r}")
    if expected["merkle_root"] != actual.get("merkle_root"):
        out.append(
            f"merkle_root: committed={actual.get('merkle_root')!r} "
            f"live={expected['merkle_root']!r}")
    return out


def verify() -> int:
    """Read-only drift check. Returns 0 if the ledger matches live state."""
    path = ledger_path()
    if not path.is_file():
        print(f"ledger missing: {path}", file=sys.stderr)
        return 1
    try:
        actual = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"ledger unreadable: {exc}", file=sys.stderr)
        return 1
    diffs = _diff(_compute(), actual)
    if not diffs:
        print(f"ledger OK: merkle {actual.get('merkle_root')}")
        return 0
    print(f"ledger STALE: {len(diffs)} field(s) disagree with live state:",
          file=sys.stderr)
    for line in diffs:
        print(f"  {line}", file=sys.stderr)
    print("  re-run: python3 scripts/audit_system_state.py --write",
          file=sys.stderr)
    return 1

class WorkspaceNotFoundError(RuntimeError):
    """No ancestor directory contains all three workspace repos."""


def _find_workspace_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` until a dir containing all three repos.

    Fail-closed: raises :class:`WorkspaceNotFoundError` instead of guessing.
    """
    anchor = (start or Path(__file__)).resolve()
    for parent in anchor.parents:
        if all((parent / r).is_dir() for r in REPOS):
            return parent
    raise WorkspaceNotFoundError(
        f"no workspace root with {REPOS} found above {anchor}")

ROOT = _find_workspace_root()

def _run(repo: Path, *args: str) -> str:
    try:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else ""
    except OSError:
        return ""

def _is_dirty(name: str) -> bool:
    """Is *name* dirty, ignoring the ledger's own file?

    The ledger file lives in the tether checkout, so writing it makes the tether
    repo dirty by definition. Counting that as drift is wrong twice over: it is
    true of every freshly-written ledger, and it is the ledger judging itself.

    ``dirty`` therefore means "this repo has uncommitted changes *other than the
    ledger itself*". Any other uncommitted work is still reported, which is the
    property that matters.
    """
    repo = ROOT / name
    if not _is_ledger_host(name):
        return bool(_run(repo, "status", "--porcelain"))
    ledger_name = ledger_path().name
    for line in _run(repo, "status", "--porcelain").splitlines():
        entry = line[3:].strip().strip('"')
        if entry == ledger_name:
            continue
        return True
    return False


def audit_repo(name: str) -> dict:
    repo = ROOT / name
    head = _run(repo, "rev-parse", "HEAD")
    dirty = _is_dirty(name)
    rec = {"repo": name, "head": head, "dirty": dirty,
           "sorry_free": _check_sorry(repo),
           "lean_files": _lean_digests(repo)}
    if _is_ledger_host(name):
        # This row's head is exempt from the "must equal live HEAD" check; see
        # _is_ledger_host. Recorded in the schema so the exemption is data a
        # verifier can read rather than prose it has to trust.
        rec["ledger_host"] = True
    return rec


def _lean_digests(repo: Path) -> dict[str, str]:
    """SHA-256 of every Lean file in this repo's verified surface.

    WHY THIS EXISTS. The tri-repo gate's mission file documents a deliberate
    LEDGER DUALITY: the host checkout's ``QED/VeriTrialExport.lean`` is Fin 14
    (committed at QED 8207365), while the clean room's copy of that same path
    is rewritten to Fin 6 for ``rebuild_qed_oleans.py`` and the ``#print
    axioms`` assertions. Two ledgers therefore cover two different Lean
    artifacts. That is only a coherent story if this ledger names WHICH Lean
    bytes it covers -- otherwise "the host ledger hashes the Fin 14 file" is
    an unverifiable claim, and a reader cannot tell the two ledgers apart.

    It did not. This function's caller previously recorded only
    ``head``/``dirty``/``sorry_free``, and ``_lean_files`` -- which does
    enumerate exactly this surface, including the sibling QED export for
    VeriTrial -- was consumed solely by the ``sorry`` scan. The merkle_root
    was consequently a hash over three commits and three booleans: it did not
    cover a single byte of Lean, so it could not change when a theorem file
    changed. These digests close that gap.

    Keys are repo-relative POSIX paths, except the sibling QED export, which
    is keyed ``QED/VeriTrialExport.lean`` so the ledger records that it is a
    borrowed cross-repo artifact rather than VeriTrial's own file.

    FAIL-LOUD on unreadable files. An earlier revision wrapped the whole loop
    in ``except OSError: return {}``, which is indistinguishable from "this
    repo genuinely ships no Lean" -- so a permissions or I/O fault silently
    shrank the ledger's coverage to nothing and the merkle_root still looked
    healthy. The gate's reviewer named exactly this nit on the 2026-10-04 run.
    A missing repo and a repo with no ``.lean`` files are both legitimately
    ``{}``; a file we *found* and could not read is not, and is recorded as
    ``"<unreadable>"`` so it is visible in the ledger rather than inferred
    from its absence.
    """
    digests: dict[str, str] = {}
    repo = repo.resolve()
    for p in _lean_files(repo):
        if not p.is_file():
            continue
        try:
            key = p.resolve().relative_to(repo).as_posix()
        except ValueError:
            # The sibling QED export lives outside VeriTrial's tree.
            key = f"{p.resolve().parent.name}/{p.name}"
        try:
            digests[key] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError as exc:
            digests[key] = f"<unreadable: {exc.errno}>"
    return dict(sorted(digests.items()))

def _lean_files(repo: Path) -> list[Path]:
    """Every Lean source in *repo* that is part of the verified surface.

    Skips vendored/derived trees (.lake, .venv, output) and caches. For
    VeriTrial the sibling QED/VeriTrialExport.lean is included as well: it is
    the Lean artifact VeriTrial itself generates and ships to the prover, so
    it is squarely in this repo's verified surface even though the file
    physically lives next door.
    """
    skip = {".lake", ".venv", "output", "__pycache__", ".git", "node_modules"}
    found: list[Path] = []
    for p in repo.rglob("*.lean"):
        if skip & set(p.relative_to(repo).parts):
            continue
        found.append(p)
    if repo.name == "VeriTrial":
        sibling = ROOT / "QED" / "VeriTrialExport.lean"
        if sibling.is_file():
            found.append(sibling)
    return sorted(found)

def _check_sorry(repo: Path):
    """sorry-freedom of *repo*: True, False, or "n/a" if it ships no Lean.

    Every repo is checked, not just QED. This used to report "unknown" for
    tether and VeriTrial by construction, so the tri-repo ledger could only
    ever certify one of the three repositories it claims to cover -- and
    "unknown" is exactly the kind of soft pass that survives review.

    "n/a" is reported when a repo ships no Lean at all (tether is a pure
    Python control plane). Claiming True there would assert a soundness
    property of nothing; claiming False would libel a repo that simply has no
    theorems.
    """
    try:
        import re as _re
        files = _lean_files(repo)
        if not files:
            return "n/a"
        hits = []
        for p in files:
            if not p.is_file():
                continue
            t = p.read_text(encoding="utf-8", errors="replace")
            t = _re.sub(r"/-.*?-/", "", t, flags=_re.DOTALL)
            lines = [ln for ln in t.splitlines() if not ln.strip().startswith("--")]
            if _re.search(r"\bsorry\b", "\n".join(lines)):
                hits.append(p.name)
        return not hits
    except OSError:
        return False

def main(argv: list[str] | None = None) -> int:
    # argv defaults to [] rather than None. argparse would otherwise fall back to
    # sys.argv, so importing this module and calling main() from a test runner
    # would try to parse pytest's own command line and exit 2.
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true",
                      help="recompute and overwrite the ledger (default)")
    mode.add_argument("--check", action="store_true",
                      help="verify the committed ledger against live state; "
                           "read-only, exits non-zero on drift")
    args = ap.parse_args([] if argv is None else argv)

    if args.check:
        return verify()

    state = _compute()
    ledger_path().write_text(json.dumps(state, indent=2))
    print(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
