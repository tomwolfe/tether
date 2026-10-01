"""LLM-synthesized behavioral probes (dogfood-43).

After an agent's execution is captured, this module lets Tether ask a
reviewer-class adapter to INVENT behavioral probes from the mission goal
plus the captured change, parse the response fail-safe into strict
:class:`ProbeSpec` objects, and — via :func:`summarize_teeth` — gate those
generated probes on their measured ability to kill real mutants of the
changed files. This breaks the manual-verification-authoring boundary:
verification content that adapts to what the agent actually did.

Fail-safe posture: a response that yields NO valid probe raises
:class:`ProbeSynthesisError` and the caller records a synthesis failure;
the mission then simply falls back to its human-authored battery (today's
behavior), never to unverified success. One broken entry never costs the
response its other probes — salvage is per entry, at the YAML level and at
the spec level alike.
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

# A generator that answers through a shell-quoted context escapes the fence
# markers along with the rest of its command text, so the block arrives as
# ``\`\`\`yaml``. An optional backslash before each backtick is that echo and
# nothing else; a response with no fence at all still has no match.
_FENCE_RE = re.compile(
    r"(?:\\?`){3}[ \t]*(?:ya?ml)?[ \t]*\r?\n(.*?)(?:\\?`){3}", re.DOTALL)

_PROMPT_TEMPLATE = """\
You are acting as a verification engineer. Invent behavioral probes for the \
mission below: small non-interactive commands that EXERCISE the captured \
change and would FAIL (or go silent) if the change were broken or \
incomplete, and PASS on the finished work.

Mission goal:
{goal}

Captured change ({artifact_name}):
{excerpt}

Respond with EXACTLY ONE fenced yaml block shaped exactly like this example:
```yaml
probes:
  - command: "python -c \\"import mymodule; print(mymodule.test())\\""
    contains: "expected_output"
  - command: 'python -m pytest -q tests/test_mymodule.py'
    matches: '1 passed'
```

Quoting (the examples above are the rule, restated):
- A command that contains a double quote, a colon followed by a space, or a \
leading special character MUST be written as a double-quoted YAML scalar \
with every inner double quote escaped as \\". Unquoted, YAML reads only the \
text up to the first quote and then chokes on the rest, so the probe is \
silently dropped and less gets verified than looks verified.
- Otherwise prefer a single-quoted YAML scalar, e.g. command: 'python -m \
pytest -q', whenever the command itself contains no single quote: single \
quotes take backslashes, colons and inner double quotes literally, so a \
regex or a shell fragment cannot break out of the scalar.
- Put a real command and a real marker in every field; a bracketed \
placeholder left in a field is rejected and never executed.

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


# A generator that cannot solve the task will often echo the schema back
# verbatim -- "<single-line command, run with cwd = project root>" and
# friends. Those entries are structurally VALID: they are non-empty strings,
# they shlex-parse, and they carry a `contains`. Passing them to the probe
# runner then execs a program literally named "<single-line", which fails
# with "binary not found" and burns a whole verification attempt on
# nonsense. A real executable name never contains angle brackets, and a real
# literal assertion is never itself a bracketed metavariable, so both are
# unambiguous placeholders and are rejected instead of executed.
_PLACEHOLDER_RE = re.compile(r"<[^<>\n]+>")


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
    if "<" in argv[0] or ">" in argv[0]:
        raise _fail(
            f"probe[{index}] 'command' echoes the schema template "
            f"(executable {argv[0]!r} is a placeholder, not a program)")
    contains = entry.get("contains")
    if isinstance(contains, str) and _PLACEHOLDER_RE.fullmatch(contains.strip()):
        raise _fail(
            f"probe[{index}] 'contains' is a schema placeholder, not a "
            f"literal to assert on")
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


def _valid_specs(entries: list[object]) -> list[ProbeSpec]:
    """Validate every entry, keeping the ones that survive."""
    specs: list[ProbeSpec] = []
    for i, entry in enumerate(entries):
        try:
            specs.append(_validated_probe(i, entry))
        except ProbeSynthesisError:
            continue
    return specs


# A ``key: value`` line inside a probe list item. Only the key, its colon
# and the separating blanks are captured so the value can be re-quoted
# without disturbing the item's indentation or a trailing comment.
_KEYED_LINE_RE = re.compile(
    r"^(?P<prefix>[ \t]*(?:-[ \t]+)?[A-Za-z_][A-Za-z0-9_]*:[ \t]*)"
    r"(?P<value>\S.*?)[ \t]*(?P<tail>\#.*)?$")

