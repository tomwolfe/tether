"""Tether CLI."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import typer

import tether.adapters as registry
from tether import __version__
from tether import smoke
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

# Granular `tether run` exit codes.
EXIT_SUCCESS = 0
EXIT_FAILED = 1
EXIT_CANCELLED = 2
EXIT_REJECTED = 3
EXIT_SANDBOX_VIOLATION = 4

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


def _setup_logging(verbose: bool, config_level: Optional[str] = None) -> None:
    """Configure logging. ``--verbose`` forces DEBUG; otherwise the resolved
    config's ``log_level`` applies (invalid values fall back to INFO)."""
    if verbose:
        level = logging.DEBUG
    else:
        level = getattr(logging, str(config_level or "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("tether").setLevel(level)


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
    strict: bool = typer.Option(
        False, "--strict", help="Treat unknown adapter settings as errors."),
) -> None:
    """Validate the project tether config file."""
    pd = _project_dir(project_dir)
    try:
        cfg = load_project_config(pd)
        resolved = resolve_config(pd)
        registry.check_adapter_settings(resolved.adapters, strict=strict)
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
                   f"({len(mission.verification.commands or [])} verification command(s))")
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


@adapters_app.command("smoke")
def adapters_smoke(
    name: str = typer.Argument(..., help="Adapter name (see `tether adapters list`)."),
    prompt: str = typer.Option(
        smoke.DEFAULT_PROMPT, "--prompt", help="Trivial prompt sent to the adapter."),
) -> None:
    """Send a trivial prompt to an adapter inside a throwaway directory."""
    pd = _project_dir(None)
    try:
        adapter_instance = smoke.build_smoke_adapter(name, pd)
        result = smoke.run_smoke(adapter_instance, name, prompt)
    except Exception as e:
        typer.echo(f"Smoke FAILED: {e}", err=True)
        raise typer.Exit(code=1)
    availability = "available" if result.available else f"unavailable ({result.reason})"
    typer.echo(f"{'Adapter:':<14}{name}")
    typer.echo(f"{'Availability:':<14}{availability}")
    if not result.available:
        raise typer.Exit(code=1)
    exit_label = "-" if result.exit_code is None else str(result.exit_code)
    typer.echo(f"{'Prompt:':<14}{prompt}")
    typer.echo(f"{'Status:':<14}{result.status}")
    typer.echo(f"{'Exit code:':<14}{exit_label}")
    typer.echo(f"{'Elapsed:':<14}{result.elapsed_seconds:.2f}s")
    typer.echo("Output excerpt:")
    for line in (result.excerpt or "(no output)").splitlines() or ["(no output)"]:
        typer.echo(f"  {line}")
    if not result.ok:
        detail = result.error or f"adapter reported status {result.status!r}"
        typer.echo(f"Smoke FAILED: {detail}", err=True)
        raise typer.Exit(code=1)
    typer.echo("Smoke PASSED")


