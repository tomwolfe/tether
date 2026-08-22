"""Git checkpoint and rollback safety.

Checkpoint strategy:
- Record original HEAD sha.
- Create a Tether-specific ref: refs/tether/checkpoint/<session-id> at HEAD.
- Never force-push, never delete user branches, never rewrite history.

Rollback: `git reset --hard <original_head>`; `git clean -fd` is NEVER run
(blanket cleans are destructive). When the tree is dirty (e.g. agent-created
untracked files), the default rollback refuses and reports exact manual steps;
an opt-in `clean=True` performs a scoped restore that only removes untracked
files attributable to the session (per its report's changed_files). A caller
may additionally pass `preserve` (paths recorded as untracked *before* the
session ran); those are never removed.
"""
from __future__ import annotations
import subprocess
from pathlib import Path
from typing import Optional

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
                      allow_dirty: bool = False,
                      write_ref: bool = True) -> CheckpointInfo:
    """Record HEAD and (unless write_ref is False, e.g. during dry-run) create
    the checkpoint ref. Never mutates the working tree."""
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
    if not write_ref:
        # Dry-run: record what would happen without mutating the repo.
        info.ref = ref
        return info
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


def list_checkpoint_refs(project_dir: Path) -> list[tuple[str, str]]:
    """Return (session_id, full_ref) pairs for all tether checkpoint refs."""
    proc = _git(project_dir, "for-each-ref", REF_PREFIX,
                "--format=%(refname)", check=False)
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        ref = line.strip()
        if ref.startswith(REF_PREFIX + "/"):
            out.append((ref[len(REF_PREFIX) + 1:], ref))
    return out


def resolve_checkpoint_ref(project_dir: Path, session_id_or_prefix: str,
                           audit_dir: str = ".tether/sessions") -> tuple[str | None, str]:
    """Resolve a session id or prefix to a checkpoint ref.

    Resolution order:
      1. exact session id,
      2. audit session directory lookup (report.json session_id),
      3. git checkpoint ref prefix match.

    Returns (ref, error_message); exactly one is set.
    """
    sid = session_id_or_prefix
    # 1. exact session id
    exact = f"{REF_PREFIX}/{sid}"
    proc = _git(project_dir, "rev-parse", "--verify", exact, check=False)
    if proc.returncode == 0:
        return exact, ""

    # 2. audit session directory lookup
    from tether.audit import find_session_dir
    try:
        session = find_session_dir(project_dir, audit_dir, sid)
    except ValueError as e:
        return None, str(e)
    if session is not None:
        report_path = session / "report.json"
        if report_path.exists():
            try:
                import json
                reported = json.loads(report_path.read_text(encoding="utf-8")).get("session_id")
            except (OSError, json.JSONDecodeError):
                reported = None
            if isinstance(reported, str) and reported != sid:
                proc = _git(project_dir, "rev-parse", "--verify",
                            f"{REF_PREFIX}/{reported}", check=False)
                if proc.returncode == 0:
                    return f"{REF_PREFIX}/{reported}", ""
        # fall through to prefix match using the original input

    # 3. git checkpoint ref prefix match
    matches = [(s, r) for s, r in list_checkpoint_refs(project_dir) if s.startswith(sid)]
    if len(matches) == 1:
        return matches[0][1], ""
    if len(matches) > 1:
        listing = "\n".join(f"  {REF_PREFIX}/{s}" for s, _ in matches)
        return None, (
            f"Ambiguous session id prefix {sid!r}; matches:\n{listing}\n"
            "Use a longer prefix."
        )
    return None, f"No checkpoint found for session {sid!r}."


def _untracked_files(project_dir: Path) -> list[str]:
    """Untracked files (excluding .tether/), relative to project_dir."""
    proc = _git(project_dir, "ls-files", "--others", "--exclude-standard",
                check=False)
    if proc.returncode != 0:
        return []
    return sorted(
        line for line in proc.stdout.splitlines()
        if line.strip() and not line.startswith(".tether/")
    )


