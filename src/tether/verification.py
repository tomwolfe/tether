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


def summarize(results: list[VerificationResult]) -> tuple[bool, str]:
    """Return (all_passed, combined failing output for recovery prompts)."""
    failures = [r for r in results if not r.passed]
    if not failures:
        return True, ""
    parts = []
    for r in failures:
        reason = "timed out" if r.timed_out else f"exit code {r.exit_code}"
        parts.append(f"--- COMMAND: {r.command} ({reason}) ---\n{r.stdout}\n{r.stderr}".strip())
    return False, "\n\n".join(parts)
