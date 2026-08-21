"""Adapter smoke test: check availability, then run one trivial prompt.

The run always happens inside a throwaway temporary directory (used as the
adapter's project dir), so the caller's working tree — git or otherwise — is
never touched. No audit sessions are created.
"""
from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import tether.adapters as registry
from tether.adapters.base import AgentAdapter
from tether.audit import new_session_id
from tether.config import resolve_config

DEFAULT_PROMPT = "Reply with the single word OK"
EXCERPT_CHARS = 2000


@dataclass
class SmokeResult:
    name: str
    available: bool = False
    reason: str = ""
    ran: bool = False
    status: str = ""
    error: Optional[str] = None
    exit_code: Optional[int] = None
    excerpt: str = ""
    elapsed_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        """True only when the adapter was available and its run completed."""
        return self.available and self.ran and self.status == "completed"


def build_smoke_adapter(name: str, project_dir: Path) -> AgentAdapter:
    """Resolve `name` against the project's tether.yaml config."""
    config = resolve_config(project_dir)
    return registry.resolve_adapter(
        name, config.adapters, default_timeout=config.command_timeout_seconds
    )


def run_smoke(adapter: AgentAdapter, name: str, prompt: str = DEFAULT_PROMPT) -> SmokeResult:
    """Check availability and send `prompt` inside a fresh temp directory.

    The excerpt carries the adapter's combined output (stdout/stderr for
    CommandAdapter); exit_code is populated when the adapter reports one.
    """
    result = SmokeResult(name=name)
    ok, reason = adapter.is_available()
    result.available = ok
    result.reason = reason
    if not ok:
        return result
    with tempfile.TemporaryDirectory(prefix="tether-smoke-") as tmp:
        session = adapter.start_session(tmp, new_session_id())
        start = time.monotonic()
        state = adapter.send(prompt, session)
        result.elapsed_seconds = time.monotonic() - start
    result.ran = True
    result.status = state.status
    result.error = state.error
    if isinstance(state.result, dict):
        code = state.result.get("exit_code")
        result.exit_code = int(code) if isinstance(code, int) else None
    logs = state.logs or ""
    result.excerpt = logs[:EXCERPT_CHARS] + ("... [truncated]" if len(logs) > EXCERPT_CHARS else "")
    return result
