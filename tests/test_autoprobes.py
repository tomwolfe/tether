"""dogfood-43: LLM-synthesized behavioral probes + mutation teeth gate.

Unit contract for ``tether.autoprobes``: prompt building, fail-safe parsing
of generated probe specs, and the teeth summarizer that gates generated
probes on their measured ability to kill mutants of the captured change.
The end-to-end orchestrator integration lives in
``tests/test_autoprobes_mission.py``.
"""
import re
import shlex
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


# ------------------------------------------- the template's quoting lesson


# What the template must teach, pinned from the outside: the escaped-inner-
# quote double-quoted scalar for a shell one-liner, and a single-quoted
# scalar for a command with no shell quoting of its own.
DQ_ONE_LINER = ('command: "python -c \\"import mymodule; '
                'print(mymodule.test())\\"\"')
SQ_PYTEST = "command: 'python -m pytest -q tests/test_mymodule.py'"

# Each advertised example, with the argv it must produce once the real parse
# and validation path has read it.
ADVERTISED = [
    (DQ_ONE_LINER,
     'python -c "import mymodule; print(mymodule.test())"',
     ["python", "-c", "import mymodule; print(mymodule.test())"],
     "expected_output", None),
    (SQ_PYTEST,
     "python -m pytest -q tests/test_mymodule.py",
     ["python", "-m", "pytest", "-q", "tests/test_mymodule.py"],
     None, "1 passed"),
]


def _prompt() -> str:
    return build_synthesis_prompt("g", "patch.diff", "x")


def test_prompt_teaches_escaped_double_quoted_shell_one_liner():
    prompt = _prompt()
    assert DQ_ONE_LINER in prompt
    # The inner double quotes are backslash-escaped, i.e. the YAML scalar is
    # readable rather than truncated at the first quote.
    assert '\\"import mymodule;' in prompt
    assert "contains: \"expected_output\"" in prompt


def test_prompt_teaches_single_quoted_scalar_and_both_keys():
    prompt = _prompt()
    assert SQ_PYTEST in prompt
    assert "matches: '1 passed'" in prompt
    # Both criteria keys are demonstrated, and the prose names the
    # single-quoted form as the safer default with a reason.
    assert "single-quoted YAML scalar" in prompt
    assert "literally" in prompt


def test_prompt_teaches_the_quoting_rule_in_prose():
    prompt = _prompt()
    lowered = prompt.lower()
    assert "double-quoted yaml scalar" in lowered
    assert "escaped as" in prompt
    for trigger in ("double quote", "colon", "special character"):
        assert trigger in lowered


def test_prompt_keeps_the_rules_that_guard_probe_teeth():
    prompt = _prompt()
    assert "failure output" in prompt
    assert "exit code is recorded but never decides" in prompt
    assert "At least one of contains/matches" in prompt


def test_prompt_examples_all_survive_the_real_parse_and_validate_path():
    # Route the prompt's OWN examples through parse_generated_probes -- the
    # fence -> YAML -> salvage -> _validated_probe path a generated response
    # goes through. A template that advertises a form the validator rejects
    # (an unescaped double-quoted shell fragment, say) loses a probe here and
    # the count/equality assertions below catch it.
    prompt = _prompt()
    specs = _parse(prompt)
    assert len(specs) == len(ADVERTISED)
    for spec, (_, command, argv, contains, matches) in zip(specs, ADVERTISED):
        assert isinstance(spec, ProbeSpec)
        assert spec.command == command
        assert shlex.split(spec.command) == argv
        assert spec.contains == contains
        assert spec.matches == matches


def test_prompt_advertises_one_example_per_advertised_command():
    # Guards ADVERTISED against drifting out of sync with the template: every
    # pinned entry must actually appear in the prompt it claims to pin.
    prompt = _prompt()
    for entry, _command, _argv, _contains, _matches in ADVERTISED:
        assert entry in prompt
    assert prompt.count("- command:") == len(ADVERTISED)


def test_prompt_teaches_no_bare_bracketed_metavariable_command():
    # RAW-TEXT check on purpose. The validator drops a placeholder probe
    # before any caller can see it, so routing the template through
    # parse_generated_probes and asserting on the result is tautological --
    # it would still pass with the offending line re-added. The prompt text
    # itself has to be free of it.
    prompt = _prompt()
    assert re.search(r"command:[ \t]*<[^>\n]*>", prompt) is None
    # And no field of a probe example may be a bare bracketed metavariable.
    assert re.findall(r"^[ \t]*(?:-[ \t]+)?\w+:[ \t]*<[^>\n]+>[ \t]*$",
                      prompt, re.MULTILINE) == []


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


