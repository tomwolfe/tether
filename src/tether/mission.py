"""Mission contract loading and validation (YAML or JSON)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import yaml

from pydantic import ValidationError

from tether.models import (
    AssertionSpec,
    BudgetSpec,
    MissionContract,
    ProbeSpec,
    RecoverySpec,
    ReviewSpec,
    VerificationSpec,
)


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

    # Structural validation only: existence of the referenced files is
    # enforced per attempt by the orchestrator against the target project.
    artifacts = verification.get("artifacts")
    if artifacts is not None and (
        not isinstance(artifacts, list) or not all(isinstance(a, str) for a in artifacts)
    ):
        raise MissionError("'verification.artifacts' must be a list of strings")

    # Structural validation only: content checks run per attempt by the
    # orchestrator against the target project (tether.verification).
    raw_assertions = verification.get("assertions")
    assertions: list[AssertionSpec] | None = None
    if raw_assertions is not None:
        if not isinstance(raw_assertions, list) or not all(
                isinstance(a, dict) for a in raw_assertions):
            raise MissionError(
                "'verification.assertions' must be a list of mappings")
        parsed_assertions: list[AssertionSpec] = []
        for idx, entry in enumerate(raw_assertions):
            where = f"'verification.assertions[{idx}]'"
            path_value = entry.get("path")
            if not isinstance(path_value, str) or not path_value:
                raise MissionError(f"{where}.path' must be a non-empty string")
            for key in ("contains", "matches"):
                value = entry.get(key)
                if value is not None and not isinstance(value, str):
                    raise MissionError(f"{where}.{key}' must be a string")
            min_occurrences = entry.get("min_occurrences", 1)
            if (not isinstance(min_occurrences, int)
                    or isinstance(min_occurrences, bool) or min_occurrences < 1):
                raise MissionError(
                    f"{where}.min_occurrences' must be a positive integer")
            parsed_assertions.append(AssertionSpec(
                path=path_value,
                contains=entry.get("contains"),
                matches=entry.get("matches"),
                min_occurrences=min_occurrences,
            ))
        assertions = parsed_assertions

    # Structural validation only (dogfood-20): probes run per attempt by the
    # orchestrator against the target project; existence/behavior of the
    # commands is a run-time concern (tether.verification).
    raw_probes = verification.get("probes")
    probes: list[ProbeSpec] | None = None
    if raw_probes is not None:
        if not isinstance(raw_probes, list) or not all(
                isinstance(p, dict) for p in raw_probes):
            raise MissionError(
                "'verification.probes' must be a list of mappings")
        parsed_probes: list[ProbeSpec] = []
        for idx, entry in enumerate(raw_probes):
            where = f"'verification.probes[{idx}]'"
            command = entry.get("command")
            if not isinstance(command, str) or not command:
                raise MissionError(
                    f"{where}.command' must be a non-empty string")
            for key in ("contains", "matches"):
                value = entry.get(key)
                if value is not None and not isinstance(value, str):
                    raise MissionError(f"{where}.{key}' must be a string")
            parsed_probes.append(ProbeSpec(
                command=command,
                contains=entry.get("contains"),
                matches=entry.get("matches"),
            ))
        probes = parsed_probes

    recovery = data.get("recovery") or {}
    if not isinstance(recovery, dict):
        raise MissionError("'recovery' must be a mapping")
    max_attempts = recovery.get("max_attempts")
    if max_attempts is not None and (
        not isinstance(max_attempts, int) or isinstance(max_attempts, bool)
        or not (1 <= max_attempts <= 20)
    ):
        raise MissionError("'recovery.max_attempts' must be an integer between 1 and 20")

    # Structural validation only: the review gate itself runs at mission
    # runtime (orchestrator), never during validate-mission. Registry
    # resolution of review.adapter is likewise a run-time concern.
    review_block = data.get("review")
    review: ReviewSpec | None = None
    if review_block is not None:
        if not isinstance(review_block, dict):
            raise MissionError("'review' must be a mapping")
        for key in ("enabled", "required", "retry_on_rejection"):
            value = review_block.get(key)
            if key in review_block and not isinstance(value, bool):
                raise MissionError(f"'review.{key}' must be a boolean")
        review_adapter = review_block.get("adapter")
        if review_adapter is not None and not isinstance(review_adapter, str):
            raise MissionError("'review.adapter' must be a string")
        review_context = review_block.get("context", "excerpt")
        if review_context not in ("excerpt", "full"):
            raise MissionError(
                "'review.context' must be 'excerpt' or 'full'")
        unknown = set(review_block) - {
            "enabled", "required", "adapter", "retry_on_rejection",
            "context"}
        if unknown:
            raise MissionError(
                "'review' accepts only 'enabled', 'required', 'adapter', "
                "'retry_on_rejection', and 'context'; got: "
                + ", ".join(sorted(unknown)))
        review = ReviewSpec(
            enabled=review_block.get("enabled", False),
            required=review_block.get("required", True),
            adapter=review_adapter,
            retry_on_rejection=review_block.get("retry_on_rejection", False),
            context=review_context,
        )

    adapter = data.get("adapter")
    if adapter is not None and not isinstance(adapter, str):
        raise MissionError("'adapter' must be a string")

    # Structural validation only (dogfood-21): budgets are enforced at run
    # time by the orchestrator against cumulative usage telemetry.
    raw_budget = data.get("budget")
    budget: BudgetSpec | None = None
    if raw_budget is not None:
        if not isinstance(raw_budget, dict):
            raise MissionError("'budget' must be a mapping")
        unknown = set(raw_budget) - {"max_wall_seconds", "max_sends",
                                     "max_usage"}
        if unknown:
            raise MissionError(
                "'budget' accepts only 'max_wall_seconds', 'max_sends', "
                "and 'max_usage'; got: " + ", ".join(sorted(unknown)))
        for key in ("max_wall_seconds", "max_sends"):
            value = raw_budget.get(key)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool)
                or value <= 0
            ):
                raise MissionError(f"'budget.{key}' must be a positive integer")
        raw_max_usage = raw_budget.get("max_usage")
        max_usage: Dict[str, float] | None = None
        if raw_max_usage is not None:
            if not isinstance(raw_max_usage, dict):
                raise MissionError(
                    "'budget.max_usage' must be a mapping of metric name "
                    "to number")
            for metric, ceiling in raw_max_usage.items():
                if not isinstance(metric, str) or not metric:
                    raise MissionError(
                        "'budget.max_usage' keys must be non-empty strings")
                if (isinstance(ceiling, bool)
                        or not isinstance(ceiling, (int, float))
                        or ceiling <= 0):
                    raise MissionError(
                        f"'budget.max_usage.{metric}' must be a positive "
                        "number")
            max_usage = {str(k): float(v) for k, v in raw_max_usage.items()}
        budget = BudgetSpec(
            max_wall_seconds=raw_budget.get("max_wall_seconds"),
            max_sends=raw_budget.get("max_sends"),
            max_usage=max_usage,
        )

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

    # Structural checks only: existence/size/binary validation happens at
    # run time against the target project (tether.context_files).
    context_files = data.get("context_files", [])
    if not isinstance(context_files, list):
        raise MissionError("'context_files' must be a list of relative file paths")
    if not all(isinstance(x, str) for x in context_files):
        raise MissionError("'context_files' must be a list of strings")

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
            verification=VerificationSpec(
                commands=commands, timeout_seconds=timeout_seconds,
                artifacts=artifacts,
                assertions=assertions,
                probes=probes,
            ),
            recovery=RecoverySpec(max_attempts=max_attempts),
            review=review,
            budget=budget,
            adapter=adapter,
            adapters=adapters_block,
            allowed_paths=sandbox_globs.get("allowed_paths"),
            forbidden_paths=sandbox_globs.get("forbidden_paths"),
            context_files=context_files,
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
