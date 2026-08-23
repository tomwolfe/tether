"""Verification engine: runs only explicitly declared commands, safely."""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import os
import random
import re
import shlex
import subprocess
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel

from tether.models import (
    ArtifactResult,
    AssertionResult,
    AssertionSpec,
    MutantStatus,
    MutationSpec,
    MutationSummary,
    MutantResult,
    ProbeSpec,
    VerificationResult,
)


class ProbeResult(BaseModel):
    """Outcome of one behavioral probe against the target project.

    ``passed`` reflects the probe's OUTPUT criteria (contains/matches over the
    combined stdout+stderr); ``exit_code`` is recorded for evidence but is NOT
    itself the pass criterion.
    """
    command: str
    passed: bool = False
    exit_code: Optional[int] = None
    matched: bool = False
    detail: str = ""


def run_verification(
    commands: list[str],
    project_dir: Path,
    timeout_seconds: int = 600,
    dry_run: bool = False,
) -> list[VerificationResult]:
    results: list[VerificationResult] = []
    for command in commands:
        if dry_run:
            results.append(VerificationResult(command=command, skipped_dry_run=True, passed=True))
            continue
        results.append(_run_one(command, project_dir, timeout_seconds))
    return results


def _run_one(command: str, project_dir: Path, timeout_seconds: int) -> VerificationResult:
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return VerificationResult(command=command, stderr=f"failed to parse command: {e}")
    if not argv:
        return VerificationResult(command=command, stderr="empty command")
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(project_dir),
            shell=False,
        )
    except FileNotFoundError:
        return VerificationResult(command=command, stderr=f"binary not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        return VerificationResult(command=command, timed_out=True,
                                  stderr=f"timed out after {timeout_seconds}s")
    except OSError as e:
        return VerificationResult(command=command, stderr=f"failed to execute: {e}")
    return VerificationResult(
        command=command,
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        passed=proc.returncode == 0,
    )


REPAIR_OUTPUT_BUDGET = 8192


def _project_files(project_dir: Path) -> list[str]:
    """Existing files under project_dir as sorted relative POSIX paths.

    Tether's own bookkeeping directory (.tether/) is excluded so audit
    artifacts can never satisfy a mission deliverable.
    """
    files: list[str] = []
    for root, dirnames, filenames in os.walk(project_dir):
        rel_root = os.path.relpath(root, project_dir)
        if rel_root == ".":
            # Prune before descending; os.walk does not follow symlinked
            # directories by default.
            dirnames[:] = [d for d in dirnames if d != ".tether"]
        for name in filenames:
            rel = name if rel_root == "." else f"{rel_root}{os.sep}{name}"
            files.append(Path(rel).as_posix())
    return sorted(files)


def check_artifacts(patterns: list[str], project_dir: Path) -> list[ArtifactResult]:
    """Match each artifact pattern against existing files in the target project.

    Patterns use fnmatch semantics relative to project_dir (same globs as the
    write sandbox); every pattern must match at least one existing file.
    """
    files = _project_files(project_dir)
    results: list[ArtifactResult] = []
    for pattern in patterns:
        matched = [f for f in files if fnmatch.fnmatch(f, pattern)]
        results.append(ArtifactResult(
            pattern=pattern,
            matched_files=matched,
            passed=bool(matched),
        ))
    return results


def summarize_artifacts(results: list[ArtifactResult]) -> tuple[bool, str]:
    """Return (all_matched, reason naming every unmatched pattern)."""
    unmatched = [r.pattern for r in results if not r.passed]
    if not unmatched:
        return True, ""
    return False, "missing required artifacts: " + ", ".join(unmatched)


def check_assertions(
    assertions: list[AssertionSpec],
    project_dir: Path,
) -> list[AssertionResult]:
    """Structural content checks over files in the target project.

    Each assertion glob-matches its ``path`` pattern against existing files
    (excluding .tether/, same walk as artifacts), then filters the matches
    by ``contains`` (literal substring) and/or ``matches`` (re.search) on
    file content read as UTF-8 with errors='replace'. An assertion passes
    when at least ``min_occurrences`` files satisfy all conditions.
    """
    files = _project_files(project_dir)
    results: list[AssertionResult] = []
    for assertion in assertions:
        candidates = [f for f in files if fnmatch.fnmatch(f, assertion.path)]
        matched: list[str] = []
        first_error = ""
        for relpath in candidates:
            try:
                text = (project_dir / relpath).read_text(
                    encoding="utf-8", errors="replace")
            except OSError as e:
                if not first_error:
                    first_error = f"{relpath}: unreadable ({e})"
                continue
            if assertion.contains is not None \
                    and assertion.contains not in text:
                continue
            if assertion.matches is not None \
                    and re.search(assertion.matches, text) is None:
                continue
            matched.append(relpath)
        enough = len(matched) >= assertion.min_occurrences
        detail = ""
        if not enough:
            if not candidates:
                detail = f"no files match {assertion.path}"
                if first_error:
                    detail += f"; {first_error}"
            else:
                detail = (
                    f"{len(matched)} of {len(candidates)} matching file(s) "
                    f"satisfied the assertion; min_occurrences is "
                    f"{assertion.min_occurrences}")
                if first_error:
                    detail += f"; {first_error}"
        results.append(AssertionResult(
            path=assertion.path,
            matched_files=matched,
            passed=enough,
            detail=detail,
        ))
    return results


