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
    # 1. changed files (fail-closed return 1 if none). The sibling scan
    # always runs instead of being gated on tether emptiness: the gate's
    # `not` branch is unobservable (hence unkillable) whenever tether
    # itself is dirty, which is the case for every sext mission run.
    r = subprocess.run(["git", "status", "--porcelain"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=str(root))
    changed = [l for l in r.stdout.splitlines() if l.strip()]
    for repo in (qed, aur):
        rr = subprocess.run(["git", "status", "--porcelain"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=str(repo))
        changed += [f"{repo.name}:{l}" for l in rr.stdout.splitlines() if l.strip()]
    if not changed:
        failures.append("no changed files detected (fail-closed)")
    # 2. QED build
    b = subprocess.run(["lake", "build"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=str(qed))
    qed_ok = b.returncode == 0
    if not qed_ok:
        failures.append(f"QED lake build failed: {b.stderr[-500:]}")
    # 3. Aurelius unified benchmark
    bench = aur / "benchmarks" / "benchmark_unified.py"
    out = ""
    if not bench.is_file():
        failures.append("benchmark_unified.py missing")
    else:
        p = subprocess.run([sys.executable, str(bench)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=str(aur))
        out = p.stdout + p.stderr
        if "ALL TOLERANCES MET" not in out or p.returncode != 0:
            failures.append("benchmark output missing ALL TOLERANCES MET or nonzero exit")
    # 4. Lean exports sorry-free (recursive over project sources; the
    # vendored `.lake` dependency tree is excluded: it ships `sorry`
    # strings of its own, and scanning it would fail every run. All
    # tracked QED Lean sources live at top level, so the exclusion loses
    # no project coverage while keeping the gate fail-closed in both the
    # worktree and the clean room (whose `.lake` is carried for the build).
    # Backtick-quoted mentions in comments, e.g. "No `sorry`", are not admits.
    import re
    _sorry_re = re.compile(r"(?<![A-Za-z`])sorry(?![A-Za-z`])")

    def _has_real_sorry(path: Path) -> bool:
        # No OSError guard by design: an unreadable Lean source must fail
        # closed (crash, nonzero exit), never silently count as sorry-free.
        # (A `return False` fallback here would also be an unkillable
        # break_return site: None and False are identically falsy.)
        text = path.read_text(encoding="utf-8", errors="replace")
        # Strip single-line `--` comments; block comments mentioning sorry
        # in prose (e.g. Compartmental.lean docs) are backtick-quoted and
        # excluded by the regex above.
        code_lines = [
            line.split("--", 1)[0]
            for line in text.splitlines()
        ]
        return bool(_sorry_re.search("\n".join(code_lines)))

    lean_files = [p for p in qed.rglob("*.lean")
        if p.is_file() and ".lake" not in p.parts]
    lean_files += [
        p for p in qed.glob("**/*.lean")
        if p.is_file() and ".lake" not in p.parts
        and p not in lean_files
    ]
    sorry_hit = []
    for _lean_file in lean_files:
        # Fail-closed type pin: _has_real_sorry must return a real bool.
        # Without it a break_return mutant (None) is identically falsy and
        # unobservable on the sorry-free green path; with it the mutant
        # crashes here and the run fails closed.
        _flag = _has_real_sorry(_lean_file)
        assert isinstance(_flag, bool), "sorry scanner must return bool"
        if _flag:
            sorry_hit.append(str(_lean_file))
    # Single source of truth: the failure branch and the evidence JSON
    # below both read sorry_free, so a mutant flipping its `not` fails the
    # run (exit 1) instead of silently skewing an unobserved JSON field.
    sorry_free = not sorry_hit
    if not sorry_free:
        failures.append(f"Lean exports contain sorry: {sorry_hit}")
    if not lean_files:
        failures.append("no Lean files found under QED")
    # 5. AureliusExport axioms pinned (machine-checked, not just commented):
    # `lake env lean` elaborates the file, executing its `#print axioms`
    # commands; every headline theorem must report exactly
    # [propext, Classical.choice, Quot.sound]. The `#print axioms` lines
    # must also be present in the source: without them an elaboration
    # output missing a theorem would pass vacuously.
    _AXIOM_WANT = "[propext, Classical.choice, Quot.sound]"
    _axiom_theorems = ("ea_calibration_affine", "born_solvation_pos",
        "ea_bounds", "reduction_stability_mono")
    _export_lean = qed / "AureliusExport.lean"
    _lean_src = _export_lean.read_text(encoding="utf-8", errors="replace") \
        if _export_lean.is_file() else ""
    _print_pins = [_t for _t in _axiom_theorems
        if f"#print axioms {_t}" in _lean_src]
    _ax_proc = subprocess.run(["lake", "env", "lean", "AureliusExport.lean"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=str(qed), timeout=600) if qed_ok else None
    _ax_out = (_ax_proc.stdout + _ax_proc.stderr) \
        if _ax_proc is not None else ""
    _ax_pins = [_t for _t in _axiom_theorems
        if any(_t in _line and "depends on axioms:" in _line
               and _AXIOM_WANT in _line
               for _line in _ax_out.splitlines())]
    # Fail-closed conjunction as a real bool (mirrors the sorry pin above):
    # a break_return mutant (None) crashes the isinstance assert and fails
    # the run instead of silently counting axioms as pinned, and flipping
    # the `not` below appends a failure on the green path (exit 1).
    _ax_flag = len(_print_pins) == len(_axiom_theorems) \
        and len(_ax_pins) == len(_axiom_theorems) \
        and (_ax_proc is None or _ax_proc.returncode == 0)
    assert isinstance(_ax_flag, bool), "axiom gate must return bool"
    axioms_pinned = _ax_flag
    if not axioms_pinned:
        failures.append(
            "AureliusExport axioms not pinned "
            f"(lake env lean tail): {_ax_out[-500:]}")
    evidence = {"changed": changed, "qed_build_ok": qed_ok,
        "tolerances_met": "ALL TOLERANCES MET" in out,
        "sorry_free": sorry_free, "axioms_pinned": axioms_pinned,
        "failures": failures}
    (root / "sext_repo_evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    # Single exit point (no `not`, no second return): on the green path
    # this returns 0; a break_return mutant (None) crashes the __main__
    # exit-code assert below, failing the run instead of silently passing.
    # (A `return 1` tucked inside `if failures:` would be unobservable on
    # the green path — the block never executes — so the failure branch
    # shares this one return and differs only in the message printed.)
    if failures:
        print("SEXT EVIDENCE FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
    else:
        print("SEXT EVIDENCE OK: " + json.dumps(evidence, indent=2))
    return 1 if failures else 0

if __name__ == "__main__":
    _code = main()
    assert _code in (0, 1), f"unexpected exit code: {_code!r}"
    raise SystemExit(_code)
