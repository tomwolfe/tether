"""Tether CLI."""
from __future__ import annotations

import json
import logging
import re
import shutil
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Optional

import typer

import tether.adapters as registry
from tether import __version__
from tether import certify, conformance, smoke
from tether.adapters.base import AgentAdapter
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


# Verification-strength lint (dogfood-15): authoring-time guidance only.
# A TRIVIAL command proves nothing; missions should not pass on these alone.
_TRIVIAL_COMMANDS = {"true", ":"}


def _is_trivial_command(command: str) -> bool:
    stripped = command.strip()
    return stripped in _TRIVIAL_COMMANDS or stripped.startswith("echo ")


@app.command()
def validate_mission(
    mission_file: Path,
    strict: bool = typer.Option(
        False, "--strict",
        help="Fail on weak verification: all-trivial commands without "
             "artifacts, or neither commands nor artifacts declared."),
) -> None:
    """Load and validate a mission contract file."""
    try:
        mission = load_mission(mission_file)
    except MissionError as e:
        typer.echo(f"INVALID: {e}", err=True)
        raise typer.Exit(code=1)
    # Authoring-time guidance only: runtime verification behavior unchanged.
    commands = mission.verification.commands or []
    artifacts = mission.verification.artifacts or []
    if not commands and not artifacts:
        if strict:
            typer.echo("INVALID: no verification commands and no artifact "
                       "patterns declared; verification would pass "
                       "trivially (--strict)", err=True)
            raise typer.Exit(code=1)
    elif all(_is_trivial_command(c) for c in commands) and not artifacts:
        message = ("all verification commands are trivial (true/:/echo*) "
                   "and no artifact patterns are declared")
        if strict:
            typer.echo(f"INVALID: {message} (--strict)", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"WARNING: {message}")
    typer.echo(f"OK: mission {mission.name!r} is valid "
               f"({len(commands)} verification "
               f"command(s), {len(artifacts)} "
               f"artifact pattern(s))")


@adapters_app.command("list")
def adapters_list() -> None:
    """List known adapters with availability, capabilities and maturity."""
    cfg = TetherConfig()
    rows = []
    for name in registry.adapter_names():
        try:
            adapter = registry.resolve_adapter(name, {}, default_timeout=cfg.command_timeout_seconds)
            ok, reason = adapter.is_available()
            status = "available" if ok else "unavailable"
            tag = "verified" if adapter.verified else "experimental"
            caps = conformance.capability_flags(adapter)
            issue = reason or "-"
        except Exception as e:
            status, tag, caps, issue = "error", "unknown", "-", str(e)
        rows.append((name, status, tag, caps, issue))
    typer.echo(f"{'NAME':<12} {'STATUS':<12} {'MATURITY':<13} {'CAPABILITIES':<28} ISSUE")
    for name, status, tag, caps, issue in rows:
        typer.echo(f"{name:<12} {status:<12} {tag:<13} {caps:<28} {issue}")


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


@adapters_app.command("conformance")
def adapters_conformance(
    name: str = typer.Argument(..., help="Adapter name (see `tether adapters list`)."),
    project_dir: Optional[Path] = typer.Option(
        None, "--project-dir",
        help="Project whose tether.yaml configures the adapter."),
) -> None:
    """Run the behavioral conformance battery; exit nonzero on FAIL."""
    pd = _project_dir(project_dir)
    try:
        adapter_instance = smoke.build_smoke_adapter(name, pd)
        report = conformance.run_conformance(adapter_instance)
    except Exception as e:
        typer.echo(f"Conformance ERROR: {e}", err=True)
        raise typer.Exit(code=1)
    typer.echo(report.summary())
    if not report.ok:
        raise typer.Exit(code=1)


