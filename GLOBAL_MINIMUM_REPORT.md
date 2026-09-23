# GLOBAL MINIMUM REPORT — Tri-Repo Convergence

> **FINAL: MISSION SUCCESS (2026-09-20).** `tri-repo-full-stack-gate` passed
> end-to-end in clean-room isolation with adversarial review approval (tether
> session `22952746c5d3`): 5/5 verification green, mutation kill-rate 1.0
> (11/11 effective, 9 documented-equivalent skipped), review verdict `approve`.
> Provenance Merkle `640e7aad…` emitted into the validation output and sealed
> in `VeriTrial/output/vvv40_report.html` (`<meta name="merkle-root">`).

## Proofs
- QED `Compartmental.lean`: Metzler / column-sum / mass-dissipation theorems, zero `sorry` (comment-stripped audit: `sorry_free: true` in `SYSTEM_STATE.json`).
- `QED/VeriTrialExport.lean` regenerated from live `model.py` via symbolic AST differentiation (`extracted_matrix` if-chain synthesized from `compute_jacobian`, not aliased to `pbpkK`); DILI 9-state block delegates its 6×6 block to `extracted_matrix` (structural `pbpkDiliSystem` link).

## Metrics
- Tether `tests/test_workspace.py`: 5 passed.
- VeriTrial `test_sign_flip_dynamically_alters_lean_and_fails_gate`: passes (sign flip → `SystemExit` fail-closed).
- Export: 18 lemmas; Jacobian spot-checks: `J[0][0]=-ka`, `J[1][1]=-Ql/(Kpl*Vl)`, `J[2][2]=(-CL-Qe-Ql-Qp)/Vc`, `J[5][2]=CL/Vc`.
- Merkle root: see `SYSTEM_STATE.json` (`merkle_root` over per-repo HEAD+dirty+sorry state).

## Closed gaps
1. **Static templates → AST differentiation**: `emit_lean_export` body and `extract_column_sum_lemmas` list are now computed from `pbpk_ode` AST (`_sym_diff` product/quotient rules + sympy simplification).
2. **Single-repo capture → atomic multi-repo workspaces**: `_gate_and_capture` unions `workspace_repos` changes, persists `patch_<repo>.diff`/`untracked_<repo>.txt`; `materialize_clean_room` uses `git archive` + patch per sibling (no dirty `copytree`).
3. **Emax fallbacks → mechanistic QSP**: ALT sampled from solver idx 8, bilirubin from GSH idx 6 (`_last_gsh_batch`); `_qsp_*_from_exposure` methods deleted; SDIRK2 path also fills 9-state DILI trajectories.
4. **Point estimates → Bayesian coupling**: `cmd_demo` runs NumPyro NUTS `calibrate_pk_1comp` (100 samples) + `posterior_predictive_pk` into `result.uncertainty`; `cmd_validate` attaches posterior summaries to the V&V payload.
5. **Ledger**: `scripts/audit_system_state.py` → `SYSTEM_STATE.json` with per-repo HEAD/dirty/sorry-free + SHA-256 Merkle root.

## Verification status (2026-09-19)
- `verify_formal_gate.py`: **FORMAL GATE PASSED — all 17 lemmas verified by QED (no sorry)**; SHA-256 traces in `VeriTrial/output/validation/qed_traces.json`.
- DILI isomorphism fixed: `extracted_dili_matrix` mirrors `pbpkDiliSystem`'s Prop-`dite` guard byte-for-byte in structure; the full-function equality is proved by `by_cases` + `dif_pos`/`dif_neg` + congruence with the 6×6 AST theorem (a `fin_cases`-only proof could not reduce the `dite` guards — gate caught this, now closed).
- Tri-repo mission `tri-repo-full-stack-gate`: **success** in clean-room isolation (tether session `b228fbb76950`): export ✓, direct-lean olean rebuild ✓, formal gate ✓, full validation + NUTS ✓. Two environment bugs found and fixed en route: clean-room `.lake` cache absence (now carried as pinned build env + rebuilt with direct `lean`, since `lake build` SIGTRAPs) and elan toolchain resolution outside a toolchain dir.
- `make validate`: **overall_pass true** — all 5 benchmarks (warfarin n=300, moxi, midazolam, metformin, hepatic) + formal gate.
- Merkle root `cd65963e…` embedded in `VeriTrial/output/vvv40_report.html` (`<meta name="merkle-root">`), tied to tether session `b228fbb76950` via `build_regulatory_provenance`.
- Merkle root in `tether/SYSTEM_STATE.json` (QED `sorry_free: true`).

