#!/usr/bin/env python3
"""Verify the required VeriTrial kinetic equations without importing the project."""
from __future__ import annotations

import argparse
import ast
from pathlib import Path
from typing import Iterable


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"required function missing: {name}")


def _contains_ast(function: ast.FunctionDef, node_type: type[ast.AST], expected: ast.AST) -> None:
    for node in ast.walk(function):
        if isinstance(node, node_type) and ast.dump(node, include_attributes=False) == ast.dump(expected, include_attributes=False):
            return
    raise AssertionError(f"required AST control altered in {function.name}: {ast.unparse(expected)}")


def _contains_assignment(function: ast.FunctionDef, target: str, expression: str) -> None:
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign):
            continue
        names = [part.id for target_node in node.targets for part in ast.walk(target_node) if isinstance(part, ast.Name)]
        if target in names and ast.unparse(node.value) == expression:
            return
    raise AssertionError(f"required equation missing: {target} = {expression}")


def _contains_dict_entry(function: ast.FunctionDef, key: str, expected: str) -> None:
    for node in ast.walk(function):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(k, ast.Constant) and k.value == key and ast.dump(v, include_attributes=False) == ast.dump(ast.parse(expected, mode="eval").body, include_attributes=False):
                    return
    raise AssertionError(f"required dictionary entry altered: {key} = {expected}")


def _contains_source(function: ast.FunctionDef, fragments: Iterable[str]) -> None:
    source = ast.unparse(function)
    missing = [fragment for fragment in fragments if fragment not in source]
    if missing:
        raise AssertionError(f"equation altered in {function.name}: {missing}")


def verify_model(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = _function(tree, "make_pbpk_ode")
    _contains_assignment(function, "dA_gut", "-ka * A_gut")
    _contains_assignment(function, "dA_elim", "CL * C_p")
    _contains_source(
        function,
        (
            "Q[k] * (C_p - y[k] / V[k] / Kp[k])",
            "dA_central = ka * A_gut - sum(flows) - CL * C_p",
        ),
    )
    kp_function = _function(tree, "rodgers_rowland_kp")
    _contains_source(kp_function, ("kp = 10.0 ** log_kp_base * ion_factor * (water_fraction + lipid_adjustment) / 0.7",))
    _contains_source(_function(tree, "pbpk_dili_ode"), (
        "network = DEFAULT_ORGAN_NETWORK if args['Q'].shape[0] == 6 else STANDARD_14_ORGAN_NETWORK",
    ))
    params = _function(tree, "build_pbpk_params")
    _contains_dict_entry(params, "Q", 'ref["Q"] * scaling["w_scaling"]')
    _contains_source(params, ("'CL': float(max(drug.typical_cl_f * scaling['w_scaling'] *", "scaling['age_factor'] *", "(0.2 + 0.8 * genotype_scale) ** 2 *", "egfr_scale, 1e-06))"))
    _contains_assignment(_function(tree, "solve_pbpk_full"), "n_steps", "int((t1 - t0) / dt) + 1")


def verify_export(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    _contains_source(_function(tree, "_dynamic_lemmas"), ("if parametric:", "lemmas.append('(ka_rate) + (-ka_rate) = 0')"))
    _contains_source(_function(tree, "_substitute"), (
        "if n.id in env:",
        'return ast.fix_missing_locations(_substitute(env[n.id], {k: v for k, v in env.items() if k != n.id}))',
        "return ast.fix_missing_locations(_S().visit(ast.parse(ast.unparse(expr)).body[0].value))",
    ))
    _contains_assignment(_function(tree, "main"), "verifiable", "[l for l in lemmas if not l.strip().startswith('--')]" )


def verify_formal_gate(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    _contains_source(_function(tree, "_independent_column_sums"), ("changed = False", "if not changed:"))
    _contains_source(_function(tree, "_detect_mathlib_env"), ("if os.environ.get('HAS_MATHLIB') or os.environ.get('MATHLIB'):", "return True"))


def verify_pd(path: Path) -> None:
    function = _function(ast.parse(path.read_text(encoding="utf-8")), "cardiac_apd_effect")
    _contains_source(
        function,
        (
            "bkr = C / (ic50_kr + C)",
            "bna = C / (ic50_na + C)",
            "bca = C / (ic50_cal + C)",
            "baseline_apd90 * (1.0 + 0.45 * bkr - 0.25 * bca - 0.05 * bna)",
            "return float(apd + 80.0)",
        ),
    )
    _contains_assignment(function, "scale", "emax / (full_block * 0.45)")
    _contains_assignment(function, "apd", "baseline_apd90 + (apd - baseline_apd90) * scale")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path("../VeriTrial"))
    args = parser.parse_args()
    try:
        verify_model(args.source_root / "src/insilico_trial/pbpk/model.py")
        verify_export(args.source_root / "scripts/export_pbpk_to_qed.py")
        verify_formal_gate(args.source_root / "scripts/verify_formal_gate.py")
        verify_pd(args.source_root / "src/insilico_trial/pd/__init__.py")
    except (AssertionError, OSError, SyntaxError) as error:
        print(f"equation gate failed: {error}")
        return 1
    print("VeriTrial kinetic and cardiac APD equation gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