@adapters_app.command("certify")
def adapters_certify(
    name: str = typer.Argument(..., help="Adapter name (see `tether adapters list`)."),
    project_dir: Optional[Path] = typer.Option(
        None, "--project-dir",
        help="Project whose tether.yaml configures the adapter."),
    json_output: bool = typer.Option(
        False, "--json",
        help="Also print the full certificate JSON to stdout."),
) -> None:
    """Certify an adapter: availability, conformance, then a live probe.

    The live probe sends a trivial prompt through the adapter's REAL
    configured command inside a throwaway directory; stub-driven conformance
    alone never certifies. Exit nonzero when any stage fails.
    """
    pd = _project_dir(project_dir)
    try:
        adapter_instance = smoke.build_smoke_adapter(name, pd)
    except Exception as e:
        typer.echo(f"Certify FAILED: {e}", err=True)
        raise typer.Exit(code=1)
    result = certify.run_certify(adapter_instance, name)

    # Auditable artifact: written for every run (pass or fail) before any
    # nonzero exit so failures are recorded too.
    cert = certify.to_dict(result)
    cert_dir = pd / ".tether" / "certificates"
    cert_dir.mkdir(parents=True, exist_ok=True)
    filename_stamp = str(cert["utc_timestamp"]).replace("-", "").replace(":", "")
    cert_path = cert_dir / f"{name}-{filename_stamp}.json"
    cert_path.write_text(json.dumps(cert, indent=2) + "\n", encoding="utf-8")

    typer.echo(f"{'Adapter:':<14}{name}")
    availability = ("available" if result.available
                    else f"unavailable ({result.availability_reason})")
    if result.failed_stage == certify.STAGE_AVAILABILITY:
        typer.echo(f"{'Availability:':<14}{availability}")
    elif result.availability_note:
        typer.echo(f"{'Availability:':<14}{availability} -- {result.availability_note}")
    else:
        typer.echo(f"{'Availability:':<14}{availability}")

    if result.conformance is not None:
        for line in result.conformance.summary().splitlines():
            typer.echo(f"  {line}")
    else:
        typer.echo("  Conformance: skipped (availability failed)")

    probe = result.live_probe
    if probe is not None:
        exit_label = "-" if probe.exit_code is None else str(probe.exit_code)
        status_line = (
            f"status={probe.status or '-'} exit_code={exit_label} "
            f"elapsed={probe.elapsed_seconds:.2f}s"
        )
        typer.echo(f"  Live probe [{'PASS' if probe.ok else 'FAIL'}] ({status_line}):")
        for line in (probe.excerpt or "(no output)").splitlines() or ["(no output)"]:
            typer.echo(f"    {line}")
    else:
        typer.echo("  Live probe: skipped (previous stage failed)")

    typer.echo(result.verdict_line)
    typer.echo(f"Certificate: {cert_path}")
    if json_output:
        typer.echo(json.dumps(cert, indent=2))
    if not result.ok:
        raise typer.Exit(code=1)


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

    # Independent reviewer (dogfood-17): when the contract enables review AND
    # names a review.adapter, resolve it via the same registry/adapters config
    # and fail fast here — an unavailable reviewer aborts before any agent
    # runs. Review disabled or adapter unset keeps today's self-review path.
    reviewer_instance: Optional[AgentAdapter] = None
    if (mission.review is not None and mission.review.enabled
            and mission.review.adapter):
        try:
            reviewer_instance = registry.resolve_adapter(
                mission.review.adapter, config.adapters,
                default_timeout=config.command_timeout_seconds,
            )
        except ValueError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(code=1)
        rok, rreason = reviewer_instance.is_available()
        if not rok and not config.dry_run:
            typer.echo(f"Reviewer adapter {mission.review.adapter!r} "
                       f"unavailable: {rreason}", err=True)
            raise typer.Exit(code=1)

    orch = Orchestrator(adapter_instance, config, pd,
                        session_id=new_session_id(), reviewer=reviewer_instance)
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
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Print the rollback plan (checkpoint/backup target, dirty state, "
             "files that would be reset or deleted) without touching anything."),
) -> None:
    """Roll the target project back to a session's checkpoint."""
    pd = _project_dir(project_dir)
    try:
        config = resolve_config(pd)
        audit_dir = config.audit_dir
    except Exception:
        config, audit_dir = TetherConfig(), ".tether/sessions"
    from tether.git_safety import is_git_repo, restore_from_backup
    if dry_run:
        from tether.git_safety import plan_rollback
        ok, message = plan_rollback(pd, session_id, audit_dir=audit_dir,
                                    backup_dir=config.backup_dir, clean=clean)
    elif not is_git_repo(pd):
        ok, message = restore_from_backup(
            pd, session_id, backup_dir=config.backup_dir, audit_dir=audit_dir)
    else:
        ok, message = git_rollback(pd, session_id, audit_dir=audit_dir, clean=clean)
    typer.echo(message)
    if not ok:
        raise typer.Exit(code=1)


