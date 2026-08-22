"""Pydantic models for mission contracts, configuration, and agent state."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class VerificationSpec(BaseModel):
    """Optional fields distinguish 'unset' from explicit values so that
    precedence (mission > project config > defaults) can be applied later."""
    commands: Optional[List[str]] = None
    timeout_seconds: Optional[int] = Field(default=None, gt=0)
    # Required deliverables: fnmatch globs relative to the target project;
    # each pattern must match at least one existing file after the commands
    # pass, or the attempt fails.
    artifacts: Optional[List[str]] = None


class RecoverySpec(BaseModel):
    max_attempts: Optional[int] = Field(default=None, ge=1, le=20)


class ReviewSpec(BaseModel):
    """Optional review gate: an adversarial reviewer pass over the captured
    change after verification passes (dogfood-15). Default OFF so existing
    missions validate and behave unchanged."""
    enabled: bool = False   # default OFF; existing missions unchanged
    required: bool = True   # when enabled, a rejection fails the mission


class MissionContract(BaseModel):
    mission: Dict[str, Any]
    name: str
    goal: str
    context: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    verification: VerificationSpec = Field(default_factory=VerificationSpec)
    recovery: RecoverySpec = Field(default_factory=RecoverySpec)
    # Optional review gate; None (absent) keeps existing missions unchanged.
    review: Optional[ReviewSpec] = None
    adapter: Optional[str] = None
    adapters: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    # Optional write sandbox (post-execution gate): fnmatch globs relative to
    # the project dir. None/empty means unrestricted.
    allowed_paths: Optional[List[str]] = None
    forbidden_paths: Optional[List[str]] = None
    # Optional bounded reference context: relative paths read at mission start
    # (before planning) and embedded into the prompt context. Limits, path
    # rules, and binary refusal live in tether.context_files.
    context_files: List[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("mission.name must not be empty")
        return v

    @field_validator("goal")
    @classmethod
    def _goal_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("mission.goal must not be empty")
        return v


class TetherConfig(BaseModel):
    default_adapter: str = "mock"
    audit_dir: str = ".tether/sessions"
    backup_dir: str = ".tether/backups"
    dry_run: bool = False
    log_level: str = "INFO"
    command_timeout_seconds: int = 1800
    verification_timeout_seconds: int = 600
    max_attempts: int = 3
    allow_dirty: bool = False
    # Opt-in: automatically roll back failed/cancelled missions (scoped clean
    # rollback for git projects, backup restore otherwise). Never applies to
    # successful runs or dry-runs.
    auto_rollback: bool = False
    redact_prompts: bool = False
    # Write-sandbox posture: "warn" (default) detects violations after
    # execution and fails the mission; "enforce" additionally snapshots the
    # project tree and unions filesystem-metadata diffs into the sandbox
    # check. Best-effort either way — not OS-level containment.
    sandbox_mode: Literal["warn", "enforce"] = "warn"
    writer_lock_stale_seconds: int = 43200  # 12 hours
    # Optional retention policy for audit sessions (days). Null disables
    # automatic pruning; `tether sessions clean` uses it when --older-than is
    # not given.
    retention_days: Optional[int] = Field(default=None, ge=0)
    adapters: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    verification: VerificationSpec = Field(default_factory=VerificationSpec)


AdapterStatus = Literal[
    "pending", "running", "needs_input", "completed", "failed", "cancelled", "unavailable"
]


class AgentState(BaseModel):
    status: AdapterStatus = "pending"
    logs: str = ""
    result: Optional[Dict[str, Any]] = None
    changed_files: List[str] = []
    error: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None


class VerificationResult(BaseModel):
    command: str
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    skipped_dry_run: bool = False
    passed: bool = False


class ArtifactResult(BaseModel):
    """Outcome of one verification artifact pattern against the project."""
    pattern: str
    matched_files: List[str] = []
    passed: bool = False
    detail: str = ""


class CheckpointInfo(BaseModel):
    created: bool = False
    is_git_repo: bool = False
    original_head: Optional[str] = None
    ref: Optional[str] = None
    dirty: bool = False
    warning: Optional[str] = None
