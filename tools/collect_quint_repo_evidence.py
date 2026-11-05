#!/usr/bin/env python3
"""Collect and fail-closed-check quint-repo formal evidence.

Extends collect_quad_repo_evidence.py with the Emergentia export:
reads QED/VeriTrial/DRIFT artifacts plus /tmp/emergentia_axioms.json and
writes ``quint_repo_evidence.json`` in the current (tether) directory.

Required evidence:
  * ``../QED/traces.json`` reports ``success: true``.
  * ``../QED/output.lean`` contains no ``sorry``/``sorryAx``.
  * ``/tmp/fin6_traces.json`` and ``/tmp/fin14_traces.json`` report
    ``verified: true`` with axioms exactly [propext, Classical.choice,
    Quot.sound].
  * ``/tmp/drift_axioms.json`` and ``/tmp/emergentia_axioms.json`` report
    the same axiom set.
  * The JSON lemma lists are pinned: DRIFT to hill_nonneg/hill_le_one/
    hill_mono/hill_limits and Emergentia to the seven F(r) = -dV/dr
    theorems (harmonic, lennard_jones, morse, gravity, buckingham,
    yukawa, conservative_energy).
  * ``../QED/EmergentiaExport.lean`` contains those seven theorems,
    sorry-free, and ``/tmp/drift_lemmas.txt`` /
    ``/tmp/emergentia_lemmas.txt`` contain the matching markers, tying
    the JSON record to the isolated DRIFT/Emergentia builds.

Exits non-zero if any requirement is missing or wrong.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

EXPECTED = ["Classical.choice", "Quot.sound", "propext"]

EXPECTED_DRIFT_LEMMAS = ["hill_le_one", "hill_limits", "hill_mono", "hill_nonneg"]

EXPECTED_EMERGENTIA_LEMMAS = [
    "emergentia_harmonic",
    "emergentia_lennard_jones",
    "emergentia_morse",
    "emergentia_gravity",
    "emergentia_buckingham",
    "emergentia_yukawa",
    "emergentia_conservative_energy",
]


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
        ("emergentia", Path("/tmp/emergentia_axioms.json")),
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
        # Pin the certified lemma names so the gate cannot pass on a
        # self-asserting JSON payload with the right axioms but the wrong
        # (or missing) theorems.
        if label == "drift":
            lemmas = sorted(payload.get("lemmas") or [])
            if lemmas != EXPECTED_DRIFT_LEMMAS:
                failures.append(
                    f"drift lemma set wrong: {lemmas} expected "
                    f"{EXPECTED_DRIFT_LEMMAS}")
                evidence["drift_verified"] = False
        if label == "emergentia":
            lemmas = sorted(payload.get("lemmas") or [])
            if lemmas != sorted(EXPECTED_EMERGENTIA_LEMMAS):
                failures.append(
                    f"emergentia lemma set wrong: {lemmas} expected "
                    f"{sorted(EXPECTED_EMERGENTIA_LEMMAS)}")
                evidence["emergentia_verified"] = False

    # Cross-check the Emergentia Lean export itself: the seven F(r) = -dV/dr
    # force-balance theorems (Harmonic, LennardJones, Morse, Gravity,
    # Buckingham, Yukawa) plus conservative energy must be present,
    # sorry-free, and axiom-pinned. This ties the JSON record to the
    # isolated Emergentia/DRIFT builds rather than trusting it alone.
    emergentia_lean = qed / "EmergentiaExport.lean"
    try:
        emergentia_text = emergentia_lean.read_text(encoding="utf-8")
    except OSError as exc:
        emergentia_text = ""
        failures.append(f"EmergentiaExport.lean unreadable: {exc}")
    if emergentia_text:
        if "sorry" in emergentia_text:
            failures.append("EmergentiaExport.lean contains sorry")
        for name in EXPECTED_EMERGENTIA_LEMMAS:
            if name not in emergentia_text:
                failures.append(
                    f"EmergentiaExport.lean missing theorem {name}")

    # The lemma text files prove the export steps ran in isolation.
    for label, tmp_name, markers in (
        ("drift", "/tmp/drift_lemmas.txt", EXPECTED_DRIFT_LEMMAS),
        ("emergentia", "/tmp/emergentia_lemmas.txt",
         ["harmonic", "lennard_jones", "morse", "gravity",
          "buckingham", "yukawa", "conservative_energy"]),
    ):
        try:
            body = Path(tmp_name).read_text(encoding="utf-8")
        except OSError as exc:
            failures.append(f"{label} lemmas text unreadable: {exc}")
            continue
        for marker in markers:
            if marker not in body:
                failures.append(
                    f"{label} lemmas text missing {marker} ({tmp_name})")

    if not evidence["qed_traces_success"]:
        failures.append("QED traces.json does not report success")
    if not sorry_free:
        failures.append("QED output.lean is missing or contains sorry")

    (root / "quint_repo_evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")

    if failures:
        print("QUINT EVIDENCE FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("QUINT EVIDENCE OK: " + json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
