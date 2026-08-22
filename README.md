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
tether sessions list                               # list past sessions
tether sessions show <session-id-or-prefix>        # human-readable session summary
tether diff <session-id>                           # files changed during a session
tether diff <session-id> --patch                   # saved patch.diff / manifest_diff.json
tether logs <session-id>                           # event log of a session
tether rollback <session-id-or-prefix>             # restore the git checkpoint
```

Useful `run` flags: `--adapter`, `--project-dir`, `--dry-run/--no-dry-run`, `--max-attempts`, `--allow-dirty/--no-allow-dirty`, `--auto-rollback/--no-auto-rollback`, `--strict`, `--verbose`. The boolean flags are tri-state: when omitted they do not override project config; when given they always do.

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

### Unknown adapter settings

Each adapter declares the settings keys it knows (e.g. `command`, `timeout_seconds`, `prompt_via_stdin`, `env` for the generic command adapter; `scenario` for mock). Configured keys outside that set are reported as warnings when the adapter is built, e.g. `adapter 'myagent': unknown setting 'promt_via_stdin'` — handy for typos. Pass `--strict` to `tether run` or `tether validate-config` to turn those warnings into errors that fail validation.

### Smoke-testing an adapter

Before wiring an adapter into a real mission, verify it end-to-end without touching your project:

```bash
tether adapters smoke mock                          # any registered name works
tether adapters smoke command                       # the adapter configured in tether.yaml above
tether adapters smoke opencode --prompt "Say hi"
```

`smoke` builds the named adapter from project config, reports whether it is available, then sends a trivial prompt (default: `Reply with the single word OK`) **inside a temporary directory** — your git tree and files are never touched and no audit sessions are created. It prints availability, status, exit code, an output excerpt, and elapsed time; the exit code is 0 only if the adapter was available and its run completed successfully.

`opencode` and `pi` presets exist but are **experimental/unverified** — see docs/ADAPTERS.md.

## Configuration precedence

CLI flags > mission file > project config (`tether.yaml|yml|json|toml`) > defaults.

Mission values that are **unset** (absent) fall back to the project config; only explicit mission values override it. Adapter settings are deep-merged per adapter name: mission adapter settings override project adapter settings key-by-key.

Config keys: `default_adapter`, `audit_dir`, `backup_dir`, `dry_run`, `log_level`, `command_timeout_seconds`, `verification_timeout_seconds`, `max_attempts`, `allow_dirty`, `auto_rollback`, `adapters` (per-adapter settings), `verification.commands`.

## Verification

Only commands explicitly declared in the mission (or project config) are executed, via `subprocess` with `shell=False`, in the target project directory, with timeouts. Nonzero exit = failure. All output is stored in the session audit directory. If neither the mission nor project config declares commands, none run and verification passes trivially.

## Dry-run

`--dry-run` is fully non-mutating for the target project: no git checkpoint refs are created, no tar backups are made, no adapters are invoked, and no verification commands are executed. Tether still writes its audit report under `.tether/` (its own directory, not target-project content).

## Recovery

On verification failure (or agent failure), Tether sends a concise repair prompt containing the failing output back to the adapter and re-verifies, up to `recovery.max_attempts` (total verification attempts, including the first). It never retries silently forever; every attempt is recorded in the audit trail. Repair prompts embed only a bounded excerpt of the failing output (~8KB budget); the full output is always preserved in the audit records.

A mission reports `success` **only** if the final agent step completed *and* verification passed. Non-completed agent states (`failed`, `unavailable`, `cancelled`, `needs_input`, `running`) always drive the failure/recovery path, and the orchestrator checks adapter availability itself before starting. A failed planning step aborts the mission before execution.

Repair-prompt outputs are bounded; audit records keep full output. Note that prompts, responses, and logs are stored unredacted — avoid pointing Tether at projects where agents may echo secrets into output, or scrub `.tether/` afterwards.

## Process containment

Adapter commands run via `subprocess` with `shell=False` in their own process group/session: on POSIX the child gets `start_new_session=True`; on Windows it gets `CREATE_NEW_PROCESS_GROUP`. Timeouts and `cancel()` therefore terminate the **whole process tree**, not just the immediate child — termination is graceful first (SIGTERM to the group on POSIX, `taskkill /T` on Windows), then forceful (`SIGKILL` / `taskkill /T /F`) after a short grace period. Stdlib only; no third-party dependencies. Ctrl-C during an adapter call cancels the running command tree through the same path.

## Change capture (forensics)

Right after execution (before verification can touch anything), Tether persists a change artifact into the session audit directory:

- Git projects: `patch.diff` (`git diff --no-color --binary <original_head>`, so binary changes are included) plus `untracked.txt`, because a plain diff does not include untracked file contents.
- Non-git projects: `manifest_diff.json` with added/modified/deleted files and the before/after fingerprints used by the manifest.

`tether diff <session-id> --patch` prints the saved artifact; plain `tether diff <session-id>` keeps listing changed files.

## Automatic rollback (opt-in)

With `--auto-rollback` (or `auto_rollback: true` in config), a mission that ends `failed` or `cancelled` is rolled back automatically right after the initial report is written: git projects get the scoped clean rollback (reset to checkpoint + removal of session-attributable untracked files), non-git projects are restored from their tar backup. The outcome is recorded in `report.json` under `auto_rollback` (`attempted`, `ok`, `message`). Successful runs and dry-runs are never rolled back, pre-existing untracked files that are not attributable to the session are never removed, and if the automatic rollback fails the original status and manual rollback guidance are preserved.

## Rollback / git safety

- If the target is a git repo, Tether records HEAD and creates `refs/tether/checkpoint/<session-id>` before running.
- A dirty working tree **aborts the mission before the adapter is invoked** unless `--allow-dirty` (or `allow_dirty: true` in config) is set; the report tells you to commit/stash or pass `--allow-dirty`, and the CLI exits nonzero.
- `tether rollback <session-id-or-prefix>` accepts unique prefixes. Resolution order: exact session id → audit session directory (`report.json`) → checkpoint ref prefix match. Ambiguous prefixes fail with a list of matches.
- Rollback is refused when the tree is dirty; exact manual steps are printed, including the specific untracked files found.
- `tether rollback <session-id-or-prefix> --clean` performs a scoped restore for git projects: `git reset --hard` to the checkpoint plus removal of untracked files **attributable to the session** (those listed in the session report's `changed_files`). Pre-existing untracked user files are never removed, and a blanket `git clean` is never run.
- For **non-git projects**, `tether rollback <session-id-or-prefix>` restores file contents from the session's tar backup. Files created after the backup are kept and listed for manual cleanup.
- Tether never force-pushes, deletes branches, or rewrites history.
- Non-git projects get a tar backup under `backup_dir` (default `.tether/backups/`) plus a clear warning; if backup creation fails, the mission fails rather than proceeding unprotected. For non-git projects a lightweight file manifest provides best-effort added/modified/deleted visibility in the report.
- Tether's own `.tether/` files never count as "dirty". Tip: add `.tether/` to your project's `.gitignore`.

## Audit trail

Each run creates `.tether/sessions/<timestamp>-<mission>-<short-id>/` containing: resolved config (with secrets such as adapter `env` values redacted), mission contract, prompts sent, adapter responses, verification results per attempt, recovery attempts, changed files, checkpoint info, `events.jsonl`, and a machine-readable `report.json`.

## Tests

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

Tests use only temp directories, git temp repos with local identity, and the MockAdapter. No network, no opencode/pi required.

## Current limitations

- No streaming/interactive agent sessions; adapters are one-shot prompt→result.
- Non-git changed-file detection is best-effort (size/mtime manifest; no content diff). `manifest_diff.json` captures the same fingerprints, not file contents.
- Process-tree containment is best-effort: descendants that escape the process group (e.g. by double-forking into a new session on POSIX) or survive `taskkill /T /F` on Windows cannot be force-killed by Tether.
- Token/cost usage is only reported if an adapter provides it (Mock/Command do not).
- opencode/pi presets are unverified assumptions; override their command templates in config.
- Ctrl-C during adapter interaction is handled gracefully: the adapter's `cancel()` terminates the running command tree best-effort, the report is finalized with status `cancelled` (CLI exit code 2), and the rollback hint is printed — but a second interrupt or one outside the agent loop still aborts hard.
