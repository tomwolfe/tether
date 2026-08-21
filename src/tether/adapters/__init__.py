"""Adapter registry. Resolves adapter names to instances via config settings."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from tether.adapters.base import AgentAdapter
from tether.adapters.command import CommandAdapter
from tether.adapters.experimental import OpencodeAdapter, PiAdapter
from tether.adapters.mock import MockAdapter

log = logging.getLogger("tether.adapters")

_REGISTRY: Dict[str, type[AgentAdapter]] = {
    "mock": MockAdapter,
    "command": CommandAdapter,
    "opencode": OpencodeAdapter,
    "pi": PiAdapter,
}


def register(name: str, cls: type[AgentAdapter]) -> None:
    _REGISTRY[name] = cls


def adapter_names() -> list[str]:
    return sorted(_REGISTRY)


def unknown_setting_messages(adapters_config: Optional[Dict[str, Any]]) -> list[str]:
    """Messages for configured adapter settings keys the adapter class does not know.

    Adapter classes that declare no `known_settings` are skipped, so custom
    registered adapters stay silent until they opt in.
    """
    messages: list[str] = []
    for name, settings in (adapters_config or {}).items():
        cls = _REGISTRY.get(name)
        if cls is None or not isinstance(settings, dict):
            continue
        known: frozenset[str] = getattr(cls, "known_settings", frozenset[str]())
        if not known:
            continue
        for key in sorted(set(settings) - set(known)):
            messages.append(f"adapter {name!r}: unknown setting {key!r}")
    return messages


def check_adapter_settings(
    adapters_config: Optional[Dict[str, Any]], strict: bool = False
) -> list[str]:
    """Warn about unknown adapter settings keys; raise ValueError in strict mode."""
    problems = unknown_setting_messages(adapters_config)
    for message in problems:
        log.warning(message)
    if strict and problems:
        raise ValueError("; ".join(problems))
    return problems


def resolve_adapter(
    name: str, adapters_config: Optional[Dict[str, Dict[str, Any]]] = None,
    default_timeout: int = 1800,
) -> AgentAdapter:
    if name not in _REGISTRY:
        raise ValueError(
            f"Unknown adapter {name!r}. Available: {', '.join(adapter_names())}"
        )
    cls = _REGISTRY[name]
    settings = (adapters_config or {}).get(name, {})
    # Validate what the user configured; the timeout default below is ours,
    # not theirs, and must not count as an unknown key.
    check_adapter_settings({name: settings})
    if isinstance(settings, dict) and "timeout_seconds" not in settings:
        settings = {**settings, "timeout_seconds": default_timeout}
    elif not settings:
        settings = {"timeout_seconds": default_timeout}
    return cls(settings=settings)
