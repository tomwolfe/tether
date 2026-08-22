"""Adapter conformance harness: prove behavior, not mere existence.

``run_conformance`` drives any ``AgentAdapter`` through a battery of
deterministic behavioral checks (availability reporting, success/failure/
timeout state mapping, cancellation, log capture, project-directory
containment, spawn failure) and returns structured per-check results.
Checks that genuinely do not apply to an adapter class are reported as
``skipped`` with the reason — never silently ignored and never counted as
failures.

CLI exposure: ``tether adapters conformance <name>``. See docs/ADAPTERS.md
for what conformance means for an adapter's verified/experimental maturity.
"""
from __future__ import annotations

import sys
import tempfile
import textwrap
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from tether.adapters.base import AgentAdapter
from tether.adapters.command import CommandAdapter
from tether.adapters.mock import MockAdapter
from tether.audit import new_session_id
from tether.models import AgentState

CONFORMANCE_PROMPT = "Reply with the single word OK"

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"

_AVAILABILITY = "availability"
_SUCCESS = "success_completes"
_LOGS = "logs_capture_output"
_FAILURE = "failure_maps_failed"
_TIMEOUT = "timeout_fails_and_terminates_tree"
_CANCEL = "cancel_terminates_active"
_SPAWN = "spawn_failure_unavailable"
_PROJECT_DIR = "runs_in_project_dir"

ALL_CHECKS = (
    _AVAILABILITY,
    _SUCCESS,
    _LOGS,
    _FAILURE,
    _TIMEOUT,
    _CANCEL,
    _SPAWN,
    _PROJECT_DIR,
)

STATUS_MARKS = {PASSED: "[PASS]", FAILED: "[FAIL]", SKIPPED: "[SKIP]"}

# Deterministic stub executables, mirroring the technique proven in
# tests/test_adapter_harness.py: plain scripts run via sys.executable so no
# real agent binary or network is needed.
_ECHO_STUB = """\
    import sys
    print("stdout:" + sys.argv[1])
    sys.stderr.write("stderr:" + sys.argv[1] + "\\n")
    """

_CWD_STUB = """\
    import sys
    open(sys.argv[1], "w").write("cwd-ok\\n")
    """

_FAIL_STUB = """\
    import sys
    print("about to fail")
    sys.exit(3)
    """

# Spawns a heartbeating grandchild, then sleeps: liveness of the tree member
# proves spawn; silence after timeout/cancel proves full-tree termination.
_HEARTBEAT_STUB = """\
    import subprocess, sys, time
    hb_path = sys.argv[1]
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time\\n"
         "while True:\\n"
         "    open(sys.argv[1], 'a').write('tick\\\\n')\\n"
         "    time.sleep(0.05)\\n",
         hb_path],
    )
    time.sleep(60)
    """

LOG_MARKER = "conf-log-marker"
CWD_MARKER = "conf-cwd-marker.txt"
MISSING_BINARY = "tether-conformance-missing-binary"

# Bounds for liveness observations and fault-injection variants.
_QUIET_SECONDS = 1.0
_LIVENESS_DEADLINE = 15.0
_CANCEL_PROBE_SECONDS = 5.0
_VARIANT_TIMEOUT_SECONDS = 30


@dataclass
class CheckResult:
    """Outcome of one conformance check."""

    name: str
    status: str  # PASSED | FAILED | SKIPPED
    detail: str = ""

    @property
    def passed(self) -> Optional[bool]:
        """True/False verdict; None when the check was skipped."""
        return {PASSED: True, FAILED: False}.get(self.status)


@dataclass
class ConformanceReport:
    """Structured results of a full conformance battery for one adapter."""

    adapter: str
    checks: List[CheckResult] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append(CheckResult(name=name, status=status, detail=detail))

    @property
    def ok(self) -> bool:
        """True when nothing failed; skipped checks do not block a PASS."""
        return all(check.status != FAILED for check in self.checks)

    @property
    def verdict(self) -> str:
        return "PASS" if self.ok else "FAIL"

    @property
    def failed_checks(self) -> List[CheckResult]:
        return [check for check in self.checks if check.status == FAILED]

    def counts(self) -> Tuple[int, int, int]:
        passed = sum(1 for c in self.checks if c.status == PASSED)
        skipped = sum(1 for c in self.checks if c.status == SKIPPED)
        failed = sum(1 for c in self.checks if c.status == FAILED)
        return passed, skipped, failed

    def verdict_line(self) -> str:
        passed, skipped, failed = self.counts()
        return (f"Verdict: {self.verdict} "
                f"({passed} passed, {skipped} skipped, {failed} failed)")

    def summary(self) -> str:
        lines = [f"Conformance for {self.adapter!r}:"]
        for check in self.checks:
            line = f"  {STATUS_MARKS[check.status]} {check.name}"
            if check.detail:
                line += f" -- {check.detail}"
            lines.append(line)
        lines.append(self.verdict_line())
        return "\n".join(lines)


