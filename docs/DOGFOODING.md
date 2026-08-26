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

Sessions: v1 `7f460335` (failed: payload pre-committed => empty captured
change; four correct review rejections; forensics below), v2 `981c4003ea16`
(success attempt 1: verification green incl. the kill-rate gate, adversarial
review APPROVE with substantive reason).

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

Outcome: the dogfood-40 v2 mission landed the defect-5 fix —
`_parse_review_verdict` now strips ANSI escape sequences before scanning,
so escape-prefixed verdict lines decide, the recorded reason prefers the
decisive line's post-token remainder when it carries substance (e.g.
`REVIEW: REQUEST_CHANGES — patch.diff is empty`) and otherwise walks
forward past blank/escape-only lines to the first substantive line.
Clean-output parsing is unchanged; pinned by the committed acceptance
tests in `tests/test_review_gate.py` plus a docs-truth pin in
`tests/test_docs.py`. The v2 session's own review gate then ran through
the FIXED parser against the real opencode reviewer and returned an
approve whose reason was substantive reviewer text — the exact path that
produced `"\x1b[0m"` in v1.

## Git-state guard (dogfood-41)

The same session-`7f460335` forensics that exposed the ANSI verdict defect
also proved a structural blind spot: mid-mission the nested agent ran
`git reset`, moving the user's branch pointer. The path-based write sandbox
saw no forbidden paths (identical trees — only refs moved), capture bases
were silently corrupted, and nothing failed. dogfood-41 closes it with an
opt-in contract key, `git_state_guard: true`: after EVERY send (initial
execution and every recovery attempt alike), alongside the write-sandbox
gate, Tether verifies strict integrity — HEAD still equals the checkpointed
original_head AND the session's checkpoint ref
(`refs/tether/checkpoint/<session-id>`) still resolves to that same sha.
Any drift fails the mission exactly like a sandbox violation: a
`git_state_violations` audit event records human-readable drifts,
`report["git_state_violations"]` carries them with a `tether rollback`
next-step naming the drift, verification is skipped (never trusted over a
rewritten base), and status is failed. Dry-runs and non-git projects are
inert; `reset_to_checkpoint` recovery never trips the guard because its
resets restore HEAD to original_head; leaving the key unset keeps behavior
byte-identical (no new checks, events, or report keys).

Landed with committed acceptance tests in `tests/test_git_state_guard.py`
(HEAD-move and ref-deletion fail closed; default-OFF legacy pin;
innocent-agent, reset-recovery, and non-git inertness pins) plus a
docs-truth pin in `tests/test_docs.py`. One deliberate scope addition: the
mission whitelist named only models/orchestrator/docs, but
`tether.mission.load_mission` constructs contracts from explicit kwargs and
silently drops unknown top-level keys — every prior mission-only key
(`adapter`, `allowed_paths`, `budget`) needed the same loader passthrough —
so the field would never have reached the orchestrator without a 5-line,
sibling-pattern amendment there (`'git_state_guard' must be a boolean`
MissionError + constructor kwarg).

Sessions: first run `935aa42e` (aborted by the write sandbox itself — the
agent correctly amended `src/tether/mission.py`, which the mission's
allowlist omitted; allowlist widened). Second run `8fd9c8bd044d`: success
on attempt 1, executed WITH `git_state_guard: true` in its own contract —
the guard shipped by a mission that ran under itself, zero false trips,
adversarial review APPROVE citing implementation line numbers.

## Guard adoption and recovery-round drift (dogfood-42)

Policy: from dogfood-42 onward, every dogfood mission sets
`git_state_guard: true` in its own contract — self-hosting runs prove
their own guard, so any regression in the guard surfaces in the very
mission that ships code.

Record: corpus audit of `tests/test_git_state_guard.py` proved two gaps.
(1) Every drift case tripped the guard on the INITIAL send; nothing
covered drift during a RECOVERY send under cumulative strategy, after
verification had already failed once. (2) `.git`-internals plants beyond
HEAD/refs were invisible: an agent could write `.git/hooks/*` or set
`core.hooksPath` and hijack every future git operation, unseen by path
globs and by HEAD/ref checks alike. The mission extends the opt-in guard:
at mission start (after checkpoint creation) Tether snapshots a baseline —
the sha256 of every file under `.git/hooks/` keyed by relative path plus
the current `core.hooksPath` value — and recomputes both after EVERY send;
any added/modified/deleted hook file or changed/newly-set hooksPath fails
the mission under the exact same `_GitStateViolationError` contract
(`git_state_violations` event, report key, skipped verification). Baseline
semantics mean pre-existing hooks and hooksPath config never trip; with
the key unset no snapshot or check runs at all. Recovery-round coverage
needed no new machinery: because the gate re-runs after every recovery
send, a repair round that moves HEAD trips before the next verification
can legitimize the drifted tree. Pinned by three new acceptance cases
(hook plant, hooksPath redirect, cumulative-recovery drift) plus a
pre-existing-hooks inertness pin in `tests/test_git_state_guard.py`, and
by docs-truth pins in `tests/test_docs.py` — including the rule that all
`dogfood-*.yaml` missions numbered 42+ must carry `git_state_guard: true`.
This mission itself ran under the guard it extended (session
`585a762fa949`, success attempt 1, adversarial review APPROVE).