## Mutation kill-rate accounting (real CLI execution, per-file evidence JSON)
| Changed code | Suite | Result |
|---|---|---|
| tether `orchestrator.py` + `cleanroom.py` + `audit_system_state.py` (new-code lines) | 4 pytest files, 48 tests | **24/24 = 1.0** (`mutation_evidence_tether.json`) |
| VeriTrial `export_pbpk_to_qed.py` (new-code, non-equivalent) | export + formal gate (Lean) | **8/8 = 1.0** (`mutation_evidence_export.json`) |
| VeriTrial `engine.py` + `cli/__init__.py` (sampled new-code) | test_engine + test_safety | **1/1 = 1.0** |
| VeriTrial `rebuild_qed_oleans.py` (whole file is new) | 7 unit tests (incl. real rebuild) | **23/25 = 0.92** (`mutation_evidence_rebuild.json`) |

- Documented equivalent mutants (excluded, with proof): `_sym_diff` defensive fallthroughs (466/469/471 — execution trace shows 0 hits across full export; unreachable for current `model.py`, pinned by unit tests); rebuild capture-flag flips (77:54/65 — exit-status-preserving, script gates solely on returncode).
- Genuine holes found BY mutation testing and closed: gate oracle imported the mutated bridge (self-consistent) → added independent sympy term-accounting cross-check to `verify_formal_gate.py` (kills term-dropping exporter mutants fail-closed); rebuild error paths returned success-capable codes → `raise SystemExit(1)`; sibling `HEAD` fallback → fail-closed `CleanRoomError`; ledger root discovery → fail-closed `WorkspaceNotFoundError`.

## Verification status (2026-09-20) — verification-theater elimination round
- **Solvers (Task 1):** cosmetic no-op `jnp.where(mass_drift < 1e-6, y_next, y_next)` replaced with fail-closed NaN poisoning on mass **creation** in `fixed_step.py` and `solvers.py` (pure-abs form would NaN every legitimate CL-dissipation step); `test_solvers.py` indices aligned to `model.py` (`_CENTRAL_IDX=2`, `_PERIPHERAL_IDX=3`, `_EFFECT_SITE_IDX=4`); 5/5 solver tests pass.
- **Lean export (Task 2):** `build_structural_theorem` no longer emits a `--` comment (discarded by the gate): it returns the full genuine `theorem veritrial_mass_dissipation` (transport of `mass_dissipation_rate` via `veritrial_model_matches_pbpkK`), also appended by `emit_lean_export`; `#print axioms` on all three export theorems reports only `[propext, Classical.choice, Quot.sound]`. `Compartmental.lean` gains `sdirk_stage_mmatrix_offdiag` (M-matrix stage operator → SDIRK2 non-negativity). QED `run_tests.py`: 18/18, zero `sorry`.
- **Toolchain (Task 3):** `lean-toolchain` pins `leanprover/lean4:v4.34.0-rc2` (matches Lake); bare `lake build` clean but any `lake env` invocation SIGTRAPs (exit 133) — hermetic path is pinned-toolchain direct `lean` with explicit LEAN_PATH; `rebuild_qed_oleans.py` now resolves via `elan run <toolchain>` with no hardcoded home-directory paths (same fix applied to `verify_formal_gate.py::_elan_bin_dir/_lean_bin`).
- **Gate hardening found by measurement:** the axiom-set regex missed Lean's `depends on axioms: [...]` colon format (dead check) — fixed; `build_structural_theorem`'s first one-line replacement was not QED-provable (pipeline rejected it) — replaced with the full provable theorem kept out of the line-lemma set.
- **Mutants (Task 4):** export `129/130 = 0.992` (3 proven equivalents), gate `177/177 = 1.0` excl. 10 proven equivalents (`0.947` raw) — evidence in `VeriTrial/mutation_evidence_export.json` + `mutation_evidence_gate.json`; tether `cleanroom.py` sibling-copy window 14/14 killed; mission-sampled 11/12 killed + 1 documented equivalent (`254:56` vacuous `exist_ok`).
- **Gauntlet (Task 5):** `tri-repo-full-stack-gate` **success** (session `22952746c5d3`): export ✓, direct-lean olean rebuild ✓, formal gate (17/17 lemmas, no sorry) ✓, full validation + NUTS ✓, tether cleanroom suites (37 tests) ✓. Two mission-config bugs found and fixed en route: host editable install shadowing room sources (5th command now uses `env PYTHONPATH=src`) and the shell-less runner rejecting `VAR=x` prefixes. `make validate`: **overall_pass true** (5 benchmarks + formal gate). Room Merkle `640e7aad…`; host Merkle `b230641a…` sealed in `VeriTrial/output/vvv40_report.html`.
- Commit hashes: tether `fbf9d63`, QED `d0ba8b8`, VeriTrial `490d48d` (working trees carry the verified changes; see `SYSTEM_STATE.json`).

