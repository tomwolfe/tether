"""Unit tests for _resolve_session_dir (dogfood-39 session-prefix dedup).

Pins the shared resolver used by `sessions show`, `sessions scrub`, `diff`,
`logs`, and `report`: unique prefix resolves to exactly one directory,
ambiguous prefixes fail with today's listing error, unknown ids fail with
today's "no session found" error.
"""
import pytest
import typer
from typer.testing import CliRunner

from tether.cli import _resolve_session_dir, app

runner = CliRunner()

REPORT_BODY = '{"status": "success", "mission_name": "alpha"}\n'


def _make_sessions(pd):
    root = pd / ".tether" / "sessions"
    s1 = root / "20260101-000000-alpha-aaaa1111"
    s2 = root / "20260101-000001-beta-aaaa2222"
    s3 = root / "20260101-000002-gamma"  # no report.json inside
    for d in (s1, s2, s3):
        d.mkdir(parents=True)
    (s1 / "report.json").write_text(REPORT_BODY, encoding="utf-8")
    (s2 / "report.json").write_text(REPORT_BODY, encoding="utf-8")
    return root


def test_unique_prefix_returns_exactly_one_session_dir(tmp_path):
    root = _make_sessions(tmp_path)
    session = _resolve_session_dir(tmp_path, "aaaa1111")
    assert session == root / "20260101-000000-alpha-aaaa1111"


def test_ambiguous_prefix_exits_1_with_listing_error(tmp_path, capsys):
    _make_sessions(tmp_path)
    with pytest.raises(typer.Exit) as excinfo:
        _resolve_session_dir(tmp_path, "aaaa")
    assert excinfo.value.exit_code == 1
    err = capsys.readouterr().err
    assert err == (
        "Ambiguous session id prefix 'aaaa'; matches:\n"
        "  20260101-000000-alpha-aaaa1111\n"
        "  20260101-000001-beta-aaaa2222\n"
        "Use a longer prefix.\n"
    )


def test_no_match_exits_1_with_no_session_error(tmp_path, capsys):
    root = _make_sessions(tmp_path)
    with pytest.raises(typer.Exit) as excinfo:
        _resolve_session_dir(tmp_path, "zzzz9999")
    assert excinfo.value.exit_code == 1
    expected = (f"No session found for id 'zzzz9999' under {root}\n")
    assert capsys.readouterr().err == expected


def test_report_command_routes_through_helper(tmp_path):
    """End-to-end pin of `tether report` over the helper's paths."""
    root = _make_sessions(tmp_path)
    p = str(tmp_path)
    ok = runner.invoke(app, ["report", "aaaa1111", "--project-dir", p])
    assert ok.exit_code == 0, ok.output
    assert REPORT_BODY in ok.output
    amb = runner.invoke(app, ["report", "aaaa", "--project-dir", p])
    assert amb.exit_code == 1
    assert "Ambiguous session id prefix 'aaaa'" in amb.output
    missing = runner.invoke(app, ["report", "zzzz9999", "--project-dir", p])
    assert missing.exit_code == 1
    assert (f"No session found for id 'zzzz9999' under {root}") in missing.output
