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
  git_safety.py    Checkpoint refs, dirty detection, rollback, changed files, backups
  audit.py         Session directories, events.jsonl, report.json
  orchestrator.py  The core loop (adapter-agnostic)
  cli.py           Typer CLI
```

## Core loop (orchestrator.py)

1. Load + validate mission contract.
2. Resolve adapter via registry using config settings.
3. Create session id and audit directory.
4. Git checkpoint (record HEAD, create `refs/tether/checkpoint/<session-id>`); refuse on dirty tree without `--allow-dirty`; tar-backup non-git projects.
5. Planning prompt -> adapter; response stored.
6. Execution prompt -> adapter; agent state stored.
7. Changed-file detection (`git diff` vs checkpoint HEAD + untracked).
8. Verification of declared commands.
9. Pass => success. Fail => recovery loop: repair prompt with failing output, re-verify, up to `max_attempts`.
10. Final `report.json` + rollback guidance on failure.

The loop never references a concrete adapter type; it only calls the `AgentAdapter` interface (`is_available`, `start_session`, `send`, `cancel`, plus prompt builders). Adding an agent = adding a registry entry or a config block.

## Design rules

- Verification commands must be explicitly declared; nothing arbitrary is executed.
- Failures are never faked: dry-run marks results as skipped, not passed-by-execution.
- All state goes to the audit trail before any destructive operation is possible.
- Rollback prefers reporting manual steps over risky automatic cleanup.