## Remaining for full gauntlet
(none — all items executed above.)

## Global-minimum generalization (2026-09-22)
- **Zero domain leakage**: `grep -riE "pbpk|liver|dili|cyp|ka_rate|c_tissue|kp|a_gut" QED/parser.py QED/agentic_pipeline.py` returns zero matches (exit 1). Domain detectors replaced by generic structural relations: `is_positivity` (E>0 division), `is_nonneg_product` (E>=0 product-with-division), `is_linear_conservation` (linear sum = 0), `is_matrix_entry_equality` (shared-support identity), `is_discrete_step_conservation`. Historic names kept as aliases.
- **Dimensional invariance**: `QED/Compartmental.lean` core theorems (`mass_dissipation_rate`, `mass_conservation_rate`, `orthant_invariance_fwdEuler[_divBound]`, `diag_nonpos`) are generic over `[Fintype n] [DecidableEq n]`; added SDIRK2 stage theory (`sdirkStage`, `sdirk_stage_isMmatrix` Z-matrix + diag>=1 structure, `mmatrix_inv_preserves_nonneg`); `lake env lean Compartmental.lean` compiles clean, no sorry.
- **OOD verification**: `QED/test_pipeline.py` OOD tests (SEIR conservation `S+E+I+R-N=0`, `beta/gamma>0`; 3-tank cascade `h1+h2+h3-H=0`, `(q/V)*h1>=0`) pass with zero QED modifications (21 passed in scoped run).
- **VeriTrial N-generic bridge**: `export_pbpk_to_qed.emit_lean_export` derives state dimension N from the symbolic Jacobian (`Fin N`); `verify_formal_gate` uses generic QED detectors + dynamic perfused-compartment count; `run_negative_controls` passes (bad-gut refused, liver-sign-flip refused, zeroed sums refused, theater filters sensitive).
- **Tether mission**: `tri-repo-full-stack-gate.yaml` context updated to the decoupled generic architecture.