def test_parse_accepts_shell_escaped_fence_markers():
    # A generator that answers through a shell-quoted context backslash-escapes
    # the fence markers along with its command text. That echo must not hide the
    # block: the healthy probe still comes through, the empty command does not.
    response = (
        "\\`\\`\\`yaml\n"
        "probes:\n"
        "  - command: \"echo hi\"\n"
        "    contains: \"hi\"\n"
        "  - command: \"\"\n"
        "    contains: \"x\"\n"
        "\\`\\`\\`"
    )
    probes = _parse(response)
    assert [p.command for p in probes] == ["echo hi"]
    assert probes[0].contains == "hi"


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
    msg = str(ei.value)
    assert fragment in msg or "no valid probes after salvage" in msg


def test_parse_rejects_overlong_command():
    long_cmd = "c" * (AUTOPROBES_COMMAND_MAX_CHARS + 1)
    response = f"```yaml\nprobes:\n  - command: {long_cmd}\n    contains: x\n```"
    with pytest.raises(ProbeSynthesisError):
        _parse(response)


def test_parse_salvages_valid_probes_from_mixed_response():
    response = "```yaml\nprobes:\n  - command: \"echo hi\"\n    contains: hi\n  - command: \"\"\n    contains: x\n```"
    specs = _parse(response)
    assert len(specs) == 1
    assert specs[0].command == "echo hi"


# ------------------------------------------------- salvage across bad YAML


def test_parse_salvages_when_one_entry_breaks_yaml():
    # An entry YAML can never read costs only itself. (Quoted scalars with
    # unescaped inner quotes are no longer such a case — those are repaired;
    # see the single-quote repair tests below. This uses a flow-sequence
    # left open, which no scalar repair can rescue.)
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: 'echo broken'\n"
        "    contains: [unclosed\n"
        "  - command: \"echo hi\"\n"
        "    contains: \"hi\"\n"
        "```"
    )
    specs = _parse(response)
    assert len(specs) == 1
    assert specs[0].command == "echo hi"


def test_parse_salvages_when_broken_entry_comes_last():
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: \"echo first\"\n"
        "    contains: \"first\"\n"
        "  - command: 'echo broken'\n"
        "    contains: [unclosed\n"
        "  - command: \"echo third\"\n"
        "    matches: 'thi'\n"
        "```"
    )
    specs = _parse(response)
    assert [p.command for p in specs] == ["echo first", "echo third"]


def test_parse_relaxes_double_quoted_shell_fragment():
    # A SINGLE raw backslash inside a double-quoted scalar is an unknown YAML
    # escape, so the block must be read with that scalar re-quoted — and the
    # backslash must survive as the data the shell fragment needs. The fixture
    # deliberately holds one backslash, not two: `\\` is a valid YAML escape,
    # so a doubled one would parse natively and this test would never reach
    # the repair it is named for.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: \"printf 'a\\db'\"\n"
        "    contains: \"a\\db\"\n"
        "    matches: 'id \\d+'\n"
        "```"
    )
    specs = _parse(response)
    assert len(specs) == 1
    assert specs[0].command == "printf 'a\\db'"
    assert specs[0].contains == "a\\db"
    assert specs[0].matches == "id \\d+"


def test_parse_keeps_properly_escaped_double_quoted_scalar():
    # The repair must not re-quote what YAML already reads correctly.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: \"python -c \\\"print(1)\\\"\"\n"
        "    contains: \"1\"\n"
        "```"
    )
    specs = _parse(response)
    assert specs[0].command == 'python -c "print(1)"'


def test_relaxed_scalar_keeps_regex_backslash_and_its_character():
    # A regex class is the common shell fragment, and the repair must not
    # "simplify" it: `\d+` is a raw backslash followed by `d`, and only the
    # backslash is the YAML complaint. Yielding `\d+` back as `d+` or `\+`
    # would leave a command that still runs but no longer matches.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: \"grep '\\d+' src/*.py\"\n"
        "    contains: \"x\"\n"
        "```"
    )
    specs = _parse(response)
    assert specs[0].command == "grep '\\d+' src/*.py"


