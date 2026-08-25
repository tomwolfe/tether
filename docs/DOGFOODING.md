# Dogfooding Tether on Itself

Tether was used to close its own review gaps. Each mission below was run as
`tether run missions/<file> --project-dir .` with a real (nested) `opencode`
agent, checkpointed, verified with `pytest`/`ruff`/`mypy`, and committed.
Session audit trails live under `.tether/sessions/`.

## Missions

| Mission | File | Session | Result |
|---|---|---|---|
| Graceful KeyboardInterrupt handling | `dogfood-01-interrupt.yaml` | `a557a43e03b6` | success (1 verification attempt) |
| `tether adapters smoke <name>` | `dogfood-02-smoke.yaml` | `399dafad1c14` | success (1 attempt) |
| Adapter settings validation (`known_settings`, warnings, `--strict`) | `dogfood-03-validation.yaml` | `9a38cabf369d` | success (recovery loop: verify attempt 1 failed, attempt 2 passed) |
| Strict exit-code + unregistered-name fixes | `dogfood-04-strict-exit-code.yaml` | `ac8bacf5493f` | success |
| Clean-room verification (`verification.clean_room`) | `dogfood-23-clean-room-verification.yaml` | `7e73b94228a2` | success (2 verification attempts: assertions failed on attempt 1, recovery fixed; review APPROVE) [evidence: real opencode adapter] |

## Defects dogfooding surfaced in Tether itself

1. **Nested-agent failure mode**: the first mission failed at planning in 1s
   because the user's default opencode model was unresolvable
   (`ProviderModelNotFoundError`). Diagnosis took one
   `opencode run --print-logs`; fix was pinning `-m <model>` in adapter
   config — exactly what `adapters smoke` now surfaces without wasting a
   full mission.
2. **Dirty-tree abort fired on our own mission files** — working as designed,
   and it enforced committing mission files before running them.
3. **Recovery loop worked for real** in mission 3: the agent's first pass
   failed verification and the second passed with no human intervention.
4. **Two bugs shipped by mission 3 were caught immediately** by manual use:
   `validate-config --strict` exited 0 despite printing INVALID, and
   unregistered adapter names were silently skipped. Both became mission 4.

## Re-running

```bash
git status --short            # must be clean; tether aborts on dirty trees
python -m tether run missions/dogfood-01-interrupt.yaml --project-dir .
python -m tether sessions list
```

Missions are idempotent-ish: later runs will find the work already done and
should produce a no-op diff that still passes verification.

## Documentation truth audit (dogfood-29, completed in dogfood-36)

A claim-by-claim audit compared every mechanically checkable statement in
README.md, docs/ARCHITECTURE.md, docs/SECURITY.md, docs/ADAPTERS.md, and this
file against the code in `src/tether/` (config defaults, CLI commands/flags,
event kinds, env vars, adapter settings and capabilities, limits like
context-file caps and budgets, retry/backoff numbers, sandbox-mode semantics,
`retention_days`, review-gate consensus options, clean-room copy rules).

- **Audited**: all five files above; ~70 concrete claims verified against
  `models.py`, `cli.py`, `orchestrator.py`, `reliability.py`, `cleanroom.py`,
  `git_safety.py`, `audit.py`, `conformance.py`, and the adapters package.
- **Corrected**: 1 — the README "Current limitations" list omitted the
  dogfood-33/34 reader-straggler behavior; it now states that reader threads
  drain the pipes behind a bounded join grace
  (`command.READER_JOIN_GRACE_SECONDS`) and that logs from such a send stay
  incomplete while a straggler holds them.
- **Pinned**: `tests/test_docs.py` now fails if any audited claim drifts
  again — pinned against source of truth (imported defaults/constants,
  CLI-app introspection) wherever practical.
- **Not mechanically checkable** (accepted on recorded evidence, left
  unpinned): historical/narrative records (promotion dates, session ids,
  live-fire field notes, defect stories), subjective-strength phrasing such
  as "best-effort" or "not OS-level containment", and command shapes observed
  against specific locally installed agent-CLI versions.
