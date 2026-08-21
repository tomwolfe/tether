"""Core orchestration loop. Adapter-agnostic: depends only on AgentAdapter."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from tether.adapters.base import AgentAdapter
from tether.audit import AuditTrail, new_session_id, redact_secrets, utcnow
from tether.git_safety import (
    changed_files_since,
    create_checkpoint,
    make_file_backup,
)
from tether.manifest import diff_manifests, snapshot_manifest
from tether.models import AgentState, TetherConfig, VerificationResult
from tether.verification import run_verification, summarize

log = logging.getLogger("tether")

# Single-writer lock: taken under <project_dir>/.tether/ for the duration of
# each run so two Tether sessions never mutate the same project concurrently.
WRITER_LOCK_RELPATH = Path(".tether") / "tether.lock"
# A lock older than this is considered abandoned and may be taken over.
WRITER_LOCK_STALE_SECONDS = 12 * 3600


def writer_lock_path(project_dir: Path) -> Path:
    return project_dir / WRITER_LOCK_RELPATH


def fresh_lock_holder(lock_path: Path,
                      stale_seconds: int = WRITER_LOCK_STALE_SECONDS) -> Optional[str]:
    """Session id recorded in a live lock file, else None.

    Returns None when the lock is absent, blank, unreadable, or older than
    ``stale_seconds`` (i.e. safe to treat as abandoned).
    """
    try:
        holder = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not holder:
        return None
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return None
    if age > stale_seconds:
        return None
    return holder


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

    def run(self, mission: Any, allow_dirty: Optional[bool] = None,
            dry_run: Optional[bool] = None) -> Dict[str, Any]:
        """Run a mission. allow_dirty/dry_run default to the resolved config."""
        if allow_dirty is None:
            allow_dirty = self.config.allow_dirty
        if dry_run is None:
            dry_run = self.config.dry_run

        started_at = utcnow()

        # Single-writer lock: refuse fast when another live session holds it;
        # otherwise take the lock and release it on every exit path below.
        lock_path = writer_lock_path(self.project_dir)
        holder = fresh_lock_holder(lock_path)
        if holder is not None:
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

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(self.session_id + "\n", encoding="utf-8")
        try:
            return self._run_locked(mission, allow_dirty, dry_run, started_at)
        finally:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass

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
            self.project_dir, self.config.audit_dir, mission.name, self.session_id
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
        manifest_before: dict[str, tuple[int, int]] | None = None

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
            audit.log_event("changed_files", {"files": changed})

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
            "usage": state.usage if state is not None else None,
            "checkpoint_info": checkpoint.model_dump(),
            "plan": plan_text[:2000],
            "next_steps": next_steps,
            "audit_dir": str(audit.dir),
        }
        report_path = audit.write_report(report)
        audit.log_event("session_end", {"status": status})
        log.info("Mission %s finished with status %s (report: %s)",
                 mission.name, status, report_path)
        return report
