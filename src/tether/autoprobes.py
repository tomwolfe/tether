"""LLM-synthesized behavioral probes (dogfood-43).

After an agent's execution is captured, this module lets Tether ask a
reviewer-class adapter to INVENT behavioral probes from the mission goal
plus the captured change, parse the response fail-safe into strict
:class:`ProbeSpec` objects, and — via :func:`summarize_teeth` — gate those
generated probes on their measured ability to kill real mutants of the
changed files. This breaks the manual-verification-authoring boundary:
verification content that adapts to what the agent actually did.

Fail-safe posture: any malformed model response raises
:class:`ProbeSynthesisError` and the caller records a synthesis failure;
the mission then simply falls back to its human-authored battery (today's
behavior), never to unverified success.
"""
from __future__ import annotations

import re
import shlex
from typing import Optional

import yaml

from tether.models import MutationSummary, MutantResult, ProbeSpec

# How much of the captured change artifact the synthesis prompt may embed.
AUTO_PROBE_CONTEXT_BUDGET = 64 * 1024

# Upper bound on accepted generated probes (deterministic truncation).
DEFAULT_MAX_PROBES = 6

# Single-command length cap; generated commands run shell=False exactly like
# declared probes, so the only extra risk surface is size.
AUTOPROBES_COMMAND_MAX_CHARS = 2000

# Reviewer output may carry ANSI escapes (dogfood-40); strip before parsing.
ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])")

_FENCE_RE = re.compile(r"```[ \t]*(?:ya?ml)?[ \t]*\r?\n(.*?)```", re.DOTALL)

_PROMPT_TEMPLATE = """\
You are acting as a verification engineer. Invent behavioral probes for the \
mission below: small non-interactive commands that EXERCISE the captured \
change and would FAIL (or go silent) if the change were broken or \
incomplete, and PASS on the finished work.

Mission goal:
{goal}

Captured change ({artifact_name}):
{excerpt}

Respond with EXACTLY ONE fenced yaml block shaped like:
```yaml
probes:
  - command: <single-line command, run with cwd = project root>
    contains: <literal substring required in combined stdout+stderr>
  - command: <single-line command>
    matches: <python regex required in combined stdout+stderr>
```

Rules:
- At least one of contains/matches per probe; both are allowed.
- Commands must be non-interactive, shell-free, deterministic, and finish \
quickly; prefer interpreter one-liners over the changed code paths.
- A probe's success marker must NOT be able to appear in any possible \
failure output of that same probe (a traceback echoes the command source, \
so never spell the marker inside the command; assemble it from fragments).
- The exit code is recorded but never decides: put the verdict in the \
output criteria.
"""

_PROMPT_COUNT_LINE = (
    "- Produce at most {max_probes} probes; the most behavioral ones first.\n")


