"""Adapter registry. Resolves adapter names to instances via config settings."""
from __future__ import annotations

from typing import Any, Dict, Optional

from tether.adapters.base import AgentAdapter
from tether.adapters.command import CommandAdapter
from tether.adapters.experimental import OpencodeAdapter, PiAdapter
from tether.adapters.mock import MockAdapter

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
    if isinstance(settings, dict) and "timeout_seconds" not in settings:
        settings = {**settings, "timeout_seconds": default_timeout}
    elif not settings:
        settings = {"timeout_seconds": default_timeout}
    return cls(settings=settings)
