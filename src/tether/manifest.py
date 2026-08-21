"""Lightweight file manifests for non-git change visibility (best-effort)."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

MANIFEST_EXCLUDED_DIRS = {".tether", ".git", "__pycache__", "node_modules",
                          ".venv", ".hg", ".svn", ".mypy_cache", ".pytest_cache"}

# Files strictly below this size are fingerprinted by content (sha256); files
# at or above it fall back to (size, mtime_ns) to keep snapshots cheap.
HASH_SIZE_LIMIT = 1024 * 1024


def _sha256_file(p: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def snapshot_manifest(project_dir: Path) -> dict[str, tuple[int, str | int]]:
    """Map of relative file path -> fingerprint, excluding VCS/cache dirs.

    Small files (< HASH_SIZE_LIMIT): (size, sha256_hex) so same-length edits
    are detected. Large files: (size, mtime_ns).
    """
    result: dict[str, tuple[int, str | int]] = {}
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in MANIFEST_EXCLUDED_DIRS]
        for name in files:
            p = Path(root) / name
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size < HASH_SIZE_LIMIT:
                digest = _sha256_file(p)
                if digest is None:
                    continue
                result[str(p.relative_to(project_dir))] = (st.st_size, digest)
            else:
                result[str(p.relative_to(project_dir))] = (st.st_size, st.st_mtime_ns)
    return result


def diff_manifests(before: dict[str, tuple[int, str | int]],
                   after: dict[str, tuple[int, str | int]]) -> dict[str, list[str]]:
    added = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(
        f for f in set(before) & set(after) if before[f] != after[f]
    )
    return {"added": added, "modified": modified, "deleted": deleted}
