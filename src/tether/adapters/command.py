"""Generic configurable command adapter for arbitrary CLI coding agents.

This is the primary real-world integration point: a new agent can be wired up
purely through configuration (command template + settings) without changing
Tether's core.

Command template placeholders:
    {prompt}       the full prompt text (or empty if prompt_via_stdin)
    {project_dir}  absolute path of the target project
    {session_id}   the tether session id

Settings (from config `adapters.<name>`):
    command:       list of argv parts, e.g. ["myagent", "--prompt", "{prompt}"]
    prompt_via_stdin: if true, the prompt is piped to stdin instead of {prompt}
    env:           extra environment variables
    timeout_seconds: override default command timeout

The child environment always includes TETHER_SESSION_ID, TETHER_PROJECT_DIR and
(when known) TETHER_MISSION; user `env` entries take precedence.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from tether.adapters.base import AgentAdapter, SessionInfo
from tether.models import AgentState


class CommandAdapter(AgentAdapter):
    name = "command"
    verified = True
    known_settings: frozenset[str] = frozenset(
        {"command", "timeout_seconds", "prompt_via_stdin", "env"}
    )

    def __init__(self, settings: Optional[Dict[str, Any]] = None,
                 default_timeout: int = 1800) -> None:
        super().__init__(settings)
        self.default_timeout = default_timeout

    @property
    def command(self) -> list[str]:
        cmd = self.settings.get("command")
        if not cmd or not isinstance(cmd, list) or not all(isinstance(p, str) for p in cmd):
            raise ValueError("CommandAdapter requires settings['command'] as a list of strings")
        return cmd

    def is_available(self) -> tuple[bool, str]:
        try:
            cmd = self.command
        except ValueError as e:
            return False, str(e)
        if not cmd:
            return False, "empty command"
        binary = shutil_which(cmd[0]) if not Path(cmd[0]).exists() else cmd[0]
        if binary is None:
            return False, f"binary not found on PATH: {cmd[0]}"
        return True, ""

    def start_session(self, project_dir: str, session_id: str) -> SessionInfo:
        return SessionInfo(session_id=session_id, project_dir=project_dir)

    def _render(self, part: str, prompt: str, session: SessionInfo) -> str:
        return (
            part.replace("{prompt}", prompt)
            .replace("{project_dir}", session.project_dir)
            .replace("{session_id}", session.session_id)
        )

    def send(self, prompt: str, session: SessionInfo) -> AgentState:
        via_stdin = bool(self.settings.get("prompt_via_stdin"))
        # When piping the prompt via stdin, {prompt} renders as empty in argv.
        rendered_prompt = "" if via_stdin else prompt
        argv = [self._render(p, rendered_prompt, session) for p in self.command]
        stdin_data: Optional[str] = prompt if via_stdin else None
        env = dict(os.environ)
        # Standard Tether context vars (documented in docs/ADAPTERS.md);
        # user-provided env wins on conflicts.
        env["TETHER_SESSION_ID"] = session.session_id
        env["TETHER_PROJECT_DIR"] = session.project_dir
        if session.metadata.get("mission_name"):
            env["TETHER_MISSION"] = str(session.metadata["mission_name"])
        env.update({str(k): str(v) for k, v in (self.settings.get("env") or {}).items()})
        timeout = int(self.settings.get("timeout_seconds", self.default_timeout))
        cwd = session.project_dir
        try:
            proc = subprocess.run(
                argv,
                input=stdin_data,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=env,
                shell=False,
            )
        except FileNotFoundError as e:
            return AgentState(status="unavailable", error=f"command not found: {e}")
        except subprocess.TimeoutExpired as e:
            stdout = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            return AgentState(
                status="failed",
                logs=stdout + stderr,
                error=f"command timed out after {timeout}s: {argv[0]}",
            )
        except OSError as e:
            return AgentState(status="failed", error=f"failed to run command: {e}")
        def _text(data: object) -> str:
            if isinstance(data, bytes):
                return data.decode(errors="replace")
            return str(data)

        logs = (f"$ {' '.join(shlex.quote(a) for a in argv)}\n"
                f"{_text(proc.stdout)}{_text(proc.stderr)}")
        if proc.returncode == 0:
            return AgentState(status="completed", logs=logs, result={"exit_code": 0})
        return AgentState(
            status="failed", logs=logs, error=f"exit code {proc.returncode}",
            result={"exit_code": proc.returncode},
        )

    def cancel(self, session: SessionInfo) -> None:
        # One-shot subprocess model: nothing long-lived to cancel.
        pass


def shutil_which(binary: str) -> Optional[str]:
    import shutil

    return shutil.which(binary)
