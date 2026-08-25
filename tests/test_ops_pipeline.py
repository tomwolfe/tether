"""Operational intelligence pipeline on realistic session data (dogfood-35).

Integration tests over a seeded ``.tether/sessions/`` tree whose sessions are
built with the real :class:`AuditTrail` helpers (auto-chained events, real
verification attempt files) and report payloads modeled on the dogfood-14..32
live runs: a first-try clean-room/mutation gauntlet success, a reviewed
success with nonlinear reset-to-checkpoint recovery plus usage/mutation
telemetry, an aborted oscillation failure, a budget-breach abort, a legacy
pre-dogfood-14 minimal session, and a corrupted mid-write crash (truncated
report.json plus an unparseable events.jsonl). Covers stats aggregation
resilience over the mixed-era tree — including the conditional Review gate /
Budgets / Mutation / Per-mission / Usage lines — scrub end-to-end with hash
-chain re-verification (including corrupted and legacy sessions), and clean
precision across the retention boundary with the ``retention_days`` config
fallback.
"""
import hashlib
import json
import os
import time

from typer.testing import CliRunner

from tether.audit import AuditTrail, event_hash
from tether.cli import app
from tether.models import VerificationResult

runner = CliRunner()

AUDIT_DIR = ".tether/sessions"
SID_SUCCESS = "aaa11111aaaa"
SID_OSCILLATION = "bbb22222bbbb"
SID_BUDGET = "ccc33333cccc"
SID_GAUNTLET = "fff66666ffff"
SID_APPROVED = "ddd44444dddd"
SID_REJECTED = "eee55555eeee"
FAIL_CMD = "pytest -q tests/test_feature.py"
SECRET = "sk-live-0123456789abcdef0123"
OSC_SECRET = "ghp_0123456789abcdefghijklmnopqrst"
PROMPT_BODY = f'roll out config with api_key = "{SECRET}" tonight\n'


def _new_session(tmp_path, *, stamp, sid, mission):
    """AuditTrail session under a deterministic chronological dir name.

    Directory order drives the stats pipeline (mission "latest" attempts),
    so the auto-generated wall-clock stamp is replaced by a fixed one.
    """
    audit = AuditTrail(tmp_path, AUDIT_DIR, mission, sid)
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in mission)
    target = tmp_path / AUDIT_DIR / f"{stamp}-{safe}-{sid[:8]}"
    if audit.dir != target:
        audit.dir.rename(target)
        audit.dir = target
    return audit


def _reviewed_success_session(tmp_path):
    """Reviewed success that needed one failed verification + nonlinear
    reset-to-checkpoint recovery, reporting mutation telemetry."""
    audit = _new_session(tmp_path, stamp="20260820-100000",
                         sid=SID_SUCCESS, mission="dogfood-real")
    audit.log_event("session_start", {"session_id": SID_SUCCESS})
    audit.log_event("plan", {"summary": "add feature, verify, review"})
    audit.save_response("execute", {
        "status": "failed", "logs": f"pytest failed: {FAIL_CMD}"})
    audit.save_verification(1, [
        VerificationResult(command=FAIL_CMD, exit_code=1,
                           stderr="1 failed", passed=False)])
    # Nonlinear recovery: the repair round escalates to reset_to_checkpoint,
    # restoring the checkpoint state before the resend (dogfood-24).
    audit.log_event("recovery_started", {
        "attempt": 2, "strategy": "reset_to_checkpoint"})
    audit.log_event("recovery_reset", {"attempt": 2})
    audit.save_prompt("recovery-plan", PROMPT_BODY)
    audit.save_response("execute-recovery", {
        "status": "completed",
        "logs": f"deployed using token {SECRET} successfully"})
    audit.save_verification(2, [
        VerificationResult(command=FAIL_CMD, exit_code=0, passed=True)])
    audit.log_event("review", {"verdict": "approve", "reason": "solid work"})
    report = {
        "session_id": SID_SUCCESS,
        "mission_name": "dogfood-real",
        "adapter": "mock",
        "status": "success",
        "verification_results": [
            {"command": FAIL_CMD, "exit_code": 0, "passed": True}],
        "recovery_attempts": [{
            "attempt": 1,
            "failure_class": "test_failure",
            "strategy": "reset_to_checkpoint",
            "failing_output": f"pytest failed: {FAIL_CMD}",
            "changed_files_at_attempt": ["src/feature.py"],
        }],
        "changed_files": ["src/feature.py"],
        "usage": {"tokens": 1600, "send_count": 4},
        "cumulative_usage": {"wall_seconds": 41.5, "send_count": 4},
        "mutation": {"total": 8, "killed": 6, "survived": 2,
                     "skipped": 0, "kill_rate": 0.75},
        "next_steps": [],
        "review": {"enabled": True, "adapter": "mock",
                   "verdict": "approve", "reason": "solid work"},
        "audit_dir": str(audit.dir),
    }
    audit.write_report(report)
    audit.log_event("session_end", {"status": "success"})
    return audit.dir