def test_relaxed_scalar_keeps_windows_path_verbatim():
    # A path is backslashes with nothing to escape, so every one of them is
    # data. Truncating them to bare separators yields a command that runs and
    # silently addresses the wrong file.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: \"type C:\\Users\\bob\\tool.exe\"\n"
        "    contains: \"x\"\n"
        "```"
    )
    specs = _parse(response)
    assert specs[0].command == "type C:\\Users\\bob\\tool.exe"


def test_relaxed_scalar_unescapes_quotes_but_keeps_data_backslashes():
    # The two escapes are not symmetric: `\"` is the one the model escaped on
    # purpose, so it collapses to a literal quote, while the raw `\d` beside
    # it is data. Handling them alike either mangles the regex or leaves a
    # trailing backslash that shlex cannot read, losing the entry outright.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: \"python -c \\\"print('x')\\d\\\"\"\n"
        "    contains: \"1\"\n"
        "```"
    )
    specs = _parse(response)
    assert specs[0].command == "python -c \"print('x')\\d\""
    # The recovered command must survive the same shlex gate real probes do.
    assert shlex.split(specs[0].command)[-1] == "print('x')\\d"


def test_salvage_keeps_sibling_of_relaxed_shell_fragment():
    # Per-entry salvage, end to end: an entry whose double-quoted scalar
    # needed both repairs still comes through, and a healthy sibling beside
    # it is untouched.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: \"python -c \\\"print(1)\\d\\\"\"\n"
        "    contains: \"1\"\n"
        "  - command: \"echo fine\"\n"
        "    contains: \"fine\"\n"
        "```"
    )
    specs = _parse(response)
    assert [p.command for p in specs] == [
        'python -c "print(1)\\d"', "echo fine"]


def test_parse_fails_when_no_entry_survives_bad_yaml():
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: 'echo a'\n"
        "    contains: [unclosed\n"
        "  - command: 'echo b'\n"
        "    matches: [unclosed\n"
        "```"
    )
    with pytest.raises(ProbeSynthesisError):
        _parse(response)


def test_salvage_does_not_resurrect_rejected_entries():
    # Salvage re-reads entries; it must not become a way around the spec
    # gate. Here the double-quote repair makes the entry readable, and the
    # gate still has the last word: a placeholder executable and a
    # placeholder `contains` leave the valid set empty, so the response
    # fails rather than handing the probe runner a schema echo.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: \"echo <single-line command> \\d\"\n"
        "    contains: \"<literal substring>\"\n"
        "```"
    )
    with pytest.raises(ProbeSynthesisError):
        _parse(response)


def test_salvage_keeps_sibling_when_others_break():
    # A bad-regex entry that YAML reads fine and a YAML-level break in the
    # same block: the one healthy probe must still come through.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: 'echo broken'\n"
        "    contains: [unclosed\n"
        "  - command: \"echo ok\"\n"
        "    matches: '([unclosed'\n"
        "  - command: \"echo fine\"\n"
        "    contains: \"fine\"\n"
        "```"
    )
    specs = _parse(response)
    assert [p.command for p in specs] == ["echo fine"]


def test_salvage_keeps_sibling_after_stray_line_at_item_indent():
    # A line the model wrote between entries, at the item indent, belongs to
    # no entry: YAML cannot read the block, so salvage reads the entries one
    # at a time -- and the stray line must not cost the entries after it
    # their chance to be read. Ending the scan there would silently drop
    # every later sibling.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: \"echo first\"\n"
        "    contains: \"first\"\n"
        "  stray line here\n"
        "  - command: \"echo second\"\n"
        "    contains: \"second\"\n"
        "```"
    )
    specs = _parse(response)
    assert [p.command for p in specs] == ["echo first", "echo second"]


def test_salvage_keeps_sibling_after_stray_mapping_key_at_item_indent():
    # Same shape, but the stray line is a mapping key: a key at the item
    # indent is not part of any entry, so it is dropped while the entries
    # on both sides of it survive.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: \"echo first\"\n"
        "    contains: \"first\"\n"
        "  note: something\n"
        "  - command: \"echo second\"\n"
        "    contains: \"second\"\n"
        "```"
    )
    specs = _parse(response)
    assert [p.command for p in specs] == ["echo first", "echo second"]


