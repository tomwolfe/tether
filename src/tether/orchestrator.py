"""Core orchestration loop. Adapter-agnostic: depends only on AgentAdapter."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, Optional

from tether.adapters.base import AgentAdapter
from tether.audit import AuditTrail, new_session_id, redact_secrets, utcnow
from tether.git_safety import (
    changed_files_since,
    create_checkpoint,
    make_file_backup,
    restore_from_backup,
)
from tether.git_safety import rollback as git_rollback
from tether.manifest import diff_manifests, snapshot_manifest
from tether.models import AgentState, CheckpointInfo, TetherConfig, VerificationResult
from tether.verification import run_verification, summarize

log = logging.getLogger("tether")

# Single-writer lock: taken under <project_dir>/.tether/ for the duration of
# each run so two Tether sessions never mutate the same project concurrently.
WRITER_LOCK_RELPATH = Path(".tether") / "tether.lock"
# A lock older than this is considered abandoned and may be taken over.
WRITER_LOCK_STALE_SECONDS = 12 * 3600
# Bounded retries when racing another contender over a stale lock takeover.
_WRITER_LOCK_ATTEMPTS = 5


def writer_lock_path(project_dir: Path) -> Path:
    return project_dir / WRITER_LOCK_RELPATH


def _read_lock_payload(lock_path: Path) -> Optional[Dict[str, Any]]:
    """Parse a lock file into a {session_id, pid, created_at} dict.

    Current versions write one JSON object embedding the owner's PID and
    creation timestamp; locks written by older versions (or by hand) contain a
    bare session id string. Returns None when the file is absent, blank, or
    unreadable.
    """
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    payload: Dict[str, Any] = {}
    if raw.startswith("{"):
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                payload = loaded
        except json.JSONDecodeError:
            pass
    session_id = payload.get("session_id")
    pid = payload.get("pid")
    created_at = payload.get("created_at")
    return {
        "session_id": (session_id if isinstance(session_id, str)
                       and session_id else raw),
        "pid": pid if isinstance(pid, int) and pid > 0 else None,
        "created_at": created_at if isinstance(created_at, (int, float)) else None,
    }


def _pid_alive(pid: Optional[int]) -> Optional[bool]:
    """PID liveness where it can be checked portably, else None.

    On POSIX ``kill(pid, 0)`` probes without signalling. On other platforms
    (notably Windows, where os.kill with an arbitrary signal *terminates* the
    process) liveness is undeterminable here and callers fall back to the
    configurable staleness timeout.
    """
    if pid is None or os.name != "posix":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return None
    return True


def _lock_age_seconds(lock_path: Path, payload: Dict[str, Any]) -> float:
    """Age of a lock in seconds, preferring its embedded creation timestamp."""
    created_at = payload["created_at"]
    if created_at is not None:
        return max(0.0, time.time() - float(created_at))
    try:
        return max(0.0, time.time() - lock_path.stat().st_mtime)
    except OSError:
        return 0.0


def fresh_lock_holder(lock_path: Path,
                      stale_seconds: int = WRITER_LOCK_STALE_SECONDS) -> Optional[str]:
    """Session id recorded in a live lock file, else None.

    A lock counts as live while its owning process still exists (current
    versions embed the owner PID). Legacy locks and platforms without a
    portable liveness check fall back to the age test: a lock older than
    ``stale_seconds`` is considered abandoned regardless. Returns None when
    the lock is absent, blank, unreadable, abandoned (dead PID), or stale.
    """
    payload = _read_lock_payload(lock_path)
    if payload is None:
        return None
    if _pid_alive(payload["pid"]) is False:
        # Owning process is gone: safe to take over immediately.
        return None
    if _lock_age_seconds(lock_path, payload) > stale_seconds:
        # Stale per configuration even if some process owns that PID now
        # (guards against PID reuse); keeps the timeout semantics working.
        return None
    return str(payload["session_id"])


def acquire_writer_lock(project_dir: Path, session_id: str,
                        stale_seconds: int = WRITER_LOCK_STALE_SECONDS
                        ) -> tuple[bool, Optional[str]]:
    """Atomically take the single-writer lock for this project.

    Lock files are created with O_CREAT|O_EXCL so concurrent contenders can
    never clobber each other or end up both holding the lock. A pre-existing
    lock whose owning PID is no longer alive — or that exceeds
    ``stale_seconds`` where liveness cannot be checked portably — is taken
    over.

    Returns (True, None) once the lock is held; on contention returns
    (False, holder) describing the live holder without modifying its lock
    file.
    """
    lock_path = writer_lock_path(project_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({
        "session_id": session_id,
        "pid": os.getpid(),
        "created_at": time.time(),
    })
    for _ in range(_WRITER_LOCK_ATTEMPTS):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            holder = fresh_lock_holder(lock_path, stale_seconds=stale_seconds)
            if holder is not None:
                return False, holder
            # Abandoned/stale lock: remove it so O_EXCL can win, then retry.
            # Exactly one contender wins the re-create race.
            try:
                lock_path.unlink()
            except OSError:
                pass
            continue
        except OSError as e:
            raise RuntimeError(f"Cannot create writer lock at {lock_path}: {e}") from e
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body + "\n")
        return True, None
    # Kept losing the takeover race to another contender: treat as contention.
    holder = fresh_lock_holder(lock_path, stale_seconds=stale_seconds)
    return False, holder or f"unknown holder at {lock_path}"


def _git_patch_bytes(project_dir: Path, base: str) -> Optional[bytes]:
    """`git diff --no-color --binary <base>` as bytes, or None on failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_dir), "diff", "--no-color", "--binary", base],
            capture_output=True, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _git_untracked_files(project_dir: Path) -> list[str]:
    """Untracked files (excluding .tether/), relative to project_dir."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_dir), "ls-files", "--others",
             "--exclude-standard"],
            capture_output=True, text=True, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return sorted(
        line for line in proc.stdout.splitlines()
        if line.strip() and not line.startswith(".tether/")
    )


def _release_writer_lock(project_dir: Path, session_id: str) -> None:
    """Release the writer lock if (and only if) this session still owns it."""
    lock_path = writer_lock_path(project_dir)
    payload = _read_lock_payload(lock_path)
    owner = payload["session_id"] if payload is not None else None
    if owner is not None and owner != session_id:
        # The lock changed hands (e.g. taken over after we went stale); leave
        # the new owner's file alone.
        log.warning("Writer lock at %s is now held by %s; not released.",
                    lock_path, owner)
        return
    try:
        lock_path.unlink(missing_ok=True)
    except OSError as e:
        log.warning("Failed to remove writer lock %s: %s", lock_path, e)


class Orchestrator:
    def __init__(self, adapter: AgentAdapter, config: TetherConfig,
                 project_dir: Path, session_id: Optional[str] = None) -> None:
        self.adapter = adapter
        self.config = config
        self.project_dir = project_dir
        self.session_id = session_id or new_session_id()

    def mission_summary(self, mission: Any) -> str:
        lines = [
            f"# Mission: {mission.name}",
            f"Goal: {mission.goal}",
        ]
        if mission.context:
            lines.append("Context:\n" + "\n".join(f"- {c}" for c in mission.context))
        if mission.constraints:
            lines.append("Constraints:\n" + "\n".join(f"- {c}" for c in mission.constraints))
        return "\n".join(lines)

    # -- effective value resolution (mission explicit > config > default) ----

    def _effective_max_attempts(self, mission: Any) -> int:
        if mission.recovery.max_attempts is not None:
            return mission.recovery.max_attempts
        return self.config.max_attempts

    def _effective_verification_commands(self, mission: Any) -> list[str]:
        if mission.verification.commands is not None:
            return list(mission.verification.commands)
        return list(self.config.verification.commands or [])

    def _effective_verification_timeout(self, mission: Any) -> int:
        if mission.verification.timeout_seconds is not None:
            return mission.verification.timeout_seconds
        return self.config.verification_timeout_seconds

    def _sandbox_violations(self, mission: Any,
                            changed: list[str]) -> list[Dict[str, str]]:
        """Post-execution write-sandbox check over detected changed files.

        A violation is a changed file matching a forbidden glob, or (when
        allowed_paths is non-empty) matching no allowed glob.
        """
        allowed = list(getattr(mission, "allowed_paths", None) or [])
        forbidden = list(getattr(mission, "forbidden_paths", None) or [])
        violations: list[Dict[str, str]] = []
        for path in changed:
            hit = next((g for g in forbidden if fnmatch(path, g)), None)
            if hit is not None:
                violations.append({"path": path, "rule": f"forbidden_paths: {hit}"})
                continue
            if allowed and not any(fnmatch(path, g) for g in allowed):
                violations.append(
                    {"path": path, "rule": "not matched by allowed_paths"}
                )
        return violations

    def _persist_change_artifact(
        self, audit: AuditTrail, checkpoint: CheckpointInfo,
        manifest_before: Optional[dict[str, tuple[int, str | int]]],
    ) -> None:
        """Forensic change capture into the session audit directory.

        Git projects: ``patch.diff`` (``git diff --no-color --binary
        <original_head>``) plus ``untracked.txt``, since a plain diff does not
        include untracked file contents. Non-git projects:
        ``manifest_diff.json`` with added/modified/deleted and the before/after
        fingerprints used by the manifest. Best-effort: failures are logged,
        never fatal.
        """
        if checkpoint.is_git_repo and checkpoint.original_head:
            try:
                patch = _git_patch_bytes(self.project_dir, checkpoint.original_head)
                untracked = _git_untracked_files(self.project_dir)
                if patch is not None:
                    (audit.dir / "patch.diff").write_bytes(patch)
                (audit.dir / "untracked.txt").write_text(
                    "".join(f"{f}\n" for f in untracked), encoding="utf-8"
                )
                audit.log_event("change_capture", {
                    "patch_diff": patch is not None,
                    "untracked_count": len(untracked),
                })
            except OSError as e:
                log.debug("Change capture failed: %s", e)
            return
        if manifest_before is None:
            return
        try:
            after = snapshot_manifest(self.project_dir)
            diff = diff_manifests(manifest_before, after)
            touched = sorted(
                set(diff["added"]) | set(diff["modified"]) | set(diff["deleted"])
            )
            payload = {
                **diff,
                "before": {f: list(manifest_before[f]) for f in touched
                           if f in manifest_before},
                "after": {f: list(after[f]) for f in touched if f in after},
            }
            audit.save_json("manifest_diff.json", payload)
            audit.log_event("change_capture", {"manifest_diff": True})
        except OSError as e:
            log.debug("Change capture failed: %s", e)

    def _auto_rollback(self, checkpoint: CheckpointInfo,
                       pre_existing_untracked: Optional[list[str]] = None
                       ) -> Dict[str, Any]:
        """Scoped automatic rollback for a failed/cancelled mission.

        Git projects get the scoped clean rollback (reset to the checkpoint
        plus removal of session-attributable untracked files); non-git projects
        are restored from their tar backup. Pre-existing untracked files that
        are not attributable to the session are never removed.
        """
        if checkpoint.is_git_repo:
            ok, message = git_rollback(
                self.project_dir, self.session_id,
                audit_dir=self.config.audit_dir, clean=True,
                preserve=pre_existing_untracked,
            )
        else:
            ok, message = restore_from_backup(
                self.project_dir, self.session_id,
                backup_dir=self.config.backup_dir,
                audit_dir=self.config.audit_dir,
            )
        result = {"attempted": True, "ok": ok, "message": message}
        if ok:
            first_line = message.splitlines()[0] if message else ""
            log.info("Automatic rollback applied: %s", first_line)
        else:
            # The original failed/cancelled status and the manual rollback
            # guidance in next_steps stay untouched.
            log.warning("Automatic rollback did not succeed: %s",
                        message.splitlines()[0] if message else "")
        return result

    def run(self, mission: Any, allow_dirty: Optional[bool] = None,
            dry_run: Optional[bool] = None) -> Dict[str, Any]:
        """Run a mission. allow_dirty/dry_run default to the resolved config."""
        if allow_dirty is None:
            allow_dirty = self.config.allow_dirty
        if dry_run is None:
            dry_run = self.config.dry_run

        started_at = utcnow()

        # Single-writer lock: refuse fast when another live session holds it;
        # otherwise take the lock atomically and release it on every exit
        # path below, including exceptions.
        acquired, holder = acquire_writer_lock(
            self.project_dir, self.session_id,
            stale_seconds=self.config.writer_lock_stale_seconds,
        )
        lock_path = writer_lock_path(self.project_dir)
        if not acquired:
            assert holder is not None  # contention always names the holder
            audit = AuditTrail(
                self.project_dir, self.config.audit_dir, mission.name,
                self.session_id, redact_prompts=self.config.redact_prompts,
            )
            audit.log_event("session_start", {
                "session_id": self.session_id,
                "adapter": self.adapter.name,
                "project_dir": str(self.project_dir),
                "mission_name": mission.name,
                "dry_run": dry_run,
            })
            audit.log_event("lock_contended", {"held_by": holder})
            report = {
                "session_id": self.session_id,
                "mission_name": mission.name,
                "adapter": self.adapter.name,
                "status": "failed",
                "started_at": started_at,
                "finished_at": utcnow(),
                "verification_results": [],
                "recovery_attempts": [],
                "changed_files": [],
                "checkpoint_info": None,
                "plan": "",
                "next_steps": [
                    f"Another Tether session ({holder}) holds the writer lock "
                    f"at {lock_path}; no checkpoint, backup, or agent run was started.",
                    f"If that session has finished, remove the stale lock file "
                    f"{lock_path} and re-run.",
                ],
                "audit_dir": str(audit.dir),
            }
            audit.write_report(report)
            audit.log_event("session_end", {"status": "failed"})
            log.error("Aborted: writer lock held by session %s.", holder)
            return report

        try:
            return self._run_locked(mission, allow_dirty, dry_run, started_at)
        finally:
            _release_writer_lock(self.project_dir, self.session_id)

    def _run_locked(self, mission: Any, allow_dirty: bool, dry_run: bool,
                    started_at: str) -> Dict[str, Any]:
        # Fail fast when the adapter cannot run (library callers may skip the
        # CLI-level availability guard).
        if not dry_run:
            ok, reason = self.adapter.is_available()
            if not ok:
                audit = AuditTrail(
                    self.project_dir, self.config.audit_dir, mission.name, self.session_id
                )
                audit.log_event("session_start", {
                    "session_id": self.session_id,
                    "adapter": self.adapter.name,
                    "project_dir": str(self.project_dir),
                    "mission_name": mission.name,
                    "dry_run": dry_run,
                })
                report = {
                    "session_id": self.session_id,
                    "mission_name": mission.name,
                    "adapter": self.adapter.name,
                    "status": "failed",
                    "started_at": started_at,
                    "finished_at": utcnow(),
                    "verification_results": [],
                    "recovery_attempts": [],
                    "changed_files": [],
                    "checkpoint_info": None,
                    "plan": "",
                    "next_steps": [
                        f"Adapter {self.adapter.name!r} is unavailable: {reason}"
                    ],
                    "audit_dir": str(audit.dir),
                }
                audit.write_report(report)
                audit.log_event("session_end", {"status": "failed"})
                log.error("Adapter %r unavailable: %s", self.adapter.name, reason)
                return report

        audit = AuditTrail(
            self.project_dir, self.config.audit_dir, mission.name,
            self.session_id, redact_prompts=self.config.redact_prompts,
        )
        audit.log_event("session_start", {
            "session_id": self.session_id,
            "adapter": self.adapter.name,
            "project_dir": str(self.project_dir),
            "mission_name": mission.name,
            "dry_run": dry_run,
        })
        audit.save_json("mission.json", mission.model_dump())
        audit.save_json("resolved-config.json", redact_secrets(self.config.model_dump()))

        status = "failed"
        verification_results: list[VerificationResult] = []
        recovery_attempts: list[Dict[str, Any]] = []
        plan_text = ""
        next_steps: list[str] = []
        manifest_before: dict[str, tuple[int, str | int]] | None = None
        sandbox_violations: list[Dict[str, str]] = []

        # Checkpoint / safety phase. Dry-run must not mutate the target project.
        checkpoint = create_checkpoint(
            self.project_dir, self.session_id,
            allow_dirty=allow_dirty, write_ref=not dry_run,
        )
        audit.save_json("checkpoint.json", checkpoint.model_dump())
        audit.log_event("checkpoint", checkpoint.model_dump())
        if checkpoint.warning:
            log.warning("%s", checkpoint.warning)
        elif checkpoint.created:
            log.info("Checkpoint created at %s (HEAD=%s)",
                     checkpoint.ref, (checkpoint.original_head or "")[:12])

        # P0 safety: refuse to run against a dirty git tree unless allowed.
        if checkpoint.is_git_repo and checkpoint.dirty and not allow_dirty:
            audit.log_event("aborted_dirty", {"session_id": self.session_id})
            report = {
                "session_id": self.session_id,
                "mission_name": mission.name,
                "adapter": self.adapter.name,
                "status": "failed",
                "started_at": started_at,
                "finished_at": utcnow(),
                "verification_results": [],
                "recovery_attempts": [],
                "changed_files": [],
                "checkpoint_info": checkpoint.model_dump(),
                "plan": "",
                "next_steps": [
                    "Working tree is dirty; refusing to start the agent. "
                    f"Commit or stash your changes in {self.project_dir}, "
                    "or re-run with --allow-dirty."
                ],
                "audit_dir": str(audit.dir),
            }
            audit.write_report(report)
            audit.log_event("session_end", {"status": "failed"})
            log.error("Aborted: working tree is dirty and allow_dirty is False.")
            return report

        # Non-git projects: take a file backup (never during dry-run).
        if not checkpoint.is_git_repo and not dry_run:
            try:
                backup = make_file_backup(
                    self.project_dir, self.project_dir / self.config.backup_dir,
                    self.session_id,
                )
            except RuntimeError as e:
                report = {
                    "session_id": self.session_id,
                    "mission_name": mission.name,
                    "adapter": self.adapter.name,
                    "status": "failed",
                    "started_at": started_at,
                    "finished_at": utcnow(),
                    "verification_results": [],
                    "recovery_attempts": [],
                    "changed_files": [],
                    "checkpoint_info": checkpoint.model_dump(),
                    "plan": "",
                    "next_steps": [f"{e} Fix the backup location or use a git "
                                   "repository for safe rollback."],
                    "audit_dir": str(audit.dir),
                }
                audit.write_report(report)
                audit.log_event("session_end", {"status": "failed"})
                log.error("%s", e)
                return report
            audit.log_event("backup", {"path": backup})
            log.warning("Non-git project; file backup created at %s", backup)

        # Non-git change visibility: snapshot before execution (best-effort).
        if not checkpoint.is_git_repo:
            try:
                manifest_before = snapshot_manifest(self.project_dir)
            except OSError as e:
                log.debug("Manifest snapshot failed: %s", e)

        # Git projects: remember which untracked files existed before the agent
        # ran; automatic rollback must never remove those even if change
        # detection later attributes them to the session (--allow-dirty runs).
        pre_existing_untracked: list[str] = []
        if checkpoint.is_git_repo and not dry_run:
            pre_existing_untracked = _git_untracked_files(self.project_dir)

        session = None
        state: AgentState | None = None
        try:
            commands = self._effective_verification_commands(mission)
            max_attempts = self._effective_max_attempts(mission)
            timeout = self._effective_verification_timeout(mission)

            if dry_run:
                log.info("[dry-run] would start adapter session with %s",
                         self.adapter.name)
                state = AgentState(status="completed", logs="[dry-run] skipped")
            else:
                session = self.adapter.start_session(str(self.project_dir),
                                                     self.session_id)
                session.metadata["mission_name"] = mission.name
                audit.log_event("adapter_session", {"name": self.adapter.name})

            # Planning step
            plan_prompt = self.adapter.plan_prompt(self.mission_summary(mission))
            audit.save_prompt("plan", plan_prompt)
            if dry_run:
                log.info("[dry-run] would send planning prompt to %s", self.adapter.name)
                state = AgentState(status="completed", logs="[dry-run] skipped")
            else:
                assert session is not None
                state = self.adapter.send(plan_prompt, session)
                audit.save_response("plan", state.model_dump())
                log.info("Planning step status: %s", state.status)
                if state.status != "completed":
                    # A failed plan must never silently proceed to execution.
                    next_steps.append(
                        f"Planning step ended with status {state.status!r}; "
                        "mission aborted before execution."
                    )
                    status = "failed"
                    raise RuntimeError(
                        f"planning step failed with status {state.status!r}"
                    )
            plan_text = state.logs

            # Execution step. The planning output is composed into the
            # mission summary so the agent executes against its own plan.
            exec_summary = self.mission_summary(mission)
            if plan_text:
                exec_summary += "\n\nPlan:\n" + plan_text
            exec_prompt = self.adapter.execute_prompt(exec_summary)
            audit.save_prompt("execute", exec_prompt)
            if dry_run:
                log.info("[dry-run] would send execution prompt to %s",
                         self.adapter.name)
                state = AgentState(status="completed", logs="[dry-run] skipped")
            else:
                assert session is not None
                state = self.adapter.send(exec_prompt, session)
                audit.save_response("execute", state.model_dump())
                log.info("Execution step status: %s", state.status)

            changed = changed_files_since(self.project_dir, checkpoint.original_head)
            # Non-git projects: fall back to the pre-execution manifest so
            # change detection (and the sandbox gate below) sees the files
            # the agent just wrote.
            if not checkpoint.is_git_repo and manifest_before is not None:
                try:
                    mdiff = diff_manifests(manifest_before,
                                           snapshot_manifest(self.project_dir))
                    changed = sorted(
                        set(mdiff["added"]) | set(mdiff["modified"])
                        | set(mdiff["deleted"])
                    )
                except OSError:
                    pass
            audit.log_event("changed_files", {"files": changed})

            # Forensic change capture: persist the diff evidence now, before
            # verification (or any later rollback) can alter the tree further.
            if not dry_run:
                self._persist_change_artifact(audit, checkpoint, manifest_before)

            # Write-sandbox gate (post-execution): forbid or restrict which
            # paths the agent may touch. On violation, fail the mission and
            # skip verification entirely.
            sandbox_violations = self._sandbox_violations(mission, changed)
            if sandbox_violations:
                names = ", ".join(v["path"] for v in sandbox_violations)
                audit.log_event("sandbox_violations",
                                {"violations": sandbox_violations})
                next_steps.append(
                    "Write sandbox violated by: " + names + ". Verification "
                    f"was skipped. Roll back with: tether rollback "
                    f"{self.session_id} --project-dir {self.project_dir}"
                )
                raise RuntimeError(f"write sandbox violated by: {names}")

            # Verification + recovery loop
            attempt = 0
            # Any non-completed agent state counts as failure: a mission must
            # never report success unless the last agent send completed.
            agent_failed = state.status != "completed"
            while True:
                attempt += 1
                log.info("Verification attempt %d/%d", attempt, max_attempts)
                verification_results = run_verification(
                    commands, self.project_dir,
                    timeout_seconds=timeout,
                    dry_run=dry_run,
                )
                audit.save_verification(attempt, verification_results)
                passed, failing_output = summarize(verification_results)
                if passed and not agent_failed:
                    status = "success"
                    break
                if attempt >= max_attempts:
                    status = "failed"
                    next_steps.append(
                        f"Verification failed after {max_attempts} attempts. "
                        f"Roll back with: tether rollback {self.session_id} "
                        f"--project-dir {self.project_dir}"
                    )
                    break
                # Recovery attempt
                reason = failing_output or (state.error or "agent reported failure")
                recovery_attempts.append({"attempt": attempt, "failing_output": reason[:4000]})
                repair_prompt = self.adapter.repair_prompt(
                    self.mission_summary(mission), reason
                )
                audit.save_prompt(f"repair-{attempt}", repair_prompt)
                if dry_run:
                    log.info("[dry-run] would send repair prompt")
                    state = AgentState(status="completed", logs="[dry-run] skipped")
                else:
                    assert session is not None
                    state = self.adapter.send(repair_prompt, session)
                    audit.save_response(f"repair-{attempt}", state.model_dump())
                    log.info("Recovery attempt %d status: %s", attempt, state.status)
                agent_failed = state.status != "completed"
        except RuntimeError as e:
            # Deliberate aborts (e.g. planning failure) already set next_steps.
            status = "failed"
            log.error("%s", e)
        except Exception as e:
            status = "failed"
            next_steps.append(f"Internal error: {e!r}")
            log.exception("Orchestration error")
        except KeyboardInterrupt:
            # Graceful interrupt during adapter interaction: cancel the agent,
            # finalize the audit trail, and point the user at rollback.
            status = "cancelled"
            if session is not None:
                try:
                    self.adapter.cancel(session)
                except Exception as cancel_error:
                    log.warning("Adapter cancel failed: %s", cancel_error)
            audit.log_event("cancelled", {"session_id": self.session_id})
            next_steps.append(
                "Interrupted by user; partial changes may exist. "
                f"Roll back with: tether rollback {self.session_id} "
                f"--project-dir {self.project_dir}"
            )
            log.warning("Interrupted; adapter cancelled, report marked 'cancelled'.")

        finished_at = utcnow()
        if checkpoint.is_git_repo:
            changed_files = changed_files_since(self.project_dir,
                                                checkpoint.original_head)
        elif manifest_before is not None:
            try:
                diff = diff_manifests(manifest_before,
                                      snapshot_manifest(self.project_dir))
                changed_files = sorted(
                    set(diff["added"]) | set(diff["modified"]) | set(diff["deleted"])
                )
                audit.log_event("manifest_diff", diff)
            except OSError:
                changed_files = []
        else:
            changed_files = []

        # Merge adapter-reported fields when present (git/manifest detection
        # above remains the source of truth for changed_files).
        if state is not None and state.changed_files:
            merged = set(changed_files) | set(state.changed_files)
            changed_files = sorted(merged)

        report = {
            "session_id": self.session_id,
            "mission_name": mission.name,
            "adapter": self.adapter.name,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "verification_results": [r.model_dump() for r in verification_results],
            "recovery_attempts": recovery_attempts,
            "changed_files": changed_files,
            "sandbox_violations": sandbox_violations,
            "usage": state.usage if state is not None else None,
            "checkpoint_info": checkpoint.model_dump(),
            "plan": plan_text[:2000],
            "next_steps": next_steps,
            "audit_dir": str(audit.dir),
        }
        report_path = audit.write_report(report)

        # Opt-in automatic rollback: only for failed/cancelled outcomes, never
        # on success and never during dry-run. Runs after the initial report
        # so the scoped clean path can use the recorded changed_files;
        # report.json is then updated with the auto_rollback outcome.
        if (self.config.auto_rollback and not dry_run
                and status in ("failed", "cancelled")):
            report["auto_rollback"] = self._auto_rollback(
                checkpoint, pre_existing_untracked)
            report_path = audit.write_report(report)
            audit.log_event("auto_rollback", report["auto_rollback"])

        audit.log_event("session_end", {"status": status})
        log.info("Mission %s finished with status %s (report: %s)",
                 mission.name, status, report_path)
        return report