def _session_changed_files(project_dir: Path, session_id: str,
                           audit_dir: str) -> list[str]:
    """Best-effort: files recorded as changed by the session's report."""
    import json
    from tether.audit import find_session_dir
    try:
        session = find_session_dir(project_dir, audit_dir, session_id)
    except ValueError:
        return []
    if session is None:
        return []
    report_path = session / "report.json"
    if not report_path.exists():
        return []
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    files = data.get("changed_files")
    return [f for f in files if isinstance(f, str)] if isinstance(files, list) else []


def rollback(project_dir: Path, session_id: str,
             audit_dir: str = ".tether/sessions",
             clean: bool = False,
             preserve: Optional[list[str]] = None) -> tuple[bool, str]:
    """Reset tracked files back to the checkpoint. Accepts a session id prefix.

    With clean=False (default), refuses when the tree is dirty but always
    prints exact manual commands, including the specific untracked files found.
    With clean=True, performs a scoped restore: `git reset --hard <target>`
    plus removal of untracked files attributable to the session (those listed
    in the session report's changed_files). Pre-existing untracked user files
    are never removed; callers that recorded the pre-session untracked set can
    pass it via ``preserve`` to guarantee those paths survive even when the
    report's changed_files includes them (e.g. --allow-dirty runs).
    """
    if not is_git_repo(project_dir):
        return False, f"{project_dir} is not a git repository; nothing to roll back."
    ref, err = resolve_checkpoint_ref(project_dir, session_id, audit_dir=audit_dir)
    if ref is None:
        return False, err
    proc = _git(project_dir, "rev-parse", "--verify", ref, check=False)
    target = proc.stdout.strip()

    if is_dirty(project_dir):
        untracked = _untracked_files(project_dir)
        session_files = set(_session_changed_files(project_dir, session_id, audit_dir))
        # Untracked files attributable to this session per its report.
        preserved = set(preserve or [])
        session_untracked = [
            f for f in untracked
            if f in session_files and f not in preserved
        ]
        manual = [
            "Working tree is dirty; refusing destructive rollback. Manual steps:",
            f"  git -C {project_dir} stash push -u -m 'pre-rollback {session_id}'",
            f"  git -C {project_dir} reset --hard {target}",
            f"  git -C {project_dir} stash pop   # review and reconcile manually",
        ]
        if untracked:
            listing = "\n".join(f"  {f}" for f in untracked)
            manual.append("Untracked files present (remove session-created ones "
                          "manually if desired):")
            manual.append(listing)
        if clean:
            proc = _git(project_dir, "reset", "--hard", target, check=False)
            if proc.returncode != 0:
                return False, f"Rollback failed: {proc.stderr.strip()}"
            removed, skipped = [], []
            for rel in session_untracked:
                path = project_dir / rel
                try:
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                        removed.append(rel)
                    else:
                        skipped.append(rel)
                except OSError as e:
                    skipped.append(f"{rel} ({e})")
            msg = f"Rolled back to {target} (checkpoint {ref})."
            if removed:
                msg += " Removed session-created untracked files: " + ", ".join(removed)
            if skipped:
                msg += (" Skipped (not auto-removed; handle manually): "
                        + ", ".join(skipped))
            remaining_dirty = is_dirty(project_dir)
            if remaining_dirty:
                manual[0] = ("Rolled back tracked files, but pre-existing dirty "
                             "state remains (left untouched).")
                return True, msg + "\n" + "\n".join(manual)
            return True, msg
        return False, "\n".join(manual)

    proc = _git(project_dir, "reset", "--hard", target, check=False)
    if proc.returncode != 0:
        return False, f"Rollback failed: {proc.stderr.strip()}"
    return True, f"Rolled back to {target} (checkpoint {ref})."


