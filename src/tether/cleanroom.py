"""Clean-room verification materializer (dogfood-23).

Builds a throwaway checkout of a checkpoint ref plus ONLY the session's
captured change artifact (``patch.diff`` and the non-gitignored untracked
files it lists), so verification can run somewhere the agent's working tree
— including gitignored helper files planted to game verification tools —
does not exist. Nothing outside ``dest`` is ever written.

Every failure raises :class:`CleanRoomError`: callers must fail closed,
never fall back to verifying inside the agent's tree.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from typing import List, Optional, Set


class CleanRoomError(RuntimeError):
    """Clean-room materialization failure (fail-closed contract)."""


def _git(project_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(project_dir), *args],
        capture_output=True, check=False, shell=False,
    )


def _run_in(dest: Path, argv: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=str(dest), capture_output=True, check=False, shell=False,
    )


def _extract_archive(archive: bytes, dest: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        try:
            tar.extractall(dest, filter="data")
        except TypeError:  # Python < 3.11.4 has no filter= parameter
            tar.extractall(dest)


def _apply_patch(dest: Path, patch: Path) -> None:
    """Apply ``patch`` inside ``dest``; fall back to POSIX patch."""
    proc = _run_in(dest, [
        "git", "apply", "--whitespace=nowarn", str(patch)])
    if proc.returncode == 0:
        return
    fallback = _run_in(dest, ["patch", "-p1", "-i", str(patch)])
    if fallback.returncode != 0:
        stderr = (proc.stderr or fallback.stderr).decode(
            "utf-8", "replace").strip()
        raise CleanRoomError(f"failed to apply patch.diff: {stderr}")


def _gitignored_paths(project_dir: Path,
                      rels: List[str]) -> Optional[Set[str]]:
    """Paths among ``rels`` reported by ``git check-ignore``, or None when
    gitignore status cannot be determined (unknown => callers must skip)."""
    if not rels:
        return set()
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_dir), "check-ignore", "--stdin"],
            input="\n".join(rels) + "\n",
            capture_output=True, text=True, check=False, shell=False,
        )
    except OSError:
        return None
    if proc.returncode not in (0, 1):  # 0 = some ignored, 1 = none ignored
        return None
    return {line for line in proc.stdout.splitlines() if line.strip()}


def _is_relative(rel: str) -> bool:
    candidate = PurePosixPath(rel.replace("\\", "/"))
    if candidate.is_absolute() or rel.replace("\\", "/").startswith("/"):
        return False
    if any(part == ".." for part in candidate.parts):
        return False
    return bool(candidate.parts)


def _contained(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    return resolved == root or root in resolved.parents


def materialize_clean_room(
    project_dir: Path,
    checkpoint_ref: str,
    session_dir: Path,
    copies: Optional[List[str]],
    dest: Path,
) -> None:
    """Materialize a clean room into ``dest`` (which is created).

    Steps: pristine ``git archive`` of ``checkpoint_ref`` extracted into
    ``dest``; ``session_dir/patch.diff`` applied on top (plain ``git apply``,
    falling back to ``patch -p1``); untracked files listed in
    ``session_dir/untracked.txt`` copied byte-for-byte from the project tree
    EXCEPT paths that are gitignored in the project or resolve outside
    ``project_dir`` (defensive); then each entry of ``copies`` that exists in
    the project dir is copied over recursively (missing entries are skipped
    silently). Raises :class:`CleanRoomError` on any failure.
    """
    # 1. Pristine checkout of the checkpoint ref.
    try:
        proc = _git(project_dir, "archive", checkpoint_ref)
    except OSError as e:
        raise CleanRoomError(f"git archive failed: {e}") from e
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise CleanRoomError(
            f"git archive {checkpoint_ref} failed: {stderr}")
    try:
        dest.mkdir(parents=True, exist_ok=True)
        _extract_archive(proc.stdout, dest)
    except (tarfile.TarError, OSError) as e:
        raise CleanRoomError(
            f"failed to extract checkpoint into clean room: {e}") from e

    # 2. The session's captured change.
    patch = session_dir / "patch.diff"
    if not patch.is_file():
        raise CleanRoomError(f"missing change artifact: {patch}")
    try:
        _apply_patch(dest, patch)
    except OSError as e:
        raise CleanRoomError(f"failed to apply patch.diff: {e}") from e

    # 3. Untracked files captured alongside the patch. Gitignored paths are
    # deliberately excluded: they never appear in the captured artifact, and
    # anything planted there to game verification tools must not carry into
    # the clean room. Paths escaping the project dir are skipped defensively.
    untracked = session_dir / "untracked.txt"
    try:
        rels = [line.strip() for line in
                untracked.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    except OSError:
        rels = []  # artifact capture is best-effort; nothing carried over
    ignored: Optional[Set[str]] = None
    if rels:
        ignored = _gitignored_paths(project_dir, rels)
    project_root = project_dir.resolve()
    for rel in rels:
        if not _is_relative(rel):
            continue  # defensive: never follow absolute/'..' listings
        top = PurePosixPath(rel.replace("\\", "/")).parts[0]
        if top in (".tether", ".git"):
            continue  # Tether's own bookkeeping never carries over
        if ignored is None or rel in ignored:
            continue  # cannot prove it is not gitignored => leave it out
        src = project_dir / Path(rel)
        if not _contained(src, project_root) or not src.is_file():
            continue
        target = dest / Path(*PurePosixPath(rel.replace("\\", "/")).parts)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(src.read_bytes())
        except OSError as e:
            raise CleanRoomError(
                f"failed to carry untracked file {rel!r}: {e}") from e

    # 4. Explicit copy entries (e.g. .venv). Missing entries are skipped
    # silently; protected/escaping entries fail closed.
    for entry in copies or []:
        if not _is_relative(entry):
            raise CleanRoomError(
                f"clean_room_copy entry must be a relative path: {entry!r}")
        pure = PurePosixPath(entry.replace("\\", "/"))
        if pure.parts[0] in (".git", ".tether"):
            raise CleanRoomError(
                f"refusing to copy protected path into the clean room: "
                f"{entry!r}")
        src = project_dir / os.path.normpath(Path(*pure.parts))
        if not src.exists():
            continue
        if not _contained(src, project_root):
            raise CleanRoomError(
                f"clean_room_copy entry escapes the project dir: {entry!r}")
        target = dest / Path(*pure.parts)
        try:
            if src.is_dir():
                shutil.copytree(src, target, symlinks=True,
                                dirs_exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
        except OSError as e:
            raise CleanRoomError(
                f"failed to copy {entry!r} into the clean room: {e}") from e
