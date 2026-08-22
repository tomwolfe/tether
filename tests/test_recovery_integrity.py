"""dogfood-12: recovery-loop re-gating, forensic refresh, and repair intel."""
import json
import subprocess
import sys
from pathlib import Path

from tether.adapters.base import AgentAdapter, SessionInfo
from tether.audit import find_session_dir
from tether.mission import load_mission
from tether.models import AgentState, TetherConfig
from tether.orchestrator import Orchestrator

PASS_CMD = f"{sys.executable} -c 'import sys; sys.exit(0)'"
FAIL_CMD = f"{sys.executable} -c 'import sys; sys.exit(1)'"


class _RepairingAdapter(AgentAdapter):
    """Scripted adapter: plan send -> completed.

    The execute send writes ``execute_files`` (relative to the project dir);
    each subsequent (recovery/repair) send records the prompt it received,
    writes ``repair_files``, and completes.
    """

    name = "repairing"
    verified = True

    def __init__(self, execute_files, repair_files):
        super().__init__({})
        self.execute_files = dict(execute_files)
        self.repair_files = dict(repair_files)
        self.repair_prompts: list[str] = []
        self._planned = False
        self._executed = False

    def is_available(self):
        return True, ""

    def start_session(self, project_dir, session_id):
        self.project_dir = project_dir
        return SessionInfo(session_id=session_id, project_dir=project_dir)

    def _write(self, files):
        for rel, content in files.items():
            target = Path(self.project_dir) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    def send(self, prompt, session):
        if not self._planned:
            self._planned = True
            return AgentState(status="completed", logs="plan")
        if not self._executed:
            self._executed = True
            self._write(self.execute_files)
            return AgentState(status="completed", logs="done")
        self.repair_prompts.append(prompt)
        self._write(self.repair_files)
        return AgentState(status="completed", logs="repaired")

    def cancel(self, session):
        pass


def _mission(tmp_path, body=None, name="m.yaml"):
    text = body or (
        f"mission:\n  name: m\n  goal: g\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\nadapter: mock\n"
    )
    p = tmp_path / name
    p.write_text(text)
    return p


def _git_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"],
                   cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"],
                   check=True)


def _committed_mission(tmp_path, body=None, name="m.yaml"):
    mp = _mission(tmp_path, body=body, name=name)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "mission"],
                   check=True)
    return mp


def _session_dir(tmp_path, report):
    return find_session_dir(tmp_path, ".tether/sessions", report["session_id"])


def test_recovery_send_writing_forbidden_file_fails_and_skips_verification(
        tmp_path):
    _git_repo(tmp_path)
    mp = _committed_mission(tmp_path, body=(
        "mission:\n  name: rec-sbx\n  goal: g\n"
        "forbidden_paths:\n  - '*.secret'\n"
        f"verification:\n  commands:\n    - {FAIL_CMD}\nadapter: mock\n"
    ))
    # execute writes an allowed tracked file; the repair send then writes a
    # forbidden one -- the recovery loop must catch it before re-verifying.
    adapter = _RepairingAdapter(
        execute_files={"f.txt": "changed by agent\n"},
        repair_files={"leaked.secret": "boom"},
    )
    cfg = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"
    assert {"path": "leaked.secret",
            "rule": "forbidden_paths: *.secret"} in \
        report["sandbox_violations"]
    # verification ran exactly once; the violating recovery send skipped the
    # second verification round entirely
    assert len(report["verification_results"]) == 1
    assert report["verification_results"][0]["passed"] is False
    session = _session_dir(tmp_path, report)
    assert not (session / "verification" / "attempt-02.json").exists()
    events = [json.loads(line) for line in
              (session / "events.jsonl").read_text(encoding="utf-8")
              .splitlines()]
    assert any(e.get("kind") == "sandbox_violations" and
               {"path": "leaked.secret",
                "rule": "forbidden_paths: *.secret"}
               in e["violations"] for e in events)
    assert any("leaked.secret" in s for s in report["next_steps"])
    assert "leaked.secret" in report["changed_files"]


def test_repair_prompt_contains_changed_files_and_patch_excerpt(tmp_path):
    _git_repo(tmp_path)
    mp = _committed_mission(tmp_path, body=(
        "mission:\n  name: rec-intel\n  goal: g\n"
        f"verification:\n  commands:\n    - {FAIL_CMD}\nadapter: mock\n"
    ))
    adapter = _RepairingAdapter(
        execute_files={"f.txt": "changed by agent\n"},
        repair_files={"fix.txt": "attempted fix\n"},
    )
    cfg = TetherConfig(audit_dir=".tether/sessions", max_attempts=3)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"  # verification never passes
    assert len(adapter.repair_prompts) == 2
    first = adapter.repair_prompts[0]
    assert "f.txt" in first                    # current changed-file name
    assert "diff --git a/f.txt" in first       # patch.diff excerpt
    second = adapter.repair_prompts[1]
    assert "Changed files at previous attempt:" in second  # delta view
    assert "- fix.txt" in second               # current state includes repair
    assert "- f.txt" in second                 # previous attempt's snapshot


def test_recovery_attempts_record_changed_files_and_per_attempt_patch(tmp_path):
    _git_repo(tmp_path)
    mp = _committed_mission(tmp_path, body=(
        "mission:\n  name: rec-evi\n  goal: g\n"
        f"verification:\n  commands:\n    - {FAIL_CMD}\nadapter: mock\n"
    ))
    adapter = _RepairingAdapter(
        execute_files={"f.txt": "changed by agent\n"},
        repair_files={"fix.txt": "attempted fix\n"},
    )
    cfg = TetherConfig(audit_dir=".tether/sessions", max_attempts=2)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"
    entry = report["recovery_attempts"][0]
    assert entry["attempt"] == 1
    # per-attempt changed files reflect what that attempt produced
    assert "fix.txt" in entry["changed_files_at_attempt"]
    session = _session_dir(tmp_path, report)
    patch = session / "verification" / "attempt-01.patch"
    assert patch.exists()
    assert b"diff --git a/f.txt" in patch.read_bytes()


