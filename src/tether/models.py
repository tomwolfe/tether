"""Pydantic models for mission contracts, configuration, and agent state."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class VerificationSpec(BaseModel):
    """Optional fields distinguish 'unset' from explicit values so that
    precedence (mission > project config > defaults) can be applied later."""
    commands: Optional[List[str]] = None
    timeout_seconds: Optional[int] = Field(default=None, gt=0)


class RecoverySpec(BaseModel):
    max_attempts: Optional[int] = Field(default=None, ge=1, le=20)


class MissionContract(BaseModel):
    mission: Dict[str, Any]
    name: str
    goal: str
    context: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    verification: VerificationSpec = Field(default_factory=VerificationSpec)
    recovery: RecoverySpec = Field(default_factory=RecoverySpec)
    adapter: Optional[str] = None
    adapters: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    # Optional write sandbox (post-execution gate): fnmatch globs relative to
    # the project dir. None/empty means unrestricted.
    allowed_paths: Optional[List[str]] = None
    forbidden_paths: Optional[List[str]] = None

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
    writer_lock_stale_seconds: int = 43200  # 12 hours
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


class CheckpointInfo(BaseModel):
    created: bool = False
    is_git_repo: bool = False
    original_head: Optional[str] = None
    ref: Optional[str] = None
    dirty: bool = False
    warning: Optional[str] = None
