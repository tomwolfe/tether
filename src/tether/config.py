"""Configuration loading with precedence: CLI flags > mission file > project config > defaults."""
from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from tether.models import TetherConfig

CONFIG_FILENAMES = ("tether.yaml", "tether.yml", "tether.json", "tether.toml")


def _read_config_file(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".toml":
        return tomllib.loads(text)
    if path.suffix == ".json":
        return json.loads(text)
    data = yaml.safe_load(text)
    return data or {}


def find_project_config(project_dir: Path) -> Optional[Path]:
    for name in CONFIG_FILENAMES:
        candidate = project_dir / name
        if candidate.exists():
            return candidate
    return None


def load_project_config(project_dir: Path) -> Dict[str, Any]:
    path = find_project_config(project_dir)
    if path is None:
        return {}
    try:
        return _read_config_file(path)
    except Exception as e:
        raise ValueError(f"Failed to parse project config {path}: {e}") from e


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if v is not None:
            out[k] = v
    return out


def _deep_merge_adapters(project: Any, mission: Any) -> Dict[str, Dict[str, Any]]:
    """Per-adapter deep merge: mission adapter settings override project settings
    key-by-key within each adapter name."""
    merged: Dict[str, Dict[str, Any]] = {}
    for layer in (project, mission):
        if not isinstance(layer, dict):
            continue
        for name, settings in layer.items():
            if isinstance(settings, dict):
                base = dict(merged.get(name, {}))
                base.update(settings)
                merged[name] = base
            else:
                merged[name] = settings
    return merged


def resolve_config(
    project_dir: Path,
    mission_overrides: Optional[Dict[str, Any]] = None,
    cli_overrides: Optional[Dict[str, Any]] = None,
) -> TetherConfig:
    """Merge configuration layers and validate into TetherConfig.

    Precedence (highest wins): cli_overrides > mission_overrides > project config > defaults.
    Adapter settings are deep-merged per adapter name across project config and mission.
    """
    project_cfg = load_project_config(project_dir)
    merged: Dict[str, Any] = {}
    merged = _merge(merged, project_cfg)
    if mission_overrides:
        merged = _merge(merged, mission_overrides)
    if cli_overrides:
        merged = _merge(merged, cli_overrides)
    adapters_cfg = _deep_merge_adapters(
        project_cfg.get("adapters"), (mission_overrides or {}).get("adapters")
    )
    known = {k: merged[k] for k in list(merged) if k in TetherConfig.model_fields}
    unknown = set(merged) - set(TetherConfig.model_fields)
    if unknown:
        raise ValueError(f"Unknown config keys: {sorted(unknown)}")
    cfg = TetherConfig(**known)
    cfg.adapters = adapters_cfg
    return cfg
