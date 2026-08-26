"""dogfood-43: LLM-synthesized behavioral probes + mutation teeth gate.

Unit contract for ``tether.autoprobes``: prompt building, fail-safe parsing
of generated probe specs, and the teeth summarizer that gates generated
probes on their measured ability to kill mutants of the captured change.
The end-to-end orchestrator integration lives in
``tests/test_autoprobes_mission.py``.
"""
import sys

import pytest

from tether.autoprobes import (
    AUTOPROBES_COMMAND_MAX_CHARS,
    AUTO_PROBE_CONTEXT_BUDGET,
    DEFAULT_MAX_PROBES,
    ProbeSynthesisError,
    build_synthesis_prompt,
    parse_generated_probes,
    summarize_teeth,
)
from tether.models import MutationSpec, MutationSummary, MutantResult, ProbeSpec
from tether.verification import run_mutation_testing, run_probes, summarize_probes


VALID_YAML = """\
probes:
  - command: python -c "print('data - ok')"
    contains: data - ok
  - command: python -c "print('id 42')"
    matches: '\\d+'
"""


def _parse(response: str, max_probes: int = DEFAULT_MAX_PROBES):
    return parse_generated_probes(response, max_probes)


# ------------------------------------------------------------------ prompt


def test_prompt_embeds_goal_artifact_and_format():
    prompt = build_synthesis_prompt("make f() return 42", "patch.diff",
                                    "--- a/f\n+++ b/f\n")
    assert "make f() return 42" in prompt
    assert "patch.diff" in prompt
    assert "--- a/f" in prompt
    assert "```yaml" in prompt
    assert "probes:" in prompt


def test_prompt_warns_against_marker_self_match():
    prompt = build_synthesis_prompt("g", "patch.diff", "x")
    # dogfood-28 lesson: a success marker echoed by a failure traceback
    # makes a probe match its own failure output.
    assert "failure output" in prompt


def test_prompt_clips_change_excerpt_to_budget():
    huge = "x" * (AUTO_PROBE_CONTEXT_BUDGET * 4)
    prompt = build_synthesis_prompt("g", "patch.diff", huge)
    assert len(prompt) < AUTO_PROBE_CONTEXT_BUDGET + 4096


# ------------------------------------------------------------------ parser


def test_parse_happy_path_both_criteria_styles():
    probes = _parse(f"prose before\n```yaml\n{VALID_YAML}\n```\nprose after")
    assert [p.command for p in probes] == [
        'python -c "print(\'data - ok\')"',
        'python -c "print(\'id 42\')"',
    ]
    assert probes[0].contains == "data - ok"
    assert probes[0].matches is None
    assert probes[1].contains is None
    assert probes[1].matches == "\\d+"


def test_parse_accepts_bare_fence_and_strips_ansi():
    response = "\x1b[1mSure, here you go:\x1b[0m\n```\n" + VALID_YAML + "\n```\ndone"
    probes = _parse(response)
    assert len(probes) == 2


def test_parse_takes_last_fence():
    response = (
        "```yaml\nprobes:\n  - command: first --ignore\n    contains: x\n```\n"
        "Actually, corrected:\n```yaml\n" + VALID_YAML + "\n```")
    probes = _parse(response)
    assert probes[0].contains == "data - ok"


def test_parse_truncates_to_max_probes():
    many = "probes:\n" + "".join(
        f"  - command: cmd{i}\n    contains: m{i}\n" for i in range(5))
    probes = _parse(f"```yaml\n{many}```", max_probes=2)
    assert len(probes) == 2


@pytest.mark.parametrize("response", [
    "",                                   # nothing at all
    "no fences here, just prose",         # no fenced block
    "```yaml\nnot: [valid, yaml\n```",    # invalid YAML
    "```yaml\n- a\n- b\n```,",            # top-level list, not mapping
    "```yaml\nnoroot: {}\n```",           # missing probes key
    "```yaml\nprobes: 7\n```",            # probes not a list
    "```yaml\nprobes: []\n```",           # empty probe list
])
def test_parse_fail_safe_rejections(response):
    with pytest.raises(ProbeSynthesisError):
        _parse(response)


