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
    # Structural content assertions (dogfood-19): deeper checks over files
    # matched by a glob. Default None so existing missions work unchanged.
    assertions: Optional[List[AssertionSpec]] = None
    # Behavioral probes (dogfood-20): assert on a command's OUTPUT, not its
    # exit code. Default None so existing missions work unchanged.
    probes: Optional[List[ProbeSpec]] = None
    # Mutation testing meta-verification (dogfood-22): mutate changed files,
    # re-run verification against each mutant, report a kill rate. Default
    # None so existing missions validate and behave unchanged.
    mutation: Optional[MutationSpec] = None
    # Clean-room verification (dogfood-23): when True, the entire battery
    # runs in a throwaway checkout of the checkpoint ref plus the session's
    # captured change, never in the agent's working tree. Default None (OFF)
    # so existing missions validate and behave unchanged.
    clean_room: Optional[bool] = None
    # Relative paths copied from the working tree into the clean room after
    # checkout (for things like `.venv`); never carries .tether/, .git/, or
    # sandbox-forbidden paths. Default None so existing missions unchanged.
    clean_room_copy: Optional[List[str]] = None


class MutationSpec(BaseModel):
    """Opt-in mutation-testing meta-verification (dogfood-22): measures
    whether the declared verification can CATCH an incorrect change by
    mutating the files the agent just changed and re-running the suite.
    Default OFF so existing missions behave unchanged."""
    enabled: bool = False   # default OFF; existing missions unchanged
    # Operator names; None means all built-ins (tether.verification).
    operators: Optional[List[str]] = None
    # Cap on mutants per file for determinism/cost.
    max_mutants: int = Field(default=20, gt=0)
    # Kill-rate gate in [0, 1]; None = advisory only (never fails an attempt).
    fail_below: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class ProbeSpec(BaseModel):
    """Behavioral probe: run ``command`` via subprocess (shell=False) in the
    project dir and assert on its combined stdout+stderr. ``contains`` requires
    a literal substring, ``matches`` a regex (re.search); both are optional and
    combinable. The probe passes when the output satisfies ALL conditions — the
    exit code is recorded but is not itself the pass criterion."""
    command: str
    contains: Optional[str] = None
    matches: Optional[str] = None


class AssertionSpec(BaseModel):
    """Content assertion over files matched by a fnmatch glob relative to
    the project dir. ``contains`` requires a literal substring, ``matches``
    a regex (re.search); both are optional and combinable. The assertion
    passes when at least ``min_occurrences`` files satisfy all conditions."""
    path: str
    contains: Optional[str] = None
    matches: Optional[str] = None
    min_occurrences: int = Field(default=1, ge=1)


class RecoverySpec(BaseModel):
    max_attempts: Optional[int] = Field(default=None, ge=1, le=20)
    # Recovery strategy (dogfood-24): "cumulative" (default) keeps the
    # working tree as-is across repair rounds; "reset_to_checkpoint"
    # restores the tree to the checkpoint before every repair send so a
    # fresh round cannot compound earlier damage. Default keeps existing
    # missions byte-for-byte unchanged.
    strategy: Literal["cumulative", "reset_to_checkpoint"] = "cumulative"


