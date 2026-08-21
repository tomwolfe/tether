# Tether

Tether is a local CLI orchestration layer that makes autonomous coding agents more reliable. It wraps any coding agent (opencode, pi, aider, claude-code, or an arbitrary CLI) in a structured control loop:

**Mission Contract → Planning → Execution → Verification → Recovery → Rollback/Audit**

The core loop is **agent-agnostic**: all agent-specific behavior lives behind a single `AgentAdapter` interface. Tether never depends on a specific vendor CLI to function — the built-in `MockAdapter` is fully deterministic and works offline.

## Install & run

Requires Python 3.11+ and git (for checkpoint/rollback).

```bash
cd tether
python3 -m venv .venv && .venv/bin/pip install -e .
# entrypoint:
.venv/bin/tether --help
# or without install:
.venv/bin/python -m tether --help
```

## Quick tour

```bash
tether init                                        # write a starter tether.yaml
tether validate-config                             # validate project config
tether validate-mission examples/hello-success.yaml
tether adapters list                               # availability + verified/experimental status
tether run examples/hello-success.yaml --adapter mock --project-dir /some/project
tether report <session-id>                         # print report.json of a past session
tether rollback <session-id>                       # restore the git checkpoint
```

Useful `run` flags: `--adapter`, `--project-dir`, `--dry-run`, `--max-attempts`, `--allow-dirty`, `--verbose`.

The target project defaults to the current directory and is overridden with `--project-dir`; it does not have to be the Tether repo itself.

## Mock example

```bash
tether run examples/hello-success.yaml   # mock succeeds immediately
tether run examples/hello-recovery.yaml  # mock fails once, repair prompt, then succeeds
```

## Configuring a real adapter

Everything real goes through the generic `CommandAdapter` (or its thin presets). Define it in `tether.yaml`:

```yaml
default_adapter: myagent
adapters:
  myagent:
    command: ["myagent", "--non-interactive", "--prompt", "{prompt}"]
    # prompt_via_stdin: true        # pipe prompt to stdin instead of {prompt}
    # env: {MYAGENT_KEY: "..."}
    timeout_seconds: 1800
```

Placeholders: `{prompt}`, `{project_dir}`, `{session_id}`.

`opencode` and `pi` presets exist but are **experimental/unverified** — see docs/ADAPTERS.md.

## Configuration precedence

CLI flags > mission file > project config (`tether.yaml|yml|json|toml`) > defaults.

Config keys: `default_adapter`, `audit_dir`, `dry_run`, `log_level`, `command_timeout_seconds`, `verification_timeout_seconds`, `max_attempts`, `allow_dirty`, `adapters` (per-adapter settings), `verification.commands`.

## Verification

Only commands explicitly declared in the mission (or project config) are executed, via `subprocess` with `shell=False`, in the target project directory, with timeouts. Nonzero exit = failure. All output is stored in the session audit directory. `--dry-run` prints instead of executing.

## Recovery

On verification failure (or agent failure), Tether sends a concise repair prompt containing the failing output back to the adapter and re-verifies, up to `recovery.max_attempts`. It never retries silently forever; every attempt is recorded in the audit trail.

## Rollback / git safety

- If the target is a git repo, Tether records HEAD and creates `refs/tether/checkpoint/<session-id>` before running.
- A dirty working tree aborts unless `--allow-dirty` is passed (uncommitted changes still cannot be restored).
- Rollback is refused when the tree is dirty; exact manual steps are printed instead.
- Tether never force-pushes, deletes branches, or rewrites history.
- Non-git projects get a tar backup under `.tether/backups/` plus a clear warning.
- Tether's own `.tether/` files never count as "dirty".

## Audit trail

Each run creates `.tether/sessions/<timestamp>-<mission>-<short-id>/` containing: resolved config, mission contract, prompts sent, adapter responses, verification results per attempt, recovery attempts, changed files, checkpoint info, `events.jsonl`, and a machine-readable `report.json`.

## Tests

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

Tests use only temp directories, git temp repos with local identity, and the MockAdapter. No network, no opencode/pi required.

## Current limitations

- No streaming/interactive agent sessions; adapters are one-shot prompt→result.
- Changed-file detection requires the target to be a git repo.
- Token/cost usage is only reported if an adapter provides it (Mock/Command do not).
- opencode/pi presets are unverified assumptions; override their command templates in config.
- Windows is untested.