def test_salvage_recovers_mis_indented_sibling_entry():
    # A model that indents the second entry deeper than the first leaves
    # YAML reading both as one broken item. The chunk is re-split at its own
    # item markers, so the healthy first entry survives -- and the
    # mis-indented sibling, read on its own, survives with it.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: \"echo first\"\n"
        "    contains: \"first\"\n"
        "    - command: \"echo second\"\n"
        "      contains: \"second\"\n"
        "```"
    )
    specs = _parse(response)
    assert [p.command for p in specs] == ["echo first", "echo second"]


def test_parse_still_rejects_a_missing_fence():
    # The fail-safe contract: salvage never invents a block that is not
    # there. A response without a fence is not a response with zero probes
    # -- it is a synthesis failure.
    with pytest.raises(ProbeSynthesisError) as ei:
        _parse("probes:\n  - command: \"echo hi\"\n    contains: \"hi\"\n")
    assert "no fenced yaml block" in str(ei.value)


def test_parse_still_rejects_an_empty_fence():
    # An empty fenced block carries no entries at all, so salvage has
    # nothing to salvage: the response fails instead of yielding zero.
    with pytest.raises(ProbeSynthesisError) as ei:
        _parse("```yaml\n```")
    assert "no probe entries could be read" in str(ei.value)


def test_parse_still_rejects_a_response_with_no_valid_entry():
    # Per-entry salvage keeps the VALID entries; it must not soften the
    # empty-set case. Every entry malformed is still a failed synthesis,
    # so the mission falls back to its human-authored battery.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: \"\"\n"
        "    contains: \"x\"\n"
        "  - command: \"echo broken\"\n"
        "    matches: '([unclosed'\n"
        "```"
    )
    with pytest.raises(ProbeSynthesisError) as ei:
        _parse(response)
    assert "no valid probes after salvage" in str(ei.value)


# ------------------------------------------- double-quoted shell fragments


def test_double_quoted_scalar_collapses_an_escaped_backslash():
    # `\\` is the other escape a shell fragment legitimately needs: it is a
    # literal backslash (a Windows path), not data to be doubled. The
    # repair re-emits the scalar, so it has to honor that escape or the
    # path the model wrote comes back with twice the separators. The raw
    # `\d` beside it is the invalid escape that makes the repair run at
    # all; it is data and must survive verbatim.
    response = (
        "```yaml\n"
        "probes:\n"
        r'  - command: "type C:\\Users\\bob\\tool.exe \d"'
        "\n"
        "    contains: \"x\"\n"
        "```"
    )
    specs = _parse(response)
    assert len(specs) == 1
    assert specs[0].command == r"type C:\Users\bob\tool.exe \d"


def test_hash_inside_a_quoted_shell_fragment_survives():
    # A `#` inside a quoted scalar is DATA: YAML only starts a comment at a
    # `#` outside the scalar. The repair splits the value from the comment
    # before it knows where the scalar ends, so a naive split truncates the
    # command at the `#` -- and the entry is lost with it.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: \"grep '#include' \\d+ f\"\n"
        "    contains: \"x\"\n"
        "```"
    )
    specs = _parse(response)
    assert len(specs) == 1
    assert specs[0].command == "grep '#include' \\d+ f"


def test_trailing_comment_is_not_swallowed_into_the_command():
    # The comment is the model's own text and must stay OUT of the command:
    # a command that carries `# the probe` never matches what it asserts.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: \"grep '\\d+' f\"  # the probe\n"
        "    contains: \"x\"\n"
        "```"
    )
    specs = _parse(response)
    assert len(specs) == 1
    assert specs[0].command == "grep '\\d+' f"


def test_trailing_comment_survives_the_scalar_repair():
    # The comment must survive the repair itself, not merely avoid being
    # swallowed into the value: the repair rewrites the line, and a rewrite
    # that drops the model's comment changes the line it just fixed.
    from tether.autoprobes import _relax_double_quoted_scalars

    repaired = _relax_double_quoted_scalars(
        "  - command: \"grep '\\d+' f\"  # the probe")
    assert repaired.endswith("# the probe")
    assert "grep ''\\d+'' f" in repaired


# ------------------------------------------------------- shape tolerance


def test_probes_key_is_read_case_insensitively():
    # The key is a schema the model was shown; a capitalized `Probes:` is a
    # spelling, not a different intent, and it must not cost the response.
    response = (
        "```yaml\n"
        "Probes:\n"
        "  - command: \"echo hi\"\n"
        "    contains: \"hi\"\n"
        "```"
    )
    specs = _parse(response)
    assert [p.command for p in specs] == ["echo hi"]