## Residual-gap closure (2026-09-22, this session)
- **Broken export eliminated**: `QED/VeriTrialExport.lean` referenced `pbpkK`/`pbpkDiliSystem`/`pbpk_is_metzler`, which are defined nowhere — the file could not compile. Rewrote it self-contained: `extracted_matrix` (same Jacobian entries) + `extracted_offDiag_nonneg` (fin_cases + positivity) + `extracted_colSum_eq_zero` (universal `Finset.sum_fin_eq_sum_range` script, valid for any N — verified, not `Fin.sum_univ_six`) + `extracted_colSum_nonpos` + `veritrial_compartmental : CompartmentalMatrix (Fin 6)` + `veritrial_mass_dissipation` + `extracted_dili_matrix`/`veritrial_dili_block` (top-left block identity, no external refs). `#print axioms` on all 5 export theorems AND all 7 core `Compartmental` theorems: only `[propext, Classical.choice, Quot.sound]`, zero sorry.
- **Mathlib-drift repair**: working-tree `Compartmental.lean` did not compile against current master (`sum_ite_eq` orientation → `sum_ite_eq'`, `le_of_not_lt` removed → `le_of_not_gt`, `← sum_add_distrib` non-match, `∑..in` notation unavailable, SDIRK2 block outside namespace). Fixed; also rewrote `orthant_invariance_fwdEuler` on `Finset.add_sum_erase` (the old `hterm` claimed diagonal non-negativity — mathematically false). `lean` exit 0; oleans rebuilt via `rebuild_qed_oleans.py` (exit 0).
- **Emitter = single source of truth**: `emit_lean_export` now generates the self-contained template (N from Jacobian, universal scripts); `QED/VeriTrialExport.lean` is byte-identical to emitter output (minus header comment). `build_structural_theorem` updated to the `CompartmentalMatrix`-transport text. `_ode_rhs_asts` order, `compute_jacobian` state vars, `extract_column_sum_lemmas` N, and the gate's `_independent_column_sums` oracle all derive from the live return vector (no 6-state lists). Gate axiom check enforces the 5 new certificate names.
- **N-state model**: `model.py` gains `DEFAULT_ORGAN_NETWORK`/`STANDARD_14_ORGAN_NETWORK` + `organ_indices` (fail-closed) + vectorized `make_pbpk_ode(network)` (no hardcoded indices, jit/scan-compatible). Verified: default network is numerically identical to `pbpk_ode`; 14-state conserves mass exactly (sum 0.0). `calculate_max_stable_dt` loops over resolved perfused indices (identical values for 6-state); `_initial_state` N-generic; stale `pbpk_*` QED-name references in `fixed_step.py`/`solvers.py` updated to `Compartmental.*`.
- **Results**: formal gate PASSED (17/17 lemmas, no sorry); QED `test_pipeline.py` 190 passed + fixed domain-test updated to generic semantics; VeriTrial bridge 72 passed, pbpk/fixed_step/solvers 12 passed, formal_verification 25 passed; mutation probe on exporter vs bridge suite 8/8 killed (1.00 ≥ 0.80); `run_negative_controls` green. clinical validation re-run: **overall_pass true** (`output/validation/validation_summary.json`; 17/17 lemmas, benchmarks green).
- **Mission file**: `tri-repo-full-stack-gate.yaml` no longer claims "proved by rfl"; describes the self-contained N-state export + universal scripts.

## Continuous-verification + SymPy-oracle + theater-hunt session (2026-09-23)

### Step 0 — ledger reconciled (first checkpoint)
- **Root cause of `sorry_free: "unknown"`**: `scripts/audit_system_state.py::audit_repo`
  hardcoded `"unknown"` for every repo except QED — the missing-hook gap. Neither
  tether nor VeriTrial contains any `.lean` file (verified by find), so `unknown`
  was wrong; the correct value is vacuously-true.
- **Fix**: `_check_sorry` is now generic (`rglob("*.lean")`, `.lake`-excluded,
  comment-stripped) and runs for all three repos; repos with zero Lean sources
  report `true` (vacuously sorry-free). Audit re-run: all three repos
  `sorry_free: true`, Merkle `1bb63227…`.
- **Known ledger self-reference (new gap entry, not a footnote)**: the audit samples
  `dirty` *before* writing `SYSTEM_STATE.json`, so tether reports `dirty: true`
  after any session touching the ledger or audit script. Fully-clean is unreachable
  without committing the ledger (not requested) — left dirty-by-design, documented.

### Step 2 — SymPy-oracle question RESOLVED with evidence (was: assertion)
- **Finding**: mostly (a), with one real (b)-crack now closed. `compute_jacobian`
  differentiates with local pure-Python `_sym_diff` (product/quotient rules, no CAS);
  SymPy is used only for `simplify`. Lean's kernel independently re-proves every
  *stated* identity, so a SymPy simplification error yielding a false statement
  fails closed at Lean.
- **The (b)-crack**: the gate's "independent" oracle (`_independent_column_sums`)
  re-derives with `sympy.diff` — same trusted CAS family — and Lean never checks
  *correspondence* between the stated identity and `model.py`. A CAS soundness bug
  or a provable-but-wrong export (`0 = 0` for a nonzero column) could pass all
  three symbolic checks.
- **Fix (Step 4: A over B)**: (A) numeric differential oracle beats (B) Lean-side
  re-derivation, which needs Mathlib `deriv` formalization while `lake env`
  SIGTRAPs. (A) is SymPy-free (only `model.py` + float arithmetic), N-generic:
  `verify_formal_gate._numeric_jacobian_correspondence` evaluates live `pbpk_ode`
  (jax x64) at 3 seeded random positive draws, central differences (h=1e-6),
  evaluates each emitted entry in a restricted namespace, requires rtol=1e-4.
  State mapping derives from model.py's own `... = y` unpack line (order-space is
  alphabetical, y-space positional — found by measurement). Wired fail-closed into
  the gate; negative control #6 added to `run_negative_controls` (stderr-silenced
  to preserve the controls-are-silent invariant).
