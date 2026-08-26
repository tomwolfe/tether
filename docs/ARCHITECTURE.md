# Architecture

## Module map

```
src/tether/
  models.py        Pydantic models: MissionContract, TetherConfig, AgentState,
                   VerificationResult, CheckpointInfo
   mission.py       Mission loading/validation (YAML/JSON) -> MissionContract
   context_files.py Bounded mission context_files: limits, path/binary rules,
                    prompt rendering
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
  cleanroom.py     Clean-room materializer (dogfood-23): throwaway checkout of
                   the checkpoint ref + captured change; fail-closed
  git_safety.py    Checkpoint refs, dirty detection, prefix-resolving rollback,
                   changed files, backups
  manifest.py      Best-effort file manifests for non-git change visibility
  reliability.py   Transient-failure classifier + bounded send retries (dogfood-31)
  audit.py         Session directories, events.jsonl, report.json, secret redaction
  smoke.py         One-trivial-prompt adapter probe inside a throwaway directory
  conformance.py   Behavioral battery over any AgentAdapter with per-check results
  certify.py       Availability + conformance + live-probe certification verdicts
  orchestrator.py  The core loop (adapter-agnostic): change capture, auto rollback
  cli.py           Typer CLI
```

## Core loop (orchestrator.py)

1. Load + validate mission contract. Validation is STRICT inside the `mission:` block (dogfood-25): only `name` and `goal` — the keys the parser actually honors — are accepted; any other key raises a `MissionError` naming the key(s) and hinting that every other key belongs at the top level of the file: contract-level blocks (`verification`, `recovery`, `review`, `budget`, `adapter`, `adapters`, `allowed_paths`, `forbidden_paths`) and free-form content (`tasks`, `context`, `constraints`, `context_files`). Previously unknown keys were silently ignored, so a mis-indented contract block under `mission:` validated OK and ran with all defaults.
2. Resolve adapter via registry using config settings.
3. Create session id and audit directory; save resolved config with secrets redacted.
4. Adapter availability check (non-dry-run): an unavailable adapter fails fast with a clear next step.
5. Git checkpoint (record HEAD, create `refs/tether/checkpoint/<session-id>`). Dry-run records intent but writes no ref and takes no backup. A dirty tree aborts the mission before any adapter call unless `allow_dirty` is set (config/CLI precedence applies; the mission contract has no such key). Non-git projects get a tar backup under `backup_dir`; backup failure fails the mission.
6. Planning prompt -> adapter; response stored. A non-completed planning status aborts the mission before execution — but a TRANSIENT provider/infrastructure failure (e.g. `finish_reason: network_error`, rate limit, overloaded gateway, connection reset; classified by `tether.reliability.is_transient_failure` over the returned state's adapter-reported `error` field only — never `logs`, so genuine failures whose captured output merely mentions outage words, and Tether's own agent-timeout error (`command timed out after Ns`), keep their exact prior semantics) is first retried with bounded backoff (config `retries.max_transient_retries` default 2 extra attempts / `retries.transient_backoff_seconds` default 10): each retry logs a warning, records a `transient_retry` audit event ({step, attempt, reason}), re-checks `budget.max_wall_seconds`, and only exhaustion reaches the abort below. Declared `context_files` are read and validated against the target project *before* planning (limits: 32 files / 256 KiB per file / 512 KiB total; relative paths only, no `..`, must exist, NUL byte in first 8 KiB => binary refusal); any violation fails the mission before any adapter call. Loaded content is embedded into the planning and execution summaries (and passed through the prompt redaction helper when `redact_prompts` is on), with a `context_files` audit event recording paths and byte sizes.
7. Execution prompt -> adapter; agent state stored. Execution and repair sends get the same transient-retry treatment as planning; exhausted retries flow into recovery exactly like one failed send, and retries never consume `max_attempts`. Usage metrics from every physical send merge into `cumulative_usage` while each retried step counts as ONE logical send (`send_count`).
8. Changed-file detection (`git diff` vs checkpoint HEAD + untracked; non-git projects use a before/after file manifest). Recomputed after EVERY send — the initial execution and every recovery attempt alike.
9. Forensic change capture into the session audit directory (skipped in dry-run): git projects get `patch.diff` (`git diff --no-color --binary <original_head>`, includes binary changes) plus `untracked.txt` (a plain diff misses untracked contents); non-git projects get `manifest_diff.json` (added/modified/deleted plus the before/after fingerprints). Captured and refreshed after every send, before verification, so the evidence always reflects the agent's latest changes.
10. Verification of declared commands (skipped execution in dry-run). When the resolved command list is EMPTY and not dry-run, the loop logs a prominent warning ("mission declares no verification commands; success will not exercise any checks") and records it in `next_steps` (dogfood-25) — command-less missions still succeed, never silently. On otherwise-green attempts, artifact globs, structural assertions, and behavioral PROBES run next (each deeper tier only when the previous ones are green): probes (`verification.probes`, dogfood-20) execute a declared command with shell=False in the project dir and assert on its OUTPUT — the combined stdout+stderr must satisfy all `contains`/`matches` criteria; the exit code is recorded but never decides. A failing probe fails the attempt like any other deliverable miss and recovery proceeds normally; probe results are recorded alongside command/artifact/assertion entries, and dry-runs mark them skipped. The MUTATION meta-check (dogfood-22) is the deepest tier of this ladder and runs after the probe tier on green attempts: when `verification.mutation.enabled`, each changed `.py` file (never `.tether/` or sandbox-forbidden paths) is mutated with built-in stdlib-`ast` operators (deterministic seeded selection capped by `max_mutants`) and the SAME verification helpers re-run per mutant; per-mutant outcomes go to `verification/mutation.json`, the aggregate to `report["mutation"]` plus a `mutation` audit event, a `kill_rate` below `fail_below` fails the attempt (recovery proceeds normally), an unset `fail_below` is advisory-only, and dry-runs skip it entirely. CLEAN-ROOM tier (dogfood-23): when `verification.clean_room: true`, the ENTIRE battery — commands, artifacts, assertions, probes, mutation — runs in a throwaway checkout (`tether.cleanroom`: `git archive` of the checkpoint ref + applied `patch.diff` + non-gitignored untracked files + optional `clean_room_copy` entries; gitignored and outside-project paths deliberately excluded) instead of the agent's working tree, re-materialized fresh per attempt; materialization failure fails the mission immediately with a `clean_room_error` audit event (fail-closed, no in-tree fallback), and dry-runs record it as skipped.
11. Pass AND agent completed => success. Any non-completed agent state (failed/unavailable/cancelled/needs_input/running) counts as failure. Fail => recovery loop: repair prompt with a bounded (~8KB) excerpt of failing output extended by a bounded forensic context (current changed files, an excerpt of the latest change artifact, the previous attempt's changed files), re-verify, up to effective `max_attempts`. After every recovery send the changed-file detection, forensic capture, and write-sandbox gate run again (step 8–9 semantics): a recovery attempt that violates `allowed_paths`/`forbidden_paths` fails the mission immediately and skips further verification. Git sessions also save a per-attempt patch under `verification/attempt-NN.patch`, and each `recovery_attempts` entry records its `changed_files_at_attempt`.
    Recovery strategy + oscillation detection (dogfood-24): `recovery.strategy` selects the tree posture across repair rounds — `cumulative` (default, unchanged behavior) keeps intermediate damage; `reset_to_checkpoint` restores the checkpoint state before EVERY repair send (scoped clean rollback for git — pre-existing untracked files preserved via the same `preserve` set as auto-rollback — or backup restore otherwise), logs a `recovery_reset` audit event, refreshes change detection and forensic evidence, and records a failed reset on the recovery entry (`reset_error`) best-effort without aborting; dry-runs never reset. Independently, a pure `_OscillationDetector` hashes each failed attempt's signature (whitespace-normalized failing output + sorted changed-file tuple, O(attempts) memory): the FIRST repeat logs an `oscillation_detected` event and auto-escalates cumulative mode to `reset_to_checkpoint`; a SECOND recurrence even under reset aborts the loop early with `status: failed`, a final `failure_class: "oscillation_detected"` recovery entry, and actionable rollback guidance in `next_steps` instead of burning the remaining attempt budget. Distinct alternating failures never trigger any of this.
    Budget guardrails (dogfood-21): before every adapter send and before each verification round the core loop checks the mission's optional `budget` — `max_wall_seconds` (monotonic since mission start), `max_sends`, and cumulative `max_usage` metric ceilings (enforced only for metrics already seen in cumulative usage, so configured-but-unreported metrics never false-trigger). A breach fails the mission immediately, skips remaining sends and verification, records `report["budget_exceeded"]` (`limit`/`threshold`/`observed`) plus rollback guidance in `next_steps`, logs a `budget_exceeded` audit event, and maps to CLI exit code 5 (`EXIT_BUDGET_EXCEEDED`). Every main-path report carries `cumulative_usage` (merged numeric usage metrics across all sends plus `wall_seconds` and `send_count`); usage accumulates across EVERY send (initial + recovery) while the last-send `usage` field is unchanged.
12. Review gate (optional, dogfood-15): when the contract enables it (`review.enabled`), a REVIEW GATE runs only after verification passes — every declared command AND artifact assertion green — and before success is finalized. It opens a FRESH session on the reviewer adapter (dogfood-17: `review.adapter` names an independent reviewer resolved via the registry from the same adapters config, availability-checked before the run; unset keeps the mission's own adapter instance — no signature changes; the reviewer is just another `send()`) with a prompt built from the mission goal plus a bounded excerpt of the already-captured change artifact (`patch.diff` for git, `manifest_diff.json` otherwise; no re-diff) — or, with `review.context: "full"` (dogfood-20), the ENTIRE artifact up to 64 KiB plus an instruction to cite specific hunks/lines (default `"excerpt"` keeps the old prompt byte-for-byte) — and an instruction to act as an adversarial reviewer answering with exactly one verdict line. The verdict is parsed fail-safe from the reviewer's logs after ANSI escape sequences are stripped FIRST (dogfood-40 v2: colorized reviewer output parses identically to clean output): the LAST line BEGINNING with `REVIEW: APPROVE` or `REVIEW: REQUEST_CHANGES` decides (echoed prompts and diff hunks can contain the tokens mid-line but never begin a line with them); output with no such line counts as a rejection. The recorded reason prefers the decisive line's own remainder after the verdict token when that carries substance (e.g. `REVIEW: REQUEST_CHANGES — patch.diff is empty`), otherwise it walks forward to the first substantive line after the marker, skipping blank/escape-only lines. Meta-trust (dogfood-24): when `review.credibility_probe` is set, the reviewer's raw response is piped to stdin of that command (shlex-tokenized, shell=False, cwd = project dir) BEFORE the verdict is parsed; exit 0 marks the verdict trusted, and ANY other outcome — nonzero exit, spawn failure, or timeout — forces `request_changes` with reason exactly `"reviewer credibility check failed"` (a `reviewer_credibility` audit event records the outcome when configured). The probe is fail-safe in one direction only: it can force a rejection, never an approval. The outcome lands in `report["review"]` (`enabled`, `adapter`, `verdict`, `reason`) plus a `review` audit event and saved prompt/response; a rejection with `review.required` (default true) fails the mission and appends the review reason to `next_steps`. Optionally (`review.retry_on_rejection`, dogfood-17) a required rejection instead routes back into the step-11 recovery loop — repair prompt with goal + review reason + change excerpt, one more execution, re-verification, re-review — drawing from the same bounded `max_attempts` budget; when exhausted it fails with the last review reason in `next_steps`. Multi-reviewer consensus (dogfood-32): when `review.reviewers` lists adapter names, EACH is resolved via the registry at gate time and consulted on its own fresh session — the credibility probe applies per reviewer, and unresolvable names or interaction failures count as rejections; the aggregate verdict follows `review.consensus` (`"all"` requires unanimous approval, `"majority"` strictly more approvals than rejections, ties fail safe) and per-reviewer outcomes are recorded in `report["review"]["reviewers"]`. Missions without `review.reviewers` keep the single-reviewer path and payload byte-for-byte.
13. Final `report.json` + rollback guidance on failure. Opt-in auto rollback: when `auto_rollback` is enabled and only the status is `failed` or `cancelled` (never success, never dry-run), a scoped rollback runs right after the initial report write — clean scoped restore for git (using the report's changed_files, never touching pre-session untracked files passed via `preserve`) or backup restore for non-git — and `report.json` is rewritten with an `auto_rollback` result (`attempted`, `ok`, `message`). Failure keeps the original status and manual guidance.
14. A `KeyboardInterrupt` (Ctrl-C) during adapter interaction is handled gracefully: `adapter.cancel(session)` is invoked best-effort (for CommandAdapter this terminates the running process tree), a `cancelled` event is appended to `events.jsonl`, the audit trail is finalized with a `report.json` of status `cancelled`, and `next_steps` carries rollback guidance (`tether rollback <session-id>`).

Effective values follow strict precedence: mission explicit value > project config > built-in default, applied independently for `recovery.max_attempts`, `verification.commands`, and `verification.timeout_seconds`. (`recovery.strategy` is mission-only — the project config has no such key — and additionally auto-escalates to `reset_to_checkpoint` on detected oscillation, dogfood-24.) Mission models keep these fields Optional so "unset" is distinguishable from an explicit value.

## Sandbox modes

`sandbox_mode` selects how the write sandbox (mission `allowed_paths` / `forbidden_paths` globs) reacts:

- `warn` (default): post-send gate. Detected changes (git diff + untracked files; non-git manifest) are matched against the globs after every agent send — the initial execution and every recovery attempt; violations fail the mission immediately and skip verification.
- `enforce`: additionally snapshots the tree before execution (the shared file manifest) and unions the filesystem-metadata diff into the post-send check for git repos too — untracked files are already covered in both modes, and the metadata diff catches writes content-based detection misses (e.g. gitignored paths).

Independent of the path globs, an opt-in integrity gate covers git *history*: when a mission sets `git_state_guard: true`, the same post-send gate verifies after EVERY send that HEAD still equals the checkpointed original commit and that the session's checkpoint ref (`refs/tether/checkpoint/<session-id>`) still resolves to it. Any drift — a moved branch (`git reset`), a deleted or rewritten checkpoint ref — fails the mission exactly like a sandbox violation: a `git_state_violations` audit event records human-readable violations, `report["git_state_violations"]` carries them alongside rollback guidance in `next_steps`, verification is skipped entirely, and status is failed. Dry-runs and non-git projects are inert, and `reset_to_checkpoint` recovery never trips the gate because its resets restore HEAD to the recorded original_head. Hook integrity rides the same baseline (dogfood-42): at mission start the guard records the sha256 of every file under `.git/hooks/` plus the current `core.hooksPath` value, recomputes both after every send, and treats any planted/modified/deleted hook file or changed/newly-set `core.hooksPath` as guard drift under the identical fail-and-skip contract; pre-existing hooks and hooksPath config are part of that baseline and never trip. Leaving the key unset keeps behavior byte-identical (no new checks, events, or report keys).

Enforce narrows risk but does not eliminate it: it is best-effort detection inside the normal loop, not OS-level containment (see docs/SECURITY.md).

## Quantitative verification strength (dogfood-40)

`tools/mutation_killrate.py` measures the mutation kill rate of an explicit
(source file × test suite) pair using Tether's own deterministic mutant
generator — the built-in mutation tier can only target the agent's changed
files, so audits need this explicit form. `--target` names one `.py` file,
`--suite` (repeatable) names pytest paths re-run per mutant with `-x -q`,
`--max-mutants` caps generation deterministically (0 = all sites), and
`--min-kill-rate` turns the measurement into a gate: exit code 2 below it,
exit code 0 at or above it, exit code 1 on harness error. Mutant generation
derives its per-file seed exactly like `run_mutation_testing`, so results are
reproducible against the built-in tier, and the tool's counting, crash-as-kill
semantics, deterministic cap, and gate boundaries are unit-tested without
spawning subprocesses by injecting the suite runner
(`tests/test_mutation_killrate.py`).

Pinned gate: mutations of `src/tether/cleanroom.py` against
`tests/test_cleanroom.py` must hold a kill rate ≥ 0.80; measured 0.92
(46/50) after dogfood-40 added survivor-killing probes to that suite. The
four permanent survivors are equivalent by construction (`return None`
mutated to itself via break_return; `return False` mutated to `return None`
where `_is_relative` results are only used for truthiness) and documented
in `tests/test_cleanroom.py`.

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

## Live-fire field notes (dogfood-27/28, real opencode sessions)

Two integration dogfood missions ran the full stack against this repo with a
real `opencode` agent:

- **dogfood-27 (integrated gauntlet)**: clean-room + mutation (`fail_below`)
  + full-context required review + tight budget in ONE session landed a scoped
  verification refactor; all four layers passed together on attempt 1
  (session `b584213e`). The write sandbox correctly caught a stray macOS
  `.DS_Store` outside `allowed_paths`, failing fast with an explanatory
  violation message.
- **dogfood-28 (oscillation live fire)**: a deliberately contradictory
  fixture (two tests asserting mutually exclusive values of a single module
  constant, mutable file restricted to that constant) produced a REAL
  oscillation: identical failure signature at attempts 2 and 3 fired
  `oscillation_detected`, escalated cumulative mode to `reset_to_checkpoint`
  after the first repeat, and aborted early with
  `failure_class: "oscillation_detected"` plus rollback guidance in
  `next_steps` — burning 3 of 5 allowed attempts instead of all of them
  (session `b1a75ede`).

### Adversarial findings worth pinning

1. **Probe marker self-match.** A probe whose command is a `python -c`
   one-liner asserts on combined stdout+stderr. When its assert fails, the
   traceback ECHOES THE ENTIRE `-c` SOURCE LINE — which typically contains
   the expected `contains` marker (e.g. `print('data-only: ok')` appears in
   the AssertionError traceback). The probe then matches its own failure
   output and passes despite exit code 1. Mitigation used in dogfood-28:
   assemble markers from string fragments so the literal never appears in
   the source text (`print('data', '-', 'only', ':', 'ok')` with
   `contains: "data - only : ok"`). Probe authors must ensure the success
   marker cannot appear in any possible failure output, including echoed
   source.
2. **Equality-operator loopholes.** An agent told to satisfy
   `MODE == "alpha"` and `MODE == "beta"` with only one mutable module will
   reach for custom `__eq__` objects and PEP 562 `__getattr__` caller-frame
   inspection before it ever "fails". Contradictory-fixture experiments need
   structural constraints (type checks, data-only AST probes, subprocess
   probes without test frames in the stack) to stay contradictory.