@pytest.mark.parametrize("entry,fragment", [
    ("  - contains: no-command\n", "command"),
    ("  - command: ''\n    contains: x\n", "non-empty"),
    ("  - command: 7\n    contains: x\n", "non-empty"),
    ("  - command: \"echo 'unclosed\"\n    contains: x\n", "failed to parse"),
    ("  - command: plain-cmd\n", "contains"),
    ("  - command: c\n    contains: ''\n", "contains"),
    ("  - command: c\n    contains: 9\n", "contains"),
    ("  - command: c\n    matches: ''\n", "matches"),
    ("  - command: c\n    matches: '([unclosed'\n", "regex"),
])
def test_parse_rejects_malformed_probe_entries(entry, fragment):
    response = f"```yaml\nprobes:\n{entry}```"
    with pytest.raises(ProbeSynthesisError) as ei:
        _parse(response)
    assert fragment in str(ei.value)


def test_parse_rejects_overlong_command():
    long_cmd = "c" * (AUTOPROBES_COMMAND_MAX_CHARS + 1)
    response = f"```yaml\nprobes:\n  - command: {long_cmd}\n    contains: x\n```"
    with pytest.raises(ProbeSynthesisError) as ei:
        _parse(response)
    assert str(AUTOPROBES_COMMAND_MAX_CHARS) in str(ei.value)


# ------------------------------------------------------------- teeth gate


def _mutant(operator, site, status):
    return MutantResult(file="m.py", operator=operator, site=site,
                        status=status)


def test_teeth_no_mutants_is_advisory_pass():
    ok, text = summarize_teeth(MutationSummary(), [], 0.8)
    assert ok is True
    assert "n/a" in text


def test_teeth_unset_rate_is_advisory():
    summary = MutationSummary(total=2, killed=0, survived=2, kill_rate=0.0)
    ok, text = summarize_teeth(summary, [_mutant("arithmetic", "2:11", "survived")], None)
    assert ok is True
    assert "advisory" in text
    assert "m.py:2:11" in text


def test_teeth_below_gate_fails_with_survivors():
    summary = MutationSummary(total=2, killed=0, survived=2, kill_rate=0.0)
    mutants = [_mutant("arithmetic", "2:11", "survived"),
               _mutant("break_return", "2:4", "survived")]
    ok, text = summarize_teeth(summary, mutants, 0.5)
    assert ok is False
    assert "teeth" in text
    assert "0.5" in text
    assert "m.py:2:11 [arithmetic]" in text


def test_teeth_at_gate_passes():
    summary = MutationSummary(total=2, killed=1, survived=1, kill_rate=0.5)
    ok, _ = summarize_teeth(summary, [], 0.5)
    assert ok is True


# ------------------------------------- teeth measurement against real mutants


CALC_SRC = "def triple(x):\n    return x * 3\n"


def _teeth_run(tmp_path, probes):
    (tmp_path / "calc.py").write_text(CALC_SRC)

    def run_suite():
        ok, out = summarize_probes(run_probes(probes, tmp_path))
        return ok, out

    mutants: list[MutantResult] = []
    summary = run_mutation_testing(
        MutationSpec(enabled=True, max_mutants=20), ["calc.py"],
        tmp_path, run_suite, collect_results=mutants)
    return summary, mutants


def test_strong_generated_probe_kills_real_mutants(tmp_path):
    py = sys.executable
    (tmp_path / "calc.py").write_text(CALC_SRC)
    probes = [ProbeSpec(
        command=f'{py} -c "import calc; print(calc.triple(5))"',
        contains="15")]
    assert summarize_probes(run_probes(probes, tmp_path))[0] is True
    summary, mutants = _teeth_run(tmp_path, probes)
    assert summary.total == 2  # arithmetic site + break_return site
    assert summary.killed == 2
    ok, _ = summarize_teeth(summary, mutants, 0.5)
    assert ok is True


def test_toothless_probe_kills_nothing(tmp_path):
    py = sys.executable
    probes = [ProbeSpec(command=f'{py} -c "print(\'probe ok\')"',
                        contains="probe ok")]
    summary, mutants = _teeth_run(tmp_path, probes)
    assert summary.total == 2
    assert summary.killed == 0
    assert summary.survived == 2
    ok, text = summarize_teeth(summary, mutants, 0.5)
    assert ok is False