- **Evidence**: genuine export passes; single-entry corruption refused;
  *compensating-pair* corruption (column sums still zero) refused. Bridge suite:
  68 passed, only pre-existing failures remain (below).

### Step 3 — theater-hunt institutionalized
- Standing mission `tether/missions/theater-hunt.yaml` (`tether validate-mission`
  OK): domain-leak grep tripwire + full export/formal-gate + cleanroom suite in
  clean-room isolation with `git_state_guard`.

### Step 5 — red-team probes
1. **Disguised domain terms in QED core** (`hepatic|hepat|renal|metab|clearance|
   dose|toxic|seahorse|mito` in `parser.py`/`agentic_pipeline.py`): zero matches
   (grep exit 1) — clean.
2. **Compensating-pair export corruption**: refused by numeric oracle — CAUGHT.
3. **Zero-command mission reports `success`** (`orchestrator.py:2232-2242`,
   deliberate for smoke missions, warning only). NOT fail-closed — residual
   recommendation (treat warning as gate), not fixed.

### Pre-existing failures (identical on stashed baseline — not regressions)
- `test_bridge_mutation_fast.py`: `test_fast_state_variables`,
  `test_fast_metzler_lemmas`, `test_fast_bare_array_call_model`,
  `test_fast_gate_single_source_drift` fail on the clean tree: suite pins 6-state
  expectations while HEAD (`e55f8d3`, 14-organ migration) moved on. **New gap
  entry**: bridge fast-tests assume the unstated 6-state special case.
- G1 refreshed (jax 0.10.2, jax-metal absent — no upstream change; CPU stays;
  `gap_closure_plan.md` timestamped 2026-09-23). G3/G4/G6 not started
  (one-at-a-time rule).

## Bridge single-source-drift closure (2026-09-23, this session)
- **Gap**: the 14-organ migration left the bridge certifying what isn't live.
  `extract_state_variables` ignored `model_path` and returned the hardcoded
  14-network (a 1-state toy model reported 14 states); `_ode_rhs_asts`'s
  `"d_" + n` network-ordering prefix never matched `dA_*` keys (dead branch —
  order was always `sorted(rhs)`); `build_lemmas` emitted the 3 Metzler strings
  twice (17 lines, 14 unique) after the `>`→`>=` IsMetzler migration falsified
  the "no duplicates" NOTE — and the set-based single-source check could not see
  a truncated file because `set()` collapsed the missing copy. 4 bridge tests
  failed on the clean tree.
- **Fix (Step 4: repair-source over update-tests)**: updating tests to 14-state
  would certify theater. Instead (a) `extract_state_variables` derives live
  `A_*` names from the return vector in model-position order (explicit network
  still overrides, fail-closed on mismatch); (b) `_ode_rhs_asts` ordering made
  explicit `sorted(rhs)` — byte-identical behavior, dead branch removed;
  (c) `build_lemmas` order-preserving dedupe; (d) gate single-source check
  compares `Counter` multisets; (e) stale `> 0` test pinned to documented `>= 0`
  (matches `IsMetzler`, Lean `offDiag_nonneg`, gate regex covering both);
  (f) negative control #7 (doubled-lemma file refused); (g) tether audit test
  updated from `unknown` to vacuously-`True` plus a new sorry-detected-`False` test.
- **Evidence**: `test_bridge_mutation_fast.py` **72/72 pass** (was 68+4 failed);
  `run_negative_controls` (7 controls) green; numeric oracle still passes genuine
  export; `test_formal_verification.py` failures byte-identical to stashed
  baseline (12 pre-existing, environment-dependent); tether
  `test_audit_system_state.py` 10/10 pass.

