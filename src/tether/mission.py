"""Mission contract loading and validation (YAML or JSON)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import yaml

from pydantic import ValidationError

from tether.models import MissionContract, RecoverySpec, VerificationSpec


class MissionError(ValueError):
    pass


def _parse(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".json",):
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise MissionError(f"Invalid JSON in {path}: {e}") from e
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise MissionError(f"Invalid YAML in {path}: {e}") from e
    if not isinstance(data, dict):
        raise MissionError(f"Mission file {path} must contain a mapping at top level")
    return data


def load_mission(path: str | Path) -> MissionContract:
    p = Path(path)
    if not p.exists():
        raise MissionError(f"Mission file not found: {p}")
    data = _parse(p)
    mission_block = data.get("mission")
    if not isinstance(mission_block, dict):
        raise MissionError("Mission file must contain a 'mission:' block with name and goal")
    name = mission_block.get("name")
    goal = mission_block.get("goal")
    if not isinstance(name, str) or not name.strip():
        raise MissionError("'mission.name' is required and must be a non-empty string")
    if not isinstance(goal, str) or not goal.strip():
        raise MissionError("'mission.goal' is required and must be a non-empty string")

    verification = data.get("verification") or {}
    if not isinstance(verification, dict):
        raise MissionError("'verification' must be a mapping")
    commands = verification.get("commands")
    if commands is None:
        commands = None  # unset: falls back to project config at resolve time
    elif not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
        raise MissionError("'verification.commands' must be a list of strings")
    timeout_seconds = verification.get("timeout_seconds")
    if timeout_seconds is not None and (
        not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
    ):
        raise MissionError("'verification.timeout_seconds' must be a positive integer")

    recovery = data.get("recovery") or {}
    if not isinstance(recovery, dict):
        raise MissionError("'recovery' must be a mapping")
    max_attempts = recovery.get("max_attempts")
    if max_attempts is not None and (
        not isinstance(max_attempts, int) or isinstance(max_attempts, bool)
        or not (1 <= max_attempts <= 20)
    ):
        raise MissionError("'recovery.max_attempts' must be an integer between 1 and 20")

    adapter = data.get("adapter")
    if adapter is not None and not isinstance(adapter, str):
        raise MissionError("'adapter' must be a string")

    for field in ("context", "constraints"):
        value = data.get(field, [])
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise MissionError(f"'{field}' must be a list of strings")

    sandbox_globs: Dict[str, Any] = {}
    for field in ("allowed_paths", "forbidden_paths"):
        value = data.get(field)
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise MissionError(f"'{field}' must be a list of strings")
        sandbox_globs[field] = value

    adapters_block = data.get("adapters") or {}
    if not isinstance(adapters_block, dict):
        raise MissionError("'adapters' must be a mapping of adapter name to settings")
    for aname, asettings in adapters_block.items():
        if not isinstance(asettings, dict):
            raise MissionError(f"'adapters.{aname}' must be a mapping of settings")

    try:
        return MissionContract(
            mission=data["mission"],
            name=name,
            goal=goal,
            context=data.get("context", []) or [],
            constraints=data.get("constraints", []) or [],
            verification=VerificationSpec(commands=commands, timeout_seconds=timeout_seconds),
            recovery=RecoverySpec(max_attempts=max_attempts),
            adapter=adapter,
            adapters=adapters_block,
            allowed_paths=sandbox_globs.get("allowed_paths"),
            forbidden_paths=sandbox_globs.get("forbidden_paths"),
        )
    except ValidationError as e:
        raise MissionError(_format_validation_error(e)) from e


def _format_validation_error(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "<root>"
        parts.append(f"{loc}: {err['msg']}")
    return "Invalid mission: " + "; ".join(parts)


def validate_mission_file(path: str | Path) -> MissionContract:
    """Load and validate; raises MissionError with a readable message on failure."""
    return load_mission(path)
