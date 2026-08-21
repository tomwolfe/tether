"""Lightweight file manifests for non-git change visibility (best-effort)."""
from __future__ import annotations

import os
from pathlib import Path

MANIFEST_EXCLUDED_DIRS = {".tether", ".git", "__pycache__", "node_modules",
                          ".venv", ".hg", ".svn", ".mypy_cache", ".pytest_cache"}


def snapshot_manifest(project_dir: Path) -> dict[str, tuple[int, int]]:
    """Map of relative file path -> (size, mtime_ns), excluding VCS/cache dirs."""
    result: dict[str, tuple[int, int]] = {}
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in MANIFEST_EXCLUDED_DIRS]
        for name in files:
            p = Path(root) / name
            try:
                st = p.stat()
            except OSError:
                continue
            result[str(p.relative_to(project_dir))] = (st.st_size, st.st_mtime_ns)
    return result


def diff_manifests(before: dict[str, tuple[int, int]],
                   after: dict[str, tuple[int, int]]) -> dict[str, list[str]]:
    added = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(
        f for f in set(before) & set(after) if before[f] != after[f]
    )
    return {"added": added, "modified": modified, "deleted": deleted}
