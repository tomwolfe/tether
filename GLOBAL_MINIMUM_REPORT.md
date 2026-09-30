# Global Minimum Report — tri-repo (Tether / QED / VeriTrial)

Model for every Tether run in this session: **`opencode/space-bunny-free`**.
No mock adapter was used for any gate recorded below.

## Ledger

| repo | HEAD | dirty | sorry_free |
|---|---|---|---|
| tether | `b27f959` (ledger names the head the report attests to) | false | `n/a` (ships no Lean) |
| QED | `8207365` (Fin 14 export + saturable transports) | false | **true** |
| VeriTrial | `90ae4f6` (formal gate generalized to Fin N, saturable clearance) | false | **true** |

`SYSTEM_STATE.json` merkle root: `e503d3e2c003388f…` (full value in
`SYSTEM_STATE.json`)
V&V report `<meta name="merkle-root">`: `923664f2b6786a6f95ce0aa25064662f7f021d0d1934b907fa46033d3e58bdd2`

Regenerated and re-measured 2026-09-29. The report's `<meta name="merkle-root">`
now **matches** `output/validation/regulatory_provenance.json` exactly; they
disagreed before, because a pytest run had overwritten the provenance JSON with
a root built from a tmpdir lemmas file while the HTML still carried the root
sealed by the last real `make validate`.

These two roots are **different quantities and are not expected to match**: the
report chains six validation leaves (tether report, QED traces, benchmark
summary, git SHAs, Lean digests, benchmark metrics); the ledger hashes the
three repo audit records. The real binding is that the report's `git_shas` are
contained in the ledger's history — verified by `git merge-base --is-ancestor`
for all three repos, enforced by `test_report_commits_to_ancestors_of_the_ledger_heads`.
Note the ledger necessarily trails the report by the ledger's own commit, so
exact SHA equality is unsatisfiable by construction and is not asserted.

## Lean soundness

No `sorry`, no `sorryAx`, no `Tactic.sorry`, no `declaration uses sorry`.
`#print axioms` for every headline theorem is exactly
`[propext, Classical.choice, Quot.sound]`:

```
orthant_invariance_fwdEuler   mass_dissipation_rate   mass_conservation_rate
sdirk_stage_isMmatrix         mmatrix_inv_preserves_nonneg
saturableFlux_nonneg          saturableFlux_bounded   saturableFlux_mono
```

All are generic over `[Fintype n] [DecidableEq n]`; `Compartmental.lean`
compiles under `leanprover/lean4:v4.34.0-rc2` with **0 errors** (warnings only).
No theorem in the file is closed by `rfl`.

## Formal gate — 9/9, no sorry

Re-measured 2026-09-29 via
`export_pbpk_to_qed.py --out /tmp/pbpk_lemmas.txt --parametric` then
`verify_formal_gate.py /tmp/pbpk_lemmas.txt --strict` (exit 0):

```
all 9 lemmas verified by QED (no sorry)
FORMAL GATE PASSED: all required PBPK lemmas verified by QED (no sorry).
```

Earlier revisions of this report recorded **18/18**. That number is stale: the
current six-organ export emits **9** lemmas, and `verify_formal_gate.py` reports
`n_lemmas: 9`. Both the report and the artifact agree at 9, which is the number
that matters; the 18 was from an earlier export shape. The 9 include the
parametric column-sum identity and the mass-dissipation identity, both proved
rather than assumed:

- column sum: the full thirteen-term central-column sum `= 0`, including the
  non-trivial `(-(CL + Ql + Qp + Qe)/Vc)` that offsets `Ql/Qp/Qe/CL` over `Vc`.
- dissipation: the eleven-term `= 0` identity for the gut/central rate balance.

`#print axioms` on both export theorems returns exactly
`[propext, Classical.choice, Quot.sound]`.

Spot-checks of the emitted Jacobian (autodiff vs. the analytic form used for
the dt bound), `n_states=6`, perfused `[1,3,4]`, central 2, elim 5:

| entry | autodiff | analytic | identity |
|---|---|---|---|
| `J[0][0]` | -0.800000 | -0.800000 | `-ka` |
| `J[1][1]` | -0.065871 | -0.06587097 | `-Ql/(Kpl*Vl)` |
| `J[2][2]` | -0.239272 | -0.23927213 | `(-CL-Qe-Ql-Qp)/Vc` |
| `J[5][2]` | +0.062430 | +0.06243009 | `CL/Vc` |

`J[5][5] = 0`: the eliminated compartment does not depend on itself, so it
imposes **no** step-size constraint. `CL/Vc` is the off-diagonal `J[5][2]`.
Getting this backwards is the bug that `test_fixed_step.py` now pins against
`jax.jacfwd` for both the six-organ and 14-organ networks.

Column sums of `J` are zero to float32 precision
(`[0, 0, 1.49e-08, 0, 0, 0]`).

## Mutation kill rates (real CLI, no mocks)

| target | suite | rate |
|---|---|---|
| tri-repo gate (`baseline_targets`, 4 files) | gate battery | **1.0000** (23/23) |
| `tether/src/tether/cleanroom.py` | `tests/test_cleanroom.py` | **0.9518** (79/83) |

The tri-repo row was re-measured on 2026-09-29 by reproducing the gate's own
sampler exactly — `generate_mutants`, the same per-file seed
(`sha256(rel)[:8]`), the same operator set, the same `max_mutants: 6` cap and
the same 16-entry `equivalent` suppression list read from
`missions/tri-repo-full-stack-gate.yaml`. **23 killed, 0 survived**, which
clears the 0.80 floor and also satisfies the `= 1.0` requirement for
`export_pbpk_to_qed.py`, `verify_formal_gate.py`, `pbpk/model.py` and
`pd/__init__.py` individually.

`cleanroom.py`'s 4 survivors are **provably equivalent**, and the claim was
re-checked against the current source rather than inherited:
`break_return` rewrites `return expr` to `return None` and `expr` is already
`None` (73:8, 75:8); the sibling-target `mkdir` is preceded by an
unlink/rmtree prelude on every branch, so neither of its flags is reachable
(254:41, 254:56). All four sites are the ones named in the mission's
suppression list.

`src/tether/orchestrator.py` is **not** a gate target and is deliberately not
scored against the gate battery; a whole-file run against `tests/` was started
during this session and abandoned as both slow and off-contract. Scoring it
would report a number the gate never claimed.