def _reported_session_id(session_dir: Path) -> str | None:
    """Best-effort: the real session id recorded in a session's report.json."""
    import json
    report_path = session_dir / "report.json"
    if not report_path.exists():
        return None
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    sid = data.get("session_id") if isinstance(data, dict) else None
    return sid if isinstance(sid, str) else None


def plan_rollback(project_dir: Path, session_id: str,
                  audit_dir: str = ".tether/sessions",
                  backup_dir: str = ".tether/backups",
                  clean: bool = False) -> tuple[bool, str]:
    """Preview what ``rollback`` would do, without touching anything.

    Read-only: resolves the checkpoint target (or the backup archive for
    non-git projects) and reports the dirty state of the working tree, the
    files that would be reset (from the session report's changed_files), the
    additional untracked files ``--clean`` would delete, and the pre-existing
    untracked files that would be preserved. Returns (ok, plan_text); a
    False ok means no plan could be produced (e.g. unknown session).
    """
    if not is_git_repo(project_dir):
        return _plan_backup_restore(project_dir, session_id,
                                    audit_dir=audit_dir, backup_dir=backup_dir)

    ref, err = resolve_checkpoint_ref(project_dir, session_id, audit_dir=audit_dir)
    if ref is None:
        return False, err
    proc = _git(project_dir, "rev-parse", "--verify", ref, check=False)
    target = proc.stdout.strip()
    dirty = is_dirty(project_dir)
    changed_files = sorted(set(_session_changed_files(project_dir, session_id,
                                                      audit_dir)))
    lines = [
        "Rollback plan (dry-run; nothing has been modified)",
        f"  project dir:  {project_dir}",
        f"  checkpoint:   {ref}",
        f"  target:       {target}",
        f"  working tree: {'DIRTY' if dirty else 'clean'}",
    ]
    if changed_files:
        lines.append(f"Tracked files that would be reset "
                     f"({len(changed_files)}, per session report):")
        lines.extend(f"  - {f}" for f in changed_files)
    else:
        lines.append("Tracked files that would be reset: none listed "
                     "(no session report with changed_files found)")
    if dirty:
        untracked = _untracked_files(project_dir)
        deletable = [f for f in untracked if f in set(changed_files)]
        preserved = [f for f in untracked if f not in set(changed_files)]
        if clean:
            if deletable:
                lines.append(f"Additional untracked files --clean would delete "
                             f"({len(deletable)}):")
                lines.extend(f"  - {f}" for f in deletable)
            else:
                lines.append("Additional untracked files --clean would delete: "
                             "none attributable to this session")
        else:
            lines.append("Default behavior on apply: REFUSE while the tree is "
                         "dirty and print manual steps (pass --clean for a "
                         "scoped restore).")
        if preserved:
            lines.append(f"Pre-existing untracked files that would be "
                         f"preserved ({len(preserved)}):")
            lines.extend(f"  - {f}" for f in preserved)
    else:
        lines.append("Untracked files present: none")
    lines.append("Apply with: tether rollback <session-id> [--clean]")
    return True, "\n".join(lines)


def _plan_backup_restore(project_dir: Path, session_id: str,
                         audit_dir: str, backup_dir: str) -> tuple[bool, str]:
    """Dry-run plan for non-git projects: the backup restore steps."""
    archive = find_backup_archive(project_dir, session_id, backup_dir, audit_dir)
    if archive is None:
        return False, (f"No backup archive found for session {session_id!r} "
                       f"under {project_dir / backup_dir}.")
    checksum_ok, checksum_msg = verify_backup_checksum(archive)
    lines = [
        "Rollback plan (dry-run; nothing has been modified)",
        f"  project dir:    {project_dir}",
        "  project type:   non-git",
        f"  backup archive: {archive}",
        f"  checksum:       {'verified (sha256 sidecar)' if checksum_ok else 'FAILED'}",
    ]
    if not checksum_ok:
        lines.append(f"    {checksum_msg}")
    changed = sorted(set(_session_changed_files(project_dir, session_id,
                                                audit_dir)))
    if changed:
        lines.append(f"Files that would be restored from the archive "
                     f"({len(changed)}, per session report):")
        lines.extend(f"  - {f}" for f in changed)
    else:
        lines.append("Files that would be restored: all entries in the "
                     "archive (no session report with changed_files found)")
    lines.append("Files created after the backup would be kept and reported "
                 "for manual cleanup.")
    lines.append(f"Apply with: tether rollback <session-id> --project-dir "
                 f"{project_dir}")
    return True, "\n".join(lines)


