"""dogfood-43: `tether adapters describe` acceptance tests."""
import json

import pytest
from typer.testing import CliRunner

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