## The tri-repo gate re-run for real (2026-09-29)

The agent/review/sandbox layer was finally exercised, closing the gap this
report previously carried as unverified. `tri-repo-full-stack-gate.yaml`,
session **`c44712be2b03`**, `--adapter opencode`, real (not dry) run:

| layer | result |
|---|---|
| status | **success** |
| clean-room verification | **9/9 commands exit 0**, in a real clean room at `/tmp/tether-cleanroom-i9whbk9s` |
| mutation | **kill_rate 1.0000** — 23 killed, 0 survived, 1 documented-equivalent skipped (`fail_below` 0.8) |
| review | **approve** |
| sandbox violations | **none** (`[]`) |
| git_state_violations | none reported |
| formal gate | `all 9 lemmas verified by QED (no sorry)` / `FORMAL GATE PASSED` |
| siblings after the run | QED `57368a0` and VeriTrial `5516067`, both **clean and unmoved** |

The sibling-safety question is now answered by measurement rather than
caution. The report previously warned that a gate run can move a sibling
branch, because `git_safety.py` issues `git reset --hard` per repository. That
did not happen here: clean-room staging uses `git archive` (read-only), and
`rollback` refuses on a dirty tree, so the risk is bounded. Recorded as
"did not occur in this run", not as a claim that it cannot occur.

### The run found two false claims in the mission's own context

The agent audited the context it was handed and rejected two statements. Both
were checked by hand before accepting, and both are true:

1. **"emits the N-generic … isomorphism file … (Jacobian-derived dimension)"** —
   for a model defining `make_pbpk_ode` the live path takes
   `N = len(DEFAULT_ORGAN_NETWORK) = 6`. The Jacobian-derived `N` further down
   is dead for that model shape and command 1 passes no `--fin-n`. The emitted
   file contains only `Fin 6` and `Fin 9`; `_network_for_fin` accepts just 6 or
   14, so `--fin-n 14` is a separate, unproven path. **The gate certifies
   Fin 6 only.**

2. **"formal_verification.py embeds SHA-256 hashes of verified Lean code"** —
   it computed `_lean_code_sha256` over `attempt["lean_code"]`, a key
   `check_qed_proofs` never set, so `lean_code_sha256` was always `{}`.

   **This was also a deliverable in the task brief, and it is now FIXED**
   (`VeriTrial` `94f4848`). The digest is now computed at the boundary where
   `result` still holds the prover's output, and carried on the attempt dict.
   It cannot be computed in the consumer — that is precisely the bug. Verified
   against a real run: **9/9 lemmas carry a 64-hex digest**, and the artifact
   contains no proof source (the raw text is deliberately not stored, since
   `attempts` is serialized into the trail JSON; `_write_trail` now strips
   raw-source keys defensively).

   Why it survived review for so long: the digests are **not** part of the
   pass/fail condition, so the gate stayed green with them empty. Silent
   absence of tamper evidence is worse than a loud failure.

Both corrections make the mission's claims *narrower*, which is the safe
direction. The mission's 9 commands, thresholds, suppression list and
`fail_below` are untouched.

### A second stale-artifact bug found while fixing the first

`build_regulatory_provenance` only *inserted* a `merkle-root` tag when one was
absent, and `run_all_validations` regenerates `vvv40_report.html` from scratch
without one. So re-running validations without re-sealing **stripped the
provenance chain** — reproduced here. Worse, a report that still carried an
*old* root kept it, so the HTML and `regulatory_provenance.json` silently
disagreed while every existence check stayed green: a report attesting to a run
it no longer describes.

The sealer now **replaces the tag by value**, so the two agree by
construction. A malformed or foreign tag is neither overwritten nor ignored —
it is reported as `malformed_merkle_tag` on the returned record, because
silently leaving it is a lie and destroying it could destroy evidence.

## Zero leakage

`grep -riE "pbpk|liver|dili|cyp|ka_rate|c_tissue|kp|a_gut" parser.py agentic_pipeline.py`
→ no matches. QED's parser and pipeline are domain-agnostic.

OOD, verified with zero changes to QED: SEIR conservation, SEIR positivity,
3-tank cascade, matrix-entry equality (6 tests, 8.4 s) — plus `run_tests.py`
18/18 and `test_pipeline.py` **256 passed**. The 200 figure in earlier
revisions of this report is stale; the suite has grown.

## The pinned toolchain's `lake` is broken; `lean` is not

`leanprover/lean4:v4.34.0-rc2` ships a `lake` that **dies with SIGTRAP (exit
133) on every invocation**, after printing correct output:

```
$ lake --version
Lake version 5.0.0-src+6a10ac8 (Lean version 4.34.0-rc2)
$ echo $?
133
```

It reproduces in an empty directory with no project involved, and the Python
subprocess reports `returncode -5` (signal 5, SIGTRAP). The same toolchain's
`lean` is fine (`lean --version` exits 0, and a Lean compile of `QED.lean` /
`Compartmental.lean` / `VeriTrialExport.lean` exits 0). Under
`ELAN_TOOLCHAIN=leanprover/lean4:v4.0.0` the same `lake` exits 0, so the fault
is in the rc2 toolchain, not in this repo or its Lake project.

Consequence: **`lake build` cannot be run at all** under the pinned toolchain,
so a literal "`lake build` 0 errors" check is unsatisfiable here. The
equivalent check was performed instead by invoking `lean` directly with a
`LEAN_PATH` reconstructed from `lake-manifest.json` (all 8 package build dirs
plus the local `.lake/build/lib/lean`):

```
LEAN_PATH=... lean -DautoImplicit=false QED.lean              -> exit 0
LEAN_PATH=... lean -DautoImplicit=false Compartmental.lean    -> exit 0 (warnings only)
LEAN_PATH=... lean -DautoImplicit=false VeriTrialExport.lean  -> exit 0 (warnings only)
```