def find_backup_archive(project_dir: Path, session_id: str,
                        backup_dir: str = ".tether/backups",
                        audit_dir: str = ".tether/sessions") -> Path | None:
    """Locate the tar backup archive for a session id (prefix allowed)."""
    from tether.audit import find_session_dir
    root = project_dir / backup_dir
    exact = root / f"{session_id}.tar.gz"
    if exact.exists():
        return exact
    try:
        session = find_session_dir(project_dir, audit_dir, session_id)
    except ValueError:
        return None
    if session is None:
        return None
    reported = _reported_session_id(session)
    if reported is not None and reported != session_id:
        candidate = root / f"{reported}.tar.gz"
        if candidate.exists():
            return candidate
    return None


def restore_from_backup(project_dir: Path, session_id: str,
                        backup_dir: str = ".tether/backups",
                        audit_dir: str = ".tether/sessions") -> tuple[bool, str]:
    """Restore a non-git project from its session backup archive.

    The archive's sha256 is verified against its ``.sha256`` sidecar before
    anything is touched; a missing or mismatching checksum refuses the
    restore outright. Restores file contents as of the backup; files created
    after the backup are left in place (reported so the user can remove
    them).
    """
    import tarfile

    archive = find_backup_archive(project_dir, session_id, backup_dir, audit_dir)
    if archive is None:
        return False, (f"No backup archive found for session {session_id!r} "
                       f"under {project_dir / backup_dir}.")
    ok, err = verify_backup_checksum(archive)
    if not ok:
        return False, err
    current_files = {
        str(p.relative_to(project_dir).as_posix())
        for p in project_dir.rglob("*")
        if p.is_file()
        and not any(part in BACKUP_EXCLUDED_DIRS for part in p.relative_to(project_dir).parts)
    }
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            for m in members:
                dest = (project_dir / m.name).resolve()
                if project_dir.resolve() not in dest.parents:
                    return False, f"Refusing unsafe archive entry: {m.name}"
            backed_up = {m.name for m in members if m.isfile()}
            try:
                tar.extractall(project_dir, filter="data")
            except TypeError:  # Python < 3.11.4 has no filter= parameter
                tar.extractall(project_dir)
    except (tarfile.TarError, OSError) as e:
        return False, f"Failed to restore from {archive}: {e}"
    created_after = sorted(current_files - backed_up)
    msg = f"Restored project files from backup {archive}."
    if created_after:
        listing = "\n".join(f"  {f}" for f in created_after[:50])
        msg += ("\nFiles created after the backup were kept; remove manually "
                f"if unwanted:\n{listing}")
    return True, msg


def changed_files_since(project_dir: Path, base_sha: str | None) -> list[str]:
    """List files changed in the working tree vs a base sha (tracked + untracked)."""
    if not is_git_repo(project_dir):
        return []
    files: set[str] = set()
    if base_sha:
        proc = _git(project_dir, "diff", "--name-only", base_sha, check=False)
        if proc.returncode == 0:
            files.update(line for line in proc.stdout.splitlines() if line.strip())
    proc = _git(project_dir, "ls-files", "--others", "--exclude-standard", check=False)
    if proc.returncode == 0:
        files.update(
        line for line in proc.stdout.splitlines()
        if line.strip() and not line.startswith(".tether/")
    )
    return sorted(files)