@app.command()
def doctor(
    project_dir: Optional[Path] = typer.Option(None, "--project-dir",
                                               help="Target project directory."),
) -> None:
    """Diagnose the environment and target project; print a per-check report.

    Exits nonzero only when a *critical* check fails (git missing, audit or
    backup directories not writable). Advisory findings (dirty tree, stale
    locks, unavailable adapters) are reported but do not fail the command.
    """
    import shutil as _shutil
    import subprocess as _subprocess
    import sys

    from tether.config import find_project_config, load_project_config
    from tether.git_safety import is_dirty, is_git_repo
    from tether.orchestrator import fresh_lock_holder, writer_lock_path

    pd = _project_dir(project_dir)
    critical_failures = 0
    advisory_warnings = 0

    def ok(detail: str) -> None:
        typer.echo(f"[PASS] {detail}")

    def warn(detail: str) -> None:
        nonlocal advisory_warnings
        advisory_warnings += 1
        typer.echo(f"[WARN] {detail}")

    def fail(detail: str) -> None:
        nonlocal critical_failures
        critical_failures += 1
        typer.echo(f"[FAIL] {detail}")

    typer.echo(f"Tether doctor — {pd}")

    # Python version (advisory: the interpreter already runs this command).
    minimum = (3, 11)
    if sys.version_info[:2] >= minimum:
        ok(f"python {sys.version.split()[0]} "
           f"(minimum required: {minimum[0]}.{minimum[1]})")
    else:
        warn(f"python {sys.version.split()[0]} is below the minimum required "
             f"{minimum[0]}.{minimum[1]}; Tether requires "
             f"{minimum[0]}.{minimum[1]}+")

    # git availability (critical).
    if _shutil.which("git") is None:
        fail("git not found on PATH; checkpoints and rollback are unavailable")
    else:
        try:
            proc = _subprocess.run(["git", "--version"], capture_output=True,
                                   text=True, check=False)
            version = proc.stdout.strip() or "git"
        except OSError:
            version = "git"
        ok(f"git available: {version}")

    # Project tether.yaml validity, if present (advisory).
    config_file = find_project_config(pd)
    cfg = TetherConfig()
    if config_file is None:
        ok("no tether config file found (defaults apply)")
    else:
        try:
            load_project_config(pd)
            cfg = resolve_config(pd)
            ok(f"tether config {config_file.name} valid")
        except Exception as e:
            warn(f"tether config {config_file.name} INVALID: {e}")

    # Adapter availability for every registered adapter (advisory: optional
    # binaries such as opencode/pi may legitimately be absent).
    for name in registry.adapter_names():
        try:
            adapter_instance = registry.resolve_adapter(
                name, cfg.adapters,
                default_timeout=cfg.command_timeout_seconds,
            )
            available, reason = adapter_instance.is_available()
            if available:
                ok(f"adapter '{name}' available")
            else:
                warn(f"adapter '{name}' unavailable ({reason})")
        except Exception as e:
            warn(f"adapter '{name}' error: {e}")

    def probe_writable(path: Path) -> Optional[str]:
        """None when the directory can be created and written, else why not."""
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return f"cannot create ({e})"
        probe = path / ".tether-doctor-probe"
        try:
            probe.write_text("probe\n", encoding="utf-8")
        except OSError as e:
            return f"not writable ({e})"
        try:
            probe.unlink()
        except OSError:
            pass
        return None

    # Audit + backup directory writability (critical).
    for label, relpath in (("audit dir", cfg.audit_dir),
                           ("backup dir", cfg.backup_dir)):
        error = probe_writable(pd / relpath)
        if error is None:
            ok(f"{label} {pd / relpath} writable")
        else:
            fail(f"{label} {pd / relpath}: {error}")

    # Writer lock leftovers (advisory).
    lock_path = writer_lock_path(pd)
    holder = fresh_lock_holder(lock_path,
                               stale_seconds=cfg.writer_lock_stale_seconds)
    if holder is not None:
        warn(f"writer lock at {lock_path} held by session {holder!r}; another "
             "Tether run may be active")
    elif lock_path.exists():
        warn(f"stale writer lock at {lock_path} (owning process is gone); "
             "it is safe to remove")
    else:
        ok("no writer lock present")

    # Dirty-tree status of the target project (advisory).
    if is_git_repo(pd):
        if is_dirty(pd):
            warn("working tree is DIRTY; missions refuse to run without "
                 "--allow-dirty")
        else:
            ok("working tree clean")
    else:
        warn("target project is not a git repository; rollback falls back "
             "to tar backups")

    typer.echo("")
    if critical_failures:
        typer.echo(f"Verdict: FAILED ({critical_failures} critical problem"
                   f"{'s' if critical_failures != 1 else ''}, "
                   f"{advisory_warnings} warning(s))")
        raise typer.Exit(code=1)
    verdict = "OK" if advisory_warnings == 0 else \
        f"OK ({advisory_warnings} advisory warning(s))"
    typer.echo(f"Verdict: {verdict}")


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


