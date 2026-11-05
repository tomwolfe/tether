#!/usr/bin/env python3
"""Collect and fail-closed-check sext-repo (ProjectAurelius) evidence."""
from __future__ import annotations
import json, subprocess, sys
from pathlib import Path

def main() -> int:
    root = Path.cwd()
    qed = root.parent / "QED"
    aur = root.parent / "ProjectAurelius"
    failures: list[str] = []
    # 1. changed files (fail-closed return 1 if none)
    r = subprocess.run(["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=str(root))
    changed = [l for l in r.stdout.splitlines() if l.strip()]
    if not changed:
        for repo in (qed, aur):
            rr = subprocess.run(["git", "status", "--porcelain"],
                capture_output=True, text=True, cwd=str(repo))
            changed += [f"{repo.name}:{l}" for l in rr.stdout.splitlines() if l.strip()]
    if not changed:
        failures.append("no changed files detected (fail-closed)")
    # 2. QED build
    b = subprocess.run(["lake", "build"], capture_output=True, text=True, cwd=str(qed))
    qed_ok = b.returncode == 0
    if not qed_ok:
        failures.append(f"QED lake build failed: {b.stderr[-500:]}")
    # 3. Aurelius unified benchmark
    bench = aur / "benchmark_unified.py"
    out = ""
    if not bench.is_file():
        failures.append("benchmark_unified.py missing")
    else:
        p = subprocess.run([sys.executable, str(bench)],
            capture_output=True, text=True, cwd=str(aur))
        out = p.stdout + p.stderr
        if "ALL TOLERANCES MET" not in out or p.returncode != 0:
            failures.append("benchmark output missing ALL TOLERANCES MET or nonzero exit")
    # 4. Lean exports sorry-free
    lean_files = list(qed.glob("*.lean")) + list(qed.glob("**/*.lean"))
    sorry_hit = [str(f) for f in lean_files if "sorry" in f.read_text(errors="replace")]
    if sorry_hit:
        failures.append(f"Lean exports contain sorry: {sorry_hit}")
    if not lean_files:
        failures.append("no Lean files found under QED")
    evidence = {"changed": changed, "qed_build_ok": qed_ok,
        "tolerances_met": "ALL TOLERANCES MET" in out,
        "sorry_free": not sorry_hit, "failures": failures}
    (root / "sext_repo_evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    if failures:
        print("SEXT EVIDENCE FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("SEXT EVIDENCE OK: " + json.dumps(evidence, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
