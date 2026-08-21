"""Adapter interface. The core loop knows only this contract."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from tether.models import AgentState


@dataclass
class SessionInfo:
    session_id: str
    project_dir: str
    metadata: Dict[str, Any] = field(default_factory=dict)


class AgentAdapter(abc.ABC):
    """Interface every coding-agent adapter must implement.

    The orchestration core depends only on this interface; all agent-specific
    behavior lives in adapter implementations.
    """

    name: str = "base"
    verified: bool = False

    def __init__(self, settings: Optional[Dict[str, Any]] = None) -> None:
        self.settings = settings or {}

    @abc.abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """Return (available, reason_if_not)."""

    @abc.abstractmethod
    def start_session(self, project_dir: str, session_id: str) -> SessionInfo:
        """Begin an agent session for the given project."""

    @abc.abstractmethod
    def send(self, prompt: str, session: SessionInfo) -> AgentState:
        """Send a prompt/instruction to the agent and return resulting state."""

    @abc.abstractmethod
    def cancel(self, session: SessionInfo) -> None:
        """Best-effort cancellation of the running agent work."""

    def plan_prompt(self, mission_summary: str) -> str:
        return (
            "You are asked to produce a short step-by-step plan for the following "
            f"mission. Do not modify any files yet.\n\nMission:\n{mission_summary}"
        )

    def execute_prompt(self, mission_summary: str) -> str:
        return (
            "Execute the following mission in the current project. Keep changes "
            "minimal and consistent with the constraints.\n\n"
            f"Mission:\n{mission_summary}"
        )

    def repair_prompt(self, mission_summary: str, failing_output: str) -> str:
        return (
            "The previous attempt failed verification. Diagnose the failure from "
            "the output below and fix it. Keep changes minimal.\n\n"
            f"Mission:\n{mission_summary}\n\nFailing verification output:\n{failing_output}"
        )