sessions_app = typer.Typer(help="Inspect and manage past sessions.")
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
    usage = data.get("usage")
    if usage:
        typer.echo(f"Usage:    {json.dumps(usage, sort_keys=True)}")
    changed = data.get("changed_files") or []
    typer.echo(f"Changed files ({len(changed)}):")
    for f in changed[:20]:
        typer.echo(f"  {f}")
    for step in data.get("next_steps") or []:
        typer.echo(f"Next: {step}")
    typer.echo(f"Audit dir: {data.get('audit_dir')}")


def _iter_session_reports(pd: Path, audit_dir: str) -> list[tuple[Path, dict]]:
    """(session_dir, report_dict) for every session with a readable report."""
    root = pd / audit_dir
    entries: list[tuple[Path, dict]] = []
    if not root.exists():
        return entries
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        path = d / "report.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            entries.append((d, data))
    return entries


@sessions_app.command("stats")
def sessions_stats(
    project_dir: Optional[Path] = typer.Option(None, "--project-dir"),
    json_output: bool = typer.Option(
        False, "--json", help="Emit a single JSON object to stdout."),
) -> None:
    """Cross-session analytics: statuses, attempts, recovery, failures."""
    pd = _project_dir(project_dir)
    config = resolve_config(pd)
    entries = _iter_session_reports(pd, config.audit_dir)
    total = len(entries)
    canonical = ("success", "failed", "cancelled")

    def pct(part: int) -> float:
        return round(100.0 * part / total, 1) if total else 0.0

    status_counts: Dict[str, int] = {s: 0 for s in canonical}
    adapter_counts: Dict[str, int] = {}
    adapter_successes: Dict[str, int] = {}
    attempt_counts: list[int] = []
    recovery_sessions = recovery_successes = 0
    failing_commands: Counter[str] = Counter()
    for d, data in entries:
        status = str(data.get("status", "?"))
        status_counts[status] = status_counts.get(status, 0) + 1
        adapter = str(data.get("adapter", "?"))
        adapter_counts[adapter] = adapter_counts.get(adapter, 0) + 1
        if status == "success":
            adapter_successes[adapter] = adapter_successes.get(adapter, 0) + 1
        attempt_counts.append(
            len(list((d / "verification").glob("attempt-*.json"))))
        if len(data.get("recovery_attempts") or []) > 0:
            recovery_sessions += 1
            if status == "success":
                recovery_successes += 1
        for r in data.get("verification_results") or []:
            if (isinstance(r, dict) and r.get("passed") is False
                    and r.get("command")):
                failing_commands[str(r["command"])] += 1

    top_failing = [{"command": cmd, "count": n} for cmd, n in
                   sorted(failing_commands.items(),
                          key=lambda kv: (-kv[1], kv[0]))[:5]]

    def pct_round(part: int, whole: int) -> float:
        return round(100.0 * part / whole, 1) if whole else 0.0

    adapters_stats = {
        name: {
            "count": adapter_counts[name],
            "success_rate_pct": pct_round(adapter_successes.get(name, 0),
                                          adapter_counts[name]),
        }
        for name in sorted(adapter_counts)
    }

    stats: Dict[str, Any] = {
        "total_sessions": total,
        "statuses": {s: {"count": c, "pct": pct(c)}
                     for s, c in sorted(status_counts.items())},
        "attempts": {
            "median": round(float(statistics.median(attempt_counts)), 2)
            if attempt_counts else 0.0,
            "max": max(attempt_counts) if attempt_counts else 0,
        },
        "recovery": {
            "sessions_with_recovery_attempts": recovery_sessions,
            "recoveries_ending_in_success": recovery_successes,
            "success_rate_pct": pct_round(recovery_successes, recovery_sessions),
        },
        "top_failing_commands": top_failing,
        "adapters": adapters_stats,
    }

    if json_output:
        typer.echo(json.dumps(stats, indent=2))
        return
    if total == 0:
        typer.echo("No sessions found.")
        return
    typer.echo(f"Sessions: {total} total under {pd / config.audit_dir}")
    for s in canonical:
        c = status_counts[s]
        typer.echo(f"  {s}: {c} ({pct(c)}%)")
    other = total - sum(status_counts[s] for s in canonical)
    if other:
        typer.echo(f"  other: {other} ({pct(other)}%)")
    typer.echo(f"Verification attempts: median "
               f"{stats['attempts']['median']}, max {stats['attempts']['max']}")
    typer.echo(f"Recovery success rate: {stats['recovery']['success_rate_pct']}% "
               f"({recovery_successes}/{recovery_sessions} with recovery attempts)")
    if top_failing:
        typer.echo("Most common failing verification commands:")
        for item in top_failing:
            typer.echo(f"  {item['count']}x {item['command']}")
    typer.echo("Per-adapter:")
    for name, info in adapters_stats.items():
        typer.echo(f"  {name}: {info['count']} session(s), "
                   f"success rate {info['success_rate_pct']}%")


