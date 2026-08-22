"""Experimental adapters for opencode and pi.

STATUS: EXPERIMENTAL / UNVERIFIED.

Tether makes no claims about the exact CLI flags of `opencode` or `pi`.
These adapters are thin presets over CommandAdapter with conservative,
documented assumptions. If the default command template does not match your
installed CLI version, override it in tether.yaml, e.g.:

    adapters:
      opencode:
        command: ["opencode", "run", "{prompt}"]

Both adapters fully support dry-run because dry-run is enforced by the
orchestration core before any adapter call is made.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from tether.adapters.command import CommandAdapter, shutil_which


class OpencodeAdapter(CommandAdapter):
    """EXPERIMENTAL preset for the `opencode` CLI.

    Command shape verified against `opencode --help` (installed 2026-08):
    `opencode run [message..]` runs opencode with a message non-interactively.
    The default command pins a model because bare `opencode run` fails with a
    server error on some installations; override `command` in tether.yaml if
    your default model differs.
    """

    name = "opencode"
    verified = False

    def __init__(self, settings: Optional[Dict[str, Any]] = None,
                 default_timeout: int = 1800) -> None:
        merged: Dict[str, Any] = {"command": [
            "opencode", "run", "-m", "opencode/x-preview-f-free", "{prompt}",
        ]}
        merged.update(settings or {})
        super().__init__(merged, default_timeout)

    def is_available(self) -> tuple[bool, str]:
        if shutil_which("opencode") is None:
            return False, "binary not found on PATH: opencode"
        return True, ""


class PiAdapter(CommandAdapter):
    """EXPERIMENTAL preset for the `pi` CLI.

    Command shape verified against `pi --help` (installed 2026-08):
    `--print, -p` enables non-interactive mode ("process prompt and exit").
    End-to-end behavior is NOT exercised by Tether's tests, so this adapter
    remains marked experimental/unverified.
    """

    name = "pi"
    verified = False

    def __init__(self, settings: Optional[Dict[str, Any]] = None,
                 default_timeout: int = 1800) -> None:
        merged: Dict[str, Any] = {"command": ["pi", "--print", "{prompt}"]}
        merged.update(settings or {})
        super().__init__(merged, default_timeout)

    def is_available(self) -> tuple[bool, str]:
        if shutil_which("pi") is None:
            return False, "binary not found on PATH: pi"
        return True, ""