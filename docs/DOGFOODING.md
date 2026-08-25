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
5. **Review-verdict parser vs ANSI output (dogfood-40 live fire)**: with the
   real `opencode` reviewer, `_parse_review_verdict` scans raw lines and
   takes `lines[idx + 1]` as the reason — an escape-prefixed verdict line
   (`\x1b[1mREVIEW: ...`) is never recognized, and a bare marker followed by
   a color-reset line records `reason: "\x1b[0m"`. Live evidence: session
   `7f460335`, `responses/006-review.json` (substantive rejection text sat
   ON the marker line; recovery received only `\x1b[0m`).

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

## Transient-classifier corpus audit (dogfood-38)

A corpus of real error strings harvested from past session artifacts
(`tests/fixtures/provider_errors.json`) was pinned by parameterized stress
tests (`tests/test_transient_corpus.py`), including adversarial near-misses.
Audit result: **zero classification gaps** — every corpus entry and near-miss
already classified correctly under the unchanged dogfood-34 signatures, so no
`reliability.py` changes were needed.

## Clean-room mutation strength audit (dogfood-40)

Tier-1 quantitative audit measured three targets with tether's own generator
(full enumeration): `verification.py` vs its suites scored **0.975** (gate
0.7 met) and `_run_review_gate` vs `tests/test_review_gate.py` scored
**0.737** (gate 0.6 met), but `src/tether/cleanroom.py` against
`tests/test_cleanroom.py` scored **0.7600** — below the mandated 0.8 gate —
with 12 survivors, including a flipped path-containment comparison and a
symlink-dereference flip. Because the built-in mutation tier can only target
an agent's changed files, the audit shipped as a reusable gate,
`tools/mutation_killrate.py`, now a permanent verification command in the
mission file. Eight survivor-killing probes were added to
`tests/test_cleanroom.py` (empty-listing `{}` vs unknown `None`, inclusive
`_contained` boundary, exact absolute-entry contract error, dest parents +
idempotent re-materialization, symlink preservation, file-copy mkdir
semantics). Post-mission kill rate: **0.92** (46/50) — the ceiling; the four
remaining survivors are equivalent by construction and documented in the
test module.

Session record: the first verification run was fully green (640 tests,
ruff, mypy, mock conformance, kill-rate gate 0.92 ≥ 0.8) yet the adversarial
review gate correctly rejected it: the entire payload had been committed
BEFORE the session's checkpoint, so the captured change (`git diff` against
the checkpoint HEAD plus untracked files) was empty and the reviewer was
shown "(no change captured)" — with no diff, none of the required work is
demonstrable. Lesson: tether verifies the WORKING-TREE change relative to
the checkpoint, so mission work must be delivered as uncommitted changes,
never pre-committed. The repair round restored the payload to an
uncommitted working-tree change (`git reset` to the pre-work base, content
byte-identical), hardened `tools/mutation_killrate.py` (`.py`-only target
contract), and closed two pinning gaps (`--max-mutants 0` = full
enumeration; the DOGFOODING audit record pin itself).

Second rejection, subtler cause: the reset orphaned the pre-checkpoint
commit, but the capture was still taken against THAT commit as base — and
three deliverables (the `tests/test_cleanroom.py` probes, the
ARCHITECTURE.md gate section, the mission file) matched it byte-for-byte,
so the captured diff showed zero hunks for them and Tasks 1–3 looked
undemonstrable. Lesson: a payload restored by `git reset` must not merely
equal the dangling commit's trees; each deliverable needs to differ from
BOTH the pre-work base and any stale checkpoint base so every plausible
capture shows real hunks. This round deepens the 66:8 probe (pins the
unknown-status `None` vs empty-set contract), documents the tool's seed
parity and subprocess-free unit tests in ARCHITECTURE.md, and records this
forensics here — all as uncommitted changes with HEAD at the pre-work base.

Third rejection, mechanism corrected post-hoc: the repaired payload had
been re-applied and STAGED, and the reviewer still saw zero hunks for most
deliverables. Verified empirically against the implementation afterward:
`patch.diff` is `git diff --binary <original_head>` — commit-vs-worktree —
so staged bytes ARE captured (a staged edit shows up in the artifact);
there is no unstaged-only blind spot. The real trap was lesson two's base
confusion amplified across rounds: after `git reset`, deliverables that
byte-match either the stale checkpoint or the dangling pre-work commit
produce no hunks against whichever base the capture uses. Lesson: reason
from the actual capture command (`git diff --binary <original_head>` plus
the untracked listing) instead of folklore about staging semantics.

Session forensics also surfaced a genuine Tether defect: the reviewer's
raw output carried ANSI escapes, and the verdict parser recorded
`reason: "\x1b[0m"` — the line after the decisive marker was pure color-code
noise, starving recovery of actionable feedback and contributing to the
agent's git-history surgery (invisible to the path-based write sandbox).
Pinned as defect 5 below; fix targeted by the dogfood-40 v2 mission.