# Whatever trails a double-quoted scalar's closing quote: blanks, a comment,
# or nothing. Anything else means the value is not what this repair assumes.
_SCALAR_TAIL_RE = re.compile(r"[ \t]*(?:\#.*)?")

# The only two escapes a shell fragment legitimately needs once it is being
# re-emitted as a single-quoted scalar: an escaped backslash and an escaped
# double quote. Every other backslash (a regex ``\d``, a Windows path, a
# literal newline inside a fragment) is data and must survive verbatim.
_DQ_ESCAPE_RE = re.compile(r"\\(.)")


def _relax_double_quoted_scalars(text: str) -> str:
    r"""Re-emit unreadable ``key: "..."`` lines as ``key: '...'``.

    A model that wraps a shell command in double quotes routinely emits
    fragments YAML refuses: a raw regex backslash (``"grep '\d+' f"``) is an
    unknown escape, and inner double quotes go unescaped. That is a YAML
    error, so it takes the whole block down with it. Single-quoted YAML
    takes both literally, which is exactly what a shell fragment wants.

    Only lines YAML actually chokes on are rewritten, so a properly escaped
    scalar keeps its own interpretation.
    """
    return "\n".join(_relax_scalar_line(line) for line in text.split("\n"))


def _relax_single_quoted_scalars(text: str) -> str:
    r"""Repair ``key: '...''...'`` — inner single quotes left unescaped.

    The double-quote repair above covers a model that wraps a command in
    double quotes. The mirror failure is a model that wraps it in single
    quotes but forgets to double the shell's own inner quotes:
    ``command: 'python -c 'print(1)''``. YAML reads that as the scalar
    ``python -c 'print(1)`` followed by stray text, so the whole block
    fails to parse and the probes are lost.

    The repair doubles every inner ``'``, which is exactly what YAML
    requires to mean "a literal quote inside a single-quoted scalar", so
    the value the model meant survives verbatim. Only lines YAML actually
    chokes on are rewritten, and a correctly escaped scalar keeps its own
    interpretation.
    """
    return "\n".join(_relax_single_quoted_line(line) for line in text.split("\n"))


def _relax_single_quoted_line(line: str) -> str:
    match = _KEYED_LINE_RE.match(line)
    if match is None:
        return line
    value = match.group("value")
    if not value.startswith("'"):
        return line
    # The closing quote is the LAST one whose trailing text is still a plain
    # comment. For `python -c 'print(1)'` the final quote closes the scalar
    # and everything between the first quote and it is data.
    closing = value.rfind("'")
    if closing <= 0:
        return line
    inner, tail = value[1:closing], value[closing + 1:]
    if not _SCALAR_TAIL_RE.fullmatch(tail):
        return line
    try:
        yaml.safe_load("'" + inner + "'")
    except yaml.YAMLError:
        pass
    else:
        return line
    # The model opened the scalar and then wrote the shell's own quotes
    # unescaped, so the final quote of the value is ambiguous: it may be the
    # wrapper's close, or the close of the shell argument the wrapper was
    # supposed to contain. An odd number of inner quotes is exactly the case
    # where the two collapsed into one and the shell argument is left open;
    # closing it restores the command the model meant. The count parity is
    # what decides, so a correctly escaped scalar (always an even count)
    # is left exactly as written.
    if inner.count(chr(39)) % 2:
        inner += chr(39)
    return f"{match.group('prefix')}'{inner.replace(chr(39), chr(39) * 2)}'{tail}"


def _relax_scalar_line(line: str) -> str:
    match = _KEYED_LINE_RE.match(line)
    if match is None:
        return line
    value = match.group("value")
    if not value.startswith('"'):
        return line
    closing = value.rfind('"')
    if closing <= 0:
        return line
    inner, tail = value[1:closing], value[closing + 1:]
    if not _SCALAR_TAIL_RE.fullmatch(tail):
        return line
    try:
        yaml.safe_load('"' + inner + '"')
    except yaml.YAMLError:
        pass
    else:
        return line
    literal = _DQ_ESCAPE_RE.sub(
        lambda m: "" if m.group(1) == '"' else "\\", inner).replace("'", "''")
    return f"{match.group('prefix')}'{literal}'{tail}"


_PROBES_KEY_RE = re.compile(r"^[ \t]*probes[ \t]*:")


