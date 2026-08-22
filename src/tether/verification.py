"""Verification engine: runs only explicitly declared commands, safely."""
from __future__ import annotations

import fnmatch
import os
import shlex
import subprocess
from pathlib import Path

from tether.models import ArtifactResult, VerificationResult


def run_verification(
    commands: list[str],
    project_dir: Path,
    timeout_seconds: int = 600,
    dry_run: bool = False,
) -> list[VerificationResult]:
    results: list[VerificationResult] = []
    for command in commands:
        if dry_run:
            results.append(VerificationResult(command=command, skipped_dry_run=True, passed=True))
            continue
        results.append(_run_one(command, project_dir, timeout_seconds))
    return results


def _run_one(command: str, project_dir: Path, timeout_seconds: int) -> VerificationResult:
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return VerificationResult(command=command, stderr=f"failed to parse command: {e}")
    if not argv:
        return VerificationResult(command=command, stderr="empty command")
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(project_dir),
            shell=False,
        )
    except FileNotFoundError:
        return VerificationResult(command=command, stderr=f"binary not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        return VerificationResult(command=command, timed_out=True,
                                  stderr=f"timed out after {timeout_seconds}s")
    except OSError as e:
        return VerificationResult(command=command, stderr=f"failed to execute: {e}")
    return VerificationResult(
        command=command,
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        passed=proc.returncode == 0,
    )


REPAIR_OUTPUT_BUDGET = 8192


def _project_files(project_dir: Path) -> list[str]:
    """Existing files under project_dir as sorted relative POSIX paths.

    Tether's own bookkeeping directory (.tether/) is excluded so audit
    artifacts can never satisfy a mission deliverable.
    """
    files: list[str] = []
    for root, dirnames, filenames in os.walk(project_dir):
        rel_root = os.path.relpath(root, project_dir)
        if rel_root == ".":
            # Prune before descending; os.walk does not follow symlinked
            # directories by default.
            dirnames[:] = [d for d in dirnames if d != ".tether"]
        for name in filenames:
            rel = name if rel_root == "." else f"{rel_root}{os.sep}{name}"
            files.append(Path(rel).as_posix())
    return sorted(files)


def check_artifacts(patterns: list[str], project_dir: Path) -> list[ArtifactResult]:
    """Match each artifact pattern against existing files in the target project.

    Patterns use fnmatch semantics relative to project_dir (same globs as the
    write sandbox); every pattern must match at least one existing file.
    """
    files = _project_files(project_dir)
    results: list[ArtifactResult] = []
    for pattern in patterns:
        matched = [f for f in files if fnmatch.fnmatch(f, pattern)]
        results.append(ArtifactResult(
            pattern=pattern,
            matched_files=matched,
            passed=bool(matched),
        ))
    return results


def summarize_artifacts(results: list[ArtifactResult]) -> tuple[bool, str]:
    """Return (all_matched, reason naming every unmatched pattern)."""
    unmatched = [r.pattern for r in results if not r.passed]
    if not unmatched:
        return True, ""
    return False, "missing required artifacts: " + ", ".join(unmatched)


def clip_output(text: str, budget: int = REPAIR_OUTPUT_BUDGET) -> str:
    """Clip text to ~budget chars, keeping head and tail with a marker."""
    if len(text) <= budget:
        return text
    half = budget // 2
    return (
        text[:half]
        + f"\n... [truncated {len(text) - budget} characters; full output in audit] ...\n"
        + text[-half:]
    )


def summarize(results: list[VerificationResult]) -> tuple[bool, str]:
    """Return (all_passed, combined failing output for recovery prompts).

    The combined output is clipped to a bounded budget so repair prompts stay
    small; full output remains available in the audit records.
    """
    failures = [r for r in results if not r.passed]
    if not failures:
        return True, ""
    per_command = max(REPAIR_OUTPUT_BUDGET // len(failures), 512)
    parts = []
    for r in failures:
        reason = "timed out" if r.timed_out else f"exit code {r.exit_code}"
        body = clip_output(f"{r.stdout}\n{r.stderr}".strip(), per_command)
        parts.append(f"--- COMMAND: {r.command} ({reason}) ---\n{body}")
    return False, "\n\n".join(parts)


# Coarse failure classes used to tailor recovery prompts (dogfood-14).
COMPILE_ERROR_PATTERNS: tuple[str, ...] = (
    "error:", "SyntaxError", "TypeError", "ImportError",
    "cannot find", "No such file",
)
TEST_FAILURE_PATTERNS: tuple[str, ...] = (
    "FAILED", "assert", "AssertionError", "test_",
)


def classify_failure(results: list[VerificationResult]) -> str:
    """Classify a failed verification attempt into one coarse class.

    Pure helper over the results (no I/O). Precedence per spec:
    timeout > missing_binary > compile_error > test_failure > unknown.
    Returns "compile_error", "test_failure", "timeout", "missing_binary",
    or "unknown".
    """
    if any(r.timed_out for r in results):
        return "timeout"
    if any("not found" in r.stderr for r in results):
        return "missing_binary"
    failing = [r for r in results
               if r.exit_code is not None and r.exit_code != 0]
    if any(any(p in r.stderr for p in COMPILE_ERROR_PATTERNS)
           for r in failing):
        return "compile_error"
    if any(any(p in r.stdout or p in r.stderr for p in TEST_FAILURE_PATTERNS)
           for r in failing):
        return "test_failure"
    return "unknown"
