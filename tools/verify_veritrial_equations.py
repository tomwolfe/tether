#!/usr/bin/env python3
"""Verify the required VeriTrial kinetic equations without importing the project."""
from __future__ import annotations

import argparse
import ast
from pathlib import Path
from typing import Iterable


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"required function missing: {name}")


def _contains_assignment(function: ast.FunctionDef, target: str, expression: str) -> None:
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign):
            continue
        names = [part.id for target_node in node.targets for part in ast.walk(target_node) if isinstance(part, ast.Name)]
        if target in names and ast.unparse(node.value) == expression:
            return
    raise AssertionError(f"required equation missing: {target} = {expression}")


def _contains_source(function: ast.FunctionDef, fragments: Iterable[str]) -> None:
    source = ast.unparse(function)
    missing = [fragment for fragment in fragments if fragment not in source]
    if missing:
        raise AssertionError(f"equation altered in {function.name}: {missing}")


def verify_model(path: Path) -> None:
    function = _function(ast.parse(path.read_text(encoding="utf-8")), "make_pbpk_ode")
    _contains_assignment(function, "dA_gut", "-ka * A_gut")
    _contains_assignment(function, "dA_elim", "CL * C_p")
    _contains_source(
        function,
        (
            "Q[k] * (C_p - y[k] / V[k] / Kp[k])",
            "dA_central = ka * A_gut - sum(flows) - CL * C_p",
        ),
    )


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path("../VeriTrial"))
    args = parser.parse_args()
    try:
        verify_model(args.source_root / "src/insilico_trial/pbpk/model.py")
        verify_pd(args.source_root / "src/insilico_trial/pd/__init__.py")
    except (AssertionError, OSError, SyntaxError) as error:
        print(f"equation gate failed: {error}")
        return 1
    print("VeriTrial kinetic and cardiac APD equation gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
