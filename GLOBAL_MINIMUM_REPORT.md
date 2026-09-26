# Global Minimum Report — tri-repo (Tether / QED / VeriTrial)

Model for every Tether run in this session: **`opencode/space-bunny-free`**.
No mock adapter was used for any gate recorded below.

## Ledger

| repo | HEAD | dirty | sorry_free |
|---|---|---|---|
| tether | `141e20648f7c5d3a304f25e96457cd71b470199c` | false | `n/a` (ships no Lean) |
| QED | `a873a3dd90c7c5282ad54a5c3a28f8fb05867eb9` | false | **true** |
| VeriTrial | `8f14e72819d392e1b8ed19d6f7aca5fb75437120` | false | **true** |

`SYSTEM_STATE.json` merkle root: `442344c24adaee4320f012e05ff20d24c7be17962dfe12cf0f3746e152f46208`
V&V report `<meta name="merkle-root">`: `650b530c5a438dac6cda4a267ee04b6b3d9331b31b4cec10f7f02acbf8b6f266`

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

## Formal gate — 18/18, no sorry

```
all 18 lemmas verified by QED (no sorry)
FORMAL GATE PASSED
```

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
| `tether/src/tether/cleanroom.py` | `tests/test_cleanroom.py` | **0.9518** (79/83) |
| `VeriTrial/scripts/export_pbpk_to_qed.py` | `test_bridge_mutation_fast.py` | **1.0000** (40/40) |
| `VeriTrial/scripts/verify_formal_gate.py` | `test_bridge_mutation_fast.py` | **1.0000** (40/40) |
| `VeriTrial/src/insilico_trial/pd/__init__.py` | `test_pd.py` | **1.0000** (38/38) |
| `VeriTrial/src/insilico_trial/pbpk/model.py` | `test_fixed_step.py` | **1.0000** (40/40) |

`cleanroom.py`'s 4 survivors are **provably equivalent**: `break_return` rewrites
`return expr` to `return None` and `expr` is already `None` (73:8, 75:8); the
sibling target mkdir is preceded by an unlink/rmtree prelude on every branch, so
neither of its flags is reachable (254:41, 254:56).

The tri-repo gate measured **0.7727 against a 0.8 floor** in its last full run —
not because its mutants were equivalent, but because the suite it ran
(`test_mission_regressions.py`) could not kill them. The gate's own
`baseline_targets` are now measured against the suites that cover them, and all
four score 1.0. The three genuinely-equivalent gate mutants are documented in
the mission with their proofs.

## Zero leakage

`grep -riE "pbpk|liver|dili|cyp|ka_rate|c_tissue|kp|a_gut" parser.py agentic_pipeline.py`
→ no matches. QED's parser and pipeline are domain-agnostic.

OOD, verified with zero changes to QED: SEIR conservation, SEIR positivity,
3-tank cascade, matrix-entry equality — plus `run_tests.py` 18/18 and
`test_pipeline.py` 200 passed.

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

## Honest limitations

* **The tri-repo gate mission has not been re-run to a green `success`.** Its
  last full run failed at 0.7727 mutation; the four root causes are fixed and
  each fix is measured (table above), but a complete 8-command run costs many
  hours and was not repeated. The `SUCCESS` claim for that mission is
  therefore **not** made here.
* **dogfood-43, -44, -45 did not pass; dogfood-46 did** (session
  `d7626ecb8028`, non-empty 6849-byte `patch.diff`, no sandbox violations,
  mutation 7/12 = 0.583 ≥ 0.5). 43 failed because the reviewer emitted no
  parseable verdict; 45 because the reviewer correctly rejected a cosmetic
  diff with unsubstantiated mutation evidence; 44 because I ran the audit
  against a repo with a mission in flight and dirtied its tree — the sandbox
  correctly rejected the resulting write. Under this model these payloads are
  already implemented, so the agent has no substantive change to make, and the
  review gate — working as designed — refuses to certify a vacuous diff.
* Every failure above was a **fail-closed** outcome. Nothing below was relaxed
  to manufacture a pass: no `min_teeth_rate`, `fail_below`, or `sorry_free`
  threshold was lowered.
* 22 mutants in `cardiac_apd_effect` and the whole Emax core were unmeasured
  until the gate's suite selection was fixed. Test-coverage gaps of that shape
  are the most dangerous kind of finding here, because every gate still
  reports green.
