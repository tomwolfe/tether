"""Transient-failure classification and bounded send retries (dogfood-31).

Provider/infrastructure flakes ("finish_reason: network_error", rate
limits, overloaded gateways, 502/503/504, connection resets) used to abort
missions at the planning step or burn recovery attempts on execution/repair
sends, indistinguishable from genuine agent failures. This module provides:

- :func:`is_transient_failure` — pure classifier over a returned AgentState;
- :func:`send_with_transient_retry` — calls an adapter ``send`` and re-sends
  the SAME prompt with bounded backoff while failures stay transient.

Genuine (non-transient) failures keep their exact prior semantics; dry-run
branches never reach a send, so they are untouched.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, List, Optional, Tuple

from tether.audit import AuditTrail
from tether.models import AgentState

log = logging.getLogger("tether")

# Module-level indirection so tests can stub the backoff without patching
# time.sleep globally.
sleep = time.sleep

# Case-insensitive infrastructure signatures matched as substrings over
# state.error ONLY. Never scan logs: captured agent/test output legitimately
# contains outage-sounding words ("connection reset by peer", "got 503",
# "rate limit") on GENUINE failures, which must keep their semantics.
TRANSIENT_SIGNATURES: Tuple[str, ...] = (
    "network_error",
    "finish_reason: network_error",
    "rate_limit",
    "rate limit",
    "overloaded",
    "connection reset",
    # Real provider outage messages observed while Tether dogfooded itself
    # (dogfood-34); matched case-insensitively like the signatures above.
    "endpoint is unavailable",
    "upstream request failed",
)

# Gateway status codes 502/503/504 with word boundaries so e.g. "15023"
# never matches.
_STATUS_CODE = re.compile(r"\b50[234]\b")

# Network-level timeout forms only. The generic forms ("timed out", bare
# "timeout") are deliberately NOT signatures: Tether's own genuine
# agent-timeout failure is error="command timed out after Ns: ..."
# (src/tether/adapters/command.py) and must stay non-transient.
_NETWORK_TIMEOUT_TOKENS = (
    "timeouterror",
    "socket.timeout",
    "etimedout",
    "request timeout",
)

# Bounded reason recorded with each transient_retry audit event.
REASON_BUDGET = 200


def is_transient_failure(state: Optional[AgentState]) -> bool:
    """True when a NON-completed AgentState's ``error`` field shows
    infrastructure-flake text.

    Completed states are never transient (genuine semantics preserved).
    Only adapter-authored ``error`` text is scanned — NEVER ``logs``:
    captured agent/test output may legitimately mention "connection reset",
    "503", or "rate limit" on genuine failures. Generic timeout wording
    ("timed out", bare "timeout") is not a signature either, so Tether's
    own genuine agent-timeout error ("command timed out after Ns") keeps
    its exact prior semantics; only clearly network-level timeout forms
    (TimeoutError, socket.timeout, ETIMEDOUT, "request timeout") count.
    """
    if state is None or state.status == "completed":
        return False
    error_text = (state.error or "").lower()
    if any(sig in error_text for sig in TRANSIENT_SIGNATURES):
        return True
    if _STATUS_CODE.search(error_text):
        return True
    return any(tok in error_text for tok in _NETWORK_TIMEOUT_TOKENS)


def transient_reason(state: AgentState) -> str:
    """Short human-readable reason string for warnings and audit events."""
    parts = [str(state.status)]
    for text in (state.error, state.logs):
        if text:
            parts.append(text.strip())
    reason = " / ".join(parts)
    return reason[:REASON_BUDGET]


def send_with_transient_retry(
    send_fn: Callable[[str, Any], AgentState],
    prompt: str,
    session: Any,
    *,
    step: str,
    audit: AuditTrail,
    max_transient_retries: int,
    transient_backoff_seconds: float,
    before_retry: Optional[Callable[[], None]] = None,
    on_result: Optional[Callable[[AgentState], None]] = None,
) -> Tuple[AgentState, List[AgentState]]:
    """Call ``send_fn(prompt, session)``, retrying TRANSIENT failures.

    At most ``1 + max_transient_retries`` physical sends happen. Each retry
    records a ``transient_retry`` audit event ({step, attempt, clipped
    reason}) plus a warning, re-checks the budget via ``before_retry``
    (which may raise to abort, e.g. max_wall_seconds — deliberately BEFORE
    the sleep), then backs off and re-sends the SAME prompt. Exhausted
    retries simply return the last failed state, so callers fall into their
    existing failure paths unchanged.

    ``on_result`` fires IMMEDIATELY after every physical send (before any
    retry gate), so callers can keep cumulative usage/budget totals fresh
    for the between-retry ``before_retry`` check instead of only after all
    retries complete.

    Returns ``(final_state, physical_results)`` where ``physical_results``
    holds EVERY returned AgentState so callers can merge usage metrics
    across all physical calls while counting exactly one logical send.
    """
    results: List[AgentState] = []

    def _send_once() -> AgentState:
        state_once = send_fn(prompt, session)
        results.append(state_once)
        if on_result is not None:
            on_result(state_once)
        return state_once

    state = _send_once()
    attempt = 0
    while is_transient_failure(state) and attempt < max_transient_retries:
        attempt += 1
        reason = transient_reason(state)
        log.warning(
            "Transient failure during %s send (retry %d/%d); backing off "
            "%.1fs: %s",
            step, attempt, max_transient_retries,
            transient_backoff_seconds, reason)
        audit.log_event("transient_retry", {
            "step": step, "attempt": attempt, "reason": reason})
        if before_retry is not None:
            before_retry()
        sleep(transient_backoff_seconds)
        state = _send_once()
    return state, results
