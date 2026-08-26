"""Pure metadata gathering for `tether adapters describe`.

`describe_adapter` resolves a name through the same registry path used by
conformance/certify (`resolve_adapter` with the project config's adapters
block) and returns one dict of static metadata. It never runs the adapter
and never checks availability; the CLI layer owns all I/O (dogfood-43).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import tether.adapters as registry


def describe_adapter(
    name: str, adapters_settings: Optional[Dict[str, Any]] = None,
) -> dict:
    """Return static adapter metadata for `name`, or raise ValueError.

    Resolution goes through ``registry.resolve_adapter`` with the caller's
    adapters settings, exactly like smoke/conformance/certify. The returned
    mapping holds only stdlib types (dict/list/str/bool) in this key order:
    name, class, verified, capabilities, known_settings.
    """
    adapter = registry.resolve_adapter(name, adapters_settings)
    cls = type(adapter)
    return {
        "name": adapter.name,
        "class": cls.__name__,
        "verified": bool(adapter.verified),
        "capabilities": {
            "cancel": bool(getattr(adapter, "supports_cancel", False)),
            "process_tree_kill": bool(
                getattr(adapter, "supports_process_tree_kill", False)),
            "usage": bool(getattr(adapter, "supports_usage", False)),
            "streaming": bool(getattr(adapter, "supports_streaming", False)),
            "one_shot": bool(getattr(adapter, "one_shot", True)),
        },
        "known_settings": sorted(getattr(cls, "known_settings", frozenset())),
    }