def summarize_assertions(results: list[AssertionResult]) -> tuple[bool, str]:
    """Return (all_passed, reason naming every failing assertion)."""
    failed = [r for r in results if not r.passed]
    if not failed:
        return True, ""
    parts = []
    for r in failed:
        parts.append(f"{r.path}: {r.detail}" if r.detail else r.path)
    return False, "verification assertions failed: " + "; ".join(parts)


def run_probes(
    probes: list[ProbeSpec],
    project_dir: Path,
    timeout_seconds: int = 600,
    dry_run: bool = False,
) -> list[ProbeResult]:
    """Run behavioral probes and assert on their OUTPUT, not their exit code.

    Each probe's command runs via subprocess with shell=False, in the target
    project directory, with a timeout, capturing stdout+stderr. A probe PASSES
    when the combined output satisfies ALL of its ``contains``/``matches``
    criteria; the exit code is recorded but never decides the outcome. A
    timeout, missing binary, or unparseable command fails the probe with a
    clear detail. With ``dry_run`` set, no probe executes; each records
    ``passed=True`` with a "skipped (dry-run)" detail instead.
    """
    results: list[ProbeResult] = []
    for probe in probes:
        if dry_run:
            results.append(ProbeResult(command=probe.command,
                                       detail="skipped (dry-run)",
                                       passed=True))
            continue
        results.append(_run_probe(probe, project_dir, timeout_seconds))
    return results


def _run_probe(probe: ProbeSpec, project_dir: Path,
               timeout_seconds: int) -> ProbeResult:
    try:
        argv = shlex.split(probe.command)
    except ValueError as e:
        return ProbeResult(command=probe.command,
                           detail=f"failed to parse command: {e}")
    if not argv:
        return ProbeResult(command=probe.command, detail="empty command")
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(project_dir),
            shell=False,
        )
    except FileNotFoundError:
        return ProbeResult(command=probe.command,
                           detail=f"binary not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        return ProbeResult(command=probe.command,
                           detail=f"timed out after {timeout_seconds}s")
    except OSError as e:
        return ProbeResult(command=probe.command,
                           detail=f"failed to execute: {e}")
    output = f"{proc.stdout}{proc.stderr}"
    ok = True
    if probe.contains is not None and probe.contains not in output:
        ok = False
    if probe.matches is not None and re.search(probe.matches, output) is None:
        ok = False
    detail = ""
    if not ok:
        missing = []
        if probe.contains is not None and probe.contains not in output:
            missing.append(f"output does not contain {probe.contains!r}")
        if probe.matches is not None \
                and re.search(probe.matches, output) is None:
            missing.append(f"output does not match {probe.matches!r}")
        detail = "; ".join(missing)
    return ProbeResult(
        command=probe.command,
        passed=ok,
        exit_code=proc.returncode,
        matched=ok,
        detail=detail,
    )


def summarize_probes(results: list[ProbeResult]) -> tuple[bool, str]:
    """Return (all_passed, reason naming every failing probe)."""
    failed = [r for r in results if not r.passed]
    if not failed:
        return True, ""
    parts = []
    for r in failed:
        parts.append(f"{r.command}: {r.detail}" if r.detail else r.command)
    return False, "verification probes failed: " + "; ".join(parts)


def clip_output(text: str, budget: int = REPAIR_OUTPUT_BUDGET) -> str:
    """Clip text to ~budget chars, keeping head and tail with a marker."""
    if len(text) <= budget:
        return text
    half = budget // 2
    return (
        text[:half]
        + f"\n... [truncated {len(text) - budget} characters; full output in audit] ...\n"
        + text[-half:]
    )