def test_bare_list_of_entries_is_salvaged():
    # The model wrote the entries without the `probes:` key. The shape is
    # forgiven, the entries are not: they still have to pass the spec gate.
    response = (
        "```yaml\n"
        "- command: \"echo hi\"\n"
        "  contains: \"hi\"\n"
        "- command: \"echo bye\"\n"
        "  contains: \"bye\"\n"
        "```"
    )
    specs = _parse(response)
    assert [p.command for p in specs] == ["echo hi", "echo bye"]


def test_tab_indented_list_is_salvaged():
    # YAML forbids a tab as indentation, so a tab-indented list is not
    # YAML at all -- and the whole response used to go with it. The tabs
    # are indentation, not data, so they become blanks.
    response = (
        "```yaml\n"
        "probes:\n"
        "\t- command: \"echo hi\"\n"
        "\t  contains: \"hi\"\n"
        "```"
    )
    specs = _parse(response)
    assert [p.command for p in specs] == ["echo hi"]
    assert specs[0].contains == "hi"


def test_key_without_a_blank_before_a_quoted_value_is_normalized():
    # `command:'echo hi'` is a plain scalar to YAML, not a mapping entry,
    # so the entry is lost. The blank the model forgot is put back.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command:'echo hi'\n"
        "    contains:'hi'\n"
        "```"
    )
    specs = _parse(response)
    assert [p.command for p in specs] == ["echo hi"]
    assert specs[0].contains == "hi"


def test_the_two_scalar_repairs_compose():
    # One entry can need both repairs at once: a double-quoted command
    # with a regex backslash AND a single-quoted value with a nested shell
    # quote. Each repair alone leaves the other line broken, so they have
    # to be applied together.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: 'python -c 'print(1)''\n"
        "    contains: \"x\\d\"\n"
        "```"
    )
    specs = _parse(response)
    assert len(specs) == 1
    assert specs[0].command == "python -c 'print(1)'"
    assert specs[0].contains == "x\\d"


def test_deeper_indented_entry_does_not_cost_its_siblings():
    # A tab used for indentation is the model's own spelling of the shape,
    # and the entry in front of it must not cost its siblings. The chunk
    # scan used to latch the FIRST item's indent and stop at the first
    # shallower line, so the two healthy entries behind the tab-indented
    # one were never read at all -- the response kept one probe out of
    # three and called it salvage.
    response = (
        "```yaml\n"
        "probes:\n"
        "\t- command: 'echo A'\n"
        "\t  contains: 'A'\n"
        "  - command: 'echo B'\n"
        "    contains: 'B'\n"
        "  - command: 'echo C'\n"
        "    contains: 'C'\n"
        "```"
    )
    specs = _parse(response)
    assert [p.command for p in specs] == ["echo A", "echo B", "echo C"]


def test_over_indented_entry_does_not_cost_its_siblings():
    # The same failure without a tab: an entry indented deeper than its
    # siblings used to end the scan, and the healthy entry behind it was
    # lost. A shallower item marker re-anchors the list body instead.
    response = (
        "```yaml\n"
        "probes:\n"
        "    - command: 'echo A'\n"
        "      contains: 'A'\n"
        "  - command: 'echo B'\n"
        "    contains: 'B'\n"
        "```"
    )
    specs = _parse(response)
    assert [p.command for p in specs] == ["echo A", "echo B"]


def test_deeper_indented_entry_in_last_position_keeps_its_siblings():
    # The mirror of the two tests above: the deeper entry comes LAST, so
    # the re-anchor has nothing to re-anchor. The healthy entry in front
    # of it must still be read, and the deeper one must not swallow it.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: 'echo A'\n"
        "    contains: 'A'\n"
        "\t- command: 'echo B'\n"
        "\t  contains: 'B'\n"
        "```"
    )
    specs = _parse(response)
    assert [p.command for p in specs] == ["echo A", "echo B"]


def test_missing_blank_after_colon_is_repaired_beside_a_healthy_sibling():
    # `command:'echo a'` is a PLAIN SCALAR to YAML, not a mapping entry,
    # so the bulk reading loses that entry. The entry is the model's own
    # spelling of the shape, so the per-entry reading recovers it -- and
    # the healthy sibling beside it must survive the recovery.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command:'echo a'\n"
        "    contains:'a'\n"
        "  - command: 'echo b'\n"
        "    contains: 'b'\n"
        "```"
    )
    specs = _parse(response)
    assert [p.command for p in specs] == ["echo a", "echo b"]
    assert [p.contains for p in specs] == ["a", "b"]


