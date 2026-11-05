"""Teeth for tools/collect_quint_repo_evidence.py.

Kills the break_return mutant on the failure-branch ``return 1``: with no
evidence files present the collector must fail closed with int 1 (a None
return would exit 0 via SystemExit and pass the gate on missing evidence).
"""

import os
import subprocess
import sys
from pathlib import Path


def test_collector_fail_closed_returns_int_one(tmp_path):
    tool = Path(__file__).parent.parent / "tools" / "collect_quint_repo_evidence.py"
    proc = subprocess.run(
        [sys.executable, str(tool)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 1, (
        f"collector must exit 1 on missing evidence, got {proc.returncode}\n"
        f"stdout={proc.stdout[-500:]}\nstderr={proc.stderr[-500:]}"
    )
    assert "QUINT EVIDENCE FAILED" in proc.stderr