BACKUP_EXCLUDED_DIRS = {".tether", ".git", "__pycache__", "node_modules", ".venv", ".hg", ".svn"}


def backup_checksum_path(archive: Path) -> Path:
    """Sidecar path holding the sha256 of a backup archive."""
    return Path(str(archive) + ".sha256")


def _sha256_of_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Hex sha256 of a file, streamed in chunks."""
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_backup_checksum(archive: Path) -> tuple[bool, str]:
    """Verify an archive's sha256 sidecar; (ok, message).

    A missing or unreadable sidecar counts as failure: an unverifiable
    backup must never be restored onto the project.
    """
    import binascii
    sidecar = backup_checksum_path(archive)
    if not sidecar.exists():
        return False, (
            f"Missing checksum sidecar {sidecar}; cannot verify the "
            f"integrity of {archive}. Refusing to restore."
        )
    try:
        expected = sidecar.read_text(encoding="utf-8").strip().split()[0].lower()
    except (OSError, IndexError, UnicodeDecodeError):
        return False, (f"Unreadable checksum sidecar {sidecar}; "
                       "refusing to restore.")
    try:
        actual = _sha256_of_file(archive).lower()
    except OSError as e:
        return False, f"Cannot read backup archive {archive}: {e}"
    try:
        binascii.unhexlify(expected)
    except (binascii.Error, ValueError):
        return False, (f"Malformed checksum in {sidecar}; refusing to restore.")
    if actual != expected:
        return False, (
            f"Backup archive {archive} FAILED its sha256 check (expected "
            f"{expected}, got {actual}). The archive is corrupted or was "
            "tampered with; refusing to restore."
        )
    return True, ""


def _write_text_atomically(path: Path, text: str) -> None:
    """Write text via a temp file + atomic rename so readers never see a
    partial file."""
    import os
    import tempfile
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def make_file_backup(project_dir: Path, backup_root: Path, session_id: str) -> str:
    """Tar backup of a non-git project. Returns archive path.

    The archive is written to a temp file and atomically renamed into place,
    so a crashed backup never leaves a truncated archive that looks valid. A
    ``<archive>.sha256`` sidecar records the archive checksum at creation
    time and is verified before any restore. The archive itself and its
    sidecar are never included in the backup contents.

    Raises RuntimeError on failure so callers can fail the mission clearly
    instead of proceeding without a safety net.
    """
    import os
    import tarfile
    import tempfile

    backup_root.mkdir(parents=True, exist_ok=True)
    dest = backup_root / f"{session_id}.tar.gz"
    sidecar = backup_checksum_path(dest)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp",
                                    dir=backup_root)
    os.close(fd)  # tarfile reopens by path
    tmp_dest = Path(tmp_name)
    # Never back up the archive, its sidecar, or the temp file into itself.
    self_excluded = {str(p.resolve()) for p in (dest, sidecar, tmp_dest)}
    try:
        with tarfile.open(tmp_dest, "w:gz") as tar:
            # Add files only (never directories) so each archive entry is unique;
            # tar recreates parent directories implicitly on extract.
            for item in sorted(project_dir.rglob("*")):
                if not item.is_file() and not item.is_symlink():
                    continue
                rel = item.relative_to(project_dir)
                if any(part in BACKUP_EXCLUDED_DIRS for part in rel.parts):
                    continue
                if str(item.resolve()) in self_excluded:
                    continue
                tar.add(item, arcname=rel, recursive=False)
        checksum = _sha256_of_file(tmp_dest)
        # Atomic publish: a crash before this point leaves no archive at all
        # rather than a truncated one that looks valid.
        os.replace(tmp_dest, dest)
        _write_text_atomically(sidecar, checksum + "\n")
        return str(dest)
    except OSError as e:
        for leftover in (tmp_dest, dest, sidecar):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
        raise RuntimeError(f"Failed to create file backup at {dest}: {e}") from e