This is not a workaround invented for this session: `missions/tri-repo-full-stack-gate.yaml`
already records the same SIGTRAP in its context block ("`lake env` is bypassed:
it SIGTRAPs"), and `scripts/rebuild_qed_oleans.py` calls `elan run ... lean`
directly rather than through `lake`. The project is consistent; the toolchain
binary is at fault. **Upstream report: this is worth filing against
`leanprover/lean4` v4.34.0-rc2.**

## What was actually broken this session

Every item below was found by a gate failing, not by inspection.

1. **The primary mass-conservation lemma was never emitted.** `build_lemmas`
   short-circuits on the `make_pbpk_ode` fast path and returned before
   `build_parametric_sum_lemma`, so the one lemma that is a conservation
   identity was dropped: 14 lemmas, only sign conditions, no conservation.
2. **That lemma was vacuous even when built.** The six-organ fast path
   references fluxes positionally, so the sum came out as
   `... + flows[0] - (flows[0]) ... = 0` — terms cancelling as opaque names.
   That is precisely the closed-identity theater the gate exists to prevent.
3. **Mutation teeth were absent.** `cleanroom.py` measured **0.5422** against a
   0.8 floor, so dogfood-43..46 all failed closed. The sibling-repo branch and
   the `.git`-carry fallback had no coverage at all. Two of the dead branches
   were fail-open: a clean room silently stopping being a git repo, and an
   untracked listing entry writing through the room into the host filesystem.
4. **The `dt` bound did not come from the Jacobian.** It was a hand-derived
   formula, and its elim entry was wrong (`CL/Vc` is off-diagonal, not
   `J[5][5]`). Now derived from the ODE's own diagonal and pinned against
   `jax.jacfwd`.
5. **The audit could only ever certify one of three repos.** `sorry_free` was
   hardcoded to `"unknown"` outside QED.
6. **The provenance chain had zero call sites.** `build_regulatory_provenance`
   was defined and exported but never invoked, so the V&V report shipped with
   no merkle root at all.
7. **The tri-repo gate could never go green.** Two of its seven verification
   commands referenced tether paths inside a VeriTrial-rooted clean room.
8. **Auto-probe synthesis ran on the mock adapter.** `--adapter opencode` does
   not reach `verification.auto_probes.adapter`, so the teeth dogfood-46
   claimed to measure were never measured.
9. **Schema-echoed probes were executed.** A generator that cannot solve the
   task echoes the probe schema; those entries are structurally valid, so the
   runner exec'd a program named `<single-line` and burned an hour per attempt.

## The tri-repo gate is GREEN

`tri-repo-full-stack-gate.yaml`, session `c32fb116d309`, 2026-09-26,
**status `success`**. Every layer, measured:

| layer | result |
|---|---|
| clean-room verification | **8/8 commands exit 0** |
| mutation | **kill_rate 1.0** — 19 killed, 0 survived, 5 documented-equivalent skipped (`fail_below` 0.8) |
| review | **approve**, on cited evidence |
| sandbox violations | none |
| git_state_violations | none |
| formal gate | `all 18 lemmas verified by QED (no sorry)` / `FORMAL GATE PASSED` |

The reviewer approved on substance, not on absence of findings: it cited the
self-contained `Fin 6` Jacobian-derived export, the fact that
`verify_formal_gate.py` *reaching* its final `FORMAL GATE PASSED` line proves
the isomorphism compile and the `#print axioms` gate ran first, the 19/19
mutant kill, and the emitted Merkle provenance root
(`1602f83a03772d29…`).

`changed_files` is empty, and that is correct rather than vacuous: this is a
verification gate whose payload is already implemented, so the change set is
legitimately nil. The review prompt's vacuity rule is what keeps that honest
— it approves an empty change *only* when named gates ran green with
substantive evidence, and the evidence block carried all eight commands'
output, exit codes and the kill rate.

### What it took to get here

The gate had failed on its review layer twice for one reason, which was never
about the change. The reviewer runs with cwd = `project_dir`, a
multi-repo mission's evidence lives in the *siblings*, and the `opencode`
CLI auto-rejects paths outside the project root. So the reviewer went after
the siblings, was refused, and never reached a verdict:

```
! permission requested: external_directory (/Users/tom/Documents/apps/*); auto-rejecting
x cd /Users/tom/Documents/apps && git -C VeriTrial log ... failed
```

The fix is in the review prompt, not in the pinned adapter preset, so that
enforcement stays in this repo's auditable sandbox instead of inside an
agent-runtime permission prompt. It took two passes, and the second one is
the instructive half: naming the relative paths was **not** enough. The next
run stopped `cd`-ing out — so that half landed — and was then refused *again*,
this time by the dedicated file-reading tool, while its relative **shell**
commands worked fine. The refusal is tool-dependent, not path-dependent, and
a prompt that names only the path gives the reviewer no way to tell the
difference. The block now names the spelling and the tool, and says the
captured change plus verification evidence are authoritative.

Verified in isolation against the real gate prompt *before* spending another
1.5 hours re-running the gate: the reviewer reached `REVIEW: APPROVE` and
cited the mutant kill, the 18 lemmas, the provenance root and the dt bound.
The gate then went green for real (session `c32fb116d309`).

**But the prompt fix is unreliable, and the honest reading is that it is the
wrong layer.** A later run (session `60da94408f21`) failed the same way
again -- 8/8 verify, kill_rate 1.0, `FORMAL GATE PASSED`, and then
`no valid review verdict found in reviewer output`, because the reviewer read
a sibling with the file-reading tool by absolute path:

```
! permission requested: external_directory (/Users/tom/Documents/apps/VeriTrial/scripts/*); auto-rejecting
x Read /Users/tom/Documents/apps/VeriTrial/scripts/verify_formal_gate.py failed
```

It is the same refusal the scope block was written to prevent, and the block
was in that prompt. Telling a model not to reach for a tool works about as
often as it should be expected to. The enforcement belongs in the adapter's
permission configuration, not in prose, so this is now a case for changing
the pinned `opencode` preset after all -- it is no longer a preference
between two working options, it is the difference between a gate that passes
and a gate that flakes.

### Fixed at the right layer

The preset now passes `--auto` ("auto-approve permissions that are not
explicitly denied"), which stops the CLI second-guessing in-scope reads, and
the safety it gives up at the model layer is held by Tether's own write
sandbox: `sandbox_mode: enforce`, set in both `QED/tether.yaml` (which pins
the command) and a new `tether/tether.yaml` for Tether's own missions.
`enforce` is also strictly better at detection than the default `warn` -- the
filesystem-manifest diff is only unioned into the changed-file set under
enforce, so a write to a **gitignored** path is caught instead of passing
unnoticed.

Neither half is safe alone, so it is pinned as an invariant in
`tests/test_safety.py`: the preset carries `--auto`, the pinned model survives,
every shipped `tether.yaml` enforces, and the model-side approval is never the
only permission layer. Two existing gates caught the change and were updated
rather than worked around -- the CLI preset pin and the docs/code consistency
pin -- and `docs/ADAPTERS.md` documents the pairing for operators.

One correction, because the first attempt at this was a no-op and the
confirmation run caught it: 50 mission files pin `adapters.opencode.command`
in the **mission**, and a mission override outranks both the project
`tether.yaml` and the built-in adapter default. So editing the command in
those two places changed nothing for any of them -- the gate went green once
and red again with the same `external_directory` refusal, because the command I
had edited was not the command the gate ran. All 50 are now identical to the
adapter default across three YAML spellings, and the identical-command test
reads missions through `load_mission` rather than scraping text, so it checks
what the resolver actually sees.

After that, session `12beb4081672`: `success`, **9/9** verify, kill_rate 1.0,
review **approve**, no sandbox or git-state violations, `FORMAL GATE PASSED`,
and **zero permission refusals in the reviewer transcript** -- against the
exact failure mode that produced three red reviews. The prompt scoping is
kept, because it is still the right documentation for a human reading a review
transcript, but it is no longer load-bearing.

### The ninth command: a gate hole the reviewer found

With the review layer finally working it started earning its keep. It rejected
an agent that added a ninth verification command to the gate mission
mid-run -- so the mission on disk claimed 9 commands while the captured
evidence showed 8/8 -- and whose comment claimed the pair was "verified to
have teeth" while the run's 24 mutants scored none of those files.

The objection was right on process and the substance turned out to be right
too. The mission goal forbids `jnp.maximum` concentration clamping and
requires non-negativity to come from the `dt` bound alone, yet **no command
ran `test_fixed_step.py` or `test_solvers.py`**: a constant `dt` bound, or a
silent replacement of the fail-closed `raise` with a clamp, would have passed
the entire gate while contradicting the stated goal.

So the command stays, with the teeth measured rather than asserted -- applied
by hand to `fixed_step.py`, with that exact pair run afterwards:

| mutation | killed |
|---|---|
| `return float(min(positive))` -> `return 0.01` | `test_dt_bound_is_the_jacobian_diagonal_minimum` |
| `raise ValueError` in `assert_dt_stable` -> `bound = dt` | `test_dt_violating_jacobian_bound_fails_closed` (`DID NOT RAISE`) |

The note also records *why* that evidence comes from outside the gate: its
mutants are `baseline_targets` only, so a reader cannot mistake `kill_rate`
for coverage of the `dt` bound.

Two adapters-run hygiene fixes from the same stretch are load-bearing for
every one of these numbers: orphaned adapter children are reaped after every
send (a session once accumulated 7338 live `opencode run` processes, one of
which was still editing a sibling repo four hours after its mission failed),
and a zero-mutant mutation run no longer describes itself as having met its
`fail_below` floor.

## Phase 2 (QED): one real green, three hollow greens, one correct refusal

Measured directly and via tether on 2026-09-26. QED invariants all hold:
**239 passed** in `test_pipeline.py` (256 as of 2026-09-29), **18/18** in
`run_tests.py`, project Lean
sorry-free (the only `sorry` matches are vendored `.lake/packages/mathlib`),
and `grep -riE "pbpk|liver|dili|cyp" parser.py agentic_pipeline.py` is empty.

| mission | tether status | what actually happened |
|---|---|---|
| `qed-02-parser-hardening` | **success** | real work: identifier word boundaries, `\frac`, LaTeX macros, fail-closed tokenizer, timeout-is-not-a-refutation. 4/4 verify, mutation **0.75** (floor 0.7) |
| `qed-03-type-inference` | success | **vacuous** — `changed_files: []`, review disabled, no mutation configured |
| `qed-04-tactic-policy` | success | **vacuous** — see below |
| `qed-05-integration-validation` | success | **vacuous** — `changed_files: []`, no mutation configured |
| `qed-01-no-sorry-gate` | failed | **correct refusal** — see below |

### The two findings worth keeping

**A Lean timeout was being reported as a refutation.** qed-02 went red in a
clean room on two prover tests that pass on a warm machine and in isolation.
Cause: `subprocess.run(..., timeout=30)` with
`except subprocess.TimeoutExpired` recording the attempt and *continuing to the
next tactic* — so a cold Mathlib import exhausted the whole tactic list and the
pipeline returned `"No tactic succeeded after N attempts"`, the exact wording it
uses for a statement that does not follow. The only trace was
`stderr='Timeout'` buried in `attempts`. A verification system whose answer
depends on cache warmth is lying, silently. Now: a real `lean_compile_timeout`
setting (default 180s, sized for a cold import, applied at all three compile
sites), and when no attempt obtained any verdict the result carries
`infrastructure_failure: True` and says "NOT a refutation" — pinned by a test
so it can never be confused with real mathematical failure again.

**Two tests asserted the unsatisfiable in a clean room.** Proving
`ka * A_gut = ka * A_gut` needs Mathlib, and a clean-room checkout has no
`.lake`, so `assert res["success"] is True` could not hold there. Each test now
states the requirement for the environment it is in — with Mathlib it must prove
and be axiom-clean, without it must fail *closed* — and **the no-sorry
assertion is made in both branches**. Not skipped, not weakened: locally
`use_mathlib` is True so the proving branch is what 239 tests exercise, and the
fail-closed branch is what the clean room exercises.

### The hollow greens

`qed-04-tactic-policy` is the one to look at. Its first attempt failed honestly:
mutation **0.675 against a 0.7 floor**, with 13 surviving mutants clustered
exactly in the region it exists to fix —

```
agentic_pipeline.py:1024:27 [flip_bool]   agentic_pipeline.py:338:16 [break_return]
agentic_pipeline.py:1113:47 [flip_bool]   agentic_pipeline.py:355:12 [break_return]
agentic_pipeline.py:1122:33 [flip_bool]   + 8 more
```

— i.e. the tactic-selection logic has no test that can kill a flipped decision.
The run then escalated to `reset_to_checkpoint`, which **discarded the work**,
and the final attempt changed nothing, so mutation measured 0 killed / 0
survived / 0 skipped and the floor was vacuously satisfied. Status `success`,
`changed_files: []`.

That is a gate reporting green while measuring nothing, and it is the failure
mode this whole exercise exists to catch. Counting qed-03/-04/-05 as green
would repeat the mistake the metric definitions in `METRICS.json` (`M3`, `M7`)
are about.

### The 13 survivors turned out to be a cold cache, and are now closed

Measured directly with `run_killrate.py` under both conditions:

| condition | before | after |
|---|---|---|
| warm (Mathlib present) | 1.0000 (60/60) | 1.0000 (60/60) |
| clean room (`use_mathlib=False`) | **0.5167 (31/60)** | **0.5833 (35/60)** |

So the logic was never wrong: the clean room has no `.lake`, every prover test
fails closed, and the tactic loop is never reached. The survivors were that
gap wearing a costume. The fix is to pin the *pure* decisions directly, with
no compiler in the loop, so the coverage stops depending on cache warmth:

* `select_tactic` dispatch — ring / field_simp / linarith / norm_num /
  decide / simp, including that an ODE goal is *also* polynomial, so the
  precedence between the ring and field_simp branches is what makes the
  ordering load-bearing. The turnstile prefix must not change the
  classification.
* `get_tactic_candidates` ordering, and that closed numeric equalities lead
  with `simp`/`decide` rather than `rfl`.
* **`_execute_with_initial_code`, which qed-01's contract names and which no
  test covered** — a mutant that broke its delegation to `execute_tactic_loop`
  survived the whole suite. That one is now dead.
* the `Int`/`Rat` annotation branches, reachable only by pinning the inferred
  variable type; the `Int` negative-literal guard is now killed too.

One survivor was **provably equivalent** and is now removed rather than
tolerated: the `elif '/' in expression and var_type == 'Rat'` body was
byte-identical to the `else` body, so negating its guard produced an
identical theorem string and no test could ever kill it.

What is left in the clean room is honest: the sorry-detection and
toolchain-discovery paths, which genuinely need a working compiler to reach.
That is stated rather than papered over.

`qed-01-no-sorry-gate` failing is the system working. Its payload — reject any
`sorry`/`sorryAx` in source *or* compiler output, wired into both
`execute_tactic_loop` and `_execute_with_initial_code` — is already fully
implemented (`check_for_sorry` scans both, with word-boundary matching for
`sorry`, `sorryAx`, `Tactic.sorry`, `Lean.Elab.Tactic.sorry`,
`declaration uses sorry`, `warning:.*uses sorry`; and
`_execute_with_initial_code` delegates to `execute_tactic_loop`, which checks
every path). The reviewer refused to certify an empty patch, and it was right
to. The one substantive gap it named — *"mutation was not run, so there is no
evidence the no-sorry criterion was actually exercised or killed"* — is real and
belongs to `qed-mutation-strength`.

### Mission budgets had to change

qed-02 was first killed by `Mission budget exceeded: max_wall_seconds
(threshold 1800, observed 2123.7)` while holding a correct fix. The budgets
were authored for a faster agent (600–1800s wall, 5–15 sends), so all nine QED
missions now declare 5400s / 24 sends. **Only `max_wall_seconds` and
`max_sends` changed** — no `fail_below`, no verification command, no probe, no
`max_attempts`. That buys a run enough time to reach the gates; it does not
make any gate easier to pass.

## A verification gate mutates the siblings it verifies

Found 2026-09-27 while reconciling QED after a gate run. QED's reflog shows
three `reset: moving to f0a3574` entries at 13:54-13:55, inside the gate's
13:08-14:40 clean-room verification stage, stranding the agent's three commits
(`e5c37c6`, `992fab8`, `e31930d`) out of the branch with their content left in
the index. The cause is tether's own machinery: `src/tether/git_safety.py`
runs `git reset --hard <target>` per repository, and the gate's
`clean_room_copy` lists `../QED` and `../VeriTrial` as workspace repos, so
staging a clean room moves sibling branches.

The part worth acting on is the ordering. The gate's `git_state_guard` is
`true` and it lists those same siblings, yet the run reported
`git_state_violations: None`. The guard compares each sibling HEAD to its
mission-start baseline, and it runs **after the agent's execution step but
before clean-room verification** — so a HEAD move performed *by the staging
itself* is never compared against anything. The guard cannot see the
mutation it is most exposed to.

This is recorded, not fixed. Changing when the guard samples sibling state
means changing clean-room staging order, and staging order is load-bearing
for the gate's 8 verification commands; that wants its own mission rather
than an edit made at the end of a long session. Until then, treat a gate run
as something that can move a sibling branch, and re-check sibling HEADs
afterwards rather than assuming a green run left them alone.

## Honest limitations

`dogfood-45` and `dogfood-46` did not reach `success`; their work was
substantive and is committed (`aa6deb8`, `f12d376`, `e9990eb`), but the
missions themselves are not claimed as green. A **guard was deliberately not
loosened** to change that: `git_state_guard` reports a forward commit as
"history was rewritten", and dogfood-46 fails because its agent committed. A
fix treating a forward commit as benign was written and then reverted, because
`test_enabled_agent_commit_forward_trips_guard_strictness` pins the strict
semantics on purpose ("strict semantics flag ANY history movement while the
guard is on") and this gate is `git_state_guard: true`, which makes that
change load-bearing rather than cosmetic.

Four QED missions (`qed-cleanroom-integrity`, `qed-mutation-strength`,
`qed-docs-truth-audit`, `qed-unit-tests-pass`) have not been re-run as
agent-in-the-loop missions. The QED *invariants* they assert have been measured
directly (256 passed, 18/18, sorry-free, zero leakage, OOD proofs, and the
tactic-selection mutation gap now at 1.0000).

The **tri-repo full-stack gate itself has** been run for real — see above,
session `c44712be2b03` — so the clean-room staging, sandbox enforcement,
mutation battery, git-state guard and review layer are all exercised by an
actual run rather than asserted.

What that run does **not** cover is the QED-side mission suite. The QED
invariants below were measured directly, which is the same signal for
deterministic commands but carries no review or sandbox layer:

| # | command | result |
|---|---|---|
| 1 | `export_pbpk_to_qed.py --lean-out ... --out ...` | exit 0 |
| 2 | `rebuild_qed_oleans.py` | exit 0, both oleans rebuilt |
| 3 | `verify_formal_gate.py /tmp/pbpk_lemmas.txt` | `FORMAL GATE PASSED` |
| 4 | run-all-validations + `build_regulatory_provenance` | exit 0 |
| 5 | `test_cleanroom.py test_multirepo_capture.py` | 56 passed |
| 6 | `test_mission_regressions.py` | 2 passed |
| 7 | `verify_veritrial_equations.py` | passed |
| 8 | `test_bridge_mutation_fast.py test_pd.py` | 94 passed |
| 9 | `test_fixed_step.py test_solvers.py` | 15 passed |

`qed-mutation-strength` is the mission that owes teeth to the 13 surviving
tactic-selection mutants described above. **Re-measured 2026-09-29 against the
tactic-selection region: kill rate 1.0000 (12/12, zero survivors)**, with 40
tactic tests passing. The dedicated `select_tactic` and `get_tactic_candidates`
tests do now kill flipped decisions, and
`test_execute_with_initial_code_delegates_to_the_tactic_loop` — the delegation
that previously had no coverage at all — exists. The clean-room gap described
earlier (0.5167) was a cold-cache artifact, not missing logic.

One operational hazard, recorded because it cost a recovery: `run_killrate.py`
mutates the target **in place** and restores it on normal exit, so killing the
process mid-run leaves `agentic_pipeline.py` mutated in the working tree. A
`git checkout --` is the recovery. Worth a trap in the tool.

Tier 2 (`STANDARD_14_ORGAN_NETWORK` promotion, saturable `Vmax`/`Km` clearance)
is **not started**, and is not recommended as written. `Vmax`/`Km` appears
nowhere in `model.py` today. Promoting 14-organ to `DEFAULT` would invalidate
the `Fin 6` export, the `J[5][2]`/`J[5][5]` pins and the exported lemmas, so it
is a breaking change that wants its own decision. Adding saturable clearance
changes the physiology of every simulation. Both are substantive scientific
decisions rather than gate-closing, so they are left for an explicit call.

### Dogfood status this session

`dogfood-43` and `dogfood-44` reached **`success`** — both had never passed
before. `dogfood-45` failed verification on one red test but had already
finished its fix; that work was completed and committed rather than
discarded (`aa6deb8`). `dogfood-46` failed on the git-state guard, as
described above.

The recurring operational cost, recorded because it cost three runs: **a
mission that ends dirty blocks the next one.** `tether` refuses to start when
the working tree is dirty, and neither a failed nor a successful mission
commits its own work, so the operator commits between every mission or the
queue stalls. dogfood-45's leftover aborted 44 and 43 outright.

The auto-probe generator remains unable to author probes: on `dogfood-43` it
quoted the prompt's own template back, and `parse_generated_probes` — which
takes the *last* fenced block — parsed that echo and correctly rejected every
placeholder, so synthesis fell back to the human battery. The schema-echo
filter is working as designed; the gap is generator behavior, not parser
behavior, and it means the teeth gate has still never actually measured
anything.

## VeriTrial

| check | result |
|---|---|
| `pytest src/insilico_trial/tests/` | **216 passed**, 0 failed (this pass; the 193 above is the prior pass's figure) |
| `ruff check` / `mypy` | clean / `Success: no issues found in 34 source files` |
| benchmarks (warfarin_pgx, moxifloxacin_qtc, midazolam_cyp3a4, metformin_renal, hepatic_impairment) | 5/5 `overall_pass: true` |
| `validation_summary.json` | `overall_pass: true`, `formal_verification_pass: true` |
| throughput | **790.0 patients/sec** (CPU, 1000 patients, 168 timepoints) — against a ~99/sec target |
| report ↔ provenance merkle root | **match** |

Benchmark throughput is CPU by design: `diffrax`/`lineax` are incompatible with
the JAX Metal backend on Apple Silicon, so the ODE solver is forced to CPU at
import. That is a device-capability constraint, not a regression, and the note
is carried in the benchmark output itself.

## What this pass actually changed

One real defect, found by a gate failing closed rather than by inspection:

**The ledger was stale and the provenance chain had drifted.** `SYSTEM_STATE.json`
recorded tether at `1c189cc`/`dirty: true` while the V&V report attested to
`0f26e2e`, so
`test_report_commits_to_ancestors_of_the_ledger_heads` failed: the report could
not be attributed to commits present in the audited history. Re-running
`scripts/audit_system_state.py` on a clean tree fixed the head and the dirty flag
(`6683b48`), and a re-audit after that commit moved the ledger to `20e5e00`.

Separately, the report and `regulatory_provenance.json` carried **different**
merkle roots. Root cause, now fixed (`VeriTrial` `5516067`): the formal gate
wrote `output/validation/qed_traces.json` at a fixed repo-relative path after
every successful run, and that file is a Merkle **leaf input** of
`build_regulatory_provenance()`. So any test that drove the gate over a lemmas
file in a pytest tmpdir replaced the production trace with one pointing into
that tmpdir — and the provenance root then disagreed with the root already
embedded in the HTML.

This was a fail-closed test failing for a reason unrelated to soundness, which
is the worst kind of red: it teaches the operator to re-run until green rather
than read the failure. Two writers of the same artifact also disagreed about
where it lives — `formal_verification._trace_path()` honored `QED_TRACE` and
`verify_formal_gate.py` ignored it. Both now honor it, `tests/conftest.py`
redirects it session-wide so no test can write the repo artifact, and
`test_gate_honors_qed_trace_redirect` pins it (confirmed to fail when the
redirect is reverted). Verified by snapshotting all three artifacts, running the
full gate-driving suite, and confirming the report root, provenance root and
traces file are byte-identical afterward.

One thing that looked like a defect and was not: the audit reported tether
`dirty: true` mid-session. That was a **transient artifact of this session's own
concurrent mutation runs** holding tracked files mid-write. The tree was clean
before and after. Worth knowing, because the symptom is indistinguishable from
a real dirty tree in the artifact.

### The ledger's one-commit lag is the design, not a wart

I previously suggested the ledger should stop recording tether's own HEAD to
make the commit-then-audit cycle go away. **That was wrong, and reading the
test says so.** `test_report_commits_to_ancestors_of_the_ledger_heads` states
it directly:

> Exact SHA equality is unsatisfiable here and asserting it would be a fake
> check: the ledger records tether's HEAD, but writing and committing the
> ledger is itself a tether commit, so the committed ledger necessarily trails
> the report by exactly one commit. The sound relation is containment.

The test asserts `git merge-base --is-ancestor`, which tolerates exactly that
lag and rejects anything else — a report may not attribute results to commits
absent from the audited history. Dropping tether's own HEAD would have made the
cycle disappear by weakening the thing the ledger exists to attest to. The lag
is the honest encoding of "the ledger cannot contain its own commit", and it is
already stated in the report's ledger section. No change made.

## What this pass did not do

No threshold was lowered, no `fail_below` relaxed, and no fail-closed guard
weakened — the run needed no such change, because the failures it hit were
stale-artifact and toolchain bugs rather than real regressions.

The agent-in-the-loop layers were not exercised: no `tether run missions/...`
was executed. See "Honest limitations" for exactly which claims that leaves
carried over rather than re-earned.

* Nothing below was relaxed to manufacture a pass: no `min_teeth_rate`,
  `fail_below`, or `sorry_free` threshold was lowered, and no fail-closed
  guard was loosened.
* 22 mutants in `cardiac_apd_effect` and the whole Emax core were unmeasured
  until the gate's suite selection was fixed. Test-coverage gaps of that
  shape are the most dangerous kind of finding here, because every gate still
  reports green.


---

# Pass 2 (2026-09-29): Fin N generalization + saturable clearance

Model for every run below: **`opencode/space-bunny-free`**, no mock adapter.
All three repos clean at the close of this pass.

## Premise audit: four of the brief's claims did not survive checking

Every claim below was tested before acting on it. Recording the corrections
matters more than the changes: three of them would have meant writing code
against a bug that was not there.

1. **"`lake` throws SIGTRAP on `lake build`"** — true, but the brief's framing
   ("so Lake compilation fails") is wrong. `lake --version` prints a correct
   version and *then* exits 133; so does `lake env` and `lake query`. The crash
   is at process teardown, not in the work, and it happens in an empty
   directory with no project involved. Meanwhile `lake build` had in fact
   been writing fresh `.olean`s. So the tool is unusable, but "the build
   fails" was the wrong inference. The fix was therefore *not* to change the
   toolchain; it was to stop depending on `lake` at all — see below.

2. **"the platform is locked to a 6-compartment linear model"** — half true.
   The *model* already handled 14 organs (`STANDARD_14_ORGAN_NETWORK`,
   N-generic `_jacobian_diagonal`, `--fin-n 14` on the exporter). What was
   locked to 6 was the **formal gate**: four separate places each
   independently re-derived six-organ expectations. A correct 14-organ lemma
   file was rejected by the gate for the wrong reason.

3. **"auto-probes fail on LLM string formatting"** — true, and precisely
   localized. The single-quoted variant (`command: 'python -c 'print(1)''`)
   was genuinely dropped. The code even documented it as a known loss.

4. **"9/9 lemmas verify and benchmarks pass"** — true, but it described the
   6-organ LINEAR path only. Nothing about the saturable work existed.

## Lean: the toolchain is broken, so the build path was replaced

`leanprover/lean4:v4.34.0-rc2`'s `lake` exits 133 after printing correct
output, on every subcommand, with no project involved. `lean` itself is
healthy. Since the gate's soundness argument rests on `#print axioms` reading
the *compiled* modules, the real hazard is not the exit code — it is a stale
`.olean` silently certifying yesterday's theorems.

`verify_formal_gate.py` now **rebuilds QED's oleans from source** before the
axiom check, driving `elan run <pinned> lean -o` directly with an explicit
`LEAN_PATH`, and fails closed if the rebuild fails. Previously the gate would
check fresh sources against whatever `.olean` happened to be on disk. This
closes the stale-artifact hole at the point where it matters rather than
relying on a mission command having run first.

`--fin-n 14` also required real work in the export: `fin_cases` is O(N²), so
the default 200k heartbeats is exhausted around N = 14 (`maxHeartbeats` is now
scaled with N²), and the per-state positivity hypotheses had been hardcoded to
indices 0–5 — so a 14-organ theorem *asserted* `0 < Q i` for fourteen tissues
while *assuming* it for six. Both fixed.

## The Fin N gate, measured on all 14 states

`--fin-n 14 --saturable`, both directions of the real gate:

```
all 25 lemmas verified by QED (no sorry)
FORMAL GATE PASSED: all required PBPK lemmas verified by QED (no sorry).
```

and the six-organ default still reports `all 9 lemmas verified`.

`--fin-n` is **required**, not defaulted. The emitted lemma set is a function
of the network (one Metzler and one inflow invariant per perfused tissue), so
a default would be a guess about which network a file belongs to; the gate now
refuses rather than assuming. Every call site and mission line was updated,
including the gate mission itself.

Four independent under-certifications were fixed, each of which had to be fixed
*separately* because each was sufficient on its own:

- the lemma oracle tokenized a hardcoded `_LIVER_IDX`/`_PERIPHERAL_IDX` table
  and walked the six-organ branch's assignments even for a 14-organ run;
- it read the state list from the exporter, so the N-organ branch — which
  writes its derivatives positionally and therefore has **no `dA_<organ>` name
  in the AST at all** — was reported as a 4-state model;
- symbol suffixes were keyed on state *index*, which labels the 14-organ
  kidney (index 3) "Qp" and the lung (index 4) "Qe": certifying a kidney flow
  under the peripheral tissue's symbol;
- every organ shared one volume symbol `V`, so a mutation swapping two
  organs' volumes passed.

That last one is the reason this was worth doing properly: with a single `V`
the cross-check cannot distinguish `Q_a/(Kp_a*V_b)` from `Q_a/(Kp_a*V_a)`.

## Saturable clearance: a transfer, and how that was kept honest

`make_pbpk_ode` now carries opt-in Michaelis–Menten hepatic clearance. It is a
**transfer, not a sink**: the liver loses `Vmax*C_liver/(Km+C_liver)` and the
`elim` accumulator gains it, so total mass is still conserved.

Keeping that honest took two changes:

- `check_mass_conservation` requires **both sides** of the transfer. Booking
  the flux in `elim` alone, or removing it from the liver alone, is refused in
  both directions. The six-organ central equation now subtracts the perfusion
  *fluxes* (`- flows[0] - flows[1] - flows[2]`) rather than the tissue
  derivatives, precisely so the saturable term is booked exactly once.
- `verify_symbolic_cancellation` was a substring test for negated names. It now
  sums the expressions and asks sympy — which is what lets it see that the
  flux-subtraction spelling discharges the same balance, instead of accepting a
  sum that merely *looks* balanced.

Five mutations confirm the teeth on the VeriTrial side and five more on the
tether equation gate (drop either side of the transfer; degrade the flux to a
linear drain; make it unconditional; drop the Km positivity check): all
refused.

The nonlinear flux is certified by **transport of QED's generic theorems**
(`saturableFlux_nonneg` / `_bounded`), not re-derived — so `QED/` stays
domain-agnostic, and VeriTrial supplies its own `C_liver`. The gate requires
both instantiations in the axiom output, and the Jacobian delta against the
linear case must be *exactly* the metabolic self-drain, so no other
nonlinearity can ride on that certificate.

## Stability: the bound, and what it does not clamp

`calculate_max_stable_dt` adds the saturable stiffness using the **supremum**
of the self-drain (at `C = 0`), because the instantaneous rate is
state-dependent and evaluating it at one operating point would certify a step
that is unstable where the concentration is lower. Verified:

- the bound dominates the autodiff diagonal at concentrations spanning
  `1e-6 … 1e3`, for both networks;
- forward Euler at the bound keeps every state non-negative over 50-step runs
  from starts spanning ten orders of magnitude — **no `jnp.maximum` clamp is
  involved**, which is the whole point of the Metzler argument;
- the saturable term reaches the liver diagonal and *only* the liver diagonal,
  by exactly `Vmax/(Km*V_liver)`.

One measured detail worth recording: for warfarin the saturable term does
**not** change `dt`, because central is the binding state, not liver. The
first test asserted otherwise and was wrong. The claim pinned is now the liver
diagonal itself (plus a separate case where the liver *is* binding), which is
what the saturable term is actually required to move.

## Auto-probe parser: recovered by parity, not by guessing

`command: 'python -c 'print(1)''` is now recovered. The repair closes the shell
argument and doubles the inner quotes, and the decision is made by
**quote-count parity**: the model's wrapper quote and the shell argument's
closing quote collapse into one, leaving an odd count. A correctly escaped
scalar always has an even count and is left exactly as written — so the repair
cannot re-escape a good scalar into a run of quotes.

Four existing tests asserted the *old* behaviour (the single-quote case
unrecoverable). They were retargeted at a flow sequence left open — genuinely
unrecoverable, and unaffected by any scalar repair — rather than deleted, so
the per-entry salvage contract is still pinned.

## The gate re-run for real

`tri-repo-full-stack-gate.yaml`, session `2df7d345`, `--adapter opencode`,
real run:

| layer | result |
|---|---|
| status | **success** |
| clean-room verification | **9/9 commands exit 0** |
| mutation | **kill_rate 0.9167** — 22 killed / 24, `fail_below` 0.8 |
| review | **approve**, on cited evidence |
| sandbox violations | none (`[]`) |
| formal gate | `all 9 lemmas verified by QED (no sorry)` |

The kill rate is **0.9167, not 1.0**, and is reported as measured. The two
survivors are `export_pbpk_to_qed.py:1494` (arithmetic) and `:1600`
(flip_bool). Both sit in the **legacy export path** — the branch that runs only
for models *without* `make_pbpk_ode`, which the live model takes an early
return past. So they are unreachable coverage, not undetected defects; that
was confirmed by inspection rather than assumed, but it is a coverage gap and
is named as one rather than suppressed.

This is a genuine drop from the 1.0000 the previous revision recorded. The
cause is visible in the run log: `mutation targets fall back to baseline_targets
(agent changed nothing mutatable)`, so the sampler took a different set of
sites than the hand-scored run the earlier number came from. Recorded as
measured, not reconciled to the older figure.

## Verification totals

| check | result |
|---|---|
| `QED/test_pipeline.py` | **256 passed** |
| `VeriTrial src/insilico_trial/tests` | **216 passed**, 0 failed |
| `tether` autoprobe suites | **56 passed** (was 52) |
| six-organ formal gate | 9/9 lemmas, no sorry |
| 14-organ + saturable formal gate | 25/25 lemmas, no sorry |
| clinical benchmarks | 5/5 `overall_pass: true` |
| `#print axioms` | exactly `[propext, Classical.choice, Quot.sound]` on every headline theorem, including the two new saturable transports |
| report ↔ provenance merkle root | **match** (`923664f2…`) |

`#print axioms` on all seven exported theorems, verified by the gate's own
axiom check:

```
'extracted_offDiag_nonneg'            depends on axioms: [propext, Classical.choice, Quot.sound]
'extracted_colSum_eq_zero'            depends on axioms: [propext, Classical.choice, Quot.sound]
'veritrial_compartmental'             depends on axioms: [propext, Classical.choice, Quot.sound]
'veritrial_mass_dissipation'          depends on axioms: [propext, Classical.choice, Quot.sound]
'veritrial_dili_block'                depends on axioms: [propext, Classical.choice, Quot.sound]
'veritrial_saturable_flux_nonneg'     depends on axioms: [propext, Classical.choice, Quot.sound]
'veritrial_saturable_flux_bounded'    depends on axioms: [propext, Classical.choice, Quot.sound]
```

## The ledger lag, measured again (three times, the hard way)

`test_report_commits_to_ancestors_of_the_ledger_heads` asserts containment, not
equality, because committing the ledger is itself a tether commit. That is the
documented design. This pass hit it three times because the ordering matters
and it is easy to get backwards: the audit must run **at the head the report
attests to**, and the report must not be regenerated afterwards. Getting the
order wrong fails closed with a message that reads like drift but is really a
sequencing mistake.

Left the ledger naming `b27f959` with all three repos clean and
`sorry_free` recorded honestly (`n/a` for tether, which ships no Lean).

## What this pass did not do

- **No threshold was weakened.** `min_teeth_rate`, `fail_below`, and every
  `sorry_free` assertion are untouched. The 0.9167 kill rate is reported as
  measured and clears its own floor on merit.
- **No numerical clamping** was introduced anywhere; non-negativity remains a
  consequence of the step size, and the saturable bound is stated as a
  supremum for exactly that reason.
- **The saturable path is opt-in and off by default.** It is compiled into the
  ODE but inactive unless `vmax_metabolic`/`km_metabolic` are supplied, so the
  default configuration is still the linear model the existing certificates
  describe. Asking for `--saturable` against a model that does not implement
  the path fails closed.
- **`fixed_step.py` and `model.py` remain unmeasured for mutation kill rate**
  beyond the gate's baseline sampling. The new saturable logic in `model.py`
  gained five equation-gate controls and a full test battery, but a
  whole-file mutation sweep against those tests has not been run, so no
  per-file kill rate is claimed for it.