## G3 CLOSED — mechanistic CYP liver (2026-09-23, this session)
- **Shipped (approach A, first-order hepatic extraction)**: `pbpk_ode` gains an
  optional `v_met = cyp_activity * CLint * fu_liver * C_liver` term on unbound
  liver concentration; liver keeps separate perfusion flux `dA_liver_perf`
  (reclaimed by central) vs metabolic loss (routed to `A_elim`);
  `build_pbpk_params` threads `cyp_clint/fu_liver/cyp_activity` (OFF defaults
  ⇒ byte-identical benchmarks); `calculate_max_stable_dt` gains the
  `Vl/(CLint*fu*cyp)` candidate (binds only at extreme rates: unchanged to
  CLint=5, 0.0075 at CLint=200).
- **Step 4 record**: full Michaelis-Menten was implemented first, then
  REVERTED on gate evidence — its state-dependent Jacobian entries
  (`A_liver` free in `extracted_matrix`) need state binders + mechanism-specific
  proof preludes the universal-script architecture cannot provide honestly.
  Recorded as follow-up gap **G3b (saturable kinetics)**, not smuggled in.
- **Theater caught by the machinery, in order**: (1) first cut double-counted
  (central reclaimed `dA_liver` incl. `-v_met` AND `A_elim` received it) —
  `check_mass_conservation` returned False and the column sum was genuinely
  nonzero; (2) `args.get("X", d)` string literals poisoned the gate's textual
  alias expansion (nested `args.get('(args.get(...` → sympify crash) — optionality
  reworked to try/except-KeyError with bare symbols; (3) `dA_liver_perf`
  polluted state order/perfused lists — `_ode_rhs_asts` now intersects with the
  return vector (rhs keeps all assigns for substitution, order is states-only),
  `extract_perfused_compartments` uses expanded derivatives + role exclusions,
  `check_mass_conservation` proves the total sum with sympy instead of the
  `elim == CL*C_p` exact-form special case (strictly stronger, still refuses all
  three historical mutants).
- **Evidence**: formal gate **PASSED** (14/14 lemmas, no sorry) against the
  regenerated `QED/VeriTrialExport.lean` (15-param binders, `positivity` +
  `field_simp`/`ring` close over CLint entries); numeric oracle passes with CYP
  ACTIVE (CLint drawn > 0); `test_cyp_metabolism.py` 4/4 (OFF-identical incl.
  bare-dict compat, mass < 1e-7 to CLint=20, monotonic AUC 17.5→7.7→4.4,
  inhibition 7.7→12.9); warfarin benchmark unchanged (0.1491/37.0, pass True);
  bridge 72/72; engine+pbpk+safety 26 passed.
- **Correction**: jax-metal 0.1.1 IS pip-installed (G1 note said absent); the
  upstream breakage stands regardless — CPU mitigation unchanged.

## G4 CLOSED — compound-specific mito/BSEP DILI priors (2026-09-23, this session)
- **Gap**: the 9-state QSP trajectories used literature defaults for every
  compound (engine filled `_QSP_DEFAULTS` verbatim; SAD batch lacked QSP keys
  entirely), so identical-PK drugs with different mitochondrial toxicity were
  indistinguishable in live ALT/GSH — while the post-hoc safety assessor had
  its own hand-duplicated override rule (drifting copies of one rule).
- **Fix**: central `qsp_params_for_drug` in `model.py` (`km_metabolic`→IC50,
  `gsh_depletion_rate`→k_deplete, `alt_baseline`→ALT_base; zeros fall back to
  defaults); engine SAD+MAD paths share `_with_mechanistic_keys` (also threads
  per-patient CYP keys — SAD had been silently dropping CLint, a G3
  regression caught here); safety assessor uses the same helper (duplication
  deleted); new `bsep_ic50` Drug field wired into the bile-acid QSP
  (`IC50_bsep`) and `has_qsp_dili_params`.
- **Evidence**: `test_dili_mechanistic.py` 4/4 — helper defaults/overrides,
  same-PK mito discrimination (toxic analog leaks more ALT), BSEP block raises
  BA, schema flag; engine+safety+schemas 27 passed; bridge 72/72; touched-file
  lint clean (remaining hits verified pre-existing by stash-diff).
- **Cost note**: BSEP test runs the Python-loop RK4 (~4.5 min for the file);
  a jitted QSP solver would cut this 10× — recorded, not pursued (one gap).
- **Remnant (explicit, not theater)**: bilirubin still scales by the
  `dili_emax_bili` effect-size scalar on GSH depletion — documented
  effect-size convention, now driven by compound-aware GSH rather than exposure.

