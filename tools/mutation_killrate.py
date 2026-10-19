"""Mutation kill-rate gate. Mutates a target in place and measures how many
mutants the suite kills, restoring the target around each mutant.

Restoration is done four independent ways (see GLOBAL_MINIMUM_REPORT.md for the
documented hazard of killed runs leaving the target mutated):

  1. `finally` around each mutant (normal path);
  2. SIGTERM/SIGINT handlers that restore and re-exit (Tether sends SIGTERM at
     the verification timeout, and SIGTERM does not raise in Python, so `finally`
     alone is not enough);
  3. an atexit handler (covers interpreter teardown paths that skip `finally`);
  4. on entry, repair a leftover from a previous interrupted run by comparing
     against `git show HEAD:<rel>` -- refusing to run if there is no committed
     version, rather than guessing which content is correct.

SIGKILL cannot be trapped by definition; that residual is left documented
rather than overclaimed.
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import os
import signal
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


def _committed_content(repo_root: Path, rel: str) -> bytes | None:
    """Return the committed bytes of `rel`, or None if there is no committed
    version (untracked file, or not a git repo)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"HEAD:{rel}"],
            capture_output=True, check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _restore(path: Path, original: bytes) -> None:
    """Idempotent restore: writing identical bytes is a no-op for the file
    content, and never raises on an already-correct file."""
    try:
        path.write_bytes(original)
    except OSError:
        pass


def measure(
    target: Path,
    repo_root: Path,
    suites: List[str],
    max_mutants: int = 0,
    runner_factory: Optional[Callable[[Path, List[str]], SuiteRunner]] = None,
) -> dict:
    """Mutate ``target`` (repo-relative POSIX path) and re-run ``suites``.

    Original file bytes are restored on every exit path, including SIGTERM and
    interpreter teardown. Returns a report dict with killed/survived/skipped
    counts, kill_rate, and one identifier line per surviving mutant.
    """
    from tether.verification import MUTATION_OPERATORS, generate_mutants

    factory = runner_factory or default_suite_runner
    run_suite = factory(repo_root, suites)
    target = Path(target)
    path = repo_root / target
    rel = target.as_posix()
    original = path.read_bytes()

    # Repair a leftover from a previously interrupted run before any mutant
    # is applied. Refuse to guess if there is no committed version.
    committed = _committed_content(repo_root, rel)
    if committed is not None and committed != original:
        sys.stderr.write(
            f"mutation_killrate: {rel} differs from HEAD; repairing leftover "
            f"from an interrupted run.\n")
        _restore(path, committed)
        original = path.read_bytes()
        text = original.decode("utf-8")
    elif committed is None:
        sys.stderr.write(
            f"mutation_killrate: {rel} has no committed version in HEAD; "
            f"refusing to mutate (cannot distinguish a leftover from a "
            f"legitimate change).\n")
        return {
            "target": rel,
            "suites": list(suites),
            "killed": 0,
            "survived": 0,
            "skipped": 0,
            "kill_rate": 0.0,
            "survivors": [],
            "refused": True,
        }
    else:
        text = original.decode("utf-8")

    cap = max_mutants if max_mutants > 0 else 10 ** 9
    mutants = generate_mutants(
        text, list(MUTATION_OPERATORS), seed=stable_seed(rel), max_mutants=cap)
    lines = text.splitlines()
    killed = survived = 0
    survivors: List[str] = []

    # Restore on SIGTERM/SIGINT (Tether's reaper sends SIGTERM) and on
    # interpreter teardown. `finally` alone is not enough because SIGTERM
    # does not raise in Python.
    def _handler(signum, _frame):
        _restore(path, original)
        raise SystemExit(128 + signum)

    _prev_term = signal.signal(signal.SIGTERM, _handler)
    _prev_int = signal.signal(signal.SIGINT, _handler)
    atexit.register(_restore, path, original)

    try:
        for m in mutants:
            try:
                path.write_text(m.source, encoding="utf-8")
                passed, _detail = run_suite()
                status = "survived" if passed else "killed"
            except Exception:  # noqa: BLE001 - a crashed suite kills
                status = "killed"
            finally:
                _restore(path, original)
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
        _restore(path, original)
        signal.signal(signal.SIGTERM, _prev_term)
        signal.signal(signal.SIGINT, _prev_int)

    denominator = killed + survived
    kill_rate = round(killed / denominator, 4) if denominator else 0.0
    return {
        "target": rel,
        "suites": list(suites),
        "killed": killed,
        "survived": survived,
        "skipped": 0,
        "kill_rate": kill_rate,
        "survivors": survivors,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Mutation kill-rate gate.")
    ap.add_argument("--repo-root", type=Path, required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--suite", action="append", required=True, dest="suites")
    ap.add_argument("--min-kill-rate", type=float, default=0.8)
    ap.add_argument("--max-mutants", type=int, default=0)
    ap.add_argument("--advisory", action="store_true")
    args = ap.parse_args()

    repo_root = args.repo_root.resolve()
    res = measure(
        Path(args.target), repo_root, args.suites, args.max_mutants)
    print(f"{res['kill_rate']:.4f} ({res['killed']}/{res['killed'] + res['survived']})")
    if res.get("refused"):
        return 2
    if res["kill_rate"] < args.min_kill_rate and not args.advisory:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())