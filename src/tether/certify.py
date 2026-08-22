"""Adapter certification: behavioral conformance + a live probe of the real CLI.

`tether adapters certify <name>` runs three stages in order and prints a
combined verdict:

1. Availability check (``is_available()``).
2. The full behavioral conformance battery (``tether.conformance``).
3. A live smoke probe against the adapter's REAL configured command (the
   same behavior as ``tether adapters smoke``) inside a throwaway directory.

CERTIFIED (experimental) requires conformance AND the live probe to pass.
Stub-driven conformance alone is never sufficient evidence: the live probe
always exercises the adapter's own configured command, so an unavailable real
command fails certification at the live-probe stage with the reason.
Promotion to ``verified`` additionally requires demonstrated behavior on at
least one real mission (docs/ADAPTERS.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from tether import smoke
from tether.adapters.base import AgentAdapter
from tether.adapters.command import CommandAdapter
from tether.conformance import ConformanceReport, run_conformance

STAGE_AVAILABILITY = "availability"
STAGE_CONFORMANCE = "conformance"
STAGE_LIVE_PROBE = "live_probe"

STAGE_LABELS = {
    STAGE_AVAILABILITY: "availability",
    STAGE_CONFORMANCE: "conformance",
    STAGE_LIVE_PROBE: "live probe",
}

CERTIFIED_MESSAGE = (
    "CERTIFIED (experimental): conformance passed + live probe passed; "
    "promote candidate once real-mission behavior is demonstrated."
)


@dataclass
class CertifyResult:
    """Structured outcome of one certification run for one adapter."""

    name: str
    available: bool = False
    availability_reason: str = ""
    # Recorded instead of aborting when availability failed but the battery
    # can still proceed meaningfully: generic command-family plumbing with no
    # command configured is certified via stub executables by conformance.
    availability_note: str = ""
    conformance: Optional[ConformanceReport] = None
    live_probe: Optional[smoke.SmokeResult] = None
    # "" while every stage passed; otherwise one of the STAGE_* names above.
    failed_stage: str = ""

    @property
    def ok(self) -> bool:
        return self.failed_stage == ""

    @property
    def verdict_line(self) -> str:
        if self.ok:
            return CERTIFIED_MESSAGE
        label = STAGE_LABELS.get(self.failed_stage, self.failed_stage)
        return f"FAILED at {label}: {self._failure_reason()}"

    def _failure_reason(self) -> str:
        if self.failed_stage == STAGE_AVAILABILITY:
            return self.availability_reason or "reported unavailable"
        if self.failed_stage == STAGE_CONFORMANCE:
            assert self.conformance is not None  # stage ran before failing
            failures = self.conformance.failed_checks
            names = ", ".join(check.name for check in failures)
            details = "; ".join(
                f"{check.name}: {check.detail}"
                for check in failures if check.detail
            )
            return f"failed checks: {names}" + (f" ({details})" if details else "")
        assert self.live_probe is not None  # stage ran before failing
        if not self.live_probe.available:
            return self.live_probe.reason or "real command unavailable"
        detail = self.live_probe.error or (
            f"adapter reported status {self.live_probe.status!r}")
        return f"live send against the real command did not complete ({detail})"


def to_dict(result: CertifyResult) -> dict:
    """Serialize a CertifyResult into an auditable, JSON-ready certificate."""
    if result.conformance is not None:
        conformance: Any = {
            "verdict": result.conformance.verdict,
            "checks": [
                {"name": check.name, "status": check.status}
                for check in result.conformance.checks
            ],
        }
    else:
        conformance = "skipped"
    probe = result.live_probe
    live_probe: Any = (
        {
            "status": probe.status or None,
            "exit_code": probe.exit_code,
            "elapsed_seconds": probe.elapsed_seconds,
        }
        if probe is not None
        else "skipped"
    )
    return {
        "name": result.name,
        "utc_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "availability": {
            "available": result.available,
            "reason": result.availability_reason,
        },
        "conformance": conformance,
        "live_probe": live_probe,
        "ok": result.ok,
        "failed_stage": result.failed_stage,
        "verdict_line": result.verdict_line,
    }


def run_certify(adapter: AgentAdapter, name: str) -> CertifyResult:
    """Run availability -> conformance -> live probe, short-circuiting on failure."""
    result = CertifyResult(name=name)

    ok, reason = adapter.is_available()
    result.available = ok
    result.availability_reason = reason
    if not ok:
        configured_command = bool(
            (getattr(adapter, "settings", {}) or {}).get("command"))
        stub_certifiable = isinstance(adapter, CommandAdapter) and not configured_command
        if not stub_certifiable:
            result.failed_stage = STAGE_AVAILABILITY
            return result
        result.availability_note = (
            "no command configured; conformance certifies the generic "
            "plumbing via stub executables below, and the live probe must "
            "pass against the real command to certify"
        )

    report = run_conformance(adapter)
    result.conformance = report
    if not report.ok:
        result.failed_stage = STAGE_CONFORMANCE
        return result

    # Authoritative real-command gate: reuses the exact `tether adapters
    # smoke` behavior (throwaway directory, the adapter's own configured
    # command, no stubs). An unavailable real command fails here with the
    # underlying reason surfaced by is_available().
    probe = smoke.run_smoke(adapter, name)
    result.live_probe = probe
    if not probe.ok:
        result.failed_stage = STAGE_LIVE_PROBE
    return result
