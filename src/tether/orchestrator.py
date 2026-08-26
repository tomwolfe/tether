"""Core orchestration loop. Adapter-agnostic: depends only on AgentAdapter."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from tether.adapters import resolve_adapter
from tether.adapters.base import AgentAdapter
from tether.audit import AuditTrail, new_session_id, redact_body, redact_secrets, utcnow
from tether.cleanroom import CleanRoomError, materialize_clean_room
from tether.context_files import (
    ContextFile,
    ContextFilesError,
    load_context_files,
    render_context_block,
)
from tether.git_safety import (
    REF_PREFIX,
    changed_files_since,
    create_checkpoint,
    describe_sandbox_violation,
    head_sha,
    make_file_backup,
    restore_from_backup,
    sandbox_write_violation,
)
from tether.git_safety import rollback as git_rollback
from tether.manifest import diff_manifests, snapshot_manifest
from tether.models import (
    AgentState,
    ArtifactResult,
    AssertionResult,
    AssertionSpec,
    CheckpointInfo,
    MutationSpec,
    MutationSummary,
    MutantResult,
    ProbeSpec,
    TetherConfig,
    VerificationResult,
)
from tether.reliability import send_with_transient_retry
from tether.verification import (
    REPAIR_OUTPUT_BUDGET,
    ProbeResult,
    check_artifacts,
    check_assertions,
    classify_failure,
    clip_output,
    run_mutation_testing,
    run_probes,
    run_verification,
    summarize,
    summarize_artifacts,
    summarize_assertions,
    summarize_mutation,
    summarize_probes,
)

log = logging.getLogger("tether")

# Sub-budget for the change-artifact excerpt embedded in repair prompts; the
# whole forensic context still goes through clip_output at the ~8KB budget.
FORENSIC_EXCERPT_BUDGET = REPAIR_OUTPUT_BUDGET // 2

# Review gate (dogfood-15): bounded diff excerpt for the reviewer prompt and
# a bounded reason recorded with the verdict.
REVIEW_EXCERPT_BUDGET = REPAIR_OUTPUT_BUDGET // 2
REVIEW_REASON_BUDGET = 500
# Full-context review (dogfood-20): `review.context: "full"` embeds the ENTIRE
# captured artifact up to this larger cap instead of REVIEW_EXCERPT_BUDGET and
# asks the reviewer to cite specific hunks/lines. The default excerpt path is
# unchanged byte-for-byte.
REVIEW_FULL_CONTEXT_BUDGET = 64 * 1024
# Fail-safe verdict contract (case-insensitive scan of the reviewer's logs):
# only lines STARTING with a verdict marker count, and the LAST such line
# decides. Bare substring matching is unsafe twice over: command adapters
# echo the full prompt (which mentions both tokens) ahead of the verdict,
# and captured diffs may legitimately contain marker strings inside test
# fixtures — but echoed prompts and diff hunks never BEGIN a line with
# "REVIEW:". No qualifying line => request_changes.
REVIEW_APPROVE_TOKEN = "review: approve"
REVIEW_CHANGES_TOKEN = "review: request_changes"
# dogfood-40 v2: real reviewers colorize their output, so verdict scanning
# strips ANSI escape sequences BEFORE the line scan (stdlib regex covering
# CSI sequences and two-byte ESC codes). Escape-only lines become blank, and
# escape-prefixed marker lines still decide; clean output is untouched.
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])")


def _verdict_lines(logs: str) -> list[tuple[int, str]]:
    """Indices and case-preserved text of ANSI-stripped lines starting
    with a verdict marker."""
    out: list[tuple[int, str]] = []
    for idx, line in enumerate(_ANSI_ESCAPE_RE.sub("", logs).splitlines()):
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith(REVIEW_APPROVE_TOKEN) or \
                lowered.startswith(REVIEW_CHANGES_TOKEN):
            out.append((idx, stripped))
    return out


def _parse_review_verdict(logs: str) -> tuple[str, str]:
    """Fail-safe verdict parse over raw reviewer output.

    Returns ``(verdict, reason)`` where verdict is ``"approve"`` or
    ``"request_changes"``. ANSI escape sequences are stripped first, so
    colorized output parses identically to clean output. The last line
    beginning with a verdict marker decides; output with no such line
    fails safe as request_changes. The reason prefers the decisive
    line's own remainder after the verdict token when it carries
    substance (e.g. ``REVIEW: REQUEST_CHANGES — patch.diff is empty``),
    otherwise it walks forward past blank/escape-only lines to the first
    substantive line; bounded. A short diagnostic is returned when no
    marker is present.
    """
    candidates = _verdict_lines(logs)
    if not candidates:
        return ("request_changes",
                "no valid review verdict found in reviewer output")
    idx, text = candidates[-1]
    approve = text.lower().startswith(REVIEW_APPROVE_TOKEN)
    verdict = "approve" if approve else "request_changes"
    token = REVIEW_APPROVE_TOKEN if approve else REVIEW_CHANGES_TOKEN
    remainder = text[len(token):].strip()
    if remainder:
        return verdict, clip_output(remainder, REVIEW_REASON_BUDGET)
    lines = _ANSI_ESCAPE_RE.sub("", logs).splitlines()
    for line in lines[idx + 1:]:
        substantive = line.strip()
        if substantive:
            return verdict, clip_output(substantive, REVIEW_REASON_BUDGET)
    return verdict, ""


# Meta-trust (dogfood-24): a reviewer's verdict is only trusted after an
# optional credibility probe passes over the raw response. Any probe
# failure — nonzero exit, crash, timeout — forces this exact rejection.
REVIEWER_CREDIBILITY_FAILURE = "reviewer credibility check failed"


def _run_credibility_probe(command: str, response: str,
                           project_dir: Path,
                           timeout_seconds: int) -> tuple[bool, str]:
    """Run a reviewer credibility probe over the raw response (dogfood-24).

    The command is tokenized with shlex and executed WITHOUT a shell in the
    project directory; the reviewer's full response is piped to stdin. Exit
    0 marks the reviewer credible; every other outcome (nonzero exit,
    spawn/OS error, timeout) fails closed. Never raises.
    """
    argv = shlex.split(command)
    if not argv:
        return False, "empty credibility probe command"
    try:
        proc = subprocess.run(
            argv,
            input=response.encode("utf-8"),
            cwd=str(project_dir),
            capture_output=True,
            timeout=timeout_seconds,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return False, repr(e)
    detail = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    return proc.returncode == 0, clip_output(detail.strip(), 200)


# Oscillation detection (dogfood-24): a failed attempt whose normalized
# failing output plus changed-file set repeats is a fix-A-breaks-B /
# fix-B-breaks-A loop; repeated even under reset-to-checkpoint recovery it
# means the strategy cannot converge, so the loop aborts early instead of
# burning the remaining attempt budget.

def _failure_signature(reason: str, changed: list[str]) -> str:
    """Stable hash of one failed attempt: whitespace-normalized failing
    output plus the sorted distinct changed-file set."""
    normalized = "\n".join(
        line.strip() for line in reason.splitlines() if line.strip())
    digest_input = normalized + "\n\x00" + ",".join(sorted(set(changed)))
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


class _OscillationDetector:
    """Counts failure signatures across recovery attempts (O(attempts)
    memory). ``record`` returns True only for signatures already seen."""

    def __init__(self) -> None:
        self.counts: Dict[str, int] = {}

    def record(self, signature: str) -> bool:
        self.counts[signature] = self.counts.get(signature, 0) + 1
        return self.counts[signature] >= 2

# Tailored recovery guidance per verification failure class (dogfood-14);
# emitted as the header of every repair prompt after a failed attempt.
FAILURE_CLASS_GUIDANCE: Dict[str, str] = {
    "compile_error": "Fix syntax/import/type errors first.",
    "test_failure": "Fix the failing assertions or the code they test.",
    "timeout": "The command timed out; simplify or optimize.",
    "missing_binary": "A required binary is missing; check the command.",
    "unknown": "Diagnose the failure from the output below.",
}

# Guidance header for review-triggered recovery rounds (dogfood-17): the
# reviewer's objections replace the usual failing-output section below.
REVIEW_RETRY_GUIDANCE = (
    "The adversarial review gate rejected the change; address the "
    "reviewer's objections while keeping the mission goal."
)


class _SandboxViolationError(RuntimeError):
    """Write-sandbox gate failure carrying the detected violations.

    Raised by ``_gate_and_capture`` so verification is skipped and the
    report's ``sandbox_violations`` reflects the offending paths.
    """

    def __init__(self, message: str,
                 violations: list[Dict[str, str]]) -> None:
        super().__init__(message)
        self.violations = violations


class _GitStateViolationError(RuntimeError):
    """Opt-in ``git_state_guard`` gate failure carrying human-readable
    drift descriptions.

    Raised by ``_gate_and_capture`` when an enabled guard detects that
    HEAD or the session's checkpoint ref no longer matches the
    checkpointed commit; handled exactly like :class:`_SandboxViolationError`
    so verification is skipped and ``git_state_violations`` lands in the
    report.
    """

    def __init__(self, message: str, violations: list[str]) -> None:
        super().__init__(message)
        self.violations = violations


class _BudgetExceededError(RuntimeError):
    """Mission-budget breach carrying the ``budget_exceeded`` payload.

    Raised by the pre-send / pre-verification budget checks so remaining
    sends and verification are skipped and the report records the breach.
    """

    def __init__(self, message: str, breach: Dict[str, Any]) -> None:
        super().__init__(message)
        self.breach = breach


class _CleanRoomError(RuntimeError):
    """Clean-room materialization failure (dogfood-23).

    Raised so a materialization failure fails the attempt AND the mission
    immediately (fail-closed): verification never falls back to running in
    the agent's working tree.
    """


def _budget_breach(budget: Any, cumulative: Dict[str, float],
                   wall_seconds: float, send_count: int,
                   include_sends: bool) -> Optional[Dict[str, Any]]:
    """First breached budget limit as a ``budget_exceeded`` payload, or None.

    Wall-clock and usage-metric ceilings are checked at every call site; the
    send-count cap is checked only where another send would be attempted
    (``include_sends``), so consuming exactly the allowed number of sends is
    not itself a breach. Usage-metric caps apply only once that metric has
    appeared in the cumulative totals: configured-but-never-reported metrics
    never false-trigger. No budget (None) means no breach, ever.
    """
    if budget is None:
        return None
    max_wall = getattr(budget, "max_wall_seconds", None)
    if max_wall is not None and wall_seconds >= float(max_wall):
        return {"limit": "max_wall_seconds", "threshold": max_wall,
                "observed": round(wall_seconds, 6)}
    max_sends = getattr(budget, "max_sends", None)
    if include_sends and max_sends is not None and send_count >= max_sends:
        return {"limit": "max_sends", "threshold": max_sends,
                "observed": send_count}
    for metric, ceiling in sorted((getattr(budget, "max_usage", None)
                                   or {}).items()):
        if metric in cumulative and cumulative[metric] >= ceiling:
            return {"limit": f"max_usage[{metric}]", "threshold": ceiling,
                    "observed": cumulative[metric]}
    return None


def _budget_message(breach: Dict[str, Any]) -> str:
    return (f"mission budget exceeded: {breach['limit']} "
            f"(threshold {breach['threshold']}, observed {breach['observed']})")


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


def find_incomplete_sessions(project_dir: Path, audit_dir: str,
                             exclude_session_id: str = "") -> list[str]:
    """Names of prior session directories that never reached ``session_end``.

    A session counts as incomplete when its events.jsonl is missing,
    unreadable, empty, unparseable, or its last event kind is not
    ``session_end`` — i.e. the run crashed or was interrupted mid-flight.
    Purely read-only detection for crash recovery: callers must warn, never
    auto-delete or auto-modify the flagged directories. The current session
    (identified by its short id suffix in the directory name) is excluded.
    """
    root = project_dir / audit_dir
    if not root.exists():
        return []
    suffix = f"-{exclude_session_id[:8]}" if exclude_session_id else ""
    incomplete: list[str] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or (suffix and d.name.endswith(suffix)):
            continue
        last_kind = ""
        try:
            lines = [ln for ln in (d / "events.jsonl")
                     .read_text(encoding="utf-8").splitlines() if ln.strip()]
            if lines:
                last_kind = str(json.loads(lines[-1]).get("kind", ""))
        except (OSError, json.JSONDecodeError):
            pass  # missing/unreadable/unparseable log counts as incomplete
        if last_kind != "session_end":
            incomplete.append(d.name)
    return incomplete


class Orchestrator:
    def __init__(self, adapter: AgentAdapter, config: TetherConfig,
                 project_dir: Path, session_id: Optional[str] = None,
                 reviewer: Optional[AgentAdapter] = None) -> None:
        self.adapter = adapter
        # Independent reviewer (dogfood-17): defaults to the mission adapter
        # so unset keeps today's self-review path byte-for-byte.
        self.reviewer = reviewer if reviewer is not None else adapter
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

    def _effective_recovery_strategy(self, mission: Any) -> str:
        """Cumulative (default) vs reset_to_checkpoint recovery posture.

        getattr-guarded so hand-built mission objects without the field
        keep today's behavior.
        """
        return getattr(getattr(mission, "recovery", None),
                       "strategy", None) or "cumulative"

    def _effective_verification_commands(self, mission: Any) -> list[str]:
        if mission.verification.commands is not None:
            return list(mission.verification.commands)
        return list(self.config.verification.commands or [])

    def _effective_verification_timeout(self, mission: Any) -> int:
        if mission.verification.timeout_seconds is not None:
            return mission.verification.timeout_seconds
        return self.config.verification_timeout_seconds

    def _effective_verification_artifacts(self, mission: Any) -> list[str]:
        if mission.verification.artifacts is not None:
            return list(mission.verification.artifacts)
        return list(self.config.verification.artifacts or [])

    def _effective_verification_assertions(self, mission: Any) -> list[AssertionSpec]:
        if mission.verification.assertions is not None:
            return list(mission.verification.assertions)
        return list(self.config.verification.assertions or [])

    def _effective_verification_probes(self, mission: Any) -> list[ProbeSpec]:
        if mission.verification.probes is not None:
            return list(mission.verification.probes)
        return list(self.config.verification.probes or [])

    def _effective_verification_mutation(self, mission: Any) -> Optional[MutationSpec]:
        if mission.verification.mutation is not None:
            return mission.verification.mutation
        return self.config.verification.mutation

    def _effective_verification_clean_room(self, mission: Any) -> bool:
        if mission.verification.clean_room is not None:
            return bool(mission.verification.clean_room)
        return bool(self.config.verification.clean_room)

    def _effective_verification_clean_room_copy(self, mission: Any) -> list[str]:
        if mission.verification.clean_room_copy is not None:
            return list(mission.verification.clean_room_copy)
        return list(self.config.verification.clean_room_copy or [])

    def _sandbox_violations(self, mission: Any,
                            changed: list[str]) -> list[Dict[str, str]]:
        """Post-execution write-sandbox check over detected changed files.

        A violation is a changed file matching a forbidden glob, or (when
        allowed_paths is non-empty) matching no allowed glob. Each rule
        states the cause and, for allowlist misses, the contract's globs
        (see ``sandbox_write_violation``).
        """
        allowed = list(getattr(mission, "allowed_paths", None) or [])
        forbidden = list(getattr(mission, "forbidden_paths", None) or [])
        violations: list[Dict[str, str]] = []
        for path in changed:
            violation = sandbox_write_violation(path, allowed, forbidden)
            if violation is not None:
                violations.append(violation)
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

    def _git_state_violations(self, checkpoint: CheckpointInfo) -> list[str]:
        """Strict integrity checks for opt-in ``git_state_guard`` missions
        (dogfood-41): (a) ``git rev-parse HEAD`` still equals the
        checkpointed original_head, and (b) the session's checkpoint ref
        (``refs/tether/checkpoint/<session-id>``) still resolves to that
        same sha. Returns human-readable drift strings; empty means intact.
        Read-only, shell=False, never raises.
        """
        assert checkpoint.original_head is not None
        expected = checkpoint.original_head
        violations: list[str] = []
        try:
            head = head_sha(self.project_dir)
        except OSError:
            head = None
        if head != expected:
            found = head[:12] if head else "<unresolvable>"
            violations.append(
                f"project HEAD drifted from the checkpointed commit "
                f"(expected {expected[:12]}, found {found}): history was "
                f"rewritten during the session")
        ref = f"{REF_PREFIX}/{self.session_id}"
        proc: Optional[subprocess.CompletedProcess[str]] = None
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.project_dir), "rev-parse",
                 "--verify", "--quiet", ref],
                capture_output=True, text=True, check=False,
            )
        except OSError:
            pass
        resolved = (
            proc.stdout.strip() if proc is not None
            and proc.returncode == 0 and proc.stdout.strip() else None)
        if resolved != expected:
            if resolved is None:
                violations.append(
                    f"session checkpoint ref {ref} no longer resolves to "
                    f"the checkpointed commit (deleted or rewritten); "
                    f"expected {expected[:12]}")
            else:
                violations.append(
                    f"session checkpoint ref {ref} moved off the "
                    f"checkpointed commit (expected {expected[:12]}, "
                    f"found {resolved[:12]})")
        return violations

    def _gate_and_capture(
        self, audit: AuditTrail, mission: Any, checkpoint: CheckpointInfo,
        manifest_before: Optional[dict[str, tuple[int, str | int]]],
        dry_run: bool,
    ) -> list[str]:
        """Re-detect changes, refresh forensic artifacts, re-run the gate.

        Runs after EVERY adapter send (the initial execution and every
        recovery attempt alike): recomputes changed files (``git diff`` vs
        checkpoint HEAD plus untracked; non-git projects and sandbox
        ``enforce`` mode union the filesystem-metadata diff), refreshes the
        change artifact in the session directory, then applies the
        write-sandbox gate. On violation, logs the ``sandbox_violations``
        audit event and raises :class:`_SandboxViolationError` so the mission
        fails and verification is skipped. Missions with ``git_state_guard``
        enabled additionally get the strict HEAD/checkpoint-ref integrity
        check (:class:`_GitStateViolationError`, ``git_state_violations``
        event) under the same fail-and-skip contract. Returns the detected
        changed files.
        """
        changed = changed_files_since(self.project_dir, checkpoint.original_head)
        # Non-git projects: fall back to the pre-execution manifest so change
        # detection (and the sandbox gate below) sees the files the agent
        # just wrote. In sandbox enforce mode, git repos get the same
        # filesystem-metadata diff unioned in: untracked files are already
        # covered by changed_files_since above, and the manifest additionally
        # catches writes invisible to git (e.g. gitignored paths) so they
        # still hit the sandbox gate.
        if manifest_before is not None and (
            not checkpoint.is_git_repo or self.config.sandbox_mode == "enforce"
        ):
            try:
                mdiff = diff_manifests(manifest_before,
                                       snapshot_manifest(self.project_dir))
                changed = sorted(
                    set(changed)
                    | set(mdiff["added"]) | set(mdiff["modified"])
                    | set(mdiff["deleted"])
                )
            except OSError:
                pass
        audit.log_event("changed_files", {"files": changed})

        # Forensic change capture: persist the diff evidence now, before
        # verification (or any later rollback) can alter the tree further.
        if not dry_run:
            self._persist_change_artifact(audit, checkpoint, manifest_before)

        # Write-sandbox gate: forbid or restrict which paths the agent may
        # touch. On violation, fail the mission and skip verification.
        violations = self._sandbox_violations(mission, changed)
        if violations:
            detail = "; ".join(describe_sandbox_violation(v)
                               for v in violations)
            if self.config.sandbox_mode == "warn":
                log.warning(
                    "sandbox_mode is 'warn'; 'sandbox_mode: enforce' would "
                    "have failed this attempt immediately")
            audit.log_event("sandbox_violations",
                            {"violations": violations})
            raise _SandboxViolationError(
                f"write sandbox violated: {detail}", violations)

        # Opt-in git-state guard (dogfood-41): alongside the sandbox gate
        # above, verify strict HEAD + checkpoint-ref integrity after EVERY
        # send. Any drift fails the mission exactly like a sandbox
        # violation; dry-runs, non-git projects, and missions without the
        # key are inert (byte-identical default-OFF path).
        if (not dry_run
                and getattr(mission, "git_state_guard", None)
                and checkpoint.is_git_repo and checkpoint.original_head):
            drifts = self._git_state_violations(checkpoint)
            if drifts:
                detail = "; ".join(drifts)
                audit.log_event("git_state_violations",
                                {"violations": drifts})
                raise _GitStateViolationError(
                    f"git state violated: {detail}", drifts)
        return changed

    def _save_attempt_patch(self, audit: AuditTrail, checkpoint: CheckpointInfo,
                            attempt: int) -> None:
        """Persist a per-attempt patch for git sessions (best-effort).

        Written to ``verification/attempt-NN.patch`` after each recovery send
        so the tree state produced by that attempt stays inspectable even if
        later attempts or verification overwrite it.
        """
        if not (checkpoint.is_git_repo and checkpoint.original_head):
            return
        try:
            patch = _git_patch_bytes(self.project_dir, checkpoint.original_head)
            if patch is not None:
                path = audit.dir / "verification" / f"attempt-{attempt:02d}.patch"
                path.write_bytes(patch)
        except OSError as e:
            log.debug("Per-attempt patch capture failed: %s", e)

    def _forensic_context(
        self, audit: AuditTrail, checkpoint: CheckpointInfo,
        changed: list[str], prev_changed: Optional[list[str]],
    ) -> str:
        """Bounded forensic context folded into recovery prompts.

        Includes the session's current changed files, an excerpt of the
        latest change artifact (``patch.diff`` for git, ``manifest_diff.json``
        otherwise, when present), and the previous attempt's changed files so
        the agent sees what its last repair round actually altered. Bounded;
        best-effort (missing artifacts are simply omitted).
        """
        lines = [
            "--- Forensic context ---",
            "Changed files:\n"
            + ("\n".join(f"- {c}" for c in changed) or "- (none)"),
        ]
        if prev_changed is not None:
            lines.append(
                "Changed files at previous attempt:\n"
                + ("\n".join(f"- {c}" for c in prev_changed) or "- (none)"))
        name = "patch.diff" if checkpoint.is_git_repo else "manifest_diff.json"
        try:
            artifact = (audit.dir / name).read_text(encoding="utf-8",
                                                    errors="replace")
        except OSError:
            artifact = ""
        if artifact.strip():
            lines.append(
                f"Latest change artifact ({name}):\n"
                + clip_output(artifact.strip(), FORENSIC_EXCERPT_BUDGET))
        return "\n\n".join(lines)

    def _mutation_targets(self, mission: Any, changed: list[str]) -> list[str]:
        """Changed files eligible for mutation (dogfood-22).

        Only ``.py`` files are ever mutated; anything under ``.tether/`` is
        dropped, and the write-sandbox rules apply unchanged: forbidden-glob
        matches and — when allowed_paths is set — non-matching paths are
        excluded.
        """
        allowed = list(getattr(mission, "allowed_paths", None) or [])
        forbidden = list(getattr(mission, "forbidden_paths", None) or [])
        targets: list[str] = []
        for rel in sorted(changed):
            posix = Path(rel).as_posix()
            if not posix.endswith(".py"):
                continue
            if sandbox_write_violation(posix, allowed, forbidden) is not None:
                continue
            targets.append(posix)
        return targets

    def _run_mutation_check(
        self, audit: AuditTrail, mission: Any, spec: MutationSpec,
        changed: list[str], timeout: int,
        project_dir: Optional[Path] = None,
    ) -> tuple[MutationSummary, list[MutantResult]]:
        """Run the mutation meta-check over this attempt's changed .py files.

        Builds a ``run_suite`` closure that re-runs the SAME verification
        helpers used on the green attempt (run_verification +
        check_assertions + run_probes over the declared
        commands/assertions/probes), persists per-mutant detail under
        ``verification/mutation.json``, records the ``mutation`` audit event,
        and returns ``(summary, per-mutant results)``. ``project_dir``
        selects where mutants are written and the suite re-runs (the
        clean-room directory when clean-room verification is active);
        defaults to the target project.
        """
        target = project_dir if project_dir is not None else self.project_dir

        def run_suite() -> tuple[bool, str]:
            ok, out = summarize(run_verification(
                self._effective_verification_commands(mission),
                target, timeout_seconds=timeout))
            if not ok:
                return False, out
            assertion_specs = self._effective_verification_assertions(mission)
            if assertion_specs:
                ok, out = summarize_assertions(
                    check_assertions(assertion_specs, target))
                if not ok:
                    return False, out
            probe_specs = self._effective_verification_probes(mission)
            if probe_specs:
                ok, out = summarize_probes(run_probes(
                    probe_specs, target, timeout_seconds=timeout))
                if not ok:
                    return False, out
            return True, ""

        mutants: list[MutantResult] = []
        summary = run_mutation_testing(
            spec, self._mutation_targets(mission, changed), target,
            run_suite, timeout_seconds=timeout, collect_results=mutants)
        try:
            (audit.dir / "verification" / "mutation.json").write_text(
                json.dumps([m.model_dump() for m in mutants], indent=2,
                           default=str),
                encoding="utf-8")
        except OSError as e:
            log.debug("Mutation detail capture failed: %s", e)
        audit.log_event("mutation", {
            "enabled": True,
            "targets": self._mutation_targets(mission, changed),
            "fail_below": spec.fail_below,
            **summary.model_dump(),
            "survived_operators": sorted(
                {m.operator for m in mutants if m.status == "survived"}),
        })
        return summary, mutants

    def _consult_reviewer(
        self, audit: AuditTrail, review_spec: Any, adapter: AgentAdapter,
        label: str, prompt: str,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """One reviewer pass plus its credibility probe; fail-safe verdict.

        Opens a fresh ``<label>`` session on ``adapter``, sends ``prompt``
        with transient-failure retry (dogfood-34: same bounded backoff as
        every other agent send — exhausted retries fall through to this
        method's existing fail-safe request_changes semantics), applies
        the configured credibility probe (dogfood-24) to the raw response
        when set, and parses the verdict fail-safe. Returns
        ``(outcome, state_json)`` where outcome carries ``verdict`` and
        ``reason``; any interaction failure counts as request_changes.
        """
        state_json: Dict[str, Any] = {"status": "unavailable", "logs": ""}
        try:
            session = adapter.start_session(
                str(self.project_dir), f"{self.session_id}-{label}")
            retries = self.config.retries
            state, _physical = send_with_transient_retry(
                adapter.send, prompt, session,
                step=label, audit=audit,
                max_transient_retries=retries.max_transient_retries,
                transient_backoff_seconds=retries.transient_backoff_seconds,
            )
            state_json = state.model_dump()
            probe_cmd = getattr(review_spec, "credibility_probe", None)
            if probe_cmd:
                ok, detail = _run_credibility_probe(
                    probe_cmd, state.logs, self.project_dir,
                    timeout_seconds=self.config.command_timeout_seconds)
                audit.log_event("reviewer_credibility", {
                    "adapter": adapter.name, "ok": ok, "detail": detail})
                if not ok:
                    return ({"verdict": "request_changes",
                             "reason": REVIEWER_CREDIBILITY_FAILURE},
                            state_json)
            verdict, reason = _parse_review_verdict(state.logs)
            return {"verdict": verdict, "reason": reason}, state_json
        except Exception as e:  # noqa: BLE001 - gate must fail safe
            log.warning("Reviewer interaction failed (%s): %r", adapter.name, e)
            return ({"verdict": "request_changes",
                     "reason": f"reviewer failed: {e!r}"}, state_json)

    def _run_review_gate(self, audit: AuditTrail, mission: Any,
                         checkpoint: CheckpointInfo) -> Dict[str, Any]:
        """Adversarial review gate over the captured change (dogfood-15).

        With no ``review.reviewers`` configured, consults the single
        reviewer on ``self.reviewer`` exactly as before (dogfood-17):
        the mission's adapter instance unless an independent reviewer
        adapter was injected. With ``review.reviewers`` (dogfood-32),
        every named reviewer is resolved via the registry and consulted
        in order; the credibility probe runs per reviewer and the
        aggregate verdict follows ``review.consensus`` ("all" requires
        unanimous approval, "majority" strictly more approvals than
        rejections — ties fail safe). Per-reviewer outcomes land in
        ``report["review"]["reviewers"]``.

        The prompt embeds a bounded excerpt of the already-captured
        change artifact (``patch.diff`` for git, ``manifest_diff.json``
        otherwise; no re-diff). With ``review.context: "full"``
        (dogfood-20) the ENTIRE artifact is embedded up to
        REVIEW_FULL_CONTEXT_BUDGET instead, with an instruction to cite
        specific hunks/lines; the default "excerpt" prompt stays
        byte-for-byte unchanged. Verdicts parse fail-safe from reviewer
        logs: the LAST line BEGINNING with either marker decides;
        output with no such line counts as request_changes. Returns the
        ``report["review"]`` payload recording the ACTUAL reviewer
        adapter name(s).
        """
        name = "patch.diff" if checkpoint.is_git_repo else "manifest_diff.json"
        review_spec = getattr(mission, "review", None)
        full_context = bool(
            review_spec is not None
            and getattr(review_spec, "context", "excerpt") == "full")
        try:
            excerpt = clip_output(
                (audit.dir / name).read_text(encoding="utf-8",
                                             errors="replace").strip(),
                REVIEW_FULL_CONTEXT_BUDGET if full_context
                else REVIEW_EXCERPT_BUDGET)
        except OSError:
            excerpt = ""
        verdict_instruction = (
            "Review the change above as an adversarial reviewer and answer "
            "with exactly one verdict line — 'REVIEW: APPROVE' or "
            "'REVIEW: REQUEST_CHANGES' — followed by one line of reasoning."
        )
        if full_context:
            verdict_instruction = (
                "Cite specific hunks or lines from the captured change when "
                "raising concerns.\n" + verdict_instruction)
        prompt = (
            "You are acting as an adversarial code reviewer. Judge whether "
            "the captured change below actually accomplishes the mission "
            "goal. Verification passing is NOT proof of correctness.\n\n"
            f"Mission goal:\n{mission.goal}\n\n"
            f"Captured change ({name}):\n{excerpt or '(no change captured)'}\n\n"
            + verdict_instruction
        )
        audit.save_prompt("review", prompt)
        reviewer_names = list(getattr(review_spec, "reviewers", None) or [])
        if not reviewer_names:
            # Single-reviewer path (dogfood-15/17/20/24): unchanged behavior
            # and payload keys.
            outcome, state_json = self._consult_reviewer(
                audit, review_spec, self.reviewer, "review", prompt)
            audit.save_response("review", state_json)
            info: Dict[str, Any] = {
                "enabled": True,
                "adapter": self.reviewer.name,
                **outcome,
            }
            audit.log_event("review", info)
            return info

        # Multi-reviewer consensus (dogfood-32): resolve every configured
        # reviewer via the registry, consult each on its own fresh session,
        # then aggregate per the consensus policy. Fail-safe at every step:
        # unresolvable names and interaction failures count as rejections.
        outcomes: list[Dict[str, Any]] = []
        resolved_names: list[str] = []
        for reviewer_name in reviewer_names:
            try:
                reviewer_adapter = resolve_adapter(
                    reviewer_name, self.config.adapters,
                    default_timeout=self.config.command_timeout_seconds)
            except ValueError as e:
                log.warning("Reviewer %r cannot be resolved: %s",
                            reviewer_name, e)
                outcomes.append({
                    "adapter": reviewer_name,
                    "verdict": "request_changes",
                    "reason": f"reviewer failed: {e!r}",
                })
                continue
            label = f"review-{reviewer_adapter.name}"
            outcome, state_json = self._consult_reviewer(
                audit, review_spec, reviewer_adapter, label, prompt)
            audit.save_response(label, state_json)
            outcomes.append({"adapter": reviewer_adapter.name, **outcome})
            resolved_names.append(reviewer_adapter.name)

        total = len(outcomes)
        approvals = sum(1 for o in outcomes if o["verdict"] == "approve")
        policy = getattr(review_spec, "consensus", "all")
        approved = approvals * 2 > total if policy == "majority" \
            else (total > 0 and approvals == total)
        if approved:
            verdict = "approve"
            reason = (f"{approvals}/{total} reviewers approved "
                      f"(consensus: {policy})")
        else:
            first_rejection = next(
                (o for o in outcomes if o["verdict"] != "approve"), None)
            verdict = "request_changes"
            reason = (first_rejection or {}).get("reason") or (
                f"consensus not met: {approvals}/{total} approvals "
                f"(policy: {policy})")
        info = {
            "enabled": True,
            "adapter": ",".join(resolved_names),
            "verdict": verdict,
            "reason": reason,
            "consensus": policy,
            "reviewers": outcomes,
        }
        audit.log_event("review", info)
        return info

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
        # Budget guardrails (dogfood-21): wall clock runs from mission start;
        # cumulative usage and the send counter fill in across every send.
        t_start = time.monotonic()
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
        audit.save_json("resolved-config.json", redact_secrets(
            self.config.model_dump(),
            denylist=self.config.secret_denylist,
            allowlist=self.config.secret_allowlist))

        status = "failed"
        verification_results: list[VerificationResult] = []
        artifact_results: list[ArtifactResult] = []
        assertion_results: list[AssertionResult] = []
        probe_results: list[ProbeResult] = []
        recovery_attempts: list[Dict[str, Any]] = []
        plan_text = ""
        next_steps: list[str] = []
        manifest_before: dict[str, tuple[int, str | int]] | None = None
        sandbox_violations: list[Dict[str, str]] = []
        # Git-state guard drifts (dogfood-41); empty unless the opt-in
        # guard fired, so reports of existing missions stay byte-for-byte
        # unchanged.
        git_state_violations: list[str] = []
        # Review gate outcome (dogfood-15); None unless review is enabled, so
        # reports of existing missions stay byte-for-byte unchanged.
        review_result: Optional[Dict[str, Any]] = None
        # Budget breach payload (dogfood-21); None unless a limit is breached,
        # so the "budget_exceeded" report key only appears on real breaches.
        budget_exceeded: Optional[Dict[str, Any]] = None
        # Mutation-testing outcome (dogfood-22); None unless the mission
        # enables the meta-check, so reports of existing missions stay
        # byte-for-byte unchanged.
        mutation_summary: Optional[MutationSummary] = None
        mutation_output = ""
        # Clean-room staging root (dogfood-23); None unless clean-room
        # verification is active, so unset missions behave identically.
        # Created lazily before the attempt loop; removed in finally below.
        clean_room_root: Optional[Path] = None

        # Cumulative usage tracking (dogfood-21): every adapter send merges
        # its numeric usage metrics into running totals; budgets are checked
        # before each send and around verification/recovery.
        cumulative_usage: Dict[str, float] = {}
        send_count = 0

        def _merge_usage(sent: Optional[AgentState]) -> None:
            usage = sent.usage if sent is not None else None
            if not isinstance(usage, dict):
                return
            for key, value in usage.items():
                if isinstance(value, bool) \
                        or not isinstance(value, (int, float)):
                    continue
                cumulative_usage[key] = \
                    cumulative_usage.get(key, 0.0) + float(value)

        def _send_with_retries(step_name: str, prompt: str) -> AgentState:
            """Logical adapter send with transient-failure tolerance
            (dogfood-31): TRANSIENT provider/infrastructure failures
            ("network_error", rate limits, overloaded gateways, ...) are
            retried with bounded backoff instead of aborting the mission
            (planning) or burning a recovery attempt (execution/repair);
            genuine agent failures keep their exact prior semantics. Every
            physical send's usage merges into cumulative_usage IMMEDIATELY
            (so the between-retry budget check never sees stale totals)
            while send_count increments once per logical send; the budget
            is re-checked before each retry's backoff so max_wall_seconds
            still aborts promptly.
            """
            nonlocal send_count
            retries = self.config.retries
            state_out, _physical = send_with_transient_retry(
                self.adapter.send, prompt, session,
                step=step_name, audit=audit,
                max_transient_retries=retries.max_transient_retries,
                transient_backoff_seconds=retries.transient_backoff_seconds,
                before_retry=lambda: _require_budget(True),
                on_result=_merge_usage,
            )
            send_count += 1
            return state_out

        def _require_budget(include_sends: bool) -> None:
            breach = _budget_breach(
                getattr(mission, "budget", None), cumulative_usage,
                time.monotonic() - t_start, send_count, include_sends)
            if breach is not None:
                raise _BudgetExceededError(_budget_message(breach), breach)

        # Crash-recovery detection (advisory): prior session directories
        # whose event log never reached session_end were interrupted
        # mid-flight. Flag them for manual inspection or cleanup — never
        # auto-delete or auto-modify them.
        incomplete = find_incomplete_sessions(
            self.project_dir, self.config.audit_dir,
            exclude_session_id=self.session_id)
        if incomplete:
            log.warning(
                "%d incomplete previous session(s) detected "
                "(last event is not session_end): %s",
                len(incomplete), ", ".join(incomplete))
            audit.log_event("incomplete_sessions_detected",
                            {"sessions": incomplete})
            next_steps.append(
                "Incomplete previous session(s) detected (no session_end "
                "event): " + ", ".join(incomplete)
                + "; inspect manually or prune old sessions with "
                  "`tether sessions clean --older-than <duration> --confirm`."
            )

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

        # Sandbox posture advisory (dogfood-19): with allowed_paths set,
        # warn-mode detection relies only on content-based change detection;
        # enforce additionally unions the filesystem-metadata diff that
        # catches writes invisible to diffing (e.g. gitignored paths).
        if getattr(mission, "allowed_paths", None) \
                and self.config.sandbox_mode == "warn":
            log.warning(
                "allowed_paths is set but sandbox_mode is 'warn'; consider "
                "sandbox_mode: enforce for stronger detection")
            audit.log_event("sandbox_mode_advisory", {
                "sandbox_mode": self.config.sandbox_mode,
                "allowed_paths": list(mission.allowed_paths),
            })

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

        # Change-visibility snapshot before execution (best-effort): always
        # for non-git projects; for git repos only in sandbox enforce mode,
        # where the metadata diff below adds a net over writes that
        # content-based git detection might miss (e.g. gitignored paths).
        if not checkpoint.is_git_repo or self.config.sandbox_mode == "enforce":
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

        # Bounded reference context (mission.context_files): read + validate
        # against the target project BEFORE planning; any violation fails the
        # mission here, so no adapter call ever sees an invalid contract.
        context_block = ""
        if getattr(mission, "context_files", None):
            try:
                ctx_files = load_context_files(
                    self.project_dir, list(mission.context_files)
                )
            except ContextFilesError as e:
                reasons = [line.strip("- ") for line in str(e).splitlines()
                           if line.strip() and line != "invalid context_files:"]
                audit.log_event("context_files_rejected", {"errors": reasons})
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
                    "next_steps": ["Mission aborted: invalid context_files. "
                                   + "; ".join(reasons)],
                    "audit_dir": str(audit.dir),
                }
                audit.write_report(report)
                audit.log_event("session_end", {"status": "failed"})
                log.error("Aborted: invalid context_files (%d issue(s)).",
                          len(reasons))
                return report
            audit.log_event("context_files", {
                "files": [{"path": f.relpath, "bytes": f.size_bytes}
                          for f in ctx_files],
                "total_bytes": sum(f.size_bytes for f in ctx_files),
                "redacted": bool(self.config.redact_prompts),
            })
            if self.config.redact_prompts:
                # Same redaction helper as audit-stored prompts: with
                # redact_prompts enabled, raw context content never reaches
                # the adapter either.
                ctx_files = [
                    ContextFile(relpath=f.relpath, size_bytes=f.size_bytes,
                                content=redact_body(f.content))
                    for f in ctx_files
                ]
            if ctx_files:
                context_block = render_context_block(ctx_files)

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

            # Planning step. Loaded context files (if any) are embedded in
            # the prompt summary so the agent plans against them too.
            planning_summary = self.mission_summary(mission)
            if context_block:
                planning_summary += "\n\n" + context_block
            plan_prompt = self.adapter.plan_prompt(planning_summary)
            audit.save_prompt("plan", plan_prompt)
            if dry_run:
                log.info("[dry-run] would send planning prompt to %s", self.adapter.name)
                state = AgentState(status="completed", logs="[dry-run] skipped")
            else:
                assert session is not None
                _require_budget(True)
                state = _send_with_retries("plan", plan_prompt)
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
            exec_summary = planning_summary
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
                _require_budget(True)
                state = _send_with_retries("execute", exec_prompt)
                audit.save_response("execute", state.model_dump())
                log.info("Execution step status: %s", state.status)

            # Gate + forensic capture after the initial execute send (and,
            # below, after every recovery send): recompute changed files,
            # refresh patch.diff/untracked.txt/manifest_diff.json, then apply
            # the write-sandbox gate. On violation the mission fails here and
            # verification is skipped entirely.
            changed = self._gate_and_capture(
                audit, mission, checkpoint, manifest_before, dry_run)

            # Clean-room verification (dogfood-23): when enabled and not
            # dry-run, the ENTIRE battery below runs in a throwaway checkout
            # of the checkpoint ref plus the session's captured change —
            # never in the agent's working tree, where gitignored helper
            # files could game the declared verification. Materialization
            # failure fails the attempt AND the mission immediately
            # (fail-closed; there is no in-tree fallback).
            verify_dir: Path = self.project_dir
            clean_room_on = self._effective_verification_clean_room(mission)
            if clean_room_on and dry_run:
                audit.log_event("clean_room", {
                    "enabled": True, "status": "skipped",
                    "reason": "dry-run"})
            elif clean_room_on:
                if not (checkpoint.is_git_repo and checkpoint.original_head):
                    raise _CleanRoomError(
                        "clean-room verification requires a git checkpoint; "
                        f"{self.project_dir} has no checkpoint to materialize")
                try:
                    clean_room_root = Path(tempfile.mkdtemp(
                        prefix="tether-cleanroom-"))
                except OSError as e:
                    raise _CleanRoomError(
                        f"cannot create clean-room directory: {e}") from e
                log.info("Clean-room verification active; staging under %s",
                         clean_room_root)

            # Zero-command visibility (dogfood-25): a mission whose resolved
            # verification battery has no commands succeeds without
            # exercising any checks. Not an error (smoke missions may
            # legitimately declare none) but never silent.
            if not commands and not dry_run:
                log.warning(
                    "mission declares no verification commands; success "
                    "will not exercise any checks")
                next_steps.append(
                    "mission declares no verification commands; success "
                    "will not exercise any checks")

            # Verification + recovery loop
            attempt = 0
            # Any non-completed agent state counts as failure: a mission must
            # never report success unless the last agent send completed.
            agent_failed = state.status != "completed"
            artifact_patterns = self._effective_verification_artifacts(mission)
            assertion_specs = self._effective_verification_assertions(mission)
            probe_specs = self._effective_verification_probes(mission)
            prev_changed: Optional[list[str]] = None
            # Nonlinear-recovery state (dogfood-24): failure-signature
            # tracking across attempts, plus an effective strategy that can
            # auto-escalate from cumulative to reset_to_checkpoint when the
            # agent starts oscillating. Purely observational unless a
            # repeat actually fires, so default missions are unchanged.
            oscillation = _OscillationDetector()
            effective_strategy = self._effective_recovery_strategy(mission)
            while True:
                # Budget check around the verification/recovery loop
                # (dogfood-21): wall clock and usage metrics keep growing
                # during sends and verification. The send-count cap is not
                # re-checked here: consuming exactly the allowed sends and
                # passing is not a breach.
                _require_budget(False)
                attempt += 1
                log.info("Verification attempt %d/%d", attempt, max_attempts)
                # Re-materialize a FRESH clean room for every attempt so
                # recovery rounds always verify the latest captured change.
                if clean_room_root is not None:
                    head = checkpoint.original_head
                    assert head is not None  # checked during clean-room setup
                    room = clean_room_root / f"attempt-{attempt:02d}"
                    try:
                        materialize_clean_room(
                            self.project_dir, head, audit.dir,
                            self._effective_verification_clean_room_copy(
                                mission),
                            room)
                    except CleanRoomError as e:
                        raise _CleanRoomError(str(e)) from e
                    verify_dir = room
                else:
                    verify_dir = self.project_dir
                verification_results = run_verification(
                    commands, verify_dir,
                    timeout_seconds=timeout,
                    dry_run=dry_run,
                )
                commands_passed, failing_output = summarize(verification_results)
                # Artifact assertions gate only otherwise-green attempts: when
                # every declared command passed and the agent completed, each
                # pattern must match at least one existing file in the target
                # project. A missing deliverable fails the attempt exactly like
                # a failing command (recovery proceeds normally); when commands
                # already failed there is nothing more to learn from artifacts.
                artifact_results = []
                assertion_results = []
                if commands_passed and not agent_failed:
                    if artifact_patterns:
                        if dry_run:
                            artifact_results = [
                                ArtifactResult(pattern=p, detail="skipped (dry-run)",
                                               passed=True)
                                for p in artifact_patterns
                            ]
                        else:
                            artifact_results = check_artifacts(
                                artifact_patterns, verify_dir)
                    # Structural content assertions (dogfood-19): deeper than
                    # existence checks — run on otherwise-green attempts right
                    # after artifact checks pass their gate; a failing
                    # assertion fails the attempt like any other deliverable
                    # miss and recovery proceeds normally.
                    if assertion_specs:
                        if dry_run:
                            assertion_results = [
                                AssertionResult(path=a.path,
                                                detail="skipped (dry-run)",
                                                passed=True)
                                for a in assertion_specs
                            ]
                        else:
                            assertion_results = check_assertions(
                                assertion_specs, verify_dir)
                artifacts_passed, missing_output = summarize_artifacts(artifact_results)
                if not artifacts_passed:
                    log.warning("%s", missing_output)
                assertions_passed, assertion_output = \
                    summarize_assertions(assertion_results)
                if not assertions_passed:
                    log.warning("%s", assertion_output)
                # Behavioral probes (dogfood-20): run only on otherwise-green
                # attempts (commands + artifacts + assertions all pass). A
                # probe asserts on a command's OUTPUT, not its exit status, so
                # it exercises the produced code; a failing probe fails the
                # attempt like any other deliverable miss and recovery
                # proceeds normally. Dry-run records them as skipped.
                probe_results = []
                if commands_passed and not agent_failed \
                        and artifacts_passed and assertions_passed:
                    if probe_specs:
                        probe_results = run_probes(
                            probe_specs, verify_dir,
                            timeout_seconds=timeout, dry_run=dry_run)
                        probes_passed, probe_output = \
                            summarize_probes(probe_results)
                        if not probes_passed:
                            log.warning("%s", probe_output)
                    else:
                        probes_passed, probe_output = True, ""
                else:
                    probes_passed, probe_output = True, ""
                audit.save_verification(
                    attempt, [*verification_results, *artifact_results,
                              *assertion_results, *probe_results])
                # Mutation meta-check (dogfood-22): runs after the probe tier
                # on otherwise-green attempts when the contract enables it.
                # Gating (fail_below) fails the attempt like any other
                # deliverable miss and recovery proceeds normally; without a
                # gate it is advisory only. Skipped entirely in dry-run.
                mutation_passed = True
                mutation_spec = self._effective_verification_mutation(mission)
                if (commands_passed and not agent_failed
                        and artifacts_passed and assertions_passed
                        and probes_passed and mutation_spec is not None
                        and mutation_spec.enabled):
                    if dry_run:
                        mutation_summary = MutationSummary()
                        audit.log_event("mutation", {
                            "enabled": True, "status": "skipped",
                            "reason": "dry-run"})
                    else:
                        mutation_summary, mutants = self._run_mutation_check(
                            audit, mission, mutation_spec, changed, timeout,
                            project_dir=verify_dir)
                        mutation_passed, mutation_output = \
                            summarize_mutation(
                                mutation_summary, mutants,
                                fail_below=mutation_spec.fail_below)
                        log.warning("%s", mutation_output)
                passed = commands_passed and artifacts_passed \
                    and assertions_passed and probes_passed \
                    and mutation_passed
                # Set when a required review rejects but retry_on_rejection
                # is enabled and attempt budget remains (dogfood-17): control
                # falls through into the normal recovery machinery below
                # instead of failing immediately.
                review_retry_reason: Optional[str] = None
                if passed and not agent_failed:
                    # Review gate (dogfood-15): an independent adversarial
                    # pass over the captured diff, run only after every
                    # verification command AND artifact assertion is green.
                    review_spec = getattr(mission, "review", None)
                    if review_spec is not None and review_spec.enabled:
                        if dry_run:
                            # Dry-run makes no adapter calls; record the
                            # configured-but-not-executed gate honestly.
                            review_result = {
                                "enabled": True,
                                "adapter": self.reviewer.name,
                                "verdict": "skipped",
                                "reason": "dry-run",
                            }
                            audit.log_event("review", review_result)
                        else:
                            review_result = self._run_review_gate(
                                audit, mission, checkpoint)
                        if (review_result["verdict"] == "request_changes"
                                and review_spec.required):
                            if (getattr(review_spec, "retry_on_rejection",
                                        False) and attempt < max_attempts):
                                # Review-triggered recovery routing: one
                                # more pass through the EXISTING recovery
                                # machinery, bounded by the same
                                # max_attempts budget.
                                review_retry_reason = (
                                    review_result["reason"]
                                    or "no reason given")
                                log.warning(
                                    "Review gate rejected the change; "
                                    "routing back into recovery "
                                    "(attempt %d/%d)",
                                    attempt, max_attempts)
                            else:
                                status = "failed"
                                next_steps.append(
                                    "Review gate rejected the change: "
                                    + (review_result["reason"]
                                       or "no reason given"))
                                log.warning(
                                    "Review gate rejected the change: %s",
                                    review_result["reason"])
                                break
                    if review_retry_reason is None:
                        status = "success"
                        break
                if attempt >= max_attempts:
                    status = "failed"
                    deliverable_output = missing_output or assertion_output \
                        or probe_output or mutation_output
                    if deliverable_output and not failing_output:
                        next_steps.append(deliverable_output)
                    next_steps.append(
                        f"Verification failed after {max_attempts} attempts. "
                        f"Roll back with: tether rollback {self.session_id} "
                        f"--project-dir {self.project_dir}"
                    )
                    break
                # Recovery attempt. classify_failure() is a pure helper over
                # the verification results; the class drives the tailored
                # guidance embedded in the repair prompt header below and is
                # recorded in the recovery_attempts audit entry. A review-
                # triggered round (dogfood-17) reuses this exact machinery,
                # with the review reason as the failure input.
                if review_retry_reason is not None:
                    failure_class = "review_rejection"
                    guidance = REVIEW_RETRY_GUIDANCE
                    reason = review_retry_reason
                else:
                    failure_class = classify_failure(verification_results)
                    guidance = FAILURE_CLASS_GUIDANCE.get(
                        failure_class, FAILURE_CLASS_GUIDANCE["unknown"])
                    reason = (failing_output or missing_output
                              or assertion_output or probe_output
                              or mutation_output
                              or (state.error or "agent reported failure"))
                # Oscillation detection (dogfood-24): a repeated failure
                # signature (normalized failing output + changed-file set)
                # means the loop is cycling fix-A-breaks-B /
                # fix-B-breaks-A. The first repeat escalates cumulative
                # mode into reset_to_checkpoint; a second recurrence even
                # under reset means more attempts cannot converge, so the
                # mission aborts early instead of burning the budget.
                signature = _failure_signature(reason, list(changed))
                oscillation.record(signature)
                seen = oscillation.counts[signature]
                if seen >= 2:
                    audit.log_event("oscillation_detected", {
                        "attempt": attempt,
                        "signature": signature,
                        "occurrences": seen,
                        "escalated": seen >= 3,
                    })
                    if seen >= 3:
                        recovery_attempts.append({
                            "attempt": attempt,
                            "failure_class": "oscillation_detected",
                            "failing_output": reason[:4000],
                            "changed_files_at_attempt": list(changed),
                            "oscillation_signature": signature,
                        })
                        status = "failed"
                        next_steps.append(
                            "Oscillation detected: the same failure "
                            "recurred even after reset-to-checkpoint "
                            "recovery, so further attempts cannot "
                            "converge. Address the root cause manually, "
                            f"then roll back with: tether rollback "
                            f"{self.session_id} --project-dir "
                            f"{self.project_dir}"
                        )
                        log.warning(
                            "Oscillation detected at attempt %d/%d; "
                            "aborting recovery loop", attempt, max_attempts)
                        break
                    if effective_strategy != "reset_to_checkpoint":
                        log.warning(
                            "Oscillation detected at attempt %d/%d; "
                            "escalating recovery to reset_to_checkpoint",
                            attempt, max_attempts)
                        effective_strategy = "reset_to_checkpoint"
                recovery_attempt: Dict[str, Any] = {
                    "attempt": attempt,
                    "failure_class": failure_class,
                    "failing_output": reason[:4000],
                    "changed_files_at_attempt": list(changed),
                }
                recovery_attempts.append(recovery_attempt)
                # Configurable recovery strategy (dogfood-24): before the
                # repair send, restore the tree to its checkpoint state so
                # this round starts clean instead of compounding earlier
                # damage. Reuses the exact scoped-rollback machinery;
                # best-effort: a failed reset is recorded and the round
                # proceeds (mirrors _auto_rollback tolerance).
                if effective_strategy == "reset_to_checkpoint" \
                        and not dry_run:
                    if checkpoint.is_git_repo:
                        ok, message = git_rollback(
                            self.project_dir, self.session_id,
                            audit_dir=self.config.audit_dir, clean=True,
                            preserve=pre_existing_untracked)
                    else:
                        ok, message = restore_from_backup(
                            self.project_dir, self.session_id,
                            backup_dir=self.config.backup_dir,
                            audit_dir=self.config.audit_dir)
                    audit.log_event("recovery_reset", {
                        "attempt": attempt,
                        "method": ("git_rollback" if checkpoint.is_git_repo
                                   else "backup_restore"),
                        "ok": ok,
                    })
                    if ok:
                        log.info(
                            "Recovery reset to checkpoint before repair "
                            "attempt %d", attempt)
                    else:
                        log.warning(
                            "Recovery reset did not succeed: %s",
                            message.splitlines()[0] if message else "")
                        recovery_attempt["reset_error"] = message[:500]
                    # Refresh change detection and forensic evidence so the
                    # repair prompt reflects the actual post-reset tree.
                    changed = self._gate_and_capture(
                        audit, mission, checkpoint, manifest_before, dry_run)
                # Recovery intelligence: classification header + tailored
                # guidance, then fold a bounded forensic context (current
                # changed files, latest change-artifact excerpt, previous
                # attempt's changed files) into the failing output passed to
                # the existing repair-prompt builder (signature unchanged).
                reason = clip_output(
                    f"--- Failure class: {failure_class} ---\n"
                    f"{guidance}\n\n"
                    + reason + "\n\n" + self._forensic_context(
                        audit, checkpoint, changed, prev_changed))
                repair_prompt = self.adapter.repair_prompt(
                    self.mission_summary(mission), reason
                )
                audit.save_prompt(f"repair-{attempt}", repair_prompt)
                if dry_run:
                    log.info("[dry-run] would send repair prompt")
                    state = AgentState(status="completed", logs="[dry-run] skipped")
                else:
                    assert session is not None
                    _require_budget(True)
                    state = _send_with_retries(f"repair-{attempt}",
                                               repair_prompt)
                    audit.save_response(f"repair-{attempt}", state.model_dump())
                    log.info("Recovery attempt %d status: %s", attempt, state.status)
                    # Re-gate and refresh forensic evidence after EVERY send:
                    # changes made during recovery must hit the same sandbox
                    # gate as the initial execution, before the next
                    # verification runs.
                    prev_changed = list(changed)
                    changed = self._gate_and_capture(
                        audit, mission, checkpoint, manifest_before, dry_run)
                    recovery_attempt["changed_files_at_attempt"] = list(changed)
                    self._save_attempt_patch(audit, checkpoint, attempt)
                agent_failed = state.status != "completed"
        except RuntimeError as e:
            # Deliberate aborts (e.g. planning failure) already set next_steps.
            status = "failed"
            if isinstance(e, _SandboxViolationError):
                sandbox_violations = e.violations
                detail = "; ".join(describe_sandbox_violation(v)
                                   for v in sandbox_violations)
                next_steps.append(
                    "Write sandbox violated: " + detail + ". Verification "
                    f"was skipped. Roll back with: tether rollback "
                    f"{self.session_id} --project-dir {self.project_dir}"
                )
            elif isinstance(e, _GitStateViolationError):
                git_state_violations = e.violations
                next_steps.append(
                    "Git state violated: "
                    + "; ".join(git_state_violations)
                    + ". Verification was skipped. Roll back with: tether "
                    f"rollback {self.session_id} --project-dir "
                    f"{self.project_dir}"
                )
            elif isinstance(e, _BudgetExceededError):
                budget_exceeded = e.breach
                audit.log_event("budget_exceeded", e.breach)
                breach = e.breach
                next_steps.append(
                    f"Mission budget exceeded: {breach['limit']} "
                    f"(threshold {breach['threshold']}, observed "
                    f"{breach['observed']}). Remaining sends and verification "
                    f"were skipped. Roll back with: tether rollback "
                    f"{self.session_id} --project-dir {self.project_dir}"
                )
            elif isinstance(e, _CleanRoomError):
                audit.log_event("clean_room_error", {"error": str(e)})
                next_steps.append(
                    "Clean-room verification failed; the change was NOT "
                    f"verified and the mission failed closed: {e}. Roll back "
                    f"with: tether rollback {self.session_id} --project-dir "
                    f"{self.project_dir}"
                )
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

        finally:
            # The clean room is throwaway: always removed, on every exit path.
            if clean_room_root is not None:
                shutil.rmtree(clean_room_root, ignore_errors=True)

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

        # Cumulative usage telemetry (dogfood-21): merged numeric metrics
        # across every send plus always-tracked wall clock and send count.
        cumulative_report: Dict[str, Any] = dict(cumulative_usage)
        cumulative_report["wall_seconds"] = round(
            time.monotonic() - t_start, 6)
        cumulative_report["send_count"] = send_count

        report = {
            "session_id": self.session_id,
            "mission_name": mission.name,
            "adapter": self.adapter.name,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "verification_results": [
                r.model_dump() for r in [*verification_results, *artifact_results,
                                        *assertion_results, *probe_results]
            ],
            "recovery_attempts": recovery_attempts,
            "changed_files": changed_files,
            "sandbox_violations": sandbox_violations,
            "usage": state.usage if state is not None else None,
            "cumulative_usage": cumulative_report,
            "checkpoint_info": checkpoint.model_dump(),
            "plan": plan_text[:2000],
            "next_steps": next_steps,
            "audit_dir": str(audit.dir),
        }
        if review_result is not None:
            report["review"] = review_result
        if git_state_violations:
            report["git_state_violations"] = git_state_violations
        if budget_exceeded is not None:
            report["budget_exceeded"] = budget_exceeded
        if mutation_summary is not None:
            report["mutation"] = mutation_summary.model_dump()
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