def summarize(results: list[VerificationResult]) -> tuple[bool, str]:
    """Return (all_passed, combined failing output for recovery prompts).

    The combined output is clipped to a bounded budget so repair prompts stay
    small; full output remains available in the audit records.
    """
    failures = [r for r in results if not r.passed]
    if not failures:
        return True, ""
    per_command = max(REPAIR_OUTPUT_BUDGET // len(failures), 512)
    parts = []
    for r in failures:
        reason = "timed out" if r.timed_out else f"exit code {r.exit_code}"
        body = clip_output(f"{r.stdout}\n{r.stderr}".strip(), per_command)
        parts.append(f"--- COMMAND: {r.command} ({reason}) ---\n{body}")
    return False, "\n\n".join(parts)


# Coarse failure classes used to tailor recovery prompts (dogfood-14).
COMPILE_ERROR_PATTERNS: tuple[str, ...] = (
    "error:", "SyntaxError", "TypeError", "ImportError",
    "cannot find", "No such file",
)
TEST_FAILURE_PATTERNS: tuple[str, ...] = (
    "FAILED", "assert", "AssertionError", "test_",
)


def classify_failure(results: list[VerificationResult]) -> str:
    """Classify a failed verification attempt into one coarse class.

    Pure helper over the results (no I/O). Precedence per spec:
    timeout > missing_binary > compile_error > test_failure > unknown.
    Returns "compile_error", "test_failure", "timeout", "missing_binary",
    or "unknown".
    """
    if any(r.timed_out for r in results):
        return "timeout"
    if any("not found" in r.stderr for r in results):
        return "missing_binary"
    failing = [r for r in results
               if r.exit_code is not None and r.exit_code != 0]
    if any(any(p in r.stderr for p in COMPILE_ERROR_PATTERNS)
           for r in failing):
        return "compile_error"
    if any(any(p in r.stdout or p in r.stderr for p in TEST_FAILURE_PATTERNS)
           for r in failing):
        return "test_failure"
    return "unknown"


# --------------------------------------------------------------------------
# Mutation testing (dogfood-22): a meta-verification layer that measures
# whether the declared checks would CATCH an incorrect change. Pure stdlib
# `ast` mutant generation plus suite re-execution over each mutant.
# --------------------------------------------------------------------------

MUTATION_OPERATORS: tuple[str, ...] = (
    "negate_compare",   # == <-> !=,  < <-> >=,  <= <-> >
    "flip_bool",        # True <-> False,  `not x` <-> x
    "arithmetic",       # + <-> -,  * <-> /
    "break_return",     # `return expr` -> `return None`
)

# Bounded per-mutant failure detail recorded in mutation.json / the report.
_MUTATION_DETAIL_BUDGET = 500

_COMPARE_SWAPS: dict[type, type] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.LtE: ast.Gt,
    ast.Gt: ast.LtE,
}

_ARITHMETIC_SWAPS: dict[type, type] = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.Div,
    ast.Div: ast.Mult,
}


class Mutant(BaseModel):
    """One candidate mutation of a source file (dogfood-22)."""
    operator: str
    site: str      # "<line>:<col>" of the mutated node in the original
    source: str    # full mutated source text (syntactically valid Python)


def _child_slots(node: ast.AST):
    """Deterministic (field, list-index-or-None, child) triples of a node."""
    for field, value in ast.iter_fields(node):
        if isinstance(value, ast.AST):
            yield field, None, value
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                if isinstance(item, ast.AST):
                    yield field, idx, item


def _site_id(node: ast.AST) -> str:
    return f"{getattr(node, 'lineno', 0)}:{getattr(node, 'col_offset', 0)}"


# A site path is the chain of (field, index) steps from the tree root to the
# mutated node; re-parsing identical source reproduces identical paths.
_SitePath = tuple[tuple[str, Optional[int]], ...]


def _node_at(tree: ast.AST, path: _SitePath) -> ast.AST:
    node = tree
    for field, idx in path:
        value = getattr(node, field)
        node = value[idx] if idx is not None else value
    return node


