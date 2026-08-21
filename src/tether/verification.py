"""Verification engine: runs only explicitly declared commands, safely."""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from tether.models import VerificationResult


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