## G6 CLOSED — hepatic disease axis reaches PK (2026-09-23, this session)
- **Gap**: the engine was disease-blind — `Patient.egfr_scaling` generated but
  never consumed, no hepatic axis existed, and the hepatic config/benchmark
  scaled whole-body CL through the *renal* knob (`egfr_scale=0.6`, comment
  mislabeled). Consequences: renally-cleared drugs mis-dosed in liver disease,
  and Child-Pugh B left mechanistic CYP CLint untouched (G3/G6 interaction).
- **Fix**: independent renal/hepatic axes — `Patient.hepatic_scale` (B=0.6),
  generator `hepatic_scale` from config, `build_pbpk_params(hepatic_scale)`
  scaling BOTH CL and CLint, engine applies it at all 3 params call sites;
  config honest (`egfr_scale: 1.0` normal kidneys + `hepatic_scale: 0.6`);
  validation benchmark switched to `hepatic_scale`.
- **Evidence**: hepatic benchmark **40.0% reduction, pass True** (was passing
  via the wrong knob); `test_hepatic_cohort.py` 3/3 (axis independence:
  egfr_scale never touches CLint; generator carries 0.6/1.0; full-trial
  hepatic AUC > normal × 1.3); engine 18 passed, warfarin unchanged (0.1491);
  touched-file lint clean (B905/engine cruft stash-verified pre-existing).
- **Follow-up G6b (NOT closed)**: `egfr_scaling` still unconsumed by the
  engine (renal axis in live trials) and no eGFR→CL transfer rule
  (`min(egfr/90,1)^0.5`) is applied per-patient — validation hand-applies
  scales. Same one-rule-everywhere treatment as QSP when scheduled.

## G6b CLOSED — mechanistic renal rule in live trials (2026-09-23, this session)
- **Gap**: the metformin eGFR–CL correlation passed with NO renal mechanism
  (allometric confound only — validation built params with genotype alone).
- **Fix**: one shared rule `renal_egfr_scale(egfr, fe) = (1-fe) +
  fe*min(egfr/90,1)^0.5` (ASSUMPTIONS §3); new `fraction_excreted_renal`
  schema field (default 0 = neutral, all current drugs unmoved);
  metformin.yaml fe=0.9; engine applies at all 3 call sites; metformin
  validation uses the same helper.
- **Evidence**: metformin benchmark pass (CL@90 44.55, corr 0.297);
  `test_renal_axis.py` 3/3 (rule shape incl. cap/floor/neutrality, isolated
  CL-ratio equals rule, full-trial CKD AUC separation); engine+bridge 90
  passed; touched-file lint clean (B905/engine cruft pre-existing).

## Simulation-path restoration (2026-09-23, this session — P0, preempts G3)
- **Gap (found acting as reviewer, top priority)**: HEAD `e55f8d3` (14-organ
  migration) deleted 441 lines from `model.py` — `build_pbpk_params`,
  `solve_pbpk_single/batch/full`, `run_pbpk`, `compute_mass_balance`,
  `compute_patient_kp[_early]`, `_reference_physiology`, `scale_physiological`.
  The entire engine/validation stack (`trial/engine.py`, `validation/`,
  `stats/`, `sensitivity.py`) failed at **import** (`ImportError:
  build_pbpk_params`); `make validate` and every benchmark were unrunnable at
  HEAD despite the 2026-09-22 "overall_pass true" entry describing the pre-HEAD
  tree. An empty `pbpk/model/` directory (no `__init__.py`) sits alongside
  `pbpk/model.py` — namespace-package trap, currently inert (the `.py` wins),
  left in place and noted.
- **Fix (Step 4: restore-with-provenance over rebuild-fresh)**: re-appended the
  deleted block verbatim from `HEAD~1` (single hunk `@@ -330,443`, provenance
  exact; `pbpk_ode` body identical across the hunk so semantics match). All
  callees verified present (`fixed_step` solvers, `Drug` schema fields).
- **Evidence**: `import trial.engine`, `import validation` OK;
  `run_pbpk` smoke mass balance 1.4e-15; `validate_warfarin_pgx(n=60)`:
  CL/F 0.149 vs 0.15 ✓, t½ 37.0 vs 38 ✓, IM/EM AUC 1.44, corr −0.88,
  **overall_pass True**; `test_engine.py`+`test_pbpk.py` 22 passed;
  pbpk/fixed_step/solvers/safety/population 19 passed; bridge suite still 72/72
  (`pbpk_ode` untouched → export byte-identical path).
