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
tether adapters list                               # availability, capabilities + verified/experimental status
tether adapters conformance mock                    # behavioral conformance battery (PASS/FAIL)
tether run examples/hello-success.yaml --adapter mock --project-dir /some/project
tether report <session-id>                         # print report.json of a past session
tether sessions list                               # list past sessions
tether sessions show <session-id-or-prefix>        # human-readable session summary
tether sessions stats                              # cross-session analytics (--json for machines)
tether sessions clean --older-than 30d             # preview old-session cleanup (nothing deleted)
tether sessions clean --older-than 30d --confirm   # delete session dirs older than 30 days
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

For a deeper probe, `tether adapters conformance <name>` runs a behavioral battery (availability reporting, success/failure/timeout state mapping, cancellation, stdout+stderr log capture, project-directory containment, spawn failure) and prints per-check results with a PASS/FAIL verdict; the exit code is nonzero on FAIL. `mock` passes out of the box and command-family adapters are exercised against deterministic stub executables. An adapter earns `verified` only by passing conformance plus a demonstrated real-CLI run — promotion criteria in docs/ADAPTERS.md.

## Configuration precedence

CLI flags > mission file > project config (`tether.yaml|yml|json|toml`) > defaults.

Mission values that are **unset** (absent) fall back to the project config; only explicit mission values override it. Adapter settings are deep-merged per adapter name: mission adapter settings override project adapter settings key-by-key.

Config keys: `default_adapter`, `audit_dir`, `backup_dir`, `dry_run`, `log_level`, `command_timeout_seconds`, `verification_timeout_seconds`, `max_attempts`, `allow_dirty`, `auto_rollback`, `sandbox_mode`, `retention_days` (used by `sessions clean` when `--older-than` is omitted), `adapters` (per-adapter settings), `verification.commands`.

## Context files (mission contracts)

A mission may declare top-level `context_files`: relative paths that Tether reads at mission start (before planning) and embeds into the prompt context delivered to the adapter, each delimited with headers naming the file:

```yaml
context_files:
  - docs/ARCHITECTURE.md
  - src/tether/models.py
```

Hard limits (constants live in `tether/context_files.py`): **max 32 files**, **max 256 KiB per file**, **max 512 KiB total context**. Violations fail the mission before execution with explicit reasons:

- Paths must be relative and must not escape the project directory: no absolute paths, no `..` components after normalization.
- Files must exist — a missing file is an error, not a warning.
- Binary content is refused: any NUL byte within the first 8 KiB marks a file as binary.

`tether validate-mission` checks structure only (must be a list of strings); existence/size/binary checks run at run time against the target project.

Context files are sent verbatim to the agent (modulo redaction) and appear in audit prompts. When `redact_prompts` is enabled, their content is passed through the same redaction helper as other prompts — both what the adapter receives and what the audit trail stores carry only the redacted form. Each run logs a `context_files` audit event listing which files were included and their byte sizes.

## Verification

Only commands explicitly declared in the mission (or project config) are executed, via `subprocess` with `shell=False`, in the target project directory, with timeouts. Nonzero exit = failure. All output is stored in the session audit directory. If neither the mission nor project config declares commands, none run and verification passes trivially.

## Dry-run

`--dry-run` is fully non-mutating for the target project: no git checkpoint refs are created, no tar backups are made, no adapters are invoked, and no verification commands are executed. Tether still writes its audit report under `.tether/` (its own directory, not target-project content).

## Recovery

