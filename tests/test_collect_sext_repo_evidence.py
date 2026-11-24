"""Teeth for tools/collect_sext_repo_evidence.py (v6 recovery).

Kills the arithmetic mutant on the permutation-gap direction
(``shuffled - real`` vs ``real - shuffled``): negating the gap would credit
a sabotaged suggester, so the sign is pinned directly on the helper.
"""

import importlib.util
import sys
from pathlib import Path


def _load():
    tool = Path(__file__).parent.parent / "tools" / "collect_sext_repo_evidence.py"
    spec = importlib.util.spec_from_file_location("collect_sext_repo_evidence", str(tool))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["collect_sext_repo_evidence"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_permutation_gap_direction_pinned():
    mod = _load()
    assert mod._permutation_gap(0.8, 0.5) == 0.8 - 0.5
    assert mod._permutation_gap(0.8, 0.5) > 0
    assert mod._permutation_gap(0.5, 0.8) < 0
    assert mod._permutation_gap(0.5, 0.5) == 0.0
