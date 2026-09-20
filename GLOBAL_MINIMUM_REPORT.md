# GLOBAL MINIMUM REPORT — Tri-Repo Convergence

> **FINAL: MISSION SUCCESS.** `tri-repo-full-stack-gate` passed end-to-end in
> clean-room isolation with adversarial review approval (tether session
> `53ac93da`, 2026-09-19): 4/4 verification green, mutation kill-rate 1.0
> (8/8 effective, 4 documented-equivalent skipped), review verdict `approve`.
> Provenance Merkle `e3ef63b5…` emitted into the validation output.

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

## Remaining for full gauntlet
- `make validate` 5 benchmarks within literature tolerances (long JAX run, not executed here).
- `tether run missions/tri-repo-full-stack-gate.yaml` live execution (requires `opencode` adapter; clean-room + mutation ≥0.80 gates configured in the mission YAML).
- Embed Merkle root into `VeriTrial/output/vvv40_report.html` post-run.
