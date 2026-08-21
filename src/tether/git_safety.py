"""Git checkpoint and rollback safety.

Checkpoint strategy:
- Record original HEAD sha.
- Create a Tether-specific ref: refs/tether/checkpoint/<session-id> at HEAD.
- Never force-push, never delete user branches, never rewrite history.

Rollback: `git reset --hard <original_head>` plus `git clean -fd` is NOT run
automatically (destructive to untracked user files). Manual steps are reported
instead when untracked files may be involved.
"""
from __future__ import annotations
import subprocess
from pathlib import Path

from tether.models import CheckpointInfo

REF_PREFIX = "refs/tether/checkpoint"


def _git(project_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(project_dir), *args],
        capture_output=True, text=True, check=check,
    )


def is_git_repo(project_dir: Path) -> bool:
    try:
        proc = _git(project_dir, "rev-parse", "--is-inside-work-tree", check=False)
        return proc.returncode == 0 and proc.stdout.strip() == "true"
    except OSError:
        return False


def head_sha(project_dir: Path) -> str | None:
    proc = _git(project_dir, "rev-parse", "HEAD", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def is_dirty(project_dir: Path) -> bool:
    # Tether's own audit/backup files under .tether/ must not count as dirt.
    proc = _git(project_dir, "status", "--porcelain", "--", ".", ":!.tether", check=False)
    return proc.returncode == 0 and bool(proc.stdout.strip())


def create_checkpoint(project_dir: Path, session_id: str,
                      allow_dirty: bool = False) -> CheckpointInfo:
    info = CheckpointInfo()
    if not is_git_repo(project_dir):
        info.warning = (
            f"Target project {project_dir} is NOT a git repository. "
            "No checkpoint possible; file backups are strongly recommended."
        )
        return info
    info.is_git_repo = True
    info.original_head = head_sha(project_dir)
    info.dirty = is_dirty(project_dir)
    if info.dirty and not allow_dirty:
        info.warning = (
            "Working tree is dirty. Refusing to proceed without --allow-dirty. "
            "Uncommitted changes cannot be restored by rollback."
        )
        return info
    ref = f"{REF_PREFIX}/{session_id}"
    assert info.original_head is not None
    proc = _git(project_dir, "update-ref", ref, info.original_head, check=False)
    if proc.returncode != 0:
        info.warning = f"Failed to create checkpoint ref: {proc.stderr.strip()}"
        return info
    info.created = True
    info.ref = ref
    if info.dirty:
        info.warning = (
            "Working tree was dirty (--allow-dirty). Checkpoint records the last "
            "commit only; uncommitted changes cannot be restored by rollback."
        )
    return info


def rollback(project_dir: Path, session_id: str) -> tuple[bool, str]:
    """Reset tracked files back to the checkpoint. Returns (ok, message)."""
    if not is_git_repo(project_dir):
        return False, f"{project_dir} is not a git repository; nothing to roll back."
    ref = f"{REF_PREFIX}/{session_id}"
    proc = _git(project_dir, "rev-parse", "--verify", ref, check=False)
    if proc.returncode != 0:
        return False, f"No checkpoint found for session {session_id} ({ref})."
    target = proc.stdout.strip()
    if is_dirty(project_dir):
        return False, (
            "Working tree is dirty; refusing destructive rollback. "
            f"Manual steps:\n"
            f"  git -C {project_dir} stash push -u -m 'pre-rollback {session_id}'\n"
            f"  git -C {project_dir} reset --hard {target}\n"
            f"  git -C {project_dir} stash pop   # review and reconcile manually"
        )
    proc = _git(project_dir, "reset", "--hard", target, check=False)
    if proc.returncode != 0:
        return False, f"Rollback failed: {proc.stderr.strip()}"
    return True, f"Rolled back to {target} (checkpoint {ref})."


def changed_files_since(project_dir: Path, base_sha: str | None) -> list[str]:
    """List files changed in the working tree vs a base sha (tracked + untracked)."""
    if not is_git_repo(project_dir):
        return []
    files: set[str] = set()
    if base_sha:
        proc = _git(project_dir, "diff", "--name-only", base_sha, check=False)
        if proc.returncode == 0:
            files.update(l for l in proc.stdout.splitlines() if l.strip())
    proc = _git(project_dir, "ls-files", "--others", "--exclude-standard", check=False)
    if proc.returncode == 0:
        files.update(l for l in proc.stdout.splitlines() if l.strip() and not l.startswith(".tether/"))
    return sorted(files)


def make_file_backup(project_dir: Path, backup_root: Path, session_id: str) -> str | None:
    """Best-effort tar backup of a non-git project. Returns archive path or None."""
    import tarfile

    backup_root.mkdir(parents=True, exist_ok=True)
    dest = backup_root / f"{session_id}.tar.gz"
    try:
        with tarfile.open(dest, "w:gz") as tar:
            for item in project_dir.rglob("*"):
                if ".tether" in item.parts or ".git" in item.parts:
                    continue
                tar.add(item, arcname=item.relative_to(project_dir))
        return str(dest)
    except OSError as e:
        return None