class ProbeSynthesisError(ValueError):
    """A generated-probe response could not be parsed into valid specs."""


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences (dogfood-40 lesson, parser-side)."""
    return ANSI_ESCAPE_RE.sub("", text)


def build_synthesis_prompt(goal: str, artifact_name: str, excerpt: str,
                           max_probes: int = DEFAULT_MAX_PROBES) -> str:
    """Build the probe-synthesis prompt from goal + bounded change excerpt."""
    clipped = excerpt
    if len(clipped) > AUTO_PROBE_CONTEXT_BUDGET:
        half = AUTO_PROBE_CONTEXT_BUDGET // 2
        clipped = (
            clipped[:half]
            + f"\n... [truncated {len(excerpt) - AUTO_PROBE_CONTEXT_BUDGET} "
            "characters] ...\n"
            + clipped[-half:])
    return (
        _PROMPT_TEMPLATE.format(goal=goal, artifact_name=artifact_name,
                                excerpt=clipped or "(no change captured)")
        + _PROMPT_COUNT_LINE.format(max_probes=max_probes))


def _fail(reason: str) -> "ProbeSynthesisError":
    return ProbeSynthesisError(f"probe synthesis failed: {reason}")


def _validated_probe(index: int, entry: object) -> ProbeSpec:
    if not isinstance(entry, dict):
        raise _fail(f"probe[{index}] is not a mapping")
    if "command" not in entry:
        raise _fail(f"probe[{index}] has no 'command'")
    command = entry["command"]
    if not isinstance(command, str) or not command.strip():
        raise _fail(f"probe[{index}] 'command' must be a non-empty string")
    if len(command) > AUTOPROBES_COMMAND_MAX_CHARS:
        raise _fail(
            f"probe[{index}] 'command' exceeds "
            f"{AUTOPROBES_COMMAND_MAX_CHARS} characters")
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise _fail(f"probe[{index}] 'command' failed to parse: {e}") from e
    if not argv:
        raise _fail(f"probe[{index}] 'command' must be a non-empty string")
    contains = entry.get("contains")
    if contains is not None and (
            not isinstance(contains, str) or not contains):
        raise _fail(f"probe[{index}] 'contains' must be a non-empty string")
    matches = entry.get("matches")
    if matches is not None and (not isinstance(matches, str) or not matches):
        raise _fail(f"probe[{index}] 'matches' must be a non-empty string")
    if contains is None and matches is None:
        raise _fail(f"probe[{index}] requires 'contains' or 'matches'")
    if isinstance(matches, str):
        try:
            re.compile(matches)
        except re.error as e:
            raise _fail(
                f"probe[{index}] 'matches' is not a valid regex: {e}") from e
    return ProbeSpec(command=command, contains=contains, matches=matches)


def parse_generated_probes(
        response: str,
        max_probes: int = DEFAULT_MAX_PROBES) -> list[ProbeSpec]:
    """Parse a generator response into validated :class:`ProbeSpec` objects.

    Fail-safe by construction: ANSI is stripped first, the LAST fenced yaml
    block wins (earlier drafts are ignored), and ANY structural problem —
    missing fence, invalid YAML, wrong shape, empty list, malformed entry,
    unparsable command, bad regex — raises :class:`ProbeSynthesisError`.
    More than ``max_probes`` valid probes truncate deterministically to the
    first ``max_probes``.
    """
    cleaned = strip_ansi(response or "")
    fences = _FENCE_RE.findall(cleaned)
    if not fences:
        raise _fail("no fenced yaml block found in response")
    block = fences[-1]
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as e:
        raise _fail(f"fenced block is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise _fail("fenced block is not a mapping")
    raw = data.get("probes")
    if raw is None:
        raise _fail("fenced block has no 'probes' list")
    if not isinstance(raw, list):
        raise _fail("'probes' is not a list")
    if not raw:
        raise _fail("'probes' list is empty")
    specs = [_validated_probe(i, entry) for i, entry in enumerate(raw)]
    return specs[:max_probes]


def summarize_teeth(
        summary: MutationSummary,
        mutants: list[MutantResult],
        min_teeth_rate: Optional[float]) -> tuple[bool, str]:
    """Render one teeth measurement as ``(passed, operator text)``.

    Teeth = the fraction of mutants of the changed files that the GENERATED
    probes killed while passing on the pristine tree. With no runnable
    mutants (no changed ``.py`` targets) teeth are n/a and advisory-pass;
    with ``min_teeth_rate`` unset the result is advisory too. A configured
    rate fails when strictly below it, naming surviving sites so recovery
    can regenerate sharper probes next round.
    """
    denominator = summary.killed + summary.survived
    survivors = sorted(
        f"{m.file}:{m.site} [{m.operator}]" for m in mutants
        if m.status == "survived")
    shown = ", ".join(survivors[:5])
    if len(survivors) > 5:
        shown += f", +{len(survivors) - 5} more"
    if not survivors:
        shown = "(none)"
    if not denominator:
        return True, (
            "generated-probe teeth n/a (no mutants ran against the change); "
            f"surviving mutants: {shown}")
    core = (
        f"teeth {summary.kill_rate:.0%} (kill_rate {summary.kill_rate}, "
        f"killed {summary.killed}/{denominator} mutants)")
    if min_teeth_rate is None:
        return True, (
            f"generated-probe teeth (advisory): {core}; no min_teeth_rate "
            f"configured; surviving mutants: {shown}")
    if summary.kill_rate < min_teeth_rate:
        return False, (
            "generated probes lack teeth: they pass on the pristine tree "
            f"but catch almost nothing — {core} is below min_teeth_rate "
            f"{min_teeth_rate}; surviving mutants: {shown}")
    return True, (
        f"generated-probe teeth: {core} meets min_teeth_rate "
        f"{min_teeth_rate}; surviving mutants: {shown}")