def _collect_sites(tree: ast.AST, operators: set[str]) -> \
        list[tuple[str, _SitePath, str]]:
    """One mutant site per mutable node, in stable DFS walk order.

    Each AST node matches at most one operator, so sites are unique per node;
    paths address the exact node so a fresh parse can apply the mutation.
    """
    sites: list[tuple[str, _SitePath, str]] = []

    def visit(node: ast.AST, path: _SitePath) -> None:
        if "negate_compare" in operators \
                and isinstance(node, ast.Compare) \
                and len(node.ops) == 1 \
                and type(node.ops[0]) in _COMPARE_SWAPS:
            sites.append(("negate_compare", path, _site_id(node)))
        elif "flip_bool" in operators \
                and isinstance(node, ast.Constant) \
                and (node.value is True or node.value is False):
            sites.append(("flip_bool", path, _site_id(node)))
        elif "flip_bool" in operators \
                and isinstance(node, ast.UnaryOp) \
                and isinstance(node.op, ast.Not):
            # `not x` -> x replaces this UnaryOp with its operand; the path
            # addresses the UnaryOp so the parent swap happens at apply time.
            sites.append(("flip_bool", path, _site_id(node)))
        elif "arithmetic" in operators \
                and isinstance(node, ast.BinOp) \
                and type(node.op) in _ARITHMETIC_SWAPS:
            sites.append(("arithmetic", path, _site_id(node)))
        elif "break_return" in operators \
                and isinstance(node, ast.Return) \
                and node.value is not None:
            sites.append(("break_return", path, _site_id(node)))
        for field, idx, child in _child_slots(node):
            visit(child, path + ((field, idx),))

    visit(tree, ())
    return sites


def generate_mutants(source: str, operators: list[str],
                     seed: int, max_mutants: int) -> list[Mutant]:
    """Pure, deterministic mutant generator over one Python source string.

    Enumerates one mutant per mutable site in stable AST walk order; when
    more sites exist than ``max_mutants``, selects a seeded random subset
    (indices sorted back into walk order). Files that do not parse yield no
    mutants (the caller records them as skipped). Unknown operator names
    raise ValueError.
    """
    unknown = [o for o in operators if o not in MUTATION_OPERATORS]
    if unknown:
        raise ValueError(
            f"unknown mutation operator(s): {', '.join(unknown)}")
    try:
        ast.parse(source)
    except SyntaxError:
        return []
    sites = _collect_sites(ast.parse(source), set(operators))
    chosen: list[int]
    if len(sites) > max_mutants:
        rng = random.Random(seed)
        chosen = sorted(rng.sample(range(len(sites)), max_mutants))
    else:
        chosen = list(range(len(sites)))
    mutants: list[Mutant] = []
    for i in chosen:
        operator, path, site = sites[i]
        fresh = ast.parse(source)
        node = _node_at(fresh, path)
        parent_path = path[:-1]
        if operator == "negate_compare":
            node.ops[0] = _COMPARE_SWAPS[type(node.ops[0])]()  # type: ignore[attr-defined]
        elif operator == "flip_bool" and isinstance(node, ast.Constant):
            node.value = not node.value  # type: ignore[attr-defined]
        elif operator == "flip_bool":
            # `not x` -> x: point the parent at the operand instead.
            operand = node.operand  # type: ignore[attr-defined]
            field, idx = path[-1]
            parent = _node_at(fresh, parent_path)
            container = getattr(parent, field)
            if idx is None:
                setattr(parent, field, operand)
            else:
                container[idx] = operand
        elif operator == "arithmetic":
            node.op = _ARITHMETIC_SWAPS[type(node.op)]()  # type: ignore[attr-defined]
        else:  # break_return
            node.value = ast.Constant(value=None)  # type: ignore[attr-defined]
        mutants.append(Mutant(
            operator=operator, site=site, source=ast.unparse(fresh)))
    return mutants