- **G3 design (next, one gap)**: (A) optional Michaelis-Menten hepatic intrinsic
  clearance inside `pbpk_ode` (`Vmax*C_liver_u/(Km+C_liver_u)` subtracted from
  liver, added to `A_elim`; `Vmax=0` default ⇒ byte-identical benchmarks;
  activates dormant `_find_saturable_patterns`/saturableFlux support) vs
  (B) explicit 7-state metabolite compartment (tracks metabolite exposure but
  ripples through every index consumer + recalibration, with no metabolite data
  to anchor it). **Prefer A**: smaller blast radius, linear low-conc limit
  preserves benchmarks, directly demonstrates genotype→Vmax mapping for warfarin
  (CYP2C9). Risks to close in the loop: rational Jacobian entries through the
  Lean column-sum proofs (Mathlib-gated), `calculate_max_stable_dt` accounting
  for the new term, and documenting CL-scalar vs Vmax precedence (no
  double-counting).

## Full validation sweep post-G3/G4/G6/G6b (2026-09-23, this session)
- `run_all_validations()` (warfarin n=100, moxi, midazolam n=200, metformin
  n=200, hepatic n=100): **overall_pass True** — all five benchmarks plus the
  embedded fail-closed formal gate pass with every change since 2026-09-22 in
  place (CYP extraction, QSP priors, hepatic/renal axes, bridge repairs,
  restored simulation path). Artifacts: `output/validation/*.json`,
  `output/vvv40_report.html` (regenerated).

## Red-team round 2 — new-code surface (2026-09-23, this session)
- **Hole 1 (REAL, fixed)**: negative `cyp_activity`/`fu_liver` ran metabolism
  backward — mass pumped elim→liver with total conserved, so NO mass monitor
  could ever see it; silently inflated exposure. Fixed at the build boundary
  (`cyp_activity`/`fu_liver`/`ka` floored; CLint already was) — ODE hot path
  stays clamp-free per discipline (tracer-safe).
- **Hole 2 (REAL, fixed)**: NaN rate constants slipped the mass-gain monitor
  (`NaN > 1e-6` is False). Monitor now also poisons on non-finite drift
  (tracer-safe `isFinite` disjunct, established poisoning mechanism, no value
  clamping). NaN eGFR raises `ValueError` at the helper boundary instead of
  silently clearing at full rate.
- **Evidence**: 3 new permanent adversarial tests (floors, NaN-poison marker,
  NaN-eGFR raises); mechanism suites 13 passed; engine+bridge 90 passed;
  touched-file lint clean (fixed_step hits stash-verified pre-existing).

## Gauntlet-substance round (2026-09-23, this session)
- Olean rebuild green (15-param export compiles under pinned toolchain);
  tether cleanroom suites 37 passed; full validation sweep overall True (prior
  entry); targeted 7-mutant probe on session-new code: **7/7 killed**.
- **Self-caught theater**: the `~isfinite` monitor disjunct I added was DEAD
  CODE — any NaN state already NaNs the sum, so the disjunct never changes
  behavior; the mutation probe proved it (survived with correct selection).
  Reverted; the NaN test relabeled as propagation characterization, not a guard
  control. The probe also found a REAL missing invariant (no emission-
  uniqueness assertion — dedupe-disabled mutant survived everything) → added
  `test_fast_emission_has_no_duplicates`, now killing.

## Formal-verification suite 25/25 (2026-09-23, this session)
- The flagship `test_sign_flip_dynamically_alters_lean_and_fails_gate` (named
  in this report as passing) pinned liver-diagonal index `(1, 1)` from the
  y-position era — now the effect compartment under alphabetical order. Fixed
  by deriving the index from live order + asserting liver-diagonal semantics
  symbolically (`perfusion + CYP` via sympy, not print-form).
- Bonus evidence that the bridge-drift repairs generalized: 11 sibling
  failures in the same file (classify/qed-dir/required-lemmas), recorded as
  "pre-existing" earlier, now pass UNCHANGED — they were failing on 14-state
  garbage outputs, healed by live-derived states. Full file: **25 passed**.
  Lint count identical to baseline (24, none in touched lines).
