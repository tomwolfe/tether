#!/usr/bin/env python3
"""Collect and fail-closed-check quad-repo formal evidence.

Run after the verification ladder. Reads the artifacts produced by the QED,
VeriTrial and DRIFT gates and writes ``quad_repo_evidence.json`` in the
current (tether) directory so the mission's structural assertions can check a
single, machine-written record rather than prose.

Required evidence:
  * ``../QED/traces.json`` reports ``success: true`` for the last QED proof.
  * ``../QED/output.lean`` contains no ``sorry``/``sorryAx``.
  * ``/tmp/fin6_traces.json`` and ``/tmp/fin14_traces.json`` report
    ``verified: true`` with axioms exactly [propext, Classical.choice,
    Quot.sound].
  * ``/tmp/drift_axioms.json`` reports the same axiom set for the four DRIFT
    Hill lemmas.

Exits non-zero if any requirement is missing or wrong.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

EXPECTED = ["Classical.choice", "Quot.sound", "propext"]


def _load(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    root = Path.cwd()
    qed = root.parent / "QED"
    failures: list[str] = []

    try:
        traces = _load(qed / "traces.json")
    except (OSError, json.JSONDecodeError) as exc:
        traces = {"success": False, "error": str(exc)}
        failures.append(f"QED traces unreadable: {exc}")

    lean = qed / "output.lean"
    try:
        lean_text = lean.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        lean_text = ""
        failures.append(f"QED output.lean unreadable: {exc}")
    sorry_free = bool(lean_text) and "sorry" not in lean_text

    evidence: dict = {
        "qed_traces_success": traces.get("success") is True,
        "output_lean_sorry_free": sorry_free,
    }

    for label, path in (
        ("fin6", Path("/tmp/fin6_traces.json")),
        ("fin14", Path("/tmp/fin14_traces.json")),
        ("drift", Path("/tmp/drift_axioms.json")),
    ):
        try:
            payload = _load(path)
        except (OSError, json.JSONDecodeError) as exc:
            evidence[f"{label}_axioms"] = []
            failures.append(f"{label} evidence unreadable: {exc}")
            continue
        axioms = sorted(payload.get("axioms") or [])
        ok = payload.get("verified") is True and axioms == EXPECTED
        evidence[f"{label}_axioms"] = axioms
        evidence[f"{label}_verified"] = bool(ok)
        if not ok:
            failures.append(
                f"{label} axioms/verification wrong: verified="
                f"{payload.get('verified')} axioms={axioms}")

    if not evidence["qed_traces_success"]:
        failures.append("QED traces.json does not report success")
    if not sorry_free:
        failures.append("QED output.lean is missing or contains sorry")

    (root / "quad_repo_evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")

    if failures:
        print("QUAD EVIDENCE FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("QUAD EVIDENCE OK: " + json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