@app.command()
def run(
    mission_file: Path = typer.Argument(..., help="Path to mission YAML/JSON."),
    adapter: Optional[str] = typer.Option(None, "--adapter", help="Override adapter name."),
    project_dir: Optional[Path] = typer.Option(None, "--project-dir", help="Target project directory."),
    dry_run: Optional[bool] = typer.Option(
        None, "--dry-run/--no-dry-run",
        help="Print actions without executing. Overrides project config when given."),
    max_attempts: Optional[int] = typer.Option(None, "--max-attempts", min=1),
    allow_dirty: Optional[bool] = typer.Option(
        None, "--allow-dirty/--no-allow-dirty",
        help="Proceed despite dirty git tree. Overrides project config when given."),
    auto_rollback: Optional[bool] = typer.Option(
        None, "--auto-rollback/--no-auto-rollback",
        help="Automatically roll back failed/cancelled missions (scoped clean "
             "rollback for git projects, backup restore otherwise). Never "
             "applies to success or dry-run. Overrides project config when "
             "given."),
    strict: bool = typer.Option(
        False, "--strict", help="Treat unknown adapter settings as errors."),
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

    # Mission explicit values override project config; CLI overrides both.
    mission_overrides: dict = {"adapters": mission.adapters} if mission.adapters else {}
    if mission.recovery.max_attempts is not None:
        mission_overrides["max_attempts"] = mission.recovery.max_attempts
    if mission.verification.timeout_seconds is not None:
        mission_overrides["verification_timeout_seconds"] = mission.verification.timeout_seconds
    cli_overrides: dict = {
        "default_adapter": adapter,
        "max_attempts": max_attempts,
        "dry_run": dry_run,
        "allow_dirty": allow_dirty,
        "auto_rollback": auto_rollback,
    }
    try:
        config = resolve_config(pd, mission_overrides=mission_overrides or None,
                                cli_overrides=cli_overrides)
        registry.check_adapter_settings(config.adapters, strict=strict)
    except Exception as e:
        typer.echo(f"Config error: {e}", err=True)
        raise typer.Exit(code=1)

    # Re-apply now that the config (log_level) is resolved; --verbose keeps
    # forcing DEBUG.
    _setup_logging(verbose, config.log_level)

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
    report = orch.run(mission)
    typer.echo(f"\nStatus: {report['status']}")
    typer.echo(f"Session: {report['session_id']}")
    typer.echo(f"Report: {report['audit_dir']}/report.json")
    for step in report["next_steps"]:
        typer.echo(f"Next: {step}")
    if report["status"] == "cancelled":
        typer.echo(
            "Interrupted by user; the adapter was cancelled. Use the "
            "rollback hint above to undo partial changes."
        )
        raise typer.Exit(code=EXIT_CANCELLED)
    if report["status"] != "success":
        if report.get("sandbox_violations"):
            raise typer.Exit(code=EXIT_SANDBOX_VIOLATION)
        raise typer.Exit(code=EXIT_FAILED)


@app.command()
def rollback(
    session_id: str = typer.Argument(..., help="Session id (or prefix)."),
    project_dir: Optional[Path] = typer.Option(None, "--project-dir"),
    clean: bool = typer.Option(
        False, "--clean",
        help="Git projects: also remove untracked files created by the session "
             "(never pre-existing untracked files). Non-git projects: restore "
             "from the session's file backup."),
) -> None:
    """Roll the target project back to a session's checkpoint."""
    pd = _project_dir(project_dir)
    try:
        config = resolve_config(pd)
        audit_dir = config.audit_dir
    except Exception:
        config, audit_dir = TetherConfig(), ".tether/sessions"
    from tether.git_safety import is_git_repo, restore_from_backup
    if not is_git_repo(pd):
        ok, message = restore_from_backup(
            pd, session_id, backup_dir=config.backup_dir, audit_dir=audit_dir)
    else:
        ok, message = git_rollback(pd, session_id, audit_dir=audit_dir, clean=clean)
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
    try:
        session = find_session_dir(pd, config.audit_dir, session_id)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    if session is None:
        typer.echo(f"No session found for id {session_id!r} under {pd / config.audit_dir}", err=True)
        raise typer.Exit(code=1)
    path = session / "report.json"
    if not path.exists():
        typer.echo(f"Session found at {session} but no report.json present.", err=True)
        raise typer.Exit(code=1)
    typer.echo(path.read_text(encoding="utf-8"))


sessions_app = typer.Typer(help="Inspect past sessions.")
app.add_typer(sessions_app, name="sessions")


def _find_session_or_exit(pd: Path, session_id: str) -> Path:
    config = resolve_config(pd)
    try:
        session = find_session_dir(pd, config.audit_dir, session_id)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    if session is None:
        typer.echo(f"No session found for id {session_id!r} under {pd / config.audit_dir}",
                   err=True)
        raise typer.Exit(code=1)
    return session


@sessions_app.command("list")
def sessions_list(
    project_dir: Optional[Path] = typer.Option(None, "--project-dir"),
) -> None:
    """List past sessions (id prefix, mission, status)."""
    pd = _project_dir(project_dir)
    config = resolve_config(pd)
    root = pd / config.audit_dir
    if not root.exists():
        typer.echo("No sessions found.")
        return
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        status, mission_name = "?", "?"
        report_path = d / "report.json"
        if report_path.exists():
            try:
                data = json.loads(report_path.read_text(encoding="utf-8"))
                status = data.get("status", "?")
                mission_name = data.get("mission_name", "?")
            except (OSError, json.JSONDecodeError):
                pass
        typer.echo(f"{d.name:<50} {mission_name:<24} {status}")


@sessions_app.command("show")
def sessions_show(
    session_id: str = typer.Argument(..., help="Session id (or prefix)."),
    project_dir: Optional[Path] = typer.Option(None, "--project-dir"),
) -> None:
    """Show a human-readable summary of a past session."""
    pd = _project_dir(project_dir)
    session = _find_session_or_exit(pd, session_id)
    report_path = session / "report.json"
    if not report_path.exists():
        typer.echo(f"Session found at {session} but no report.json present.", err=True)
        raise typer.Exit(code=1)
    data = json.loads(report_path.read_text(encoding="utf-8"))
    typer.echo(f"Session:  {data.get('session_id')}")
    typer.echo(f"Mission:  {data.get('mission_name')}")
    typer.echo(f"Adapter:  {data.get('adapter')}")
    typer.echo(f"Status:   {data.get('status')}")
    typer.echo(f"Started:  {data.get('started_at')}")
    typer.echo(f"Finished: {data.get('finished_at')}")
    changed = data.get("changed_files") or []
    typer.echo(f"Changed files ({len(changed)}):")
    for f in changed[:20]:
        typer.echo(f"  {f}")
    for step in data.get("next_steps") or []:
        typer.echo(f"Next: {step}")
    typer.echo(f"Audit dir: {data.get('audit_dir')}")


@app.command()
def diff(
    session_id: str = typer.Argument(..., help="Session id (or prefix)."),
    project_dir: Optional[Path] = typer.Option(None, "--project-dir"),
    patch: bool = typer.Option(
        False, "--patch",
        help="Print the saved change artifact instead of the changed-file "
             "list: patch.diff for git sessions, manifest_diff.json for "
             "non-git sessions."),
) -> None:
    """List files changed during a past session."""
    pd = _project_dir(project_dir)
    session = _find_session_or_exit(pd, session_id)
    if patch:
        for name in ("patch.diff", "manifest_diff.json"):
            path = session / name
            if path.exists():
                typer.echo(path.read_bytes())
                return
        typer.echo(
            f"No change artifact (patch.diff or manifest_diff.json) found in "
            f"{session}.", err=True)
        raise typer.Exit(code=1)
    report_path = session / "report.json"
    if not report_path.exists():
        typer.echo(f"Session found at {session} but no report.json present.", err=True)
        raise typer.Exit(code=1)
    data = json.loads(report_path.read_text(encoding="utf-8"))
    for f in data.get("changed_files") or []:
        typer.echo(f)


@app.command()
def logs(
    session_id: str = typer.Argument(..., help="Session id (or prefix)."),
    project_dir: Optional[Path] = typer.Option(None, "--project-dir"),
    verify: bool = typer.Option(
        False, "--verify",
        help="Validate the tamper-evident event hash chain instead of "
             "printing events. Exits 1 naming the first broken event."),
) -> None:
    """Print the event log of a past session."""
    pd = _project_dir(project_dir)
    session = _find_session_or_exit(pd, session_id)
    events = session / "events.jsonl"
    if not events.exists():
        typer.echo(f"No events.jsonl in {session}", err=True)
        raise typer.Exit(code=1)
    if verify:
        from tether.audit import verify_event_chain
        lines = events.read_text(encoding="utf-8").splitlines()
        ok, message = verify_event_chain(lines)
        if not ok:
            typer.echo(f"Event chain BROKEN in {events}: {message}", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"OK: event chain intact ({len(lines)} events)")
        return
    typer.echo(events.read_text(encoding="utf-8"))


@app.callback()
def main(version: bool = typer.Option(False, "--version", callback=lambda v: _version(v))) -> None:
    pass


def _version(show: bool) -> None:
    if show:
        typer.echo(f"tether {__version__}")
        raise typer.Exit()


if __name__ == "__main__":
    app()