def run_mutation_testing(
    spec: MutationSpec,
    changed_files: list[str],
    project_dir: Path,
    run_suite: Callable[[], tuple[bool, str]],
    timeout_seconds: int = 600,
    collect_results: Optional[list[MutantResult]] = None,
) -> MutationSummary:
    """Mutate changed ``.py`` files and re-run the suite per mutant.

    ``run_suite()`` must re-run the SAME verification helpers used on the
    green attempt (run_verification + check_assertions + run_probes over the
    declared commands/assertions/probes) and return ``(passed, detail)``. A
    passing suite against a mutant counts as SURVIVED; a failing (or
    crashing) suite counts as killed. Original file bytes are restored in a
    try/finally around every mutant so the project tree stays byte-identical
    after the run. Only files ending in ``.py`` are touched; anything under
    ``.tether/`` is never mutated. ``timeout_seconds`` is accepted for
    signature parity; the bound run_suite closure owns its own deadlines.
    When ``collect_results`` is a list, per-mutant :class:`MutantResult`
    entries are appended to it so callers can persist full detail without a
    second pass (the return value stays the aggregate summary).
    """
    operators = list(spec.operators) \
        if spec.operators is not None else list(MUTATION_OPERATORS)
    targets: list[str] = []
    for rel in sorted(changed_files):
        posix = Path(rel).as_posix()
        if not posix.endswith(".py"):
            continue
        if posix == ".tether" or posix.startswith(".tether/"):
            continue
        targets.append(posix)

    results: list[MutantResult] = []
    for rel in targets:
        path = project_dir / rel
        try:
            original = path.read_bytes()
        except OSError as e:
            results.append(MutantResult(
                file=rel, operator="", site="", status="skipped",
                detail=f"unreadable file ({e})"))
            continue
        try:
            text = original.decode("utf-8")
        except UnicodeDecodeError:
            results.append(MutantResult(
                file=rel, operator="", site="", status="skipped",
                detail="file is not valid UTF-8"))
            continue
        try:
            ast.parse(text)
        except SyntaxError as e:
            results.append(MutantResult(
                file=rel, operator="", site="", status="skipped",
                detail=f"file does not parse ({e})"))
            continue
        # Deterministic per-file seed derived from the stable relative path.
        seed = int.from_bytes(
            hashlib.sha256(rel.encode("utf-8")).digest()[:8], "big")
        mutants = generate_mutants(
            text, operators, seed=seed, max_mutants=spec.max_mutants)
        for m in mutants:
            status: MutantStatus = "killed"
            detail_text = ""
            try:
                path.write_text(m.source, encoding="utf-8")
                passed, suite_detail = run_suite()
                status = "survived" if passed else "killed"
                detail_text = "" if passed else clip_output(
                    suite_detail, _MUTATION_DETAIL_BUDGET)
            except Exception as e:  # noqa: BLE001 - a crashed suite kills
                status, detail_text = "killed", f"suite error: {e!r}"
            finally:
                try:
                    path.write_bytes(original)
                except OSError as e:  # pragma: no cover - best-effort
                    detail_text = detail_text or f"restore failed ({e})"
            results.append(MutantResult(
                file=rel, operator=m.operator, site=m.site,
                status=status, detail=detail_text))
    if collect_results is not None:
        collect_results.extend(results)

    killed = sum(1 for r in results if r.status == "killed")
    survived = sum(1 for r in results if r.status == "survived")
    skipped = sum(1 for r in results if r.status == "skipped")
    denominator = killed + survived
    kill_rate = round(killed / denominator, 4) if denominator else 0.0
    per_file: dict[str, dict[str, int]] = {}
    for r in results:
        counts = per_file.setdefault(r.file, {"killed": 0, "survived": 0})
        if r.status in ("killed", "survived"):
            counts[r.status] += 1
    return MutationSummary(
        total=len(results), killed=killed, survived=survived,
        skipped=skipped, kill_rate=kill_rate, per_file=per_file,
    )


# Survivor identifiers shown in operator-facing summaries before truncation.
_MUTATION_SURVIVOR_CAP = 5


def summarize_mutation(
    summary: MutationSummary,
    mutants: list[MutantResult],
    fail_below: Optional[float] = None,
) -> tuple[bool, str]:
    """Render one mutation-testing run as ``(passed, operator text)``.

    ``passed`` mirrors the historical in-orchestrator gate: False iff
    ``fail_below`` is configured, at least one mutant actually ran
    (killed + survived > 0), and the measured kill rate is below it.
    The returned kill rate summary always states the measured kill rate as
    a percentage with killed/total counts, the configured threshold (or
    "advisory" when unset), and the surviving mutant identifiers as
    ``file:site [operator]`` (capped display), so an operator can act
    without re-reading the raw mutation.json.
    """
    denominator = summary.killed + summary.survived
    if denominator:
        core = (
            f"kill rate {summary.kill_rate:.0%} "
            f"(kill_rate {summary.kill_rate}, killed "
            f"{summary.killed}/{denominator} mutants)")
    else:
        core = ("kill rate n/a (kill_rate "
                f"{summary.kill_rate}, no mutants ran)")
    survivors = sorted(
        f"{m.file}:{m.site} [{m.operator}]" for m in mutants
        if m.status == "survived")
    shown = survivors[:_MUTATION_SURVIVOR_CAP]
    overflow = len(survivors) - _MUTATION_SURVIVOR_CAP
    survivor_text = ", ".join(shown) + (f", +{overflow} more" if overflow > 0
                                        else "") if survivors else "(none)"
    if fail_below is None:
        return True, (
            f"mutation testing (advisory): {core}; no fail_below "
            f"configured; surviving mutants: {survivor_text}")
    if denominator > 0 and summary.kill_rate < fail_below:
        return False, (
            f"mutation testing exposed weak verification: {core} is below "
            f"fail_below {fail_below}; surviving mutants: {survivor_text}")
    return True, (
        f"mutation testing: {core} meets fail_below {fail_below}; "
        f"surviving mutants: {survivor_text}")
