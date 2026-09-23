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

def _check_sorry(repo: Path) -> bool:
    try:
        import re as _re
        hits = []
        lean_files = [p for p in repo.rglob("*.lean") if ".lake" not in p.parts]
        if not lean_files:
            return True  # vacuously sorry-free: no Lean sources in this repo
        for p in lean_files:
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