def test_non_git_repair_prompt_includes_manifest_excerpt(tmp_path):
    mp = _mission(tmp_path, body=(
        "mission:\n  name: rec-manifest\n  goal: g\n"
        f"verification:\n  commands:\n    - {FAIL_CMD}\nadapter: mock\n"
    ))
    (tmp_path / "seed.txt").write_text("v1")
    adapter = _RepairingAdapter(
        execute_files={"seed.txt": "v2"},
        repair_files={"created-by-repair.txt": "hi"},
    )
    cfg = TetherConfig(audit_dir=".tether/sessions", max_attempts=2)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"
    prompt = adapter.repair_prompts[0]
    assert "manifest_diff.json" in prompt
    assert "seed.txt" in prompt
    entry = report["recovery_attempts"][0]
    assert "created-by-repair.txt" in entry["changed_files_at_attempt"]


# --------------------- failure classification in recovery (dogfood-14 task 1)


def test_repair_prompt_opens_with_failure_class_and_guidance(tmp_path):
    fail_cmd = (f"{sys.executable} -c "
                "'import sys; print(\"FAILED tests/test_m.py\"); sys.exit(1)'")
    mp = _mission(tmp_path, body=(
        "mission:\n  name: rec-class\n  goal: g\n"
        f"verification:\n  commands:\n    - {fail_cmd}\nadapter: mock\n"
    ))
    adapter = _RepairingAdapter(execute_files={}, repair_files={})
    cfg = TetherConfig(audit_dir=".tether/sessions", max_attempts=3)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"
    assert len(adapter.repair_prompts) >= 1
    prompt = adapter.repair_prompts[0]
    assert "--- Failure class: test_failure ---" in prompt
    assert "Fix the failing assertions or the code they test." in prompt
    # the original failing output still follows the classification header
    assert "FAILED tests/test_m.py" in prompt
    entry = report["recovery_attempts"][0]
    assert entry["failure_class"] == "test_failure"
    saved = json.loads((_session_dir(tmp_path, report) /
                        "report.json").read_text(encoding="utf-8"))
    assert saved["recovery_attempts"][0]["failure_class"] == "test_failure"


def test_guidance_map_covers_every_failure_class():
    from tether.orchestrator import FAILURE_CLASS_GUIDANCE
    assert FAILURE_CLASS_GUIDANCE == {
        "compile_error": "Fix syntax/import/type errors first.",
        "test_failure": "Fix the failing assertions or the code they test.",
        "timeout": "The command timed out; simplify or optimize.",
        "missing_binary": "A required binary is missing; check the command.",
        "unknown": "Diagnose the failure from the output below.",
    }


# --------------------- crash-recovery detection (dogfood-14 task 4)


def test_find_incomplete_sessions_flags_crashed_and_logless_dirs(tmp_path):
    from tether.orchestrator import find_incomplete_sessions
    root = tmp_path / ".tether" / "sessions"
    done = root / "20260101-000000-done-aaaa1111"
    crashed = root / "20260101-000002-crashed-bbbb2222"
    logless = root / "20260101-000003-logless-cccc3333"
    for d in (done, crashed, logless):
        d.mkdir(parents=True)
    (done / "events.jsonl").write_text(
        '{"kind": "session_start", "prev": ""}\n'
        '{"kind": "session_end", "prev": "x"}\n', encoding="utf-8")
    (crashed / "events.jsonl").write_text(
        '{"kind": "session_start", "prev": ""}\n', encoding="utf-8")
    assert find_incomplete_sessions(tmp_path, ".tether/sessions") == [
        "20260101-000002-crashed-bbbb2222",
        "20260101-000003-logless-cccc3333",
    ]
    # the current session is excluded via its short-id directory suffix
    assert find_incomplete_sessions(
        tmp_path, ".tether/sessions",
        exclude_session_id="bbbb2222ffff") == [
            "20260101-000003-logless-cccc3333"]


def test_crashed_prior_session_flagged_in_next_steps_but_never_touched(tmp_path):
    crashed = tmp_path / ".tether" / "sessions" / \
        "20260101-000000-old-mission-deadbeef"
    crashed.mkdir(parents=True)
    original_events = (json.dumps({
        "ts": "2026-01-01T00:00:00+00:00", "kind": "session_start",
        "session_id": "deadbeefcafe01", "prev": "",
    }) + "\n")
    (crashed / "events.jsonl").write_text(original_events, encoding="utf-8")

    adapter = _RepairingAdapter(execute_files={}, repair_files={})
    cfg = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(adapter, cfg, tmp_path).run(
        load_mission(_mission(tmp_path)))
    assert report["status"] == "success"
    hits = [s for s in report["next_steps"] if "deadbeef" in s]
    assert len(hits) == 1
    assert "sessions clean" in hits[0]
    assert "session_end" in hits[0]
    # advisory only: the incomplete session was neither deleted nor modified
    assert (crashed / "events.jsonl").read_text(encoding="utf-8") == \
        original_events


def test_clean_prior_sessions_do_not_trigger_the_warning(tmp_path):
    adapter = _RepairingAdapter(execute_files={}, repair_files={})
    cfg = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(adapter, cfg, tmp_path).run(
        load_mission(_mission(tmp_path)))
    assert report["status"] == "success"
    assert not any("Incomplete previous session(s)" in s
                   for s in report["next_steps"])
