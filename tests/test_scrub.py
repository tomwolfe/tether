"""Post-session secret scrubbing (`tether sessions scrub`, dogfood-19)."""
import json

from typer.testing import CliRunner

from tether.audit import (
    AuditTrail,
    find_secret_spans,
    find_session_dir,
    redact_secret_value,
)
from tether.cli import app

runner = CliRunner()

FAKE_KEY = "sk-live-abcdef1234567890abcdef"
PROMPT_BODY = f'please deploy with api_key = "{FAKE_KEY}" tonight\n'


def _make_session(tmp_path):
    """Real audit session dir holding a fake API key in a prompt/response."""
    audit = AuditTrail(tmp_path, ".tether/sessions", "scrubme", "aaaa1111aaaa")
    audit.log_event("session_start", {"session_id": "aaaa1111aaaa"})
    audit.save_prompt("plan", PROMPT_BODY)
    audit.save_response("execute", {
        "status": "completed",
        "logs": f"using token {FAKE_KEY} now",
    })
    # a file outside the session directory carrying the same secret
    outside = tmp_path / ".tether" / "outside-note.txt"
    outside.write_text(PROMPT_BODY, encoding="utf-8")
    return audit.dir, outside


def test_find_secret_spans_and_redact_marker():
    spans = find_secret_spans(PROMPT_BODY)
    assert len(spans) == 1
    start, end = spans[0]
    assert PROMPT_BODY[start:end] == FAKE_KEY
    marker = redact_secret_value(FAKE_KEY)
    assert marker.startswith("[REDACTED sha256=")
    assert f"len={len(FAKE_KEY)}]" in marker
    assert FAKE_KEY not in marker


def test_scrub_without_confirm_prints_plan_and_changes_nothing(tmp_path):
    session, outside = _make_session(tmp_path)
    before_prompt = (session / "prompts" / "001-plan.txt").read_bytes()
    before_outside = outside.read_bytes()
    r = runner.invoke(app, ["sessions", "scrub", "aaaa1111aaaa",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "Would scrub" in r.output
    assert str(session / "prompts" / "001-plan.txt") in r.output
    assert "(1 match(es))" in r.output
    assert "Dry run: nothing modified" in r.output
    # byte-for-byte unchanged
    assert (session / "prompts" / "001-plan.txt").read_bytes() == before_prompt
    assert outside.read_bytes() == before_outside
    # no scrub event was appended
    kinds = [json.loads(ln)["kind"] for ln in
             (session / "events.jsonl").read_text().splitlines()]
    assert "scrub" not in kinds


def test_scrub_with_confirm_redacts_and_appends_event(tmp_path):
    import hashlib
    session, outside = _make_session(tmp_path)
    prompt_path = session / "prompts" / "001-plan.txt"
    response_path = sorted((session / "responses").glob("*.json"))[0]
    r = runner.invoke(app, ["sessions", "scrub", "aaaa1111aaaa", "--confirm",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "Scrub:" in r.output
    assert "Scrubbed 2 file(s)" in r.output

    prompt_text = prompt_path.read_text(encoding="utf-8")
    assert FAKE_KEY not in prompt_text
    digest = hashlib.sha256(FAKE_KEY.encode()).hexdigest()
    assert f"[REDACTED sha256={digest} len={len(FAKE_KEY)}]" in prompt_text
    response_text = response_path.read_text(encoding="utf-8")
    assert FAKE_KEY not in response_text
    assert "[REDACTED sha256=" in response_text

    # exactly one scrub event recording the timestamp and file count
    events = [json.loads(ln) for ln in
              (session / "events.jsonl").read_text().splitlines()]
    scrubs = [e for e in events if e["kind"] == "scrub"]
    assert len(scrubs) == 1
    assert scrubs[0]["files"] == 2
    assert scrubs[0]["ts"]

    # the tamper-evident chain still verifies end-to-end
    sid = session.name.rsplit("-", 1)[-1]
    full = find_session_dir(tmp_path, ".tether/sessions", sid)
    assert full == session
    rv = runner.invoke(app, ["logs", sid, "--verify",
                             "--project-dir", str(tmp_path)])
    assert rv.exit_code == 0, rv.output
    assert "OK" in rv.output and "intact" in rv.output


def test_scrub_never_touches_files_outside_session_dir(tmp_path):
    session, outside = _make_session(tmp_path)
    runner.invoke(app, ["sessions", "scrub", "aaaa1111aaaa", "--confirm",
                        "--project-dir", str(tmp_path)])
    # the same secret outside the session dir survives untouched
    assert outside.read_text(encoding="utf-8") == PROMPT_BODY
    # nothing was written anywhere else either (e.g. sibling sessions)
    siblings = [d for d in (tmp_path / ".tether" / "sessions").iterdir()
                if d != session]
    assert siblings == []


def test_scrub_clean_session_reports_nothing_to_do(tmp_path):
    audit = AuditTrail(tmp_path, ".tether/sessions", "cleanone", "bbbb2222bbbb")
    audit.save_prompt("plan", "nothing sensitive here\n")
    r = runner.invoke(app, ["sessions", "scrub", "bbbb2222bbbb",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "No secret-like material found" in r.output