## Auto-generated verification probes (dogfood-43)

Boundary broken: every prior verification layer — commands, artifacts,
assertions, probes, mutation, clean-room, review — shared one assumption:
verification content is authored by a human BEFORE the run, guessing at
write-time where behavior matters. Nothing adapted verification to what
the agent actually did. Why this breaks the local maximum: the single-
agent loop was maximally hardened around fixed-at-authoring checks, so
the next strength gain could not come from polishing any existing tier;
it had to come from making verification diff-adaptive while keeping the
hardened fail-safe posture intact.

Design shipped: `verification.auto_probes` (`src/tether/autoprobes.py`,
wired through models/loader/orchestrator). After the initial capture and
before any human-authored verification runs, Tether consults a generator
adapter on a fresh `-auto-probes` session with a prompt built from the
mission goal plus the bounded captured change (`patch.diff` AND
`untracked.txt` — a plain git diff misses untracked contents). The
response parses fail-safe (ANSI stripped first per the dogfood-40 lesson,
LAST fenced yaml block wins, strict per-entry validation, 2000-char
command cap, `max_probes` cap default 6); ANY malformation rejects the
whole response and the mission falls back to exactly today's battery.
Accepted specs run as a new ladder tier after the human probes; the TEETH
gate then mutation-tests them against the change — changed `.py` files
are mutated with the built-in seeded operators and ONLY the generated
probes re-run per mutant — so pristine-tree passage (guaranteed by the
tier above) makes the measured kill share pure probe strength. Below
`min_teeth_rate` the attempt fails and recovery receives the surviving
mutant sites; unset rate / no runnable mutants / failed synthesis are
advisory. Detail persists in `verification/autoprobes-teeth.json` plus an
`auto_probe_teeth` event per measured attempt; `report["auto_probes"]`
records synthesis status, accepted specs, and latest teeth. Key absent =
byte-identical behavior (pinned).

Proof (red -> green, in-session): unit contract
`tests/test_autoprobes.py` (30 cases: prompt shape incl. the dogfood-28
marker-self-match warning, parser happy paths/fail-safe matrix/bounds/
truncation/ANSI, teeth summarizer semantics, and REAL mutant measurement —
a behavioral probe kills both mutants of a fixture function while a
toothless constant-output probe kills none) and integration contract
`tests/test_autoprobes_mission.py` (10 cases: loader accept/reject matrix,
off-by-default full inertness, live synthesis over a scripted adapter with
prompt-evidence assertions, tier execution visible in report
verification_results, garbage-synthesis advisory fallback with no teeth
run, toothless-probe gate failure routed into recovery and failing the
mission after exhausted attempts, non-.py-change n/a advisory pass,
dry-run never synthesizes). Both suites failed before their targets
existed and pass after; full suite 704 green, ruff + mypy clean.

Live fire: `missions/dogfood-43-auto-generated-probes.yaml` ran WITH
`auto_probes.enabled` in its own contract (policy: self-hosting runs
prove their own features) — the first dogfood mission whose verification
is partly synthesized at runtime and mutation-tested mid-mission.
Session `2c80babff1cd`: SUCCESS on attempt 2 (3 sends, 38.3 min) —
verification green on attempt 1, adversarial review REJECTED, repair
round routed via `retry_on_rejection`, attempt 2 re-verified green and
review APPROVED with a substantive reason; `git_state_guard: true`
zero false trips. Payload (`tether adapters describe`) confirmed
post-run: 712 tests / ruff / mypy clean, exit-2 unknown-name path live.

Two genuine field findings (dogfood-44 hardening targets):

1. **Synthesis parser vs real-model YAML.** The real opencode generator
   echoed the prompt's own fenced TEMPLATE block first (harmless — the
   LAST-fence-wins rule skipped it) and emitted its final block using
   shell-style nested single quotes (`print('UNKNOWN_'+'GAUNTLET')`)
   inside a YAML single-quoted scalar, which is invalid YAML. The strict
   parser rejected the whole response, status "failed" with the yaml
   error as reason, and the mission fell back to the human-authored
   battery — the fail-safe posture working exactly as designed, but zero
   generated probes survived contact with a real model. Candidates:
   output-format discipline in the synthesis prompt (no template echo,
   prefer double-quoted scalars, forbid shell-quote nesting), per-entry
   salvage instead of all-or-nothing rejection, or both.

2. **Reviewer evidence starvation for untracked files.** Attempt 1 was
   fully green yet correctly rejected: `patch.diff` is
   `git diff --binary <original_head>` (commit-vs-worktree) and carries
   NO untracked file contents, so the full-context reviewer saw
   `cli.py` import `tether.describe` without ever seeing
   `describe.py` — an import of nothing reads as an undemonstrable
   change. Verification/clean-room see untracked files (untracked.txt +
   materializer copy); the review gate does not. Recovery resolved it by
   staging the new files (staged bytes ARE captured), but the gate's
   evidence should not depend on the agent knowing that: candidate fix
   is a bounded untracked-contents appendix in the review context,
   mirroring the clean-room rules.
