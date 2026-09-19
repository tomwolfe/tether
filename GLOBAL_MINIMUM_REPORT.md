# GLOBAL MINIMUM REPORT — Tri-Repo Convergence

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
- Tether workspace tests: 5 passed. Sign-flip test: passes.
- Merkle root `a9ae554b…` in `SYSTEM_STATE.json` (QED `sorry_free: true`).

## Remaining for full gauntlet
- `make validate` 5 benchmarks within literature tolerances (long JAX run, not executed here).
- `tether run missions/tri-repo-full-stack-gate.yaml` live execution (requires `opencode` adapter; clean-room + mutation ≥0.80 gates configured in the mission YAML).
- Embed Merkle root into `VeriTrial/output/vvv40_report.html` post-run.
