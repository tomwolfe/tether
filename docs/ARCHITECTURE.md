# Architecture

## Module map

```
src/tether/
  models.py        Pydantic models: MissionContract, TetherConfig, AgentState,
                   VerificationResult, CheckpointInfo
  mission.py       Mission loading/validation (YAML/JSON) -> MissionContract
  config.py        Config layering: CLI > mission > project tether.* > defaults
  adapters/
    base.py        AgentAdapter ABC + SessionInfo (the only thing core knows)
    mock.py        Deterministic MockAdapter (success / fail_then_succeed / always_fail)
    command.py     Generic CommandAdapter (configurable argv template, placeholders).
                   Children are spawned detached in their own process group
                   (POSIX start_new_session / Windows CREATE_NEW_PROCESS_GROUP)
                   so timeout and cancel() terminate the whole process tree:
                   graceful first (SIGTERM to the group / taskkill /T), then
                   force kill (SIGKILL / taskkill /T /F) after a short grace
                   period. The active subprocess is tracked per session so
                   cancel(session) can reap in-flight work.
    experimental.py OpencodeAdapter, PiAdapter (thin presets over CommandAdapter)
    __init__.py    Registry: resolve_adapter(name, settings)
  verification.py  Runs declared commands safely (shell=False, timeouts, cwd)
  git_safety.py    Checkpoint refs, dirty detection, prefix-resolving rollback,
                   changed files, backups
  manifest.py      Best-effort file manifests for non-git change visibility
  audit.py         Session directories, events.jsonl, report.json, secret redaction
  orchestrator.py  The core loop (adapter-agnostic): change capture, auto rollback
  cli.py           Typer CLI
```

## Core loop (orchestrator.py)

1. Load + validate mission contract.
2. Resolve adapter via registry using config settings.
3. Create session id and audit directory; save resolved config with secrets redacted.
4. Adapter availability check (non-dry-run): an unavailable adapter fails fast with a clear next step.
5. Git checkpoint (record HEAD, create `refs/tether/checkpoint/<session-id>`). Dry-run records intent but writes no ref and takes no backup. A dirty tree aborts the mission before any adapter call unless `allow_dirty` is set (mission/config/CLI precedence applies). Non-git projects get a tar backup under `backup_dir`; backup failure fails the mission.
6. Planning prompt -> adapter; response stored. A non-completed planning status aborts the mission before execution.
7. Execution prompt -> adapter; agent state stored.
8. Changed-file detection (`git diff` vs checkpoint HEAD + untracked; non-git projects use a before/after file manifest).
9. Forensic change capture into the session audit directory (skipped in dry-run): git projects get `patch.diff` (`git diff --no-color --binary <original_head>`, includes binary changes) plus `untracked.txt` (a plain diff misses untracked contents); non-git projects get `manifest_diff.json` (added/modified/deleted plus the before/after fingerprints). Captured before verification so the evidence reflects the agent's changes.
10. Verification of declared commands (skipped execution in dry-run).
11. Pass AND agent completed => success. Any non-completed agent state (failed/unavailable/cancelled/needs_input/running) counts as failure. Fail => recovery loop: repair prompt with a bounded (~8KB) excerpt of failing output, re-verify, up to effective `max_attempts`.
12. Final `report.json` + rollback guidance on failure. Opt-in auto rollback: when `auto_rollback` is enabled and only the status is `failed` or `cancelled` (never success, never dry-run), a scoped rollback runs right after the initial report write — clean scoped restore for git (using the report's changed_files, never touching pre-session untracked files passed via `preserve`) or backup restore for non-git — and `report.json` is rewritten with an `auto_rollback` result (`attempted`, `ok`, `message`). Failure keeps the original status and manual guidance.
13. A `KeyboardInterrupt` (Ctrl-C) during adapter interaction is handled gracefully: `adapter.cancel(session)` is invoked best-effort (for CommandAdapter this terminates the running process tree), a `cancelled` event is appended to `events.jsonl`, the audit trail is finalized with a `report.json` of status `cancelled`, and `next_steps` carries rollback guidance (`tether rollback <session-id>`).

Effective values follow strict precedence: mission explicit value > project config > built-in default, applied independently for `recovery.max_attempts`, `verification.commands`, and `verification.timeout_seconds`. Mission models keep these fields Optional so "unset" is distinguishable from an explicit value.

The loop never references a concrete adapter type; it only calls the `AgentAdapter` interface (`is_available`, `start_session`, `send`, `cancel`, plus prompt builders). Adding an agent = adding a registry entry or a config block.

## Design rules

- Verification commands must be explicitly declared; nothing arbitrary is executed.
- Failures are never faked: dry-run marks results as skipped, not passed-by-execution.
- Dry-run never mutates the target project: no checkpoint refs, no backups, no adapter calls, no verification execution.
- Safety failures (dirty tree, backup failure) stop the run before the adapter is invoked and produce a failed report with clear next steps.
- All state goes to the audit trail before any destructive operation is possible.
- Rollback prefers reporting manual steps over risky automatic cleanup. Opt-in `--clean` does a scoped restore: reset to the checkpoint plus removal of only session-attributable untracked files (per the session report); a blanket `git clean` is never run. Non-git projects restore from their tar backup. Callers may pass `preserve` (the pre-session untracked set) so pre-existing user files survive scoped restores even when change detection attributed them to the session.
- Automatic rollback (`auto_rollback`) reuses exactly that scoped path and only ever fires for failed/cancelled outcomes after the report is on disk; success and dry-run are never rolled back.
- A mission never reports success unless the final agent send returned `completed` and verification passed.
- Secrets in saved config are redacted before hitting disk.
