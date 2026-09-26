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
    # `name`, `verified` and all five capability flags are declared on
    # AgentAdapter itself, so they are read as plain attributes. A
    # `getattr(..., False)` default would be actively harmful here: it turns a
    # renamed or misspelled capability into a silent `false` -- an adapter
    # claim the JSON reports as fact but nothing ever read. Reading the real
    # attribute keeps the value honest and a wrong name fails loudly.
    # `known_settings` is the opposite case: it is opt-in (only mock/command
    # declare it), so it keeps the same defensive default the registry itself
    # uses in unknown_setting_messages.
    return {
        "name": adapter.name,
        "class": cls.__name__,
        "verified": bool(adapter.verified),
        "capabilities": {
            "cancel": bool(adapter.supports_cancel),
            "process_tree_kill": bool(adapter.supports_process_tree_kill),
            "usage": bool(adapter.supports_usage),
            "streaming": bool(adapter.supports_streaming),
            "one_shot": bool(adapter.one_shot),
        },
        "known_settings": sorted(getattr(cls, "known_settings", frozenset())),
    }
