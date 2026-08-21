"""Deterministic, fully local mock adapter for tests and demos."""
from __future__ import annotations

from typing import Any, Dict, Optional

from tether.adapters.base import AgentAdapter, SessionInfo
from tether.models import AgentState

SCENARIOS = ("success", "fail_then_succeed", "always_fail")


class MockAdapter(AgentAdapter):
    name = "mock"
    verified = True
    known_settings: frozenset[str] = frozenset({"scenario"})

    def __init__(self, settings: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(settings)
        self.scenario: str = self.settings.get("scenario", "success")
        if self.scenario not in SCENARIOS:
            raise ValueError(f"MockAdapter scenario must be one of {SCENARIOS}, got {self.scenario!r}")
        self._failures_remaining = 1 if self.scenario == "fail_then_succeed" else (
            10**9 if self.scenario == "always_fail" else 0
        )
        self.cancelled = False
        self._first_send_done = False

    def is_available(self) -> tuple[bool, str]:
        return True, ""

    def start_session(self, project_dir: str, session_id: str) -> SessionInfo:
        return SessionInfo(session_id=session_id, project_dir=project_dir,
                           metadata={"scenario": self.scenario})

    def send(self, prompt: str, session: SessionInfo) -> AgentState:
        if self.cancelled:
            return AgentState(status="cancelled", logs="cancelled before send")
        # The first send of a session is the planning step; scenarios
        # simulate execution failures, so planning always succeeds.
        if not self._first_send_done:
            self._first_send_done = True
            return AgentState(status="completed",
                              logs=f"[mock:{self.scenario}] plan ready")
        if self._failures_remaining > 0:
            self._failures_remaining -= 1
            return AgentState(
                status="failed",
                logs=f"[mock:{self.scenario}] simulated failure for prompt: {prompt[:80]}...",
                error="simulated mock failure",
            )
        return AgentState(
            status="completed",
            logs=f"[mock:{self.scenario}] completed prompt: {prompt[:80]}...",
            result={"scenario": self.scenario},
            changed_files=[],
        )

    def cancel(self, session: SessionInfo) -> None:
        self.cancelled = True