def test_missing_blank_after_colon_is_repaired_in_last_position():
    # Same, with the mangled entry last: the source order is kept, and
    # the healthy entry in front of it is not lost either.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: 'echo b'\n"
        "    contains: 'b'\n"
        "  - command:'echo a'\n"
        "    contains:'a'\n"
        "```"
    )
    specs = _parse(response)
    assert [p.command for p in specs] == ["echo b", "echo a"]


def test_bulk_read_that_lost_an_entry_is_reread_per_entry():
    # The mechanism behind the test above, pinned from the outside: the
    # bulk reading holds the mangled entry as ONE plain scalar instead of
    # a mapping, so it lost that entry -- and a healthy sibling kept the
    # valid set non-empty, so the per-entry reading never ran. The block
    # is re-read per entry exactly when the bulk reading lost something.
    from tether.autoprobes import _read_probe_list

    block = (
        "probes:\n"
        "  - command:'echo a'\n"
        "    contains:'a'\n"
        "  - command: 'echo b'\n"
        "    contains: 'b'\n"
    )
    entries, _blocked_by = _read_probe_list(block)
    assert entries is not None
    assert len(entries) == 2
    # The first entry was not read as a mapping, so it was not a probe.
    assert isinstance(entries[0], str)
    specs = _parse(f"```yaml\n{block}```")
    assert [p.command for p in specs] == ["echo a", "echo b"]


def test_per_entry_reread_does_not_resurrect_a_rejected_entry():
    # The re-read that recovers a lost entry goes through the SAME spec
    # gate, so it is not a way around it: the empty-command sibling of a
    # recoverable entry stays dropped, and the healthy one survives.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command:'echo a'\n"
        "    contains:'a'\n"
        "  - command: 'echo b'\n"
        "    contains: 'b'\n"
        "  - command: ''\n"
        "    contains: 'x'\n"
        "```"
    )
    specs = _parse(response)
    assert [p.command for p in specs] == ["echo a", "echo b"]


def test_shape_tolerance_does_not_rescue_a_response_with_no_valid_probe():
    # Forgiving the SHAPE must not forgive the CONTENT: a block that
    # survives every shape tolerance still fails when it holds no valid
    # probe, so the mission falls back to its human-authored battery.
    response = (
        "```yaml\n"
        "- command: \"\"\n"
        "  contains: \"x\"\n"
        "- command: \"echo broken\"\n"
        "  matches: '([unclosed'\n"
        "```"
    )
    with pytest.raises(ProbeSynthesisError) as ei:
        _parse(response)
    assert "no valid probes after salvage" in str(ei.value)


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


def test_schema_echoed_probes_are_rejected_not_executed():
    # A generator that cannot solve the task echoes the schema back verbatim.
    # Those entries are structurally valid (non-empty strings that shlex-parse
    # and carry a `contains`), so before this guard the probe runner exec'd a
    # program literally named "<single-line" and the whole verification tier
    # failed on "binary not found" instead of falling back cleanly.
    from tether.autoprobes import ProbeSynthesisError, parse_generated_probes

    echoed = (
        "```yaml\n"
        "probes:\n"
        "  - command: '<single-line command, run with cwd = project root>'\n"
        "    contains: '<literal substring required in combined "
        "stdout+stderr>'\n"
        "    matches: None\n"
        "```"
    )
    with pytest.raises(ProbeSynthesisError) as exc:
        parse_generated_probes(echoed)
    assert "no valid probes after salvage" in str(exc.value)


def test_placeholder_executable_is_rejected_but_real_ones_survive():
    # Narrow the guard: only the *executable name* and a pure metavariable
    # `contains` are placeholders. A genuine `matches` regex like "<[0-9]+>"
    # and a genuine command that merely mentions brackets must still parse,
    # or the guard would throw away real probes.
    from tether.autoprobes import parse_generated_probes

    real = (
        "```yaml\n"
        "probes:\n"
        "  - command: '.venv/bin/tether adapters describe mock'\n"
        "    contains: '\"verified\": true'\n"
        "  - command: \"grep -c 'a<b' README.md\"\n"
        "    matches: '<[0-9]+>'\n"
        "```"
    )
    specs = parse_generated_probes(real)
    assert len(specs) == 2
    assert specs[1].matches == "<[0-9]+>"


