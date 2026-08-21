"""Tether CLI."""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Optional

import typer

import tether.adapters as registry
from tether import __version__
from tether.audit import find_session_dir, new_session_id
from tether.config import load_project_config, resolve_config
from tether.git_safety import rollback as git_rollback
from tether.mission import MissionError, load_mission
from tether.models import TetherConfig
from tether.orchestrator import Orchestrator

app = typer.Typer(help="Tether: agent-agnostic orchestration for coding agents.",
                  no_args_is_help=True)
adapters_app = typer.Typer(help="Adapter operations.")
app.add_typer(adapters_app, name="adapters")

DEFAULT_CONFIG_TEMPLATE = """\
# Tether project configuration.
# Precedence: CLI flags > mission file > this file > defaults.
default_adapter: mock
audit_dir: .tether/sessions
dry_run: false
log_level: INFO
command_timeout_seconds: 1800
verification_timeout_seconds: 600
max_attempts: 3
verification:
  commands: []
"""


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _project_dir(value: Optional[Path]) -> Path:
    return (value or Path.cwd()).resolve()


@app.command()
def init() -> None:
    """Write a starter tether.yaml in the current directory."""
    target = Path.cwd() / "tether.yaml"
    if target.exists():
        typer.echo(f"{target} already exists; not overwriting.")
        raise typer.Exit(code=1)
    target.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    typer.echo(f"Wrote {target}")


@app.command()
def validate_config(
    project_dir: Optional[Path] = typer.Option(None, "--project-dir", help="Target project directory."),
) -> None:
    """Validate the project tether config file."""
    pd = _project_dir(project_dir)
    try:
        cfg = load_project_config(pd)
        resolve_config(pd)
        typer.echo(f"OK: config valid ({json.dumps(cfg) if cfg else 'no config file, defaults apply'})")
    except Exception as e:
        typer.echo(f"INVALID: {e}", err=True)
        raise typer.Exit(code=1)


@app.command()
def validate_mission(mission_file: Path) -> None:
    """Load and validate a mission contract file."""
    try:
        mission = load_mission(mission_file)
        typer.echo(f"OK: mission {mission.name!r} is valid "
                   f"({len(mission.verification.commands)} verification command(s))")
    except MissionError as e:
        typer.echo(f"INVALID: {e}", err=True)
        raise typer.Exit(code=1)


@adapters_app.command("list")
def adapters_list() -> None:
    """List known adapters with availability and verification status."""
    cfg = TetherConfig()
    rows = []
    for name in registry.adapter_names():
        try:
            adapter = registry.resolve_adapter(name, {}, default_timeout=cfg.command_timeout_seconds)
            ok, reason = adapter.is_available()
            status = "available" if ok else "unavailable"
            tag = "verified" if adapter.verified else "experimental"
            issue = reason or "-"
        except Exception as e:
            status, tag, issue = "error", "unknown", str(e)
        rows.append((name, status, tag, issue))
    typer.echo(f"{'NAME':<12} {'STATUS':<12} {'MATURITY':<13} ISSUE")
    for name, status, tag, issue in rows:
        typer.echo(f"{name:<12} {status:<12} {tag:<13} {issue}")


@app.command()
def run(
    mission_file: Path = typer.Argument(..., help="Path to mission YAML/JSON."),
    adapter: Optional[str] = typer.Option(None, "--adapter", help="Override adapter name."),
    project_dir: Optional[Path] = typer.Option(None, "--project-dir", help="Target project directory."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print actions without executing."),
    max_attempts: Optional[int] = typer.Option(None, "--max-attempts", min=1),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", help="Proceed despite dirty git tree."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run a mission against a target project."""
    _setup_logging(verbose)
    pd = _project_dir(project_dir)
    try:
        mission = load_mission(mission_file)
    except MissionError as e:
        typer.echo(f"Mission invalid: {e}", err=True)
        raise typer.Exit(code=1)

    cli_overrides = {
        "default_adapter": adapter,
        "max_attempts": max_attempts,
        "dry_run": True if dry_run else None,
        "allow_dirty": True if allow_dirty else None,
    }
    try:
        config = resolve_config(
            pd,
            mission_overrides={"adapters": mission.adapters} if mission.adapters else None,
            cli_overrides=cli_overrides,
        )
    except Exception as e:
        typer.echo(f"Config error: {e}", err=True)
        raise typer.Exit(code=1)

    adapter_name = adapter or mission.adapter or config.default_adapter
    try:
        adapter_instance = registry.resolve_adapter(
            adapter_name, config.adapters, default_timeout=config.command_timeout_seconds
        )
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    ok, reason = adapter_instance.is_available()
    if not ok and not config.dry_run:
        typer.echo(f"Adapter {adapter_name!r} unavailable: {reason}", err=True)
        raise typer.Exit(code=1)

    orch = Orchestrator(adapter_instance, config, pd, session_id=new_session_id())
    report = orch.run(mission, allow_dirty=allow_dirty or config.allow_dirty,
                      dry_run=config.dry_run)
    typer.echo(f"\nStatus: {report['status']}")
    typer.echo(f"Session: {report['session_id']}")
    typer.echo(f"Report: {report['audit_dir']}/report.json")
    for step in report["next_steps"]:
        typer.echo(f"Next: {step}")
    if report["status"] != "success":
        raise typer.Exit(code=2)


@app.command()
def rollback(
    session_id: str = typer.Argument(..., help="Session id (or prefix)."),
    project_dir: Optional[Path] = typer.Option(None, "--project-dir"),
) -> None:
    """Roll the target project back to a session's checkpoint."""
    pd = _project_dir(project_dir)
    ok, message = git_rollback(pd, session_id)
    typer.echo(message)
    if not ok:
        raise typer.Exit(code=1)


@app.command()
def report(
    session_id: str = typer.Argument(..., help="Session id (or prefix)."),
    project_dir: Optional[Path] = typer.Option(None, "--project-dir"),
) -> None:
    """Print the machine-readable report.json of a past session."""
    pd = _project_dir(project_dir)
    config = resolve_config(pd)
    session = find_session_dir(pd, config.audit_dir, session_id)
    if session is None:
        typer.echo(f"No session found for id {session_id!r} under {pd / config.audit_dir}", err=True)
        raise typer.Exit(code=1)
    path = session / "report.json"
    if not path.exists():
        typer.echo(f"Session found at {session} but no report.json present.", err=True)
        raise typer.Exit(code=1)
    typer.echo(path.read_text(encoding="utf-8"))


@app.callback()
def main(version: bool = typer.Option(False, "--version", callback=lambda v: _version(v))) -> None:
    pass


def _version(show: bool) -> None:
    if show:
        typer.echo(f"tether {__version__}")
        raise typer.Exit()


if __name__ == "__main__":
    app()