def _gauntlet_success_session(tmp_path):
    """First-try success from a clean-room + mutation gauntlet run
    (modeled on the dogfood-27 integrated gauntlet session)."""
    audit = _new_session(tmp_path, stamp="20260820-090000",
                         sid=SID_GAUNTLET, mission="dogfood-real")
    audit.log_event("session_start", {"session_id": SID_GAUNTLET})
    audit.log_event("plan", {"summary": "gauntlet: clean-room + mutation"})
    audit.save_response("execute", {
        "status": "completed", "logs": "all tiers green on attempt 1"})
    audit.save_verification(1, [
        VerificationResult(command=FAIL_CMD, exit_code=0, passed=True)])
    audit.log_event("mutation", {"killed": 7, "survived": 3,
                                 "kill_rate": 0.7})
    report = {
        "session_id": SID_GAUNTLET,
        "mission_name": "dogfood-real",
        "adapter": "mock",
        "status": "success",
        "verification_results": [
            {"command": FAIL_CMD, "exit_code": 0, "passed": True}],
        "recovery_attempts": [],
        "changed_files": ["src/gauntlet.py"],
        "usage": {"tokens": 2200, "send_count": 2},
        "cumulative_usage": {"wall_seconds": 15.5, "send_count": 2},
        "mutation": {"total": 10, "killed": 7, "survived": 3,
                     "skipped": 0, "kill_rate": 0.7},
        "next_steps": [],
        "audit_dir": str(audit.dir),
    }
    audit.write_report(report)
    audit.log_event("session_end", {"status": "success"})
    return audit.dir


def _oscillation_failure_session(tmp_path):
    """Aborted after the same failure recurred across reset-to-checkpoint."""
    audit = _new_session(tmp_path, stamp="20260821-100000",
                         sid=SID_OSCILLATION, mission="dogfood-real")
    signature = "exit=1|pytest -q tests/test_feature.py"
    audit.log_event("session_start", {"session_id": SID_OSCILLATION})
    audit.log_event("plan", {"summary": "fix the flaky feature test"})
    for attempt in (1, 2, 3):
        audit.save_response(f"execute-{attempt}", {
            "status": "failed",
            "logs": f"attempt {attempt}: {FAIL_CMD} still red"})
        audit.save_verification(attempt, [
            VerificationResult(command=FAIL_CMD, exit_code=1,
                               stderr="1 failed", passed=False)])
    audit.log_event("oscillation_detected", {
        "attempt": 3, "signature": signature,
        "occurrences": 3, "escalated": True})
    report = {
        "session_id": SID_OSCILLATION,
        "mission_name": "dogfood-real",
        "adapter": "mock",
        "status": "failed",
        "verification_results": [
            {"command": FAIL_CMD, "exit_code": 1, "passed": False}],
        "recovery_attempts": [
            {"attempt": 1, "failure_class": "test_failure",
             "failing_output": FAIL_CMD, "changed_files_at_attempt": []},
            {"attempt": 2, "failure_class": "test_failure",
             "failing_output": FAIL_CMD,
             "changed_files_at_attempt": ["src/feature.py"]},
            {"attempt": 3, "failure_class": "oscillation_detected",
             "failing_output": FAIL_CMD, "changed_files_at_attempt": [],
             "oscillation_signature": signature},
        ],
        "changed_files": ["src/feature.py"],
        "usage": {"tokens": 5400, "send_count": 9},
        "cumulative_usage": {"wall_seconds": 180.25, "send_count": 9},
        "next_steps": ["Oscillation detected: address the root cause "
                       "manually, then roll back."],
        "audit_dir": str(audit.dir),
    }
    audit.write_report(report)
    audit.log_event("session_end", {"status": "failed"})
    return audit.dir


def _budget_breach_session(tmp_path):
    """Aborted by max_sends before verification ever ran."""
    audit = _new_session(tmp_path, stamp="20260822-100000",
                         sid=SID_BUDGET, mission="dogfood-budgeted")
    breach = {"limit": "max_sends", "threshold": 2, "observed": 3}
    audit.log_event("session_start", {"session_id": SID_BUDGET})
    audit.log_event("budget_exceeded", breach)
    report = {
        "session_id": SID_BUDGET,
        "mission_name": "dogfood-budgeted",
        "adapter": "mock",
        "status": "failed",
        "verification_results": [],
        "recovery_attempts": [],
        "changed_files": [],
        "usage": {"tokens": 900, "send_count": 3},
        "cumulative_usage": {"wall_seconds": 12.0, "send_count": 3},
        "budget_exceeded": breach,
        "next_steps": [],
        "audit_dir": str(audit.dir),
    }
    audit.write_report(report)
    audit.log_event("session_end", {"status": "failed"})
    return audit.dir


def _seed_project(tmp_path):
    gauntlet = _gauntlet_success_session(tmp_path)
    success = _reviewed_success_session(tmp_path)
    osc = _oscillation_failure_session(tmp_path)
    budget = _budget_breach_session(tmp_path)
    # A file outside any session directory carrying the same secret.
    outside = tmp_path / ".tether" / "operator-notes.txt"
    outside.write_text(PROMPT_BODY, encoding="utf-8")
    return gauntlet, success, osc, budget, outside