def test_placeholder_probe_is_dropped_but_real_siblings_survive():
    # Salvage semantics: a mixed list keeps the real probes and discards the
    # echoed one, rather than failing the whole synthesis.
    from tether.autoprobes import parse_generated_probes

    mixed = (
        "```yaml\n"
        "probes:\n"
        "  - command: '<single-line command>'\n"
        "    contains: 'x'\n"
        "  - command: '.venv/bin/tether adapters conformance mock'\n"
        "    contains: 'PASS'\n"
        "```"
    )
    specs = parse_generated_probes(mixed)
    assert len(specs) == 1
    assert specs[0].command.endswith("conformance mock")


def test_single_quoted_command_with_inner_single_quotes_is_recovered():
    # A model that wraps a shell command in single quotes but leaves the
    # shell's own inner quotes unescaped: `command: 'python -c 'print(1)''`.
    # YAML reads that as the scalar `python -c 'print(1)` plus stray text and
    # rejects the block. The repair closes the shell argument and escapes
    # every inner quote, so the command survives verbatim.
    from tether.autoprobes import parse_generated_probes

    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: 'python -c 'print(1)''\n"
        "    contains: '1'\n"
        "```"
    )
    specs = parse_generated_probes(response)
    assert len(specs) == 1
    assert specs[0].command == "python -c 'print(1)'"
    assert specs[0].contains == "1"


def test_single_quote_repair_does_not_double_an_already_escaped_scalar():
    # A correctly escaped single-quoted scalar has an even number of quotes
    # and must keep its own interpretation: the repair must not re-escape it
    # into `'python -c ''''print(1)'''''''`.
    from tether.autoprobes import parse_generated_probes

    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: 'python -c ''print(1)'''\n"
        "    contains: '1'\n"
        "```"
    )
    specs = parse_generated_probes(response)
    assert specs[0].command == "python -c 'print(1)'"


def test_single_quote_repair_keeps_regex_backslashes_verbatim():
    # The repair is quote-only. A regex backslash is data and must survive,
    # not be treated as an escape.
    from tether.autoprobes import parse_generated_probes

    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: 'grep '\\d+' f'\n"
        "    contains: 'x'\n"
        "```"
    )
    specs = parse_generated_probes(response)
    assert specs[0].command == r"grep '\d+' f"


def test_broken_single_quoted_entry_costs_only_itself():
    # Salvage stays per-entry across both repair kinds: a single-quote
    # casualty does not sink a readable sibling.
    from tether.autoprobes import parse_generated_probes

    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: 'python -c 'print(1)''\n"
        "    contains: '1'\n"
        "  - command: 'echo hi'\n"
        "    contains: 'hi'\n"
        "```"
    )
    specs = parse_generated_probes(response)
    assert [s.command for s in specs] == ["python -c 'print(1)'", "echo hi"]


def test_repaired_single_quoted_entry_survives_an_invalid_sibling():
    # The two layers meet here: the repair makes the entry readable, and
    # the spec gate drops the empty-command sibling beside it. The repaired
    # entry must survive its invalid sibling, and the invalid sibling must
    # not be resurrected by the repair that ran next to it.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: 'python -c 'print(1)''\n"
        "    contains: '1'\n"
        "  - command: ''\n"
        "    contains: 'x'\n"
        "```"
    )
    specs = _parse(response)
    assert [s.command for s in specs] == ["python -c 'print(1)'"]
    assert specs[0].contains == "1"


def test_repaired_double_quoted_entry_survives_an_invalid_sibling():
    # The mirror case for the other repair: a double-quoted scalar holding a
    # shell fragment (a raw regex backslash is an unknown YAML escape) is
    # re-quoted, and the empty-command sibling beside it is dropped by the
    # spec gate. The fragment must come back verbatim -- the backslash is
    # data the shell needs, not an escape.
    response = (
        "```yaml\n"
        "probes:\n"
        "  - command: \"grep '\\d+' f\"\n"
        "    contains: \"x\"\n"
        "  - command: \"\"\n"
        "    contains: \"y\"\n"
        "```"
    )
    specs = _parse(response)
    assert [s.command for s in specs] == ["grep '\\d+' f"]
    assert specs[0].contains == "x"
