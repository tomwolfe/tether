"""Core orchestration loop. Adapter-agnostic: depends only on AgentAdapter."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from tether.adapters.base import AgentAdapter
from tether.audit import AuditTrail, new_session_id, utcnow
from tether.git_safety import (
    changed_files_since,
    create_checkpoint,
    is_git_repo,
    make_file_backup,
)
from tether.models import AgentState, CheckpointInfo, TetherConfig, VerificationResult
from tether.verification import run_verification, summarize

log = logging.getLogger("tether")


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

    def run(self, mission: Any, allow_dirty: bool = False,
            dry_run: bool = False) -> Dict[str, Any]:
        started_at = utcnow()
        audit = AuditTrail(
            self.project_dir, self.config.audit_dir, mission.name, self.session_id
        )
        audit.log_event("session_start", {
            "session_id": self.session_id,
            "adapter": self.adapter.name,
            "project_dir": str(self.project_dir),
            "mission_name": mission.name,
        })
        audit.save_json("mission.json", mission.model_dump())
        audit.save_json("resolved-config.json", self.config.model_dump())

        checkpoint = self._checkpoint(audit, allow_dirty)
        if checkpoint.warning and not checkpoint.is_git_repo:
            backup = make_file_backup(
                self.project_dir, self.project_dir / ".tether" / "backups", self.session_id
            )
            audit.log_event("backup", {"path": backup})
            log.warning("Non-git project; file backup created at %s", backup)

        status = "failed"
        verification_results: list[VerificationResult] = []
        recovery_attempts: list[Dict[str, Any]] = []
        plan_text = ""
        next_steps: list[str] = []

        try:
            session = self.adapter.start_session(str(self.project_dir), self.session_id)
            audit.log_event("adapter_session", {"name": self.adapter.name})

            # Planning step
            plan_prompt = self.adapter.plan_prompt(self.mission_summary(mission))
            audit.save_prompt("plan", plan_prompt)
            if dry_run:
                log.info("[dry-run] would send planning prompt to %s", self.adapter.name)
                state = AgentState(status="completed", logs="[dry-run] skipped")
            else:
                state = self.adapter.send(plan_prompt, session)
                audit.save_response("plan", state.model_dump())
                log.info("Planning step status: %s", state.status)
            plan_text = state.logs

            # Execution step
            exec_prompt = self.adapter.execute_prompt(self.mission_summary(mission))
            audit.save_prompt("execute", exec_prompt)
            if dry_run:
                log.info("[dry-run] would send execution prompt to %s", self.adapter.name)
                state = AgentState(status="completed", logs="[dry-run] skipped")
            else:
                state = self.adapter.send(exec_prompt, session)
                audit.save_response("execute", state.model_dump())
                log.info("Execution step status: %s", state.status)

            changed = changed_files_since(self.project_dir, checkpoint.original_head)
            audit.log_event("changed_files", {"files": changed})

            # Verification + recovery loop
            commands = mission.verification.commands or self.config.verification.commands
            max_attempts = mission.recovery.max_attempts or self.config.max_attempts
            attempt = 0
            agent_failed = state.status == "failed"
            while True:
                attempt += 1
                log.info("Verification attempt %d/%d", attempt, max_attempts)
                verification_results = run_verification(
                    commands, self.project_dir,
                    timeout_seconds=mission.verification.timeout_seconds,
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
                    state = self.adapter.send(repair_prompt, session)
                    audit.save_response(f"repair-{attempt}", state.model_dump())
                    log.info("Recovery attempt %d status: %s", attempt, state.status)
                agent_failed = state.status == "failed"
        except Exception as e:
            status = "failed"
            next_steps.append(f"Internal error: {e!r}")
            log.exception("Orchestration error")

        finished_at = utcnow()
        report = {
            "session_id": self.session_id,
            "mission_name": mission.name,
            "adapter": self.adapter.name,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "verification_results": [r.model_dump() for r in verification_results],
            "recovery_attempts": recovery_attempts,
            "changed_files": changed_files_since(self.project_dir, checkpoint.original_head),
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

    def _checkpoint(self, audit: AuditTrail, allow_dirty: bool) -> CheckpointInfo:
        info = create_checkpoint(self.project_dir, self.session_id,
                                 allow_dirty=allow_dirty)
        audit.save_json("checkpoint.json", info.model_dump())
        audit.log_event("checkpoint", info.model_dump())
        if info.warning:
            log.warning("%s", info.warning)
        elif info.created:
            log.info("Checkpoint created at %s (HEAD=%s)", info.ref, info.original_head[:12])
        return info
