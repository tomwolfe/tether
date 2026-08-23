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