def capability_flags(adapter: AgentAdapter) -> str:
    """Compact capability summary for `tether adapters list` rows."""
    flags: List[str] = []
    if getattr(adapter, "supports_cancel", False):
        flags.append("cancel")
    if getattr(adapter, "supports_process_tree_kill", False):
        flags.append("tree-kill")
    if getattr(adapter, "supports_usage", False):
        flags.append("usage")
    if getattr(adapter, "supports_streaming", False):
        flags.append("streaming")
    if getattr(adapter, "one_shot", True):
        flags.append("one-shot")
    return ",".join(flags) or "-"


# -- harness helpers ---------------------------------------------------------


def _write_stub(directory: Path, name: str, body: str) -> str:
    path = directory / f"{name}.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(path)


def _wait_until(condition: Callable[[], bool], timeout: float = _LIVENESS_DEADLINE,
                interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


def _wait_until_quiet(path: Path, stable_seconds: float = _QUIET_SECONDS,
                      deadline: float = _LIVENESS_DEADLINE) -> bool:
    """True once the heartbeat file stops growing (writer process died)."""
    start = time.monotonic()
    last_size, last_change = -1, time.monotonic()
    while time.monotonic() - start < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        now = time.monotonic()
        if size != last_size:
            last_size, last_change = size, now
        elif now - last_change >= stable_seconds:
            return True
        time.sleep(0.05)
    return False


def _make_variant(adapter: AgentAdapter, extra: Dict[str, Any]) -> AgentAdapter:
    """Same-class instance reconfigured for deterministic fault injection."""
    settings: Dict[str, Any] = dict(getattr(adapter, "settings", {}) or {})
    settings.update(extra)
    settings.setdefault("timeout_seconds", _VARIANT_TIMEOUT_SECONDS)
    try:
        return type(adapter)(settings=settings)
    except Exception:
        return CommandAdapter(settings=settings)


def _try_send(adapter: AgentAdapter, project_dir: Path,
              prompt: str) -> Tuple[Optional[AgentState], str]:
    try:
        session = adapter.start_session(str(project_dir), new_session_id())
    except Exception as exc:
        return None, f"start_session raised {exc!r}"
    try:
        return adapter.send(prompt, session), ""
    except Exception as exc:
        return None, f"send raised {exc!r}"


def _excerpt(text: Optional[str], limit: int = 160) -> str:
    flat = " ".join((text or "").split())
    return flat[:limit] + ("..." if len(flat) > limit else "")


@dataclass
class _Ctx:
    """Shared state threaded through the checks."""

    adapter: AgentAdapter
    usable: bool = False
    # True when the generic command plumbing is certified via stubs because
    # no usable command is configured on the instance itself.
    stub_mode: bool = False
    success_state: Optional[AgentState] = None

    def skip_if_unusable(self, report: ConformanceReport, name: str) -> bool:
        if self.usable:
            return False
        report.add(name, SKIPPED, "availability failed")
        return True


# -- checks ------------------------------------------------------------------


def _check_availability(ctx: _Ctx, report: ConformanceReport) -> None:
    adapter = ctx.adapter
    try:
        ok, reason = adapter.is_available()
    except Exception as exc:
        ok, reason = False, f"is_available raised {exc!r}"
    if not (isinstance(ok, bool) and isinstance(reason, str)):
        report.add(
            _AVAILABILITY, FAILED,
            f"is_available must return (bool, str); got "
            f"{type(ok).__name__}, {type(reason).__name__}",
        )
        return
    if ok:
        ctx.usable = True
        report.add(_AVAILABILITY, PASSED)
        return
    family = isinstance(adapter, CommandAdapter)
    configured = bool((getattr(adapter, "settings", {}) or {}).get("command"))
    if family and not configured:
        # No command configured: certify the generic command plumbing via a
        # stub instead of failing on an intentionally unconfigured adapter.
        with tempfile.TemporaryDirectory(prefix="tether-conf-avail-") as tmp:
            stub = _write_stub(Path(tmp), "conf-trivial", _ECHO_STUB)
            variant = _make_variant(
                adapter, {"command": [sys.executable, stub, "{prompt}"]})
            vok, vreason = variant.is_available()
        if vok:
            ctx.usable = True
            ctx.stub_mode = True
            report.add(
                _AVAILABILITY, PASSED,
                f"no command configured; certified via stub executable "
                f"(instance reported: {_excerpt(reason)})",
            )
            return
        reason = f"instance: {reason}; stub variant: {vreason}"
    report.add(_AVAILABILITY, FAILED, _excerpt(reason) or "reported unavailable")


def _check_success(ctx: _Ctx, report: ConformanceReport) -> None:
    if ctx.skip_if_unusable(report, _SUCCESS):
        return
    target = ctx.adapter
    with tempfile.TemporaryDirectory(prefix="tether-conf-success-") as tmp:
        if ctx.stub_mode:
            stub = _write_stub(Path(tmp), "conf-success", _ECHO_STUB)
            target = _make_variant(
                ctx.adapter, {"command": [sys.executable, stub, "{prompt}"]})
        state, err = _try_send(target, Path(tmp), CONFORMANCE_PROMPT)
    if state is None:
        report.add(_SUCCESS, FAILED, err)
        return
    ctx.success_state = state
    if state.status == "completed":
        report.add(_SUCCESS, PASSED)
    else:
        detail = f"expected completed, got {state.status!r}"
        if state.error:
            detail += f" ({_excerpt(state.error)})"
        report.add(_SUCCESS, FAILED, detail)


def _check_logs(ctx: _Ctx, report: ConformanceReport) -> None:
    if ctx.skip_if_unusable(report, _LOGS):
        return
    adapter = ctx.adapter
    if isinstance(adapter, CommandAdapter):
        # Deterministic proof that BOTH streams land in AgentState.logs.
        with tempfile.TemporaryDirectory(prefix="tether-conf-logs-") as tmp:
            stub = _write_stub(Path(tmp), "conf-logs", _ECHO_STUB)
            variant = _make_variant(
                adapter, {"command": [sys.executable, stub, "{prompt}"]})
            state, err = _try_send(variant, Path(tmp), LOG_MARKER)
        problems: List[str] = []
        if state is None:
            problems.append(err)
        else:
            if state.status != "completed":
                problems.append(f"status was {state.status!r}")
            for stream in ("stdout", "stderr"):
                if f"{stream}:{LOG_MARKER}" not in state.logs:
                    problems.append(f"{stream} marker missing from logs")
        if problems:
            report.add(_LOGS, FAILED, "; ".join(problems))
        else:
            report.add(_LOGS, PASSED, "distinct stdout/stderr markers captured")
        return
    state = ctx.success_state
    if state is None:
        report.add(_LOGS, SKIPPED, "no successful send to inspect")
        return
    if (state.logs or "").strip():
        report.add(_LOGS, PASSED, f"{len(state.logs)} chars captured in logs")
    else:
        report.add(_LOGS, FAILED, "completed send produced empty logs")


def _check_failure(ctx: _Ctx, report: ConformanceReport) -> None:
    if ctx.skip_if_unusable(report, _FAILURE):
        return
    adapter = ctx.adapter
    if isinstance(adapter, MockAdapter):
        # The documented mock contract: planning succeeds, execution fails.
        with tempfile.TemporaryDirectory(prefix="tether-conf-fail-") as tmp:
            variant = _make_variant(adapter, {"scenario": "always_fail"})
            first, err1 = _try_send(variant, Path(tmp), "execute")
            second, err2 = _try_send(variant, Path(tmp), "execute")
        outcomes = [
            attempt.status if attempt is not None else f"raised({err})"
            for attempt, err in ((first, err1), (second, err2))
        ]
        failed_seen = any(
            attempt is not None and attempt.status == "failed"
            for attempt in (first, second)
        )
        if failed_seen:
            report.add(_FAILURE, PASSED,
                       f"always_fail scenario statuses: {outcomes}")
        else:
            report.add(_FAILURE, FAILED,
                       f"expected a failed status; got {outcomes}")
        return
    if isinstance(adapter, CommandAdapter):
        with tempfile.TemporaryDirectory(prefix="tether-conf-fail-") as tmp:
            stub = _write_stub(Path(tmp), "conf-fail", _FAIL_STUB)
            variant = _make_variant(adapter, {"command": [sys.executable, stub]})
            state, err = _try_send(variant, Path(tmp), "produce a failure")
        if state is None:
            report.add(_FAILURE, FAILED, err)
        elif state.status == "failed":
            report.add(_FAILURE, PASSED,
                       f"nonzero-exit stub mapped to failed "
                       f"(error={state.error!r})")
        else:
            report.add(_FAILURE, FAILED,
                       f"expected failed, got {state.status!r}")
        return
    report.add(_FAILURE, SKIPPED,
               "no deterministic fault injection for this adapter class")


def _check_timeout(ctx: _Ctx, report: ConformanceReport) -> None:
    if ctx.skip_if_unusable(report, _TIMEOUT):
        return
    adapter = ctx.adapter
    if not (isinstance(adapter, CommandAdapter)
            and adapter.supports_process_tree_kill):
        report.add(
            _TIMEOUT, SKIPPED,
            "timeout-to-tree-termination is only exercised for command "
            "adapters claiming process-tree killing")
        return
    with tempfile.TemporaryDirectory(prefix="tether-conf-timeout-") as tmp:
        heartbeat = Path(tmp) / "heartbeat.log"
        stub = _write_stub(Path(tmp), "conf-heartbeat", _HEARTBEAT_STUB)
        variant = _make_variant(adapter, {
            "command": [sys.executable, stub, str(heartbeat)],
            "timeout_seconds": 2,
        })
        state, err = _try_send(variant, Path(tmp), "p")
        timed_out = (state is not None and state.status == "failed"
                     and "timed out" in (state.error or ""))
        tree_dead = _wait_until_quiet(heartbeat) if heartbeat.exists() else False
    problems: List[str] = []
    if state is None:
        problems.append(err)
    elif not timed_out:
        problems.append(
            f"expected failed with a timeout error, got status "
            f"{state.status!r} error {state.error!r}")
    if not tree_dead:
        problems.append("process tree still alive after timeout")
    if problems:
        report.add(_TIMEOUT, FAILED, "; ".join(problems))
    else:
        report.add(_TIMEOUT, PASSED,
                   "hanging command timed out to failed and the tree died")


def _check_cancel(ctx: _Ctx, report: ConformanceReport) -> None:
    if ctx.skip_if_unusable(report, _CANCEL):
        return
    adapter = ctx.adapter
    if not adapter.supports_cancel:
        report.add(_CANCEL, SKIPPED,
                   "adapter declares supports_cancel=False")
        return

    def _problems(state: Optional[AgentState], outcome: Dict[str, Any],
                  alive: bool, started: bool, quiet: Optional[bool]) -> List[str]:
        problems: List[str] = []
        if not started:
            problems.append("active work never became observable")
        if alive:
            problems.append("send did not return after cancel()")
        if state is None:
            problems.append(
                f"no state captured ({outcome.get('error', 'nothing recorded')})")
        elif state.status not in ("failed", "cancelled"):
            problems.append(f"status after cancel was {state.status!r}")
        if quiet is False:
            problems.append("process tree survived cancel()")
        return problems

    if isinstance(adapter, CommandAdapter):
        with tempfile.TemporaryDirectory(prefix="tether-conf-cancel-") as tmp:
            heartbeat = Path(tmp) / "cancel-heartbeat.log"
            stub = _write_stub(Path(tmp), "conf-slow-heartbeat", _HEARTBEAT_STUB)
            variant = _make_variant(adapter, {
                "command": [sys.executable, stub, str(heartbeat)],
                "timeout_seconds": 60,
            })
            session = variant.start_session(str(tmp), new_session_id())
            outcome: Dict[str, Any] = {}

            def _worker() -> None:
                try:
                    outcome["state"] = variant.send("p", session)
                except Exception as exc:
                    outcome["error"] = repr(exc)

            worker = threading.Thread(target=_worker, daemon=True)
            worker.start()
            started = _wait_until(
                lambda: heartbeat.exists() and heartbeat.stat().st_size > 0)
            time.sleep(0.25)  # let send() register the child before cancelling
            variant.cancel(session)
            worker.join(timeout=30)
            if worker.is_alive():
                variant.cancel(session)
                worker.join(timeout=30)
            state = outcome.get("state")
            quiet = (_wait_until_quiet(heartbeat)
                     if heartbeat.exists() else None)
        problems = _problems(state, outcome, worker.is_alive(), started, quiet)
        if problems:
            report.add(_CANCEL, FAILED, "; ".join(problems))
        else:
            report.add(_CANCEL, PASSED,
                       "cancel returned a terminal state and killed the tree")
        return

    # Generic best-effort for non-command adapters claiming cancel support.
    with tempfile.TemporaryDirectory(prefix="tether-conf-cancel-") as tmp:
        session = adapter.start_session(str(tmp), new_session_id())
        outcome = {}

        def _generic_worker() -> None:
            try:
                outcome["state"] = adapter.send(CONFORMANCE_PROMPT, session)
            except Exception as exc:
                outcome["error"] = repr(exc)

        worker = threading.Thread(target=_generic_worker, daemon=True)
        worker.start()
        finished = _wait_until(lambda: not worker.is_alive(),
                               timeout=_CANCEL_PROBE_SECONDS)
        if finished:
            report.add(_CANCEL, SKIPPED,
                       "send finished before cancel could be exercised; "
                       "cannot verify cancellation generically")
            return
        adapter.cancel(session)
        worker.join(timeout=30)
        state = outcome.get("state")
    problems = _problems(state, outcome, worker.is_alive(), True, None)
    if problems:
        report.add(_CANCEL, FAILED, "; ".join(problems))
    else:
        report.add(_CANCEL, PASSED,
                   "cancel interrupted in-flight send to a terminal state")


def _check_spawn_failure(ctx: _Ctx, report: ConformanceReport) -> None:
    if ctx.skip_if_unusable(report, _SPAWN):
        return
    adapter = ctx.adapter
    if not isinstance(adapter, CommandAdapter):
        report.add(_SPAWN, SKIPPED,
                   "spawn failure applies only to command-executing adapters")
        return
    with tempfile.TemporaryDirectory(prefix="tether-conf-spawn-") as tmp:
        variant = _make_variant(adapter, {"command": [MISSING_BINARY]})
        variant_available, _ = variant.is_available()
        state, err = _try_send(variant, Path(tmp), "p")
    if state is None:
        report.add(_SPAWN, FAILED, err)
    elif state.status == "unavailable":
        report.add(_SPAWN, PASSED,
                   f"missing binary mapped to unavailable "
                   f"(variant is_available={variant_available!r})")
    else:
        report.add(_SPAWN, FAILED, f"expected unavailable, got {state.status!r}")


def _check_project_dir(ctx: _Ctx, report: ConformanceReport) -> None:
    if ctx.skip_if_unusable(report, _PROJECT_DIR):
        return
    adapter = ctx.adapter
    if not isinstance(adapter, CommandAdapter):
        report.add(_PROJECT_DIR, SKIPPED,
                   "working directory can only be observed for "
                   "command-executing adapters")
        return
    with tempfile.TemporaryDirectory(prefix="tether-conf-cwd-") as tmp:
        stub = _write_stub(Path(tmp), "conf-cwd", _CWD_STUB)
        variant = _make_variant(
            adapter, {"command": [sys.executable, stub, CWD_MARKER]})
        state, err = _try_send(variant, Path(tmp), "p")
        marker_written = (Path(tmp) / CWD_MARKER).exists()
    if state is None:
        report.add(_PROJECT_DIR, FAILED, err)
    elif state.status == "completed" and marker_written:
        report.add(_PROJECT_DIR, PASSED,
                   "stub's relative-path marker appeared inside project_dir")
    else:
        report.add(
            _PROJECT_DIR, FAILED,
            f"status {state.status!r}, marker inside project_dir: "
            f"{marker_written}")


# -- entry point -------------------------------------------------------------


def run_conformance(adapter: AgentAdapter) -> ConformanceReport:
    """Run the full behavioral battery against any AgentAdapter instance."""
    report = ConformanceReport(
        adapter=str(getattr(adapter, "name", type(adapter).__name__)))
    ctx = _Ctx(adapter=adapter)
    _check_availability(ctx, report)
    _check_success(ctx, report)
    _check_logs(ctx, report)
    _check_failure(ctx, report)
    _check_timeout(ctx, report)
    _check_cancel(ctx, report)
    _check_spawn_failure(ctx, report)
    _check_project_dir(ctx, report)
    return report