On verification failure (or agent failure), Tether sends a concise repair prompt containing the failing output back to the adapter and re-verifies, up to `recovery.max_attempts` (total verification attempts, including the first). It never retries silently forever; every attempt is recorded in the audit trail. Repair prompts embed only a bounded excerpt of the failing output (~8KB budget) extended by a bounded forensic context: the session's current changed files, an excerpt of the latest change artifact (`patch.diff` / `manifest_diff.json`), and the previous attempt's changed files so the agent sees its own delta; the full output is always preserved in the audit records. After every recovery send, change detection, forensic capture, and the write-sandbox gate run again — a recovery attempt that violates `allowed_paths`/`forbidden_paths` fails the mission immediately and skips further verification. Each recovery attempt also records per-attempt evidence in the session directory: the changed files at that attempt (in the report's `recovery_attempts`) and, for git projects, a patch snapshot under `verification/attempt-NN.patch`.

A mission reports `success` **only** if the final agent step completed *and* verification passed. Non-completed agent states (`failed`, `unavailable`, `cancelled`, `needs_input`, `running`) always drive the failure/recovery path, and the orchestrator checks adapter availability itself before starting. A failed planning step aborts the mission before execution.

Repair-prompt outputs are bounded; audit records keep full output. Note that prompts, responses, and logs are stored unredacted — avoid pointing Tether at projects where agents may echo secrets into output, or scrub `.tether/` afterwards.

## Process containment

Adapter commands run via `subprocess` with `shell=False` in their own process group/session: on POSIX the child gets `start_new_session=True`; on Windows it gets `CREATE_NEW_PROCESS_GROUP`. Timeouts and `cancel()` therefore terminate the **whole process tree**, not just the immediate child — termination is graceful first (SIGTERM to the group on POSIX, `taskkill /T` on Windows), then forceful (`SIGKILL` / `taskkill /T /F`) after a short grace period. Stdlib only; no third-party dependencies. Ctrl-C during an adapter call cancels the running command tree through the same path.

## Sandbox modes

Missions can restrict agent writes with the contract's `allowed_paths` / `forbidden_paths` fnmatch globs (relative to the project dir; a forbidden match or — when `allowed_paths` is set — no allowed match is a violation). The config key `sandbox_mode` controls how violations are detected:

- `warn` (default): post-send detection. After every agent send — the initial execution and each recovery attempt — every detected changed file (git diff vs checkpoint HEAD plus untracked files; non-git projects use a before/after manifest) is checked against the globs. Violations fail the mission immediately, skip verification entirely, and point at rollback.
- `enforce`: additionally snapshots the project tree before execution (the same file manifest used for non-git projects) and unions filesystem-metadata diffs into the post-send check. Untracked git files are already checked in both modes; the metadata diff additionally catches writes that content-based detection can miss, e.g. new files under `.gitignore`d paths in git repos.

Be honest about the limit: enforce mode narrows but does **not** eliminate risk. It is best-effort detection layered onto post-hoc analysis — **not OS-level containment** (see docs/SECURITY.md). Use containers, VMs, or separate users when isolating untrusted agents.

## Change capture (forensics)

Right after every agent send — the initial execution and each recovery attempt (before verification can touch anything) — Tether persists a change artifact into the session audit directory:

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

## Review gate

Verification passing is not the same as the change being correct. Missions can opt into a **review gate** (`review: {enabled: true}` in the contract): after every verification command AND artifact assertion passes, Tether opens a fresh session on the mission's adapter and asks it to act as an adversarial reviewer over a bounded excerpt of the captured change (`patch.diff` for git projects, `manifest_diff.json` otherwise), judging the diff against the mission goal. The reviewer must answer with exactly one verdict line (`REVIEW: APPROVE` or `REVIEW: REQUEST_CHANGES`); the verdict is decided by the last such marker in the reviewer's output (command adapters echo the prompt — which mentions both tokens — ahead of the verdict line), and output with no marker fails safe as a rejection. When `required: true` (the default) a rejection fails the mission with the review reason in `next_steps`; there is no automatic re-execution. The verdict, reason, prompt, and response are recorded in `report["review"]`, an audit event, and the session directory.

This is a **heuristic adversarial pass, NOT proof of correctness**. It is also self-review today: the reviewer runs on the same adapter instance as the worker, which is weaker than an independent reviewer adapter (a documented follow-up). `tether validate-mission --strict` additionally lints weak authoring: it warns (or fails under `--strict`) when all verification commands are trivial (`true`, `:`, `echo ...`) and no artifacts are declared, and fails when neither commands nor artifacts are declared at all.

## Development & tests

The `dev` extra installs everything needed to run the test suite and the
lint/type gates:

```bash
.venv/bin/pip install -e ".[dev]"   # pytest>=8, pytest-cov, ruff, mypy, types-PyYAML
.venv/bin/python -m pytest -q       # tests
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src/tether
```

Tests use only temp directories, git temp repos with local identity, and the MockAdapter. No network, no opencode/pi required.

## Current limitations

- No streaming/interactive agent sessions; adapters are one-shot prompt→result.
- Non-git changed-file detection is best-effort: files smaller than 1 MiB (`manifest.HASH_SIZE_LIMIT`) are fingerprinted by sha256 content hash, larger files fall back to size+mtime; there is no content diff. `manifest_diff.json` carries those fingerprints, not file contents.
- Process-tree containment is best-effort: descendants that escape the process group (e.g. by double-forking into a new session on POSIX) or survive `taskkill /T /F` on Windows cannot be force-killed by Tether.
- Token/cost usage is only reported if an adapter provides it (Mock provides none; Command only elapsed time and exit code).
- opencode/pi presets are unverified assumptions; override their command templates in config.
- The review gate is a heuristic single-pass judgment of the captured diff, not proof of correctness; it reviews with the same adapter as the worker (self-review), and a rejected mission is not automatically re-executed — recovery routing after a rejection is future work.
- Ctrl-C during adapter interaction is handled gracefully: the adapter's `cancel()` terminates the running command tree best-effort, the report is finalized with status `cancelled` (CLI exit code 2), and the rollback hint is printed — but a second interrupt or one outside the agent loop still aborts hard.