_DURATION_RE = re.compile(r"^(\d+)([mhd])$")
_DURATION_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def _parse_older_than(value: str) -> float:
    """Parse '<N>m'/'<N>h'/'<N>d' into seconds; reject anything else."""
    m = _DURATION_RE.match(value.strip())
    if not m:
        raise ValueError(
            f"invalid duration {value!r}; use e.g. '30m', '24h', '30d'")
    return int(m.group(1)) * _DURATION_UNIT_SECONDS[m.group(2)]


@sessions_app.command("clean")
def sessions_clean(
    older_than: Optional[str] = typer.Option(
        None, "--older-than",
        help="Delete session directories older than this (e.g. 30m, 24h, 30d). "
             "Falls back to the configured retention_days when omitted."),
    confirm: bool = typer.Option(
        False, "--confirm",
        help="Actually delete. Without this flag nothing is removed."),
    project_dir: Optional[Path] = typer.Option(None, "--project-dir"),
) -> None:
    """Delete old session directories (dry-run unless --confirm)."""
    pd = _project_dir(project_dir)
    config = resolve_config(pd)
    try:
        if older_than is not None:
            threshold_seconds = _parse_older_than(older_than)
        elif config.retention_days is not None:
            threshold_seconds = config.retention_days * 86400
        else:
            raise ValueError(
                "no --older-than given and retention_days is not configured")
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    root = pd / config.audit_dir
    cutoff = time.time() - threshold_seconds
    candidates = [d for d in sorted(root.iterdir())
                  if d.is_dir() and d.stat().st_mtime < cutoff] \
        if root.exists() else []
    if not candidates:
        typer.echo("No session directories older than the threshold.")
        return
    verb = "Deleting" if confirm else "Would delete"
    for d in candidates:
        typer.echo(f"{verb}: {d}")
    if not confirm:
        typer.echo("Dry run: nothing deleted (pass --confirm to delete).")
        return
    for d in candidates:
        shutil.rmtree(d, ignore_errors=True)
    typer.echo(f"Deleted {len(candidates)} session "
               f"{'directory' if len(candidates) == 1 else 'directories'}")


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
