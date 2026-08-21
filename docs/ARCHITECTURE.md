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
    command.py     Generic CommandAdapter (configurable argv template, placeholders)
    experimental.py OpencodeAdapter, PiAdapter (thin presets over CommandAdapter)
    __init__.py    Registry: resolve_adapter(name, settings)
  verification.py  Runs declared commands safely (shell=False, timeouts, cwd)
  git_safety.py    Checkpoint refs, dirty detection, prefix-resolving rollback,
                   changed files, backups
  manifest.py      Best-effort file manifests for non-git change visibility
  audit.py         Session directories, events.jsonl, report.json, secret redaction
  orchestrator.py  The core loop (adapter-agnostic)
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
9. Verification of declared commands (skipped execution in dry-run).
10. Pass AND agent completed => success. Any non-completed agent state (failed/unavailable/cancelled/needs_input/running) counts as failure. Fail => recovery loop: repair prompt with a bounded (~8KB) excerpt of failing output, re-verify, up to effective `max_attempts`.
11. Final `report.json` + rollback guidance on failure.
12. A `KeyboardInterrupt` (Ctrl-C) during adapter interaction is handled gracefully: `adapter.cancel(session)` is invoked best-effort (cancel errors are swallowed), a `cancelled` event is appended to `events.jsonl`, the audit trail is finalized with a `report.json` of status `cancelled`, and `next_steps` carries rollback guidance (`tether rollback <session-id>`).

Effective values follow strict precedence: mission explicit value > project config > built-in default, applied independently for `recovery.max_attempts`, `verification.commands`, and `verification.timeout_seconds`. Mission models keep these fields Optional so "unset" is distinguishable from an explicit value.

The loop never references a concrete adapter type; it only calls the `AgentAdapter` interface (`is_available`, `start_session`, `send`, `cancel`, plus prompt builders). Adding an agent = adding a registry entry or a config block.

## Design rules

- Verification commands must be explicitly declared; nothing arbitrary is executed.
- Failures are never faked: dry-run marks results as skipped, not passed-by-execution.
- Dry-run never mutates the target project: no checkpoint refs, no backups, no adapter calls, no verification execution.
- Safety failures (dirty tree, backup failure) stop the run before the adapter is invoked and produce a failed report with clear next steps.
- All state goes to the audit trail before any destructive operation is possible.
- Rollback prefers reporting manual steps over risky automatic cleanup. Opt-in `--clean` does a scoped restore: reset to the checkpoint plus removal of only session-attributable untracked files (per the session report); a blanket `git clean` is never run. Non-git projects restore from their tar backup.
- A mission never reports success unless the final agent send returned `completed` and verification passed.
- Secrets in saved config are redacted before hitting disk.