def _snapshot(root):
    """path -> bytes for every file below root (tamper check helper)."""
    return {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# ------------------------------------------------------------- stats


def test_stats_over_realistic_dogfood_sessions(tmp_path):
    _seed_project(tmp_path)

    rj = runner.invoke(app, ["sessions", "stats", "--json",
                             "--project-dir", str(tmp_path)])
    assert rj.exit_code == 0, rj.output
    data = json.loads(rj.output)

    assert data["total_sessions"] == 4
    assert data["statuses"] == {
        "cancelled": {"count": 0, "pct": 0.0},
        "failed": {"count": 2, "pct": 50.0},
        "success": {"count": 2, "pct": 50.0},
    }
    # Attempts come from the real verification/attempt-*.json globs:
    # gauntlet landed on attempt 1, success recovered on attempt 2,
    # oscillation burned 3, budget breach aborted before any verification.
    assert data["attempts"] == {"median": 1.5, "max": 3}
    assert data["recovery"] == {
        "sessions_with_recovery_attempts": 2,
        "recoveries_ending_in_success": 1,
        "success_rate_pct": 50.0,
    }
    assert data["top_failing_commands"] == [
        {"command": FAIL_CMD, "count": 1}]
    assert data["adapters"]["mock"] == {"count": 4, "success_rate_pct": 50.0}
    assert data["review_gate"] == {
        "sessions_reviewed": 1,
        "verdicts": {"approve": 1, "request_changes": 0},
        "rejections_caused_failures": 0,
    }
    assert data["budgets"] == {"sessions_exceeded": 1}
    # Three dogfood-real sessions form a baseline whose latest entry (the
    # oscillation abort, 3 attempts) regresses against the trailing median
    # of [1, 2]; the solo budgeted mission does not qualify.
    assert set(data["missions"]) == {"dogfood-real"}
    assert data["missions"]["dogfood-real"] == {
        "count": 3,
        "success_rate_pct": 66.7,
        "median_attempts": 2.0,
        "max_attempts": 3,
        "latest_attempts": 3,
        "trend": "regression",
    }
    # Mutation kill rates aggregate across BOTH reporting sessions:
    # 6+7 killed of 8+10 total mutants.
    assert data["mutation"] == {
        "sessions_reporting": 2,
        "killed": 13,
        "survived": 5,
        "skipped": 0,
        "kill_rate": 0.7222,
    }
    assert data["usage"] == {
        "sessions_reporting": 4,
        "totals": {"send_count": 18.0, "tokens": 10100.0},
    }

    rh = runner.invoke(app, ["sessions", "stats",
                             "--project-dir", str(tmp_path)])
    assert rh.exit_code == 0, rh.output
    out = rh.output
    assert "Sessions: 4 total" in out
    assert "success: 2 (50.0%)" in out
    assert "failed: 2 (50.0%)" in out
    assert "Verification attempts: median 1.5, max 3" in out
    assert ("Recovery success rate: 50.0% "
            "(1/2 with recovery attempts)") in out
    assert f"1x {FAIL_CMD}" in out
    assert "mock: 4 session(s), success rate 50.0%" in out
    assert "Review gate: 1 reviewed session(s), approve 1, " \
           "request_changes 0, rejections causing failures 0" in out
    assert "Budgets: 1 session(s) exceeded a mission budget" in out
    assert ("dogfood-real: 3 sessions, success 66.7%, median attempts 2.0, "
            "latest: 3 attempts (regression)") in out
    assert ("Usage: 4 session(s) reporting; "
            "send_count total 18, tokens total 10100") in out
    assert ("Mutation: 2 session(s) reporting; "
            "killed 13/18 mutant(s) (kill_rate 0.7222)") in out


def test_stats_minimal_tree_omits_conditional_lines(tmp_path):
    """The Review gate / Budgets / Mutation / Per-mission / Usage lines are
    conditional human-output telemetry: they appear only when at least one
    session carries that payload, never for plain legacy-style trees."""
    for stamp, sid, mission, status in (
            ("20260823-010000", "111111111111", "solo-alpha", "success"),
            ("20260823-020000", "222222222222", "solo-beta", "failed")):
        audit = _new_session(tmp_path, stamp=stamp, sid=sid, mission=mission)
        audit.write_report({"session_id": sid, "mission_name": mission,
                            "adapter": "mock", "status": status,
                            "audit_dir": str(audit.dir)})

    rj = runner.invoke(app, ["sessions", "stats", "--json",
                             "--project-dir", str(tmp_path)])
    assert rj.exit_code == 0, rj.output
    data = json.loads(rj.output)
    assert data["total_sessions"] == 2
    assert data["budgets"] == {"sessions_exceeded": 0}
    assert data["mutation"]["sessions_reporting"] == 0
    assert "missions" not in data and "usage" not in data

    rh = runner.invoke(app, ["sessions", "stats",
                             "--project-dir", str(tmp_path)])
    assert rh.exit_code == 0, rh.output
    assert "Sessions: 2 total" in rh.output
    assert "success: 1 (50.0%)" in rh.output
    assert "failed: 1 (50.0%)" in rh.output
    for absent in ("Review gate:", "Budgets:", "Mutation:",
                   "Per-mission:", "Usage:"):
        assert absent not in rh.output


def test_stats_review_gate_rejection_accounting(tmp_path):
    """A request_changes verdict on a failed session is tallied as a
    rejection that caused the failure (dogfood-18 telemetry)."""
    approved = _new_session(tmp_path, stamp="20260818-090000",
                            sid=SID_APPROVED, mission="dogfood-reviewed")
    approved.save_verification(1, [
        VerificationResult(command=FAIL_CMD, exit_code=0, passed=True)])
    approved.write_report({
        "session_id": SID_APPROVED,
        "mission_name": "dogfood-reviewed",
        "adapter": "mock",
        "status": "success",
        "verification_results": [
            {"command": FAIL_CMD, "exit_code": 0, "passed": True}],
        "recovery_attempts": [],
        "changed_files": [],
        "review": {"enabled": True, "adapter": "mock",
                   "verdict": "approve", "reason": "ship it"},
        "audit_dir": str(approved.dir),
    })
    rejected = _new_session(tmp_path, stamp="20260819-090000",
                            sid=SID_REJECTED, mission="dogfood-reviewed")
    rejected.save_verification(1, [
        VerificationResult(command=FAIL_CMD, exit_code=1,
                           stderr="1 failed", passed=False)])
    rejected.write_report({
        "session_id": SID_REJECTED,
        "mission_name": "dogfood-reviewed",
        "adapter": "mock",
        "status": "failed",
        "verification_results": [
            {"command": FAIL_CMD, "exit_code": 1, "passed": False}],
        "recovery_attempts": [],
        "changed_files": [],
        "review": {"enabled": True, "adapter": "mock",
                   "verdict": "request_changes",
                   "reason": "the change was never finished"},
        "audit_dir": str(rejected.dir),
    })

    rj = runner.invoke(app, ["sessions", "stats", "--json",
                             "--project-dir", str(tmp_path)])
    assert rj.exit_code == 0, rj.output
    data = json.loads(rj.output)

    assert data["total_sessions"] == 2
    assert data["statuses"] == {
        "cancelled": {"count": 0, "pct": 0.0},
        "failed": {"count": 1, "pct": 50.0},
        "success": {"count": 1, "pct": 50.0},
    }
    assert data["review_gate"] == {
        "sessions_reviewed": 2,
        "verdicts": {"approve": 1, "request_changes": 1},
        "rejections_caused_failures": 1,
    }
    assert data["top_failing_commands"] == [
        {"command": FAIL_CMD, "count": 1}]
    assert set(data["missions"]) == {"dogfood-reviewed"}

    rh = runner.invoke(app, ["sessions", "stats",
                             "--project-dir", str(tmp_path)])
    assert rh.exit_code == 0, rh.output
    assert "Review gate: 2 reviewed session(s), approve 1, " \
           "request_changes 1, rejections causing failures 1" in rh.output


def _crashed_incomplete_dir(root):
    """Crashed run: events only, died before any report was written."""
    d = root / "20260815-000000-crashed-incomplete-9999abcd"
    d.mkdir()
    (d / "events.jsonl").write_text(json.dumps(
        {"ts": "2026-08-15T00:00:00+00:00", "kind": "session_start",
         "session_id": "9999abcd1234", "prev": ""}) + "\n",
        encoding="utf-8")
    return d


def _corrupted_session_dir(root):
    """Mid-write kill: report.json truncated AND events.jsonl left holding
    an unparseable tail line — the two damages a real crash leaves behind."""
    d = root / "20260816-000000-corrupt-report-8888abcd"
    d.mkdir()
    (d / "report.json").write_text('{"status": "succ', encoding="utf-8")
    (d / "events.jsonl").write_text(
        '{"ts": "not json at all",,,\n', encoding="utf-8")
    return d


def _legacy_success_dir(tmp_path):
    """dogfood-14-era layout: minimal report plus one verification file;
    predates review/usage/budget/mutation telemetry entirely. Built with the
    real AuditTrail helpers; no events are ever logged, so events.jsonl is
    absent exactly as in era backups."""
    audit = _new_session(tmp_path, stamp="20260817-000000",
                         sid="7777abcd1234", mission="dogfood-v13-legacy")
    audit.save_verification(1, [
        VerificationResult(command=FAIL_CMD, exit_code=0, passed=True)])
    audit.write_report({
        "session_id": "7777abcd1234",
        "mission_name": "dogfood-v13-legacy",
        "adapter": "codex-classic",
        "status": "success",
        "changed_files": [],
        "next_steps": [],
        "audit_dir": str(audit.dir),
    })
    return audit.dir


def test_stats_survives_malformed_and_legacy_session_dirs(tmp_path):
    """Real trees mix eras and failures: dirs that crashed before writing
    a report, reports truncated mid-write, and legacy minimal reports.
    Stats must count exactly the readable reports and never crash;
    sessions list keeps every directory visible. Corrupting one readable
    report in place afterwards proves the aggregates are driven by the
    fixture inputs: every count that depends on it moves, nothing else."""
    _seed_project(tmp_path)
    root = tmp_path / AUDIT_DIR
    incomplete = _crashed_incomplete_dir(root)
    corrupt = _corrupted_session_dir(root)
    legacy = _legacy_success_dir(tmp_path)

    rj = runner.invoke(app, ["sessions", "stats", "--json",
                             "--project-dir", str(tmp_path)])
    assert rj.exit_code == 0, rj.output
    data = json.loads(rj.output)

    # 4 readable seeded reports plus the legacy one; malformed dirs skipped.
    assert data["total_sessions"] == 5
    assert data["statuses"] == {
        "cancelled": {"count": 0, "pct": 0.0},
        "failed": {"count": 2, "pct": 40.0},
        "success": {"count": 3, "pct": 60.0},
    }
    # verification files per dir: gauntlet 1, budget 0, legacy 1,
    # success 2, osc 3.
    assert data["attempts"] == {"median": 1.0, "max": 3}
    assert data["recovery"] == {
        "sessions_with_recovery_attempts": 2,
        "recoveries_ending_in_success": 1,
        "success_rate_pct": 50.0,
    }
    assert data["top_failing_commands"] == [
        {"command": FAIL_CMD, "count": 1}]
    assert data["adapters"] == {
        "codex-classic": {"count": 1, "success_rate_pct": 100.0},
        "mock": {"count": 4, "success_rate_pct": 50.0},
    }
    assert data["review_gate"] == {
        "sessions_reviewed": 1,
        "verdicts": {"approve": 1, "request_changes": 0},
        "rejections_caused_failures": 0,
    }
    assert data["budgets"] == {"sessions_exceeded": 1}
    # The solo legacy mission stays out of baselines; dogfood-real is
    # untouched by its presence.
    assert set(data["missions"]) == {"dogfood-real"}
    assert data["missions"]["dogfood-real"]["count"] == 3
    assert data["usage"] == {
        "sessions_reporting": 4,
        "totals": {"send_count": 18.0, "tokens": 10100.0},
    }

    rh = runner.invoke(app, ["sessions", "stats",
                             "--project-dir", str(tmp_path)])
    assert rh.exit_code == 0, rh.output
    assert "Sessions: 5 total" in rh.output
    assert "success: 3 (60.0%)" in rh.output
    assert "failed: 2 (40.0%)" in rh.output
    assert "Verification attempts: median 1.0, max 3" in rh.output
    assert "codex-classic: 1 session(s), success rate 100.0%" in rh.output

    rl = runner.invoke(app, ["sessions", "list",
                             "--project-dir", str(tmp_path)])
    assert rl.exit_code == 0, rl.output
    rows = {}
    for ln in rl.output.splitlines():
        parts = ln.split()
        if parts:
            rows[parts[0]] = parts
    assert rows[incomplete.name][1:] == ["?", "?"]
    assert rows[corrupt.name][1:] == ["?", "?"]
    assert rows[legacy.name][1:] == ["dogfood-v13-legacy", "success"]

    # Sensitivity check inside the fixture tree: truncating the legacy
    # report moves exactly the aggregates that read it -- total, statuses,
    # adapters, attempts -- while usage/recovery/mutation (sourced from the
    # seeded reports only) are untouched.
    (legacy / "report.json").write_text('{"status": "succ', encoding="utf-8")
    rj = runner.invoke(app, ["sessions", "stats", "--json",
                             "--project-dir", str(tmp_path)])
    assert rj.exit_code == 0, rj.output
    data = json.loads(rj.output)
    assert data["total_sessions"] == 4
    assert data["statuses"] == {
        "cancelled": {"count": 0, "pct": 0.0},
        "failed": {"count": 2, "pct": 50.0},
        "success": {"count": 2, "pct": 50.0},
    }
    assert data["adapters"] == {"mock": {"count": 4, "success_rate_pct": 50.0}}
    assert data["attempts"] == {"median": 1.5, "max": 3}
    assert data["mutation"] == {
        "sessions_reporting": 2,
        "killed": 13,
        "survived": 5,
        "skipped": 0,
        "kill_rate": 0.7222,
    }
    assert data["usage"] == {
        "sessions_reporting": 4,
        "totals": {"send_count": 18.0, "tokens": 10100.0},
    }


# ------------------------------------------------- scrub + event chain


def test_scrub_redacts_and_extends_audit_chain_end_to_end(tmp_path):
    gauntlet, success, osc, budget, outside = _seed_project(tmp_path)
    events_path = success / "events.jsonl"

    def chain_ok():
        rv = runner.invoke(app, ["logs", SID_SUCCESS, "--verify",
                                 "--project-dir", str(tmp_path)])
        return rv.exit_code == 0 and "OK" in rv.output, rv.output

    # The AuditTrail-written chain verifies before anything is touched...
    ok, out = chain_ok()
    assert ok, out
    events_before = [json.loads(ln) for ln in
                     events_path.read_text(encoding="utf-8").splitlines()]
    siblings_before = {
        d.name: _snapshot(d) for d in (gauntlet, osc, budget)}

    # ...dry run changes nothing and records no scrub event...
    r = runner.invoke(app, ["sessions", "scrub", SID_SUCCESS,
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "Would scrub" in r.output
    assert "Dry run: nothing modified" in r.output
    assert [json.loads(ln)["kind"] for ln in
            events_path.read_text().splitlines()] == [
        e["kind"] for e in events_before]

    # ...and --confirm rewrites both seeded files to sha256 markers.
    r = runner.invoke(app, ["sessions", "scrub", SID_SUCCESS, "--confirm",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "Scrubbed 2 file(s)" in r.output
    digest = hashlib.sha256(SECRET.encode()).hexdigest()
    marker = f"[REDACTED sha256={digest} len={len(SECRET)}]"
    prompt_text = (success / "prompts" / "002-recovery-plan.txt").read_text()
    response_text = next((success / "responses").glob("*execute-recovery*")
                         ).read_text()
    assert SECRET not in prompt_text and marker in prompt_text
    assert SECRET not in response_text and marker in response_text

    # Exactly one scrub event was appended, extending the existing chain.
    lines = events_path.read_text(encoding="utf-8").splitlines()
    events_after = [json.loads(ln) for ln in lines]
    scrubs = [e for e in events_after if e["kind"] == "scrub"]
    assert len(scrubs) == 1
    assert scrubs[0]["files"] == 2 and scrubs[0]["ts"]
    assert scrubs[0]["prev"] == event_hash(events_before[-1])
    assert len(events_after) == len(events_before) + 1

    # The tamper-evident chain still verifies through the CLI afterwards.
    ok, out = chain_ok()
    assert ok, out

    # Sibling sessions and files outside the session dir are untouched.
    for d, before in siblings_before.items():
        assert _snapshot(tmp_path / AUDIT_DIR / d) == before
    assert outside.read_text(encoding="utf-8") == PROMPT_BODY

    # Scrubbing is bounded to prompts/responses/verification: report.json
    # is intact, so the stats pipeline keeps working unchanged.
    rj = runner.invoke(app, ["sessions", "stats", "--json",
                             "--project-dir", str(tmp_path)])
    assert rj.exit_code == 0, rj.output
    assert json.loads(rj.output)["total_sessions"] == 4


def test_scrub_oscillation_failure_session_end_to_end(tmp_path):
    """Scrub + chain integrity on the aborted oscillation session.

    Realistic leak: a failing retry attempt dumped an exported CI token
    into its response log. The scrub must redact it, extend only that
    session's event chain, and leave every other session untouched.
    """
    gauntlet, success, osc, _budget, _outside = _seed_project(tmp_path)
    leak_path = osc / "responses" / "004-execute-retry.json"
    leak_path.write_text(json.dumps({
        "status": "failed",
        "logs": f"attempt 4 retry dump: GH_TOKEN={OSC_SECRET} exported"},
        indent=2), encoding="utf-8")
    events_path = osc / "events.jsonl"

    def chain_ok():
        rv = runner.invoke(app, ["logs", SID_OSCILLATION, "--verify",
                                 "--project-dir", str(tmp_path)])
        return rv.exit_code == 0 and "OK" in rv.output, rv.output

    ok, out = chain_ok()
    assert ok, out
    events_before = [json.loads(ln) for ln in
                     events_path.read_text(encoding="utf-8").splitlines()]
    success_before = _snapshot(success)

    # Dry run plans the rewrite without touching anything.
    r = runner.invoke(app, ["sessions", "scrub", SID_OSCILLATION,
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "Would scrub: " in r.output and "(1 match(es))" in r.output
    assert "Dry run: nothing modified" in r.output
    assert [json.loads(ln)["kind"] for ln in
            events_path.read_text().splitlines()] == [
        e["kind"] for e in events_before]

    # --confirm rewrites exactly the leaked response file.
    r = runner.invoke(app, ["sessions", "scrub", SID_OSCILLATION, "--confirm",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "Scrubbed 1 file(s)" in r.output
    digest = hashlib.sha256(OSC_SECRET.encode()).hexdigest()
    marker = f"[REDACTED sha256={digest} len={len(OSC_SECRET)}]"
    text = leak_path.read_text()
    assert OSC_SECRET not in text and marker in text

    # Sibling sessions keep their bytes; the scrub is bounded per session.
    assert _snapshot(success) == success_before

    # One chained scrub event extends the oscillation session's log...
    lines = events_path.read_text(encoding="utf-8").splitlines()
    events_after = [json.loads(ln) for ln in lines]
    scrubs = [e for e in events_after if e["kind"] == "scrub"]
    assert len(scrubs) == 1
    assert scrubs[0]["files"] == 1 and scrubs[0]["ts"]
    assert scrubs[0]["prev"] == event_hash(events_before[-1])
    assert len(events_after) == len(events_before) + 1

    # ...and the tamper-evident chain still verifies afterwards.
    ok, out = chain_ok()
    assert ok, out

    # report.json was not scrubbed, so stats keep parsing all sessions.
    rj = runner.invoke(app, ["sessions", "stats", "--json",
                             "--project-dir", str(tmp_path)])
    assert rj.exit_code == 0, rj.output
    data = json.loads(rj.output)
    assert data["total_sessions"] == 4
    assert data["statuses"]["failed"]["count"] == 2


def test_tampered_chain_detected_while_pipeline_stays_healthy(tmp_path):
    """Tamper-evidence on the realistic tree: a rewritten history is
    caught at the next link, siblings stay verifiable, and the rest of
    the ops pipeline keeps working (chain integrity is per session)."""
    gauntlet, success, osc, _budget, _outside = _seed_project(tmp_path)
    events_path = success / "events.jsonl"

    def verify(sid):
        return runner.invoke(app, ["logs", sid, "--verify",
                                   "--project-dir", str(tmp_path)])

    rv = verify(SID_SUCCESS)
    assert rv.exit_code == 0 and "intact (6 events)" in rv.output

    # Attack: rewrite a historical event's payload while keeping its
    # recorded prev hash (hide the recovery step). The next link exposes it.
    original = events_path.read_bytes()
    events = [json.loads(ln) for ln in
              events_path.read_text(encoding="utf-8").splitlines()]
    events[1]["summary"] = "nothing happened here"
    events_path.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    rv = verify(SID_SUCCESS)
    assert rv.exit_code == 1
    assert "Event chain BROKEN" in rv.output
    assert "event 3 (kind='recovery_started')" in rv.output

    # Sibling sessions remain verifiable and stats still reads every
    # report: a broken chain never takes the rest of the tree down.
    rv = verify(SID_OSCILLATION)
    assert rv.exit_code == 0 and "intact (4 events)" in rv.output
    rj = runner.invoke(app, ["sessions", "stats", "--json",
                             "--project-dir", str(tmp_path)])
    assert rj.exit_code == 0, rj.output
    assert json.loads(rj.output)["total_sessions"] == 4

    # Restoring the original bytes heals the chain (no sticky state)...
    events_path.write_bytes(original)
    rv = verify(SID_SUCCESS)
    assert rv.exit_code == 0 and "intact (6 events)" in rv.output

    # ...and a forged appended event claiming a bogus prev is caught too.
    with events_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "2026-08-23T00:00:00+00:00",
                            "kind": "scrub", "files": 0,
                            "prev": "f" * 64}) + "\n")
    rv = verify(SID_SUCCESS)
    assert rv.exit_code == 1
    assert "event 7 (kind='scrub')" in rv.output


# ---------------------------------------------------------------- clean


def test_clean_removes_only_backdated_session(tmp_path):
    gauntlet, success, osc, budget, _outside = _seed_project(tmp_path)
    now = time.time()
    old_mtime = now - 40 * 86400
    os.utime(budget, (old_mtime, old_mtime))

    # Dry run: only the backdated session is a candidate; nothing deleted.
    r = runner.invoke(app, ["sessions", "clean", "--older-than", "30d",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert f"Would delete: {budget}" in r.output
    rest = r.output.split("Would delete", 1)[1]
    assert (success.name not in rest and osc.name not in rest
            and gauntlet.name not in rest)
    assert "Dry run: nothing deleted" in r.output
    assert budget.exists() and success.exists() and osc.exists()

    # Confirm: exactly the backdated directory is removed.
    r = runner.invoke(app, ["sessions", "clean", "--older-than", "30d",
                            "--confirm", "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "Deleted 1 session directory" in r.output
    assert not budget.exists()
    assert success.exists() and osc.exists()

    # Surviving sessions remain fully usable.
    rv = runner.invoke(app, ["logs", SID_OSCILLATION, "--verify",
                             "--project-dir", str(tmp_path)])
    assert rv.exit_code == 0, rv.output
    assert "intact (4 events)" in rv.output


def test_scrub_survives_corrupted_and_legacy_sessions(tmp_path):
    """Malformed eras never crash scrub: the corrupted session redacts and
    best-effort anchors a fresh chain segment onto its unparseable log; the
    secret-free legacy session is a no-op; neighbors stay byte-identical."""
    gauntlet, success, osc, _budget, _outside = _seed_project(tmp_path)
    root = tmp_path / AUDIT_DIR
    corrupted = _corrupted_session_dir(root)
    _legacy_success_dir(tmp_path)
    leak = corrupted / "prompts" / "001-plan.txt"
    leak.parent.mkdir(parents=True)
    leak.write_text(PROMPT_BODY, encoding="utf-8")
    success_before = _snapshot(success)

    def verify_corrupted():
        return runner.invoke(app, ["logs", "8888abcd1234", "--verify",
                                   "--project-dir", str(tmp_path)])

    # The unparseable log is reported broken before anything runs...
    rv = verify_corrupted()
    assert rv.exit_code == 1
    assert "event 1: invalid JSON" in rv.output

    # ...dry run plans the redaction without writing...
    r = runner.invoke(app, ["sessions", "scrub", "8888abcd1234",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "Would scrub" in r.output and "(1 match(es))" in r.output
    assert SECRET in leak.read_text()

    # ...and --confirm redacts, appending a fresh chain segment: the scrub
    # event anchors on an empty prev because the garbage tail cannot be
    # hashed; pre-existing corruption above it is never silently healed.
    r = runner.invoke(app, ["sessions", "scrub", "8888abcd1234", "--confirm",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert SECRET not in leak.read_text()
    lines = (corrupted / "events.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert len(lines) == 2
    scrub_event = json.loads(lines[1])
    assert scrub_event["kind"] == "scrub"
    assert scrub_event["files"] == 1 and scrub_event["ts"]
    assert scrub_event["prev"] == ""
    rv = verify_corrupted()
    assert rv.exit_code == 1
    assert "event 1: invalid JSON" in rv.output

    # Sibling sessions keep their bytes; stats still parses every report.
    assert _snapshot(success) == success_before
    rj = runner.invoke(app, ["sessions", "stats", "--json",
                             "--project-dir", str(tmp_path)])
    assert rj.exit_code == 0, rj.output
    assert json.loads(rj.output)["total_sessions"] == 5

    # Legacy session: no secret-like material -> clean no-op, no writes,
    # and still no events.jsonl (nothing was appended).
    r = runner.invoke(app, ["sessions", "scrub", "7777abcd1234",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "No secret-like material found" in r.output
    legacy_dir = root / "20260817-000000-dogfood-v13-legacy-7777abcd"
    assert not (legacy_dir / "events.jsonl").exists()


def test_clean_threshold_boundary_and_retention_fallback(tmp_path):
    """Only strictly-over-age directories are candidates across the cutoff
    boundary; survivors stay fully usable; omitting --older-than falls back
    to the configured retention_days; with neither knob clean refuses."""
    gauntlet, success, osc, budget, _outside = _seed_project(tmp_path)
    now = time.time()
    old = now - 31 * 86400     # strictly beyond a 30d threshold
    young = now - 29 * 86400   # strictly inside it
    os.utime(budget, (old, old))
    os.utime(osc, (old, old))
    os.utime(success, (young, young))
    os.utime(gauntlet, (young - 3600, young - 3600))

    # Dry run lists exactly the over-age directories; nothing is deleted.
    r = runner.invoke(app, ["sessions", "clean", "--older-than", "30d",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert f"Would delete: {budget}" in r.output
    assert f"Would delete: {osc}" in r.output
    rest = r.output[r.output.index("Would delete"):]
    assert success.name not in rest and gauntlet.name not in rest
    assert "Dry run: nothing deleted" in r.output
    assert all(d.exists() for d in (budget, osc, success, gauntlet))

    # Confirm removes exactly those directories.
    r = runner.invoke(app, ["sessions", "clean", "--older-than", "30d",
                            "--confirm", "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "Deleted 2 session directories" in r.output
    assert not budget.exists() and not osc.exists()
    assert success.exists() and gauntlet.exists()

    # Survivors remain fully usable: chains verify, stats includes them.
    rv = runner.invoke(app, ["logs", SID_SUCCESS, "--verify",
                             "--project-dir", str(tmp_path)])
    assert rv.exit_code == 0, rv.output
    assert "intact (6 events)" in rv.output
    rj = runner.invoke(app, ["sessions", "stats", "--json",
                             "--project-dir", str(tmp_path)])
    assert rj.exit_code == 0, rj.output
    data = json.loads(rj.output)
    assert data["total_sessions"] == 2
    assert data["statuses"]["success"]["count"] == 2

    # retention_days fallback when --older-than is omitted: only the 8-day-
    # old session crosses a 7-day retention window.
    proj2 = tmp_path / "retention-project"
    proj2.mkdir()
    (proj2 / "tether.yaml").write_text("retention_days: 7\n",
                                       encoding="utf-8")
    g2, s2, o2, b2, _out2 = _seed_project(proj2)
    stale = time.time() - 8 * 86400
    os.utime(b2, (stale, stale))
    r = runner.invoke(app, ["sessions", "clean",
                            "--project-dir", str(proj2)])
    assert r.exit_code == 0, r.output
    assert f"Would delete: {b2}" in r.output
    rest = r.output[r.output.index("Would delete"):]
    assert s2.name not in rest and o2.name not in rest
    assert g2.name not in rest
    r = runner.invoke(app, ["sessions", "clean", "--confirm",
                            "--project-dir", str(proj2)])
    assert r.exit_code == 0, r.output
    assert "Deleted 1 session directory" in r.output
    assert not b2.exists()
    assert s2.exists() and o2.exists() and g2.exists()

    # Neither knob configured: clear failure, nothing deleted.
    proj3 = tmp_path / "bare-project"
    proj3.mkdir()
    lone = _new_session(proj3, stamp="20260823-030000",
                        sid="333333333333", mission="solo")
    lone.write_report({"session_id": "333333333333",
                       "mission_name": "solo", "adapter": "mock",
                       "status": "success", "audit_dir": str(lone.dir)})
    r = runner.invoke(app, ["sessions", "clean",
                            "--project-dir", str(proj3)])
    assert r.exit_code == 1
    assert "retention_days is not configured" in r.output
    assert lone.dir.exists()
