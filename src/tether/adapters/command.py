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

Process containment: each child is spawned in its own process group/session so
timeouts and cancel() can terminate the whole process tree, not just the
immediate child. Stdlib only: on POSIX the group gets SIGTERM then SIGKILL; on
Windows the child gets CREATE_NEW_PROCESS_GROUP and the tree is terminated via
`taskkill /PID <pid> /T [/F]`.
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from tether.adapters.base import AgentAdapter, SessionInfo
from tether.models import AgentState

# Seconds between graceful termination (SIGTERM / taskkill) and force kill.
TERMINATE_GRACE_SECONDS = 3.0


def _spawn_kwargs() -> Dict[str, Any]:
    """Platform-specific process-group isolation kwargs for Popen."""
    kwargs: Dict[str, Any] = {}
    if os.name == "nt":
        # New console process group so the tree can be signalled as a unit;
        # guard for builds that lack the constant.
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", None)
        if flags is not None:
            kwargs["creationflags"] = flags
    else:
        # Own session/process group: signals can address the whole tree.
        kwargs["start_new_session"] = True
    return kwargs


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
        # Active child processes by session id, so cancel(session) can
        # terminate work that is currently in flight.
        self._active_procs: Dict[str, subprocess.Popen] = {}
        self._proc_lock = threading.Lock()

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

    # -- process-tree termination --------------------------------------------

    def _terminate_tree(self, proc: subprocess.Popen,
                        grace_seconds: float = TERMINATE_GRACE_SECONDS) -> None:
        """Best-effort termination of the full process tree behind ``proc``.

        Graceful termination first (SIGTERM to the process group on POSIX,
        ``taskkill /T`` without /F on Windows), then a force kill after
        ``grace_seconds``. Never raises.
        """
        if proc.poll() is not None:
            return
        if os.name == "nt":
            self._windows_terminate_tree(proc, grace_seconds)
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            try:
                proc.terminate()
            except OSError:
                pass
        self._await_exit(proc, grace_seconds)
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
        self._await_exit(proc, grace_seconds)

    def _windows_terminate_tree(self, proc: subprocess.Popen,
                                grace_seconds: float) -> None:
        """Windows best-effort tree kill: taskkill /T, graceful then forced."""
        pid = str(proc.pid)
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for force in (False, True):
            argv = ["taskkill", "/PID", pid, "/T"]
            if force:
                argv.append("/F")
            try:
                subprocess.run(argv, capture_output=True, check=False,
                               timeout=grace_seconds, creationflags=no_window)
            except (OSError, subprocess.SubprocessError):
                pass
            self._await_exit(proc, grace_seconds)
            if proc.poll() is not None:
                return
        try:
            proc.kill()
        except OSError:
            pass

    @staticmethod
    def _await_exit(proc: subprocess.Popen, timeout: float) -> None:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        except OSError:
            pass

    # -- adapter contract -----------------------------------------------------

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
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if via_stdin else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                env=env,
                shell=False,
                **_spawn_kwargs(),
            )
        except FileNotFoundError as e:
            return AgentState(status="unavailable", error=f"command not found: {e}")
        except OSError as e:
            return AgentState(status="failed", error=f"failed to run command: {e}")

        with self._proc_lock:
            self._active_procs[session.session_id] = proc
        timed_out = False
        interrupted = False
        try:
            try:
                stdout, stderr = proc.communicate(input=stdin_data, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                # Terminate the whole tree, then collect whatever output the
                # child produced before it died. Bounded so an escaped
                # descendant holding the pipes cannot hang us forever.
                self._terminate_tree(proc)
                try:
                    stdout, stderr = proc.communicate(
                        timeout=2 * TERMINATE_GRACE_SECONDS
                    )
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", ""
        except BaseException:
            # Keep the handle registered (do not unregister below) so a later
            # cancel(session) can still reap an orphaned process, e.g. after
            # Ctrl-C interrupts this send().
            interrupted = True
            raise
        finally:
            if not interrupted:
                with self._proc_lock:
                    if self._active_procs.get(session.session_id) is proc:
                        del self._active_procs[session.session_id]

        def _text(data: object) -> str:
            if isinstance(data, bytes):
                return data.decode(errors="replace")
            return str(data)

        logs = (f"$ {' '.join(shlex.quote(a) for a in argv)}\n"
                f"{_text(stdout)}{_text(stderr)}")
        if timed_out:
            return AgentState(
                status="failed",
                logs=logs,
                error=f"command timed out after {timeout}s: {argv[0]}",
            )
        if proc.returncode == 0:
            return AgentState(status="completed", logs=logs, result={"exit_code": 0})
        return AgentState(
            status="failed", logs=logs, error=f"exit code {proc.returncode}",
            result={"exit_code": proc.returncode},
        )

    def cancel(self, session: SessionInfo) -> None:
        """Terminate the active command for this session, if any.

        Graceful termination first, then a force kill of the whole process
        tree after a short grace period. No-op when nothing is running.
        """
        with self._proc_lock:
            proc = self._active_procs.get(session.session_id)
        if proc is None or proc.poll() is not None:
            return
        self._terminate_tree(proc)


def shutil_which(binary: str) -> Optional[str]:
    import shutil

    return shutil.which(binary)
