#!/usr/bin/env python3
"""Unified ledger: audit tether/QED/VeriTrial repos into SYSTEM_STATE.json."""
from __future__ import annotations
import hashlib, json, subprocess, sys
from pathlib import Path

REPOS = ["tether", "QED", "VeriTrial"]

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

def audit_repo(name: str) -> dict:
    repo = ROOT / name
    head = _run(repo, "rev-parse", "HEAD")
    dirty = bool(_run(repo, "status", "--porcelain"))
    return {"repo": name, "head": head, "dirty": dirty,
            "sorry_free": _check_sorry(repo)}

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
            lines = [l for l in t.splitlines() if not l.strip().startswith("--")]
            if _re.search(r"\bsorry\b", "\n".join(lines)):
                hits.append(p.name)
        return not hits
    except OSError:
        return False

def main() -> int:
    repos = [audit_repo(r) for r in REPOS]
    blob = json.dumps(repos, sort_keys=True).encode()
    merkle = hashlib.sha256(blob).hexdigest()
    state = {"repos": repos, "merkle_root": merkle}
    (ROOT / "tether" / "SYSTEM_STATE.json").write_text(json.dumps(state, indent=2))
    print(json.dumps(state, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