class ReviewSpec(BaseModel):
    """Optional review gate: an adversarial reviewer pass over the captured
    change after verification passes (dogfood-15). Default OFF so existing
    missions validate and behave unchanged."""
    enabled: bool = False   # default OFF; existing missions unchanged
    required: bool = True   # when enabled, a rejection fails the mission
    # Independent reviewer adapter name (dogfood-17); None defaults to the
    # mission adapter (self-review). Resolved via the registry at run time,
    # never at validation time.
    adapter: Optional[str] = None
    # When a required review rejects, route back into the bounded recovery
    # loop instead of failing immediately (default off = current behavior).
    retry_on_rejection: bool = False
    # How much of the captured change artifact the reviewer sees (dogfood-20):
    # "excerpt" (today, bounded ~4KB) or "full" (entire artifact up to 64KiB,
    # with an instruction to cite specific hunks/lines). Default keeps
    # existing review behavior unchanged.
    context: str = "excerpt"
    # Reviewer credibility probe (dogfood-24): when set, the command is run
    # (shell-free, reviewer response piped to stdin, cwd = project dir)
    # BEFORE the verdict is trusted; exit 0 marks the reviewer credible.
    # Any other outcome forces request_changes. Default None keeps existing
    # review behavior unchanged.
    credibility_probe: Optional[str] = None
    # Multi-reviewer consensus (dogfood-32): optional list of reviewer adapter
    # names. When set, each is resolved via the registry at run time and
    # consulted in order (credibility probe applied per reviewer); when None,
    # a mission with only `adapter` set keeps today's single-reviewer path.
    reviewers: Optional[List[str]] = None
    # Aggregate verdict policy across reviewers: "all" requires every
    # reviewer to approve; "majority" requires strictly more approvals than
    # rejections (ties fail safe). Ignored for a single effective reviewer.
    consensus: Literal["all", "majority"] = "all"


class BudgetSpec(BaseModel):
    """Optional mission-level budgets (dogfood-21): hard caps enforced by the
    core loop at run time. All fields default to None (unset); a budget with
    no limits set behaves like no budget at all."""
    max_wall_seconds: Optional[int] = Field(default=None, gt=0)
    max_sends: Optional[int] = Field(default=None, gt=0)
    # Cumulative usage-metric ceilings (metric name -> cap). Enforced only
    # while that metric has appeared in the session's cumulative usage, so a
    # configured-but-never-reported metric never false-triggers.
    max_usage: Optional[Dict[str, float]] = None


class RetriesSpec(BaseModel):
    """Transient-failure retry policy (dogfood-31): bounded backoff retries
    for TRANSIENT provider/infrastructure failures during agent sends
    (planning, execution, repair). Genuine agent failures are never retried;
    dry-run behavior is unchanged."""
    # Extra attempts AFTER the first physical send (2 => up to 3 total).
    max_transient_retries: int = Field(default=2, ge=0)
    transient_backoff_seconds: float = Field(default=10, ge=0)


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
    # Optional mission-level budgets (dogfood-21); None (absent) keeps
    # existing missions unchanged.
    budget: Optional[BudgetSpec] = None
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
    # Explicit redaction tuning for resolved-config secrets: denylisted keys
    # are always redacted, allowlisted keys never redacted even when their
    # name contains a secret marker; exact case-insensitive names and the
    # denylist wins. Empty lists (default) keep marker behavior unchanged.
    secret_denylist: List[str] = []
    secret_allowlist: List[str] = []
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
    # Transient-failure tolerance (dogfood-31): bounded retry policy for
    # provider/infrastructure flakes during agent sends.
    retries: RetriesSpec = Field(default_factory=RetriesSpec)


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


class AssertionResult(BaseModel):
    """Outcome of one structural content assertion against the project."""
    path: str
    matched_files: List[str] = []
    passed: bool = False
    detail: str = ""


MutantStatus = Literal["killed", "survived", "skipped"]


class MutantResult(BaseModel):
    """Outcome of one mutant under the verification suite (dogfood-22).

    ``killed`` = the suite failed against the mutated file; ``survived`` =
    the suite still passed (hard evidence of weak verification); ``skipped``
    = the file did not parse or was unreadable.
    """
    file: str
    operator: str
    site: str
    status: MutantStatus
    detail: str = ""


class MutationSummary(BaseModel):
    """Aggregate mutation-testing outcome for one attempt (dogfood-22)."""
    total: int = 0
    killed: int = 0
    survived: int = 0
    skipped: int = 0
    kill_rate: float = 0.0
    per_file: Dict[str, Dict[str, int]] = Field(default_factory=dict)


class CheckpointInfo(BaseModel):
    created: bool = False
    is_git_repo: bool = False
    original_head: Optional[str] = None
    ref: Optional[str] = None
    dirty: bool = False
    warning: Optional[str] = None