def _iter_probe_entry_chunks(block: str) -> list[str]:
    """Split the ``probes`` list body of ``block`` into one text chunk per item.

    Purely textual and deliberately dumb: the point is to hand each ``- ``
    item to the YAML parser on its own so that one unparsable entry cannot
    hide its siblings. Continuation lines (deeper indentation) stay with the
    item they belong to, and the body ends at the first line that is neither.
    """
    lines = block.splitlines()
    body_start = None
    for i, line in enumerate(lines):
        if _PROBES_KEY_RE.match(line):
            body_start = i + 1
            break
    if body_start is None:
        return []
    chunks: list[str] = []
    current: list[str] = []
    item_indent: Optional[int] = None
    for line in lines[body_start:]:
        if not line.strip():
            if current:
                current.append(line)
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        stripped = line.strip()
        is_item = stripped == "-" or stripped.startswith("- ")
        if item_indent is None:
            if not is_item:
                if current:
                    break
                continue
            item_indent = indent
        if is_item and indent == item_indent:
            chunks.append("\n".join(current).rstrip("\n"))
            current = []
        elif indent <= item_indent:
            break
        current.append(line)
    if current:
        chunks.append("\n".join(current).rstrip("\n"))
    return [chunk for chunk in chunks if chunk.strip()]


def _read_probe_list(block: str) -> tuple[Optional[list[object]], str]:
    """Read the whole block as YAML, returning ``(entries, blocked_by)``.

    ``entries`` is the raw list when the block is readable and shaped right;
    otherwise it is ``None`` and ``blocked_by`` is the reason the block
    could not be used as-is, which becomes the failure text if salvage also
    comes up empty.
    """
    blocked_by = ""
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as e:
        blocked_by = f"fenced block is not valid YAML: {e}"
        data = None
    if data is None:
        return None, blocked_by
    if not isinstance(data, dict):
        return None, "fenced block is not a mapping"
    raw = data.get("probes")
    if raw is None:
        return None, "fenced block has no 'probes' list"
    if not isinstance(raw, list):
        return None, "'probes' is not a list"
    if not raw:
        return None, "'probes' list is empty"
    return raw, blocked_by


def _parse_entry_chunk(chunk: str) -> list[object]:
    """Parse one raw list item as the sole entry of a ``probes`` list.

    Returns an empty list when the chunk stays unreadable — the caller then
    treats that entry as malformed and moves on to the next one.
    """
    document = "probes:\n" + chunk
    repairs = (None, _relax_double_quoted_scalars, _relax_single_quoted_scalars)
    for relax in repairs:
        text = document if relax is None else relax(document)
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and isinstance(data.get("probes"), list):
            return data["probes"]
        return []
    return []


def _salvaged_specs(block: str) -> list[ProbeSpec]:
    """Recover probes from a block that would not parse as a whole.

    Each ``- `` item is read and validated independently, so a single
    unparsable entry costs only itself. Unreadable items are dropped, never
    guessed at: a chunk that still will not parse after either scalar repair
    — a flow sequence the model left open, say — simply yields nothing.
    """
    specs: list[ProbeSpec] = []
    examined = 0
    for chunk in _iter_probe_entry_chunks(block):
        for entry in _parse_entry_chunk(chunk):
            try:
                specs.append(_validated_probe(examined, entry))
            except ProbeSynthesisError:
                pass
            examined += 1
    return specs


def parse_generated_probes(
        response: str,
        max_probes: int = DEFAULT_MAX_PROBES) -> list[ProbeSpec]:
    """Parse a generator response into validated :class:`ProbeSpec` objects.

    Fail-safe by construction: ANSI is stripped first, the LAST fenced yaml
    block wins (earlier drafts are ignored, and a fence whose markers the
    model backslash-escaped still counts as a fence), and a response that
    yields no valid probe — missing fence, unreadable block, wrong shape,
    empty list, every entry malformed — raises :class:`ProbeSynthesisError`.

    One bad entry does not sink the response. Salvage is per entry at two
    layers: the whole block is read as YAML first and each entry is then
    validated on its own (a malformed entry is skipped); and if the block
    cannot be read as YAML at all — the common real-model failure being a
    shell command's quotes nested inside a quoted scalar — the ``probes``
    list is re-read one list item at a time so the readable entries still
    parse. More than ``max_probes`` valid probes truncate deterministically
    to the first ``max_probes``.
    """
    cleaned = strip_ansi(response or "")
    fences = _FENCE_RE.findall(cleaned)
    if not fences:
        raise _fail("no fenced yaml block found in response")
    block = fences[-1]
    entries, blocked_by = _read_probe_list(block)
    if entries is None:
        specs = _salvaged_specs(block)
        if not specs:
            raise _fail(blocked_by or "no probe entries could be read")
        return specs[:max_probes]
    specs = _valid_specs(entries)
    if not specs:
        raise _fail("no valid probes after salvage; all entries malformed")
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
