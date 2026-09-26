"""dogfood-43: `tether adapters describe` acceptance tests."""
import json

import pytest
from typer.testing import CliRunner

import tether.adapters as registry
from tether.adapters.base import AgentAdapter
from tether.cli import app
from tether.describe import describe_adapter

runner = CliRunner()


def test_describe_mock_happy_path_json(tmp_path):
    r = runner.invoke(app, ["adapters", "describe", "mock",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)  # parses verbatim (indent=2 output)
    assert list(data) == ["name", "class", "verified",
                          "capabilities", "known_settings"]
    assert data["name"] == "mock"
    assert data["class"] == "MockAdapter"
    assert data["verified"] is True
    caps = data["capabilities"]
    assert list(caps) == ["cancel", "process_tree_kill", "usage",
                          "streaming", "one_shot"]
    assert caps["cancel"] is False
    assert caps["process_tree_kill"] is False
    assert caps["usage"] is False
    assert caps["streaming"] is False
    assert caps["one_shot"] is True
    assert data["known_settings"] == ["scenario"]
    # indent=2 formatting is pinned, not just JSON validity
    assert r.stdout.startswith('{\n  "name": "mock"')


def test_describe_unknown_name_exits_2_with_stderr_and_empty_stdout(tmp_path):
    r = runner.invoke(app, ["adapters", "describe", "no-such-adapter",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 2
    assert "unknown adapter: no-such-adapter" in r.stderr
    assert r.stdout == ""


def test_describe_opencode_resolves_without_running(tmp_path):
    r = runner.invoke(app, ["adapters", "describe", "opencode",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert data["name"] == "opencode"
    assert data["class"] == "OpencodeAdapter"


def test_describe_adapter_raises_valueerror_for_unresolvable_name():
    with pytest.raises(ValueError):
        describe_adapter("no-such-adapter", {})


# ---------------------------------------------------------------------------
# Field-level teeth. `mock` is a degenerate subject: every capability is False
# except one_shot, it declares a single setting, and it is verified. Each test
# below pins one field against a subject that makes that field observable, so
# a hardcoded/short-circuited/wrong-key implementation cannot survive.
# ---------------------------------------------------------------------------


class _StaticAdapter(AgentAdapter):
    """Adapter stub that refuses to be used: every operation raises.

    `describe` is a static-metadata command, so any availability probe,
    session start or send must fail loudly here instead of quietly passing
    the test. Every construction is recorded class-side so a test can assert
    how many instances were made and with which settings.
    """

    known_settings = frozenset({"timeout_seconds", "marker"})

    def __init__(self, settings=None):
        super().__init__(settings)
        type(self).seen.append(dict(self.settings))

    def is_available(self):
        raise AssertionError("describe must not check availability")

    def start_session(self, project_dir, session_id):
        raise AssertionError("describe must not start a session")

    def send(self, prompt, session):
        raise AssertionError("describe must not send a prompt")

    def cancel(self, session):
        raise AssertionError("describe must not cancel")


def _register(monkeypatch, key, **attrs):
    """Register a fresh never-runnable adapter class under `key`."""
    cls = type(f"_Stub_{key}", (_StaticAdapter,),
               {"name": key, "seen": [], **attrs})
    monkeypatch.setitem(registry._REGISTRY, key, cls)
    return cls


def test_known_settings_are_sorted_not_frozenset_order():
    # `mock` declares one setting, so it cannot tell sorted() from any
    # iteration order. `command` declares five: frozenset order is not
    # alphabetical, so only a real sort satisfies this.
    expected = ["command", "env", "prompt_via_stdin", "timeout_seconds",
                "usage_patterns"]
    settings = describe_adapter("command", {})["known_settings"]
    assert settings == expected
    assert settings == sorted(settings)
    assert settings != list(
        registry._REGISTRY["command"].known_settings)


def test_verified_flag_is_read_from_the_adapter_not_hardcoded():
    # mock and opencode are both verified, so only an unverified adapter
    # distinguishes "read the flag" from "return True".
    assert describe_adapter("pi", {})["verified"] is False
    assert describe_adapter("mock", {})["verified"] is True


def test_verified_false_reaches_the_command_line(tmp_path):
    r = runner.invoke(app, ["adapters", "describe", "pi",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["verified"] is False


def test_each_capability_key_reads_its_own_attribute(monkeypatch):
    # A mixed truth pattern: any key swap or key reuse changes the mapping.
    _register(monkeypatch, "mixedcaps", supports_cancel=True,
              supports_process_tree_kill=False, supports_usage=True,
              supports_streaming=False, one_shot=False)
    caps = describe_adapter("mixedcaps", {})["capabilities"]
    assert caps == {"cancel": True, "process_tree_kill": False,
                    "usage": True, "streaming": False, "one_shot": False}


def test_command_capabilities_are_not_confused_with_the_mock_ones():
    # The real command-backed adapter inverts most of mock's flags.
    caps = describe_adapter("command", {})["capabilities"]
    assert caps == {"cancel": True, "process_tree_kill": True,
                    "usage": False, "streaming": True, "one_shot": True}


def test_capability_and_verified_values_are_real_json_booleans(monkeypatch):
    # Truthy non-bool attributes must be normalized, not passed through.
    _register(monkeypatch, "truthy", verified=1, supports_cancel="yes",
              one_shot=0)
    info = describe_adapter("truthy", {})
    assert info["verified"] is True
    assert info["capabilities"]["cancel"] is True
    assert info["capabilities"]["one_shot"] is False
    # And the serialized form is `true`/`false`, never 1/"yes"/0.
    payload = json.loads(json.dumps(info))
    assert payload["verified"] is True
    assert payload["capabilities"]["cancel"] is True
    assert payload["capabilities"]["one_shot"] is False


def test_name_is_the_resolved_instance_name(monkeypatch):
    # A registry key is not an instance name: an adapter that renames itself
    # must be reported under its own name.
    def renaming_init(self, settings=None):
        _StaticAdapter.__init__(self, settings)
        self.name = "renamed-by-adapter"

    _register(monkeypatch, "renamer", __init__=renaming_init)
    info = describe_adapter("renamer", {})
    assert info["name"] == "renamed-by-adapter"
    assert info["class"] == "_Stub_renamer"


def test_an_unreadable_capability_fails_loudly(monkeypatch):
    # The capability flags are declared on AgentAdapter, so a miss means the
    # attribute was renamed or dropped. `false` is a real answer meaning
    # "this adapter cannot cancel", so it must never be manufactured by a
    # failed lookup: the caller could not tell the two apart. The error has to
    # escape instead of being swallowed into a plausible-looking payload.
    cls = _register(monkeypatch, "renamedattr")

    def deny(self, attr):
        if attr == "supports_cancel":
            raise AttributeError(attr)
        return object.__getattribute__(self, attr)

    monkeypatch.setattr(cls, "__getattribute__", deny)
    with pytest.raises(AttributeError):
        describe_adapter("renamedattr", {})


def test_class_name_is_reported_for_a_registered_stub(monkeypatch):
    _register(monkeypatch, "custom")
    assert describe_adapter("custom", {})["class"] == "_Stub_custom"


# ------------------------------------------ the project config's adapters block


def test_describe_resolves_through_the_project_config_adapters_block(
        tmp_path, monkeypatch):
    cls = _register(monkeypatch, "recorder")
    (tmp_path / "tether.yaml").write_text(
        "adapters:\n  recorder:\n    marker: ZEBRA-77\n", encoding="utf-8")
    r = runner.invoke(app, ["adapters", "describe", "recorder",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    # The configured settings reached the instance through resolve_adapter,
    # which also injects the registry's default timeout.
    assert cls.seen == [{"marker": "ZEBRA-77", "timeout_seconds": 1800}]
    assert json.loads(r.stdout)["name"] == "recorder"


def test_describe_adapter_instantiates_through_resolve_adapter(monkeypatch):
    # Even without a config block, the helper goes through the registry
    # constructor, so the registry's own default timeout is applied.
    cls = _register(monkeypatch, "bare")
    info = describe_adapter("bare", {})
    assert cls.seen == [{"timeout_seconds": 1800}]
    assert info["name"] == "bare"


def test_describe_adapter_does_not_mutate_the_settings_it_is_given():
    settings = {"mock": {"scenario": "success"}}
    describe_adapter("mock", settings)
    assert settings == {"mock": {"scenario": "success"}}


# ------------------------------------------------- never runs the adapter


def test_describe_never_runs_or_probes_the_adapter(tmp_path, monkeypatch):
    # Every behavioural method raises; a successful describe proves none of
    # them was called, and the stub records exactly one construction.
    cls = _register(monkeypatch, "untouchable")
    r = runner.invoke(app, ["adapters", "describe", "untouchable",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["class"] == "_Stub_untouchable"
    assert len(cls.seen) == 1


def test_describe_never_probes_availability_of_a_real_adapter(
        tmp_path, monkeypatch):
    # `pi` is unavailable in most environments; that must not matter.
    unavailable = []
    monkeypatch.setattr(registry._REGISTRY["pi"], "is_available",
                        lambda self: unavailable.append(True) or (False, "no"))
    r = runner.invoke(app, ["adapters", "describe", "pi",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["name"] == "pi"
    assert unavailable == []


# ------------------------------------------------------- output is one object


def test_stdout_is_exactly_one_indented_json_object(tmp_path):
    r = runner.invoke(app, ["adapters", "describe", "command",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    parsed = json.loads(r.stdout)
    # Re-serializing the parsed payload with indent=2 must reproduce stdout
    # byte for byte: no header, no trailing prose, no other object.
    assert r.stdout == json.dumps(parsed, indent=2) + "\n"
    assert r.stdout.count('"name"') == 1


def test_unknown_name_from_a_config_adapters_block_is_still_unknown(
        tmp_path):
    # A name in the config's adapters block is not a registered adapter.
    (tmp_path / "tether.yaml").write_text(
        "adapters:\n  ghost-adapter:\n    marker: ZEBRA-77\n",
        encoding="utf-8")
    r = runner.invoke(app, ["adapters", "describe", "ghost-adapter",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 2
    assert r.stderr.strip() == "unknown adapter: ghost-adapter"
    assert r.stdout == ""


def test_describe_adapter_raises_for_a_name_only_the_config_knows():
    with pytest.raises(ValueError):
        describe_adapter("ghost-adapter", {"ghost-adapter": {"marker": "x"}})
