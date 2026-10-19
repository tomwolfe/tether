"""Mutation kill-rate measurement for an explicit (target file, suite) pair.

Tether's built-in mutation tier derives its targets from the agent's changed
files, so it cannot express "measure module X against exactly suite Y" — the
shape every quantitative-strength audit needs. This tool reuses tether's own
deterministic mutant generator (:func:`tether.verification.generate_mutants`,
same seed derivation as ``run_mutation_testing``) against an explicit pytest
subset, restores the target byte-for-byte around every mutant, and gates on a
minimum kill rate.

Exit codes: 0 = at/above the gate (or advisory), 2 = below the gate,
1 = usage or harness error.
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

SuiteRunner = Callable[[], tuple[bool, str]]


def default_suite_runner(repo_root: Path, suites: List[str]) -> SuiteRunner:
    """Run the pytest subset via the same interpreter running this tool."""
    cmd = [sys.executable, "-m", "pytest", "-x", "-q",
           "-p", "no:cacheprovider", *suites]

    def run() -> tuple[bool, str]:
        proc = subprocess.run(
            cmd, cwd=str(repo_root), capture_output=True, text=True,
            timeout=1800, shell=False,
        )
        detail = (proc.stdout or "")[-2000:] + (proc.stderr or "")[-1000:]
        return proc.returncode == 0, detail

    return run


def stable_seed(rel_target: str) -> int:
    """Same per-file seed derivation as verification.run_mutation_testing."""
    return int.from_bytes(
        hashlib.sha256(rel_target.encode("utf-8")).digest()[:8], "big")


def measure(
    target: Path,
    repo_root: Path,
    suites: List[str],
    max_mutants: int = 0,
    runner_factory: Optional[Callable[[Path, List[str]], SuiteRunner]] = None,
) -> dict:
    """Mutate ``target`` (repo-relative POSIX path) and re-run ``suites``.

    Returns a report dict with killed/survived/skipped counts, kill_rate,
    and one identifier line per surviving mutant. Original file bytes are
    restored in a finally block around every mutant. A crashed suite counts
    as killed, mirroring production semantics.
    """
    from tether.verification import MUTATION_OPERATORS, generate_mutants

    factory = runner_factory or default_suite_runner
    run_suite = factory(repo_root, suites)
    target = Path(target)
    path = repo_root / target
    rel = target.as_posix()
    original = path.read_bytes()
    text = original.decode("utf-8")
    cap = max_mutants if max_mutants > 0 else 10 ** 9
    mutants = generate_mutants(
        text, list(MUTATION_OPERATORS), seed=stable_seed(rel), max_mutants=cap)
    lines = text.splitlines()
    killed = survived = 0
    survivors: List[str] = []
    try:
        for m in mutants:
            try:
                path.write_text(m.source, encoding="utf-8")
                passed, _detail = run_suite()
                status = "survived" if passed else "killed"
            except Exception:  # noqa: BLE001 - a crashed suite kills
                status = "killed"
            finally:
                path.write_bytes(original)
            if status == "killed":
                killed += 1
            else:
                survived += 1
                lineno = int(m.site.split(":")[0])
                source_line = (lines[lineno - 1].strip()
                               if lineno <= len(lines) else "?")
                survivors.append(
                    f"{rel}:{m.site} [{m.operator}] {source_line}")
    finally:
        path.write_bytes(original)
    denominator = killed + survived
    kill_rate = round(killed / denominator, 4) if denominator else 0.0
    return {
        "target": rel,
        "suites": list(suites),
        "generated": len(mutants),
        "killed": killed,
        "survived": survived,
        "kill_rate": kill_rate,
        "survivors": survivors,
    }


def format_report(report: dict) -> str:
    denom = report["killed"] + report["survived"]
    head = (
        f"mutation kill rate for {report['target']} "
        f"against {' '.join(report['suites'])}: "
        f"{report['kill_rate']:.4f} "
        f"(killed {report['killed']}/{denom} of "
        f"{report['generated']} generated mutants)")
    if report["survivors"]:
        return head + "\nsurviving mutants:\n" + \
            "\n".join(f"  {s}" for s in report["survivors"])
    return head + "\nsurviving mutants: (none)"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure a mutation kill rate for one file vs one suite.")
    parser.add_argument("--target", required=True,
                        help="repo-relative path of the .py file to mutate")
    parser.add_argument("--suite", action="append", required=True,
                        help="pytest path passed to the per-mutant suite; "
                             "repeatable")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT,
                        help="repository root (default: this checkout)")
    parser.add_argument("--max-mutants", type=int, default=0,
                        help="deterministic cap on mutants; 0 = all sites")
    parser.add_argument("--min-kill-rate", type=float, default=None,
                        help="fail with exit code 2 when the measured kill "
                             "rate is below this value; omit = advisory")
    args = parser.parse_args(argv)
    if not args.target.endswith(".py"):
        print(f"target must be a .py file: {args.target}", file=sys.stderr)
        return 1
    if not (args.repo_root / args.target).is_file():
        print(f"target not found: {args.repo_root / args.target}",
              file=sys.stderr)
        return 1
    report = measure(
        target=args.target, repo_root=args.repo_root, suites=args.suite,
        max_mutants=args.max_mutants)
    print(format_report(report))
    if args.min_kill_rate is not None \
            and report["kill_rate"] < args.min_kill_rate:
        print(
            f"FAIL: kill rate {report['kill_rate']:.4f} is below "
            f"--min-kill-rate {args.min_kill_rate}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
