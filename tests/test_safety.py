"""P0/P1 safety, correctness, and configuration behavior tests."""
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tether.adapters.base import AgentAdapter, SessionInfo
from tether.adapters.mock import MockAdapter
from tether.audit import find_session_dir
from tether.cli import EXIT_FAILED, app
from tether.config import resolve_config
from tether.git_safety import (
    create_checkpoint,
    head_sha,
    list_checkpoint_refs,
    make_file_backup,
    rollback,
)
from tether.mission import MissionError, load_mission
from tether.models import AgentState, TetherConfig
from tether.orchestrator import Orchestrator
from tether.manifest import diff_manifests, snapshot_manifest

runner = CliRunner()


def py_cmd(code: str) -> str:
    return f"{sys.executable} -c '{code}'"


PASS_CMD = py_cmd("import sys; sys.exit(0)")
FAIL_CMD = py_cmd("import sys; sys.exit(1)")


class SpyAdapter(MockAdapter):
    """Mock adapter that records interactions (for proving non-invocation)."""

    def __init__(self, settings=None):
        super().__init__(settings)
        self.calls: list[str] = []

    def start_session(self, project_dir, session_id):
        self.calls.append("start_session")
        return super().start_session(project_dir, session_id)

    def send(self, prompt, session):
        self.calls.append("send")
        return super().send(prompt, session)


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
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)


# ---------------------------------------------------------------- Task 1


def test_dirty_git_aborts_before_adapter(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / "f.txt").write_text("dirty\n")
    adapter = SpyAdapter({"scenario": "success"})
    cfg = TetherConfig(audit_dir=".tether/sessions")
    mp = _mission(tmp_path)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"
    assert adapter.calls == []  # no adapter interaction at all
    assert any("--allow-dirty" in s for s in report["next_steps"])
    assert report["verification_results"] == []
    # no checkpoint ref was created either
    assert list_checkpoint_refs(tmp_path) == []


def test_dirty_git_runs_with_allow_dirty(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / "f.txt").write_text("dirty\n")
    adapter = SpyAdapter({"scenario": "success"})
    cfg = TetherConfig(audit_dir=".tether/sessions")
    mp = _mission(tmp_path)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp), allow_dirty=True)
    assert report["status"] == "success"
    assert "start_session" in adapter.calls


def test_cli_run_dirty_git_exits_nonzero(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / "f.txt").write_text("dirty\n")
    r = runner.invoke(app, ["run", str(_mission(tmp_path)),
                            "--project-dir", str(tmp_path)])
    assert r.exit_code != 0
    assert "--allow-dirty" in r.output


# ---------------------------------------------------------------- Task 2


def test_dry_run_does_not_mutate_target_project(tmp_path):
    # mission file lives inside the repo; commit it so the tree starts clean
    mp = _mission(tmp_path, body=(
        "mission:\n  name: dry\n  goal: g\n"
        "verification:\n  commands: ['touch marker.txt']\nadapter: mock\n"
        "adapters:\n  mock:\n    scenario: success\n"
    ))
    _git_repo(tmp_path)
    base = head_sha(tmp_path)
    cfg = TetherConfig(audit_dir=".tether/sessions", dry_run=True)
    report = Orchestrator(SpyAdapter({"scenario": "success"}), cfg, tmp_path).run(
        load_mission(mp))
    assert report["status"] == "success"
    # no checkpoint refs created
    assert list_checkpoint_refs(tmp_path) == []
    assert head_sha(tmp_path) == base
    # no backup archives
    assert not (tmp_path / ".tether/backups").exists()
    # verification command not executed
    assert not (tmp_path / "marker.txt").exists()
    # audit report still written (documented exception)
    assert (find_session_dir(tmp_path, ".tether/sessions",
                             report["session_id"]) / "report.json").exists()


def test_dry_run_never_calls_adapter(tmp_path):
    adapter = SpyAdapter({"scenario": "success"})
    cfg = TetherConfig(audit_dir=".tether/sessions", dry_run=True)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(_mission(tmp_path)))
    assert report["status"] == "success"
    assert adapter.calls == []


# ---------------------------------------------------------------- Task 3


def test_cli_max_attempts_overrides_mission_unset(tmp_path):
    (tmp_path / "tether.yaml").write_text("max_attempts: 5\n")
    mp = _mission(tmp_path, body=(
        f"mission:\n  name: m\n  goal: g\nverification:\n  commands:\n    - {PASS_CMD}\n"
    ))  # no recovery block -> unset
    mission = load_mission(mp)
    assert mission.recovery.max_attempts is None
    cfg = resolve_config(tmp_path, cli_overrides={"max_attempts": 2})
    assert cfg.max_attempts == 2


def test_mission_explicit_max_attempts_beats_project_config(tmp_path):
    (tmp_path / "tether.yaml").write_text("max_attempts: 5\n")
    mp = _mission(tmp_path, body=(
        f"mission:\n  name: m\n  goal: g\nverification:\n  commands:\n    - {PASS_CMD}\n"
        "recovery:\n  max_attempts: 2\n"
    ))
    mission = load_mission(mp)
    cfg = resolve_config(tmp_path, mission_overrides={"max_attempts": 2})
    assert cfg.max_attempts == 2
    orch_cfg = TetherConfig(audit_dir=".tether/sessions", max_attempts=5)
    orch = Orchestrator(SpyAdapter(), orch_cfg, tmp_path)
    assert orch._effective_max_attempts(mission) == 2


def test_cli_overrides_mission_explicit_max_attempts(tmp_path):
    _mission(tmp_path, body=(
        f"mission:\n  name: m\n  goal: g\nverification:\n  commands:\n    - {PASS_CMD}\n"
        "recovery:\n  max_attempts: 2\n"
    ))
    cfg = resolve_config(tmp_path,
                         mission_overrides={"max_attempts": 2},
                         cli_overrides={"max_attempts": 4})
    assert cfg.max_attempts == 4


def test_project_config_applies_when_mission_absent(tmp_path):
    (tmp_path / "tether.yaml").write_text(
        "max_attempts: 7\nverification_timeout_seconds: 42\n"
        "verification:\n  commands: ['echo hi']\n"
    )
    mission = load_mission(_mission(tmp_path, body=(
        "mission:\n  name: m\n  goal: g\n"
    )))  # nothing set in mission
    cfg = resolve_config(tmp_path)
    orch = Orchestrator(SpyAdapter(), cfg, tmp_path)
    assert orch._effective_max_attempts(mission) == 7
    assert orch._effective_verification_timeout(mission) == 42
    assert orch._effective_verification_commands(mission) == ["echo hi"]


def test_mission_adapters_override_project_adapters_per_key(tmp_path):
    (tmp_path / "tether.yaml").write_text(
        "adapters:\n  mock:\n    scenario: always_fail\n    extra: keepme\n"
    )
    cfg = resolve_config(
        tmp_path,
        mission_overrides={"adapters": {"mock": {"scenario": "success"}}},
    )
    assert cfg.adapters["mock"] == {"scenario": "success", "extra": "keepme"}


def test_orchestrator_uses_effective_values(tmp_path):
    # mission explicit timeout + config commands fallback
    mp = _mission(tmp_path, body=(
        "mission:\n  name: m\n  goal: g\n"
        "verification:\n  timeout_seconds: 30\n"
        "recovery:\n  max_attempts: 1\n"
    ))
    mission = load_mission(mp)
    cfg = TetherConfig(audit_dir=".tether/sessions",
                       verification_timeout_seconds=999, max_attempts=9)
    orch = Orchestrator(SpyAdapter(), cfg, tmp_path)
    assert orch._effective_verification_timeout(mission) == 30
    assert orch._effective_max_attempts(mission) == 1
    assert orch._effective_verification_commands(mission) == []


# ---------------------------------------------------------------- Task 4


def test_tri_state_dry_run_flag(tmp_path):
    (tmp_path / "tether.yaml").write_text("dry_run: true\n")
    mp = _mission(tmp_path)
    # flag unset -> config applies (dry run: adapter never called)
    r = runner.invoke(app, ["run", str(mp), "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    # --no-dry-run overrides config -> adapter runs
    r = runner.invoke(app, ["run", str(mp), "--project-dir", str(tmp_path),
                            "--no-dry-run"])
    assert r.exit_code == 0, r.output
    sid = r.output.split("Session: ")[1].split()[0]
    report = json.loads((find_session_dir(tmp_path, ".tether/sessions", sid) /
                         "report.json").read_text())
    assert report["checkpoint_info"]["is_git_repo"] is False  # backup path taken => real run


def test_tri_state_allow_dirty_flag(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / "f.txt").write_text("dirty\n")
    mp = _mission(tmp_path)
    r = runner.invoke(app, ["run", str(mp), "--project-dir", str(tmp_path)])
    assert r.exit_code != 0
    r = runner.invoke(app, ["run", str(mp), "--project-dir", str(tmp_path),
                            "--allow-dirty"])
    assert r.exit_code == 0, r.output


# ---------------------------------------------------------------- Task 5


def test_rollback_accepts_prefix_and_audit_lookup(tmp_path):
    _git_repo(tmp_path)
    base = head_sha(tmp_path)
    create_checkpoint(tmp_path, "abcdef123456")
    (tmp_path / "f.txt").write_text("changed\n")
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "--", "."], check=True)
    ok, msg = rollback(tmp_path, "abcdef123456")  # exact
    assert ok, msg
    assert head_sha(tmp_path) == base

    create_checkpoint(tmp_path, "123456abcdef")
    ok, msg = rollback(tmp_path, "123456ab")  # ref prefix
    assert ok, msg
    assert head_sha(tmp_path) == base


def test_rollback_via_audit_session_directory(tmp_path):
    _git_repo(tmp_path)
    base = head_sha(tmp_path)
    from tether.audit import AuditTrail
    audit = AuditTrail(tmp_path, ".tether/sessions", "sess", "fedcba654321")
    audit.write_report({"session_id": "fedcba654321"})
    create_checkpoint(tmp_path, "fedcba654321")
    (tmp_path / "f.txt").write_text("changed\n")
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "--", "."], check=True)
    # clean tree so rollback proceeds; use a prefix that only matches via audit
    ok, msg = rollback(tmp_path, "fedcba")
    assert ok, msg
    assert head_sha(tmp_path) == base


def test_rollback_ambiguous_prefix_fails_clearly(tmp_path):
    _git_repo(tmp_path)
    create_checkpoint(tmp_path, "aaaa1111aaaa")
    create_checkpoint(tmp_path, "aaaa2222aaaa")
    ok, msg = rollback(tmp_path, "aaaa")
    assert not ok
    assert "Ambiguous" in msg and "aaaa1111" in msg and "aaaa2222" in msg


def test_find_session_dir_ambiguous_raises(tmp_path):
    from tether.audit import AuditTrail
    AuditTrail(tmp_path, ".tether/sessions", "a", "1111aaaaaaaa")
    AuditTrail(tmp_path, ".tether/sessions", "b", "1111bbbbbbbb")
    with pytest.raises(ValueError, match="Ambiguous"):
        find_session_dir(tmp_path, ".tether/sessions", "1111")


# ---------------------------------------------------------------- Task 6


def test_make_file_backup_unique_entries(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep").mkdir()
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub" / "b.txt").write_text("b")
    (tmp_path / "sub" / "deep" / "c.txt").write_text("c")
    dest = make_file_backup(tmp_path, tmp_path / ".tether/backups", "bk1")
    with tarfile.open(dest) as tar:
        names = tar.getnames()
    assert len(names) == len(set(names)), f"duplicate entries: {names}"
    assert sorted(names) == ["a.txt", "sub/b.txt", "sub/deep/c.txt"]
    assert not any(n.endswith("/") for n in names)


def test_backup_failure_fails_non_git_mission(tmp_path, monkeypatch):
    import tether.git_safety as gs
    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(gs, "make_file_backup", boom)
    import tether.orchestrator as orc
    monkeypatch.setattr(orc, "make_file_backup", boom)
    adapter = SpyAdapter({"scenario": "success"})
    cfg = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(_mission(tmp_path)))
    assert report["status"] == "failed"
    assert adapter.calls == []
    assert any("backup" in s.lower() for s in report["next_steps"])


def test_backup_uses_config_backup_dir(tmp_path):
    adapter = SpyAdapter({"scenario": "success"})
    cfg = TetherConfig(audit_dir=".tether/sessions", backup_dir="custom-backups")
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(_mission(tmp_path)))
    assert report["status"] == "success"
    assert (tmp_path / "custom-backups").exists()


# ---------------------------------------------------------------- Task 7


def test_command_adapter_prompt_via_stdin(tmp_path):
    from tether.adapters.command import CommandAdapter
    reader = [
        sys.executable, "-c",
        "import sys; data = sys.stdin.read(); "
        "print('STDIN:' + data); print('ARGV:' + str(sys.argv[1:]))",
    ]
    adapter = CommandAdapter({"command": reader + ["{prompt}"],
                              "prompt_via_stdin": True})
    session = adapter.start_session(str(tmp_path), "sid1")
    state = adapter.send("secret prompt text", session)
    assert state.status == "completed", state.error
    assert "STDIN:secret prompt text" in state.logs
    assert "'secret prompt text'" not in state.logs  # prompt not leaked into argv
    assert "''" in state.logs  # {prompt} rendered empty in argv


def test_command_adapter_without_stdin_keeps_prompt_in_argv(tmp_path):
    from tether.adapters.command import CommandAdapter
    adapter = CommandAdapter({
        "command": [sys.executable, "-c", "import sys; print(sys.argv[1:])", "{prompt}"],
    })
    session = adapter.start_session(str(tmp_path), "sid2")
    state = adapter.send("hello world", session)
    assert state.status == "completed"
    assert "hello world" in state.logs


# ---------------------------------------------------------------- Task 8


@pytest.mark.parametrize("bad", [
    "mission:\n  name: x\n  goal: y\nrecovery:\n  max_attempts: abc\n",
    "mission:\n  name: x\n  goal: y\nrecovery:\n  max_attempts: 0\n",
    "mission:\n  name: x\n  goal: y\nrecovery:\n  max_attempts: 99\n",
    "mission:\n  name: x\n  goal: y\nverification:\n  timeout_seconds: -5\n",
    "mission:\n  name: x\n  goal: y\nverification: not-a-mapping\n",
    "mission:\n  name: x\n  goal: y\nadapters: not-a-mapping\n",
    "mission:\n  name: x\n  goal: y\nadapters:\n  mock: not-a-mapping\n",
])
def test_invalid_mission_values_raise_mission_error(tmp_path, bad):
    p = tmp_path / "bad.yaml"
    p.write_text(bad)
    try:
        load_mission(p)
    except MissionError as e:
        assert str(e)  # readable, non-empty message
    else:
        pytest.fail("expected MissionError")


def test_validate_mission_cli_no_traceback(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("mission:\n  name: x\n  goal: y\nrecovery:\n  max_attempts: abc\n")
    r = runner.invoke(app, ["validate-mission", str(p)])
    assert r.exit_code == 1
    assert "Traceback" not in r.output
    assert "INVALID" in r.output or "invalid" in r.output.lower()


# ---------------------------------------------------------------- Task 9


def test_manifest_detects_changes_for_non_git(tmp_path):
    (tmp_path / "existing.txt").write_text("v1")
    before = snapshot_manifest(tmp_path)
    (tmp_path / "existing.txt").write_text("v2 longer")
    (tmp_path / "added.txt").write_text("new")
    (tmp_path / ".tether").mkdir()
    (tmp_path / ".tether" / "junk.txt").write_text("x")
    after = snapshot_manifest(tmp_path)
    diff = diff_manifests(before, after)
    assert diff["added"] == ["added.txt"]
    assert diff["modified"] == ["existing.txt"]
    assert ".tether/junk.txt" not in diff["added"]
    assert diff["deleted"] == []


def test_non_git_report_includes_changed_files(tmp_path):
    (tmp_path / "seed.txt").write_text("seed")

    class TouchingAdapter(AgentAdapter):
        name = "touching"
        verified = True

        def __init__(self):
            super().__init__({})
            self.session = None

        def is_available(self):
            return True, ""

        def start_session(self, project_dir, session_id):
            self.session = SessionInfo(session_id=session_id, project_dir=project_dir)
            return self.session

        def send(self, prompt, session):
            (tmp_path / "created-by-agent.txt").write_text("hi")
            return AgentState(status="completed", logs="ok")

        def cancel(self, session):
            pass

    cfg = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(TouchingAdapter(), cfg, tmp_path).run(
        load_mission(_mission(tmp_path)))
    assert report["status"] == "success"
    assert "created-by-agent.txt" in report["changed_files"]
    assert "seed.txt" not in report["changed_files"]


# ---------------------------------------------------------------- Task 10


def test_resolved_config_redacts_secrets(tmp_path):
    adapter = SpyAdapter({"scenario": "success"})
    cfg = TetherConfig(
        audit_dir=".tether/sessions",
        adapters={"command": {
            "command": ["agent"],
            "env": {"API_KEY": "super-secret", "HOME": "/Users/x"},
            "api_token": "tok",
        }},
    )
    from tether.audit import find_session_dir
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(_mission(tmp_path)))
    saved = json.loads((find_session_dir(tmp_path, ".tether/sessions",
                                         report["session_id"]) /
                        "resolved-config.json").read_text())
    env = saved["adapters"]["command"]["env"]
    # every env value is redacted (safer: can't tell secrets apart by name)
    assert env["API_KEY"] == "[REDACTED]"
    assert env["HOME"] == "[REDACTED]"
    assert saved["adapters"]["command"]["api_token"] == "[REDACTED]"
    # live config untouched
    assert cfg.adapters["command"]["env"]["API_KEY"] == "super-secret"


# ---------------------------------------------------------------- Task 11


def test_sessions_list_show_diff_logs(tmp_path):
    mp = _mission(tmp_path, body=(
        "mission:\n  name: sess-cmds\n  goal: g\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\nadapter: mock\n"
        "adapters:\n  mock:\n    scenario: success\n"
    ))
    r = runner.invoke(app, ["run", str(mp), "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    sid = r.output.split("Session: ")[1].split()[0]

    r = runner.invoke(app, ["sessions", "list", "--project-dir", str(tmp_path)])
    assert r.exit_code == 0 and "sess-cmds" in r.output and "success" in r.output

    r = runner.invoke(app, ["sessions", "show", sid[:6], "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert f"Session:  {sid}" in r.output
    assert "sess-cmds" in r.output

    r = runner.invoke(app, ["diff", sid, "--project-dir", str(tmp_path)])
    assert r.exit_code == 0

    r = runner.invoke(app, ["logs", sid, "--project-dir", str(tmp_path)])
    assert r.exit_code == 0 and "session_start" in r.output

    r = runner.invoke(app, ["sessions", "show", "nope", "--project-dir", str(tmp_path)])
    assert r.exit_code == 1


# ------------------------------------------------- rollback usefulness (B3)


def _git_repo_with_session(tmp_path):
    _git_repo(tmp_path)
    from tether.audit import AuditTrail
    audit = AuditTrail(tmp_path, ".tether/sessions", "sess", "aaaa1111aaaa")
    return audit


def test_rollback_refuses_on_untracked_files_with_manual_steps(tmp_path):
    _git_repo_with_session(tmp_path)
    base = head_sha(tmp_path)
    create_checkpoint(tmp_path, "aaaa1111aaaa")
    # agent adds an untracked file -> tree is "dirty"
    (tmp_path / "agent-added.txt").write_text("new\n")
    ok, msg = rollback(tmp_path, "aaaa1111aaaa")
    assert not ok
    assert "Manual steps" in msg
    assert "agent-added.txt" in msg  # precise reporting of the actual files
    assert head_sha(tmp_path) == base


def test_rollback_clean_removes_only_session_added_untracked(tmp_path):
    audit = _git_repo_with_session(tmp_path)
    base = head_sha(tmp_path)
    create_checkpoint(tmp_path, "aaaa1111aaaa")
    # pre-existing untracked user file (NOT attributable to the session)
    (tmp_path / "user-notes.txt").write_text("keep me\n")
    # session adds a tracked modification and an untracked file
    (tmp_path / "f.txt").write_text("changed\n")
    (tmp_path / "agent-added.txt").write_text("new\n")
    audit.write_report({"session_id": "aaaa1111aaaa",
                        "changed_files": ["f.txt", "agent-added.txt"]})
    ok, msg = rollback(tmp_path, "aaaa1111aaaa", clean=True)
    assert ok, msg
    assert head_sha(tmp_path) == base
    assert (tmp_path / "f.txt").read_text() == "hello\n"
    assert not (tmp_path / "agent-added.txt").exists()
    assert (tmp_path / "user-notes.txt").read_text() == "keep me\n"


def test_rollback_clean_without_report_leaves_untracked_alone(tmp_path):
    _git_repo_with_session(tmp_path)
    base = head_sha(tmp_path)
    create_checkpoint(tmp_path, "aaaa1111aaaa")
    (tmp_path / "agent-added.txt").write_text("new\n")
    # no report.json changed_files -> nothing attributable to remove; the
    # tracked rollback still happens but the untracked file is left alone
    ok, msg = rollback(tmp_path, "aaaa1111aaaa", clean=True)
    assert ok, msg
    assert head_sha(tmp_path) == base
    assert (tmp_path / "agent-added.txt").exists()
    assert "agent-added.txt" in msg  # surfaced for manual cleanup


# --------------------------------------------- dogfood-08: tether doctor


def test_doctor_clean_project_passes_and_creates_dirs(tmp_path):
    r = runner.invoke(app, ["doctor", "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    out = r.output
    assert "minimum required: 3.11" in out
    assert "[PASS]" in out
    # audit/backup dir probes create the directories
    assert (tmp_path / ".tether/sessions").is_dir()
    assert (tmp_path / ".tether/backups").is_dir()
    assert not (tmp_path / ".tether" / "tether.lock").exists()
    assert "Verdict: OK" in out
    assert "[FAIL]" not in out


def test_doctor_dirty_tree_is_advisory_only(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / "f.txt").write_text("dirty\n")
    r = runner.invoke(app, ["doctor", "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output  # advisory findings never fail doctor
    assert "DIRTY" in r.output or "dirty" in r.output
    assert "allow-dirty" in r.output
    assert "Verdict: OK" in r.output


def test_doctor_reports_invalid_config_and_stale_lock_as_warnings(tmp_path):
    import json
    import os
    import time as _time
    (tmp_path / "tether.yaml").write_text("max_attempts: [broken\n")
    lock = tmp_path / ".tether" / "tether.lock"
    lock.parent.mkdir(exist_ok=True)
    lock.write_text(json.dumps({
        "session_id": "deadsess0001",
        "pid": 2 ** 22,  # nobody owns this PID
        "created_at": _time.time(),
    }) + "\n", encoding="utf-8")
    old = _time.time() - 13 * 3600
    os.utime(lock, (old, old))
    r = runner.invoke(app, ["doctor", "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "INVALID" in r.output
    assert "stale writer lock" in r.output.lower()
    assert "safe to remove" in r.output.lower()
    assert "Verdict: OK" in r.output  # both findings are advisory


def test_doctor_live_lock_reported_but_not_fatal(tmp_path):
    import os
    lock = tmp_path / ".tether" / "tether.lock"
    lock.parent.mkdir(exist_ok=True)
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")  # legacy live-ish
    r = runner.invoke(app, ["doctor", "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "held by session" in r.output


def test_doctor_fails_when_git_missing(tmp_path, monkeypatch):
    import shutil as _shutil
    real_which = _shutil.which
    monkeypatch.setattr(_shutil, "which",
                        lambda name, *a, **k: None if name == "git"
                        else real_which(name, *a, **k))
    r = runner.invoke(app, ["doctor", "--project-dir", str(tmp_path)])
    assert r.exit_code != 0
    assert "[FAIL] git" in r.output
    assert "Verdict: FAILED" in r.output


def test_doctor_fails_when_directories_not_writable(tmp_path):
    # A regular file where .tether must be created makes both directory
    # probes fail -> critical.
    (tmp_path / ".tether").write_text("not a directory\n")
    r = runner.invoke(app, ["doctor", "--project-dir", str(tmp_path)])
    assert r.exit_code != 0
    assert "[FAIL]" in r.output
    assert "audit dir" in r.output
    assert "Verdict: FAILED" in r.output


# ------------------------------------- dogfood-08: rollback --dry-run preview


def test_cli_rollback_dry_run_prints_plan_without_touching_anything(tmp_path):
    audit = _git_repo_with_session(tmp_path)  # session aaaa1111aaaa
    base = head_sha(tmp_path)
    create_checkpoint(tmp_path, "aaaa1111aaaa")
    (tmp_path / "f.txt").write_text("modified by agent\n")   # tracked change
    (tmp_path / "agent-added.txt").write_text("session\n")   # would be deleted
    (tmp_path / "user-notes.txt").write_text("precious\n")   # preserved
    audit.write_report({"session_id": "aaaa1111aaaa",
                        "changed_files": ["f.txt", "agent-added.txt",
                                          "user-notes.txt"]})
    r = runner.invoke(app, ["rollback", "aaaa", "--dry-run", "--clean",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    out = r.output
    assert "dry-run" in out.lower()
    assert "refs/tether/checkpoint/aaaa1111aaaa" in out
    assert base in out                       # target sha is shown
    assert "DIRTY" in out
    assert "- f.txt" in out                  # would be reset
    assert "- agent-added.txt" in out        # --clean would delete
    assert "- user-notes.txt" in out         # pre-existing, preserved
    # nothing was actually touched:
    assert head_sha(tmp_path) == base
    assert list_checkpoint_refs(tmp_path) != []  # checkpoint still there
    assert (tmp_path / "f.txt").read_text() == "modified by agent\n"
    assert (tmp_path / "agent-added.txt").exists()
    assert (tmp_path / "user-notes.txt").read_text() == "precious\n"


def test_cli_rollback_dry_run_default_refusal_noted(tmp_path):
    _git_repo_with_session(tmp_path)
    create_checkpoint(tmp_path, "aaaa1111aaaa")
    (tmp_path / "agent-added.txt").write_text("new\n")
    r = runner.invoke(app, ["rollback", "aaaa1111aaaa", "--dry-run",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "REFUSE" in r.output  # default behavior on apply is documented
    assert "--clean" in r.output
    assert (tmp_path / "agent-added.txt").exists()  # untouched


def test_cli_rollback_dry_run_clean_tree(tmp_path):
    _git_repo_with_session(tmp_path)
    base = head_sha(tmp_path)
    create_checkpoint(tmp_path, "aaaa1111aaaa")
    r = runner.invoke(app, ["rollback", "aaaa1111aaaa", "--dry-run",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "clean" in r.output
    assert head_sha(tmp_path) == base


def test_cli_rollback_dry_run_non_git_backup_plan(tmp_path):
    from tether.audit import AuditTrail
    (tmp_path / "data.txt").write_text("v1")
    audit = AuditTrail(tmp_path, ".tether/sessions", "sess", "cccc9999cccc")
    audit.write_report({"session_id": "cccc9999cccc",
                        "changed_files": ["data.txt"]})
    make_file_backup(tmp_path, tmp_path / ".tether/backups", "cccc9999cccc")
    r = runner.invoke(app, ["rollback", "cccc9", "--dry-run",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    out = r.output
    assert ".tether/backups/cccc9999cccc.tar.gz" in out
    assert "verified" in out                 # checksum sidecar status shown
    assert "- data.txt" in out               # restore plan lists the file
    assert (tmp_path / "data.txt").read_text() == "v1"  # untouched


def test_cli_rollback_dry_run_unknown_session_fails(tmp_path):
    _git_repo(tmp_path)
    r = runner.invoke(app, ["rollback", "no-such-session", "--dry-run",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code != 0


# --------------------------------------------- non-git backup restore (B4)


def test_restore_from_backup_restores_non_git_project(tmp_path):
    from tether.audit import AuditTrail
    from tether.git_safety import restore_from_backup
    (tmp_path / "data.txt").write_text("v1")
    AuditTrail(tmp_path, ".tether/sessions", "sess", "bbbb2222bbbb")
    dest = make_file_backup(tmp_path, tmp_path / ".tether/backups", "bbbb2222bbbb")
    assert dest
    # simulate the agent changing a file and creating a new one
    (tmp_path / "data.txt").write_text("clobbered by agent")
    (tmp_path / "agent-added.txt").write_text("extra\n")
    ok, msg = restore_from_backup(tmp_path, "bbbb2222bbbb")
    assert ok, msg
    assert (tmp_path / "data.txt").read_text() == "v1"
    # files created after the backup are kept and reported
    assert (tmp_path / "agent-added.txt").exists()
    assert "agent-added.txt" in msg


def test_cli_rollback_non_git_restores_backup(tmp_path):
    from tether.audit import AuditTrail
    (tmp_path / "data.txt").write_text("v1")
    AuditTrail(tmp_path, ".tether/sessions", "sess", "cccc3333cccc")
    make_file_backup(tmp_path, tmp_path / ".tether/backups", "cccc3333cccc")
    (tmp_path / "data.txt").write_text("clobbered")
    r = runner.invoke(app, ["rollback", "cccc3333cccc",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "data.txt").read_text() == "v1"


def test_restore_from_backup_accepts_short_prefix(tmp_path):
    # Non-git session: only a short prefix is known; resolution must go
    # through the audit session directory's report.json session_id.
    from tether.audit import AuditTrail
    from tether.git_safety import restore_from_backup
    (tmp_path / "data.txt").write_text("v1")
    audit = AuditTrail(tmp_path, ".tether/sessions", "sess", "dddd4444dddd")
    audit.write_report({"session_id": "dddd4444dddd"})
    make_file_backup(tmp_path, tmp_path / ".tether/backups", "dddd4444dddd")
    (tmp_path / "data.txt").write_text("clobbered by agent")
    ok, msg = restore_from_backup(tmp_path, "dddd")  # short prefix
    assert ok, msg
    assert (tmp_path / "data.txt").read_text() == "v1"


# ------------------------------ dogfood-08: backup checksum verification


def test_backup_writes_sha256_sidecar_and_restores(tmp_path):
    from tether.audit import AuditTrail
    from tether.git_safety import backup_checksum_path, verify_backup_checksum
    (tmp_path / "data.txt").write_text("v1")
    AuditTrail(tmp_path, ".tether/sessions", "sess", "ffff6666ffff")
    dest = Path(make_file_backup(tmp_path, tmp_path / ".tether/backups",
                                 "ffff6666ffff"))
    sidecar = backup_checksum_path(dest)
    assert sidecar.exists()
    import hashlib
    expected = hashlib.sha256(dest.read_bytes()).hexdigest()
    assert sidecar.read_text().strip().split()[0] == expected
    ok, msg = verify_backup_checksum(dest)
    assert ok, msg
    # end-to-end: verification runs inside restore and succeeds
    (tmp_path / "data.txt").write_text("clobbered")
    from tether.git_safety import restore_from_backup
    ok, msg = restore_from_backup(tmp_path, "ffff6666ffff")
    assert ok, msg
    assert (tmp_path / "data.txt").read_text() == "v1"


def test_corrupted_archive_is_detected_and_restore_refused(tmp_path):
    from tether.audit import AuditTrail
    from tether.git_safety import restore_from_backup
    (tmp_path / "data.txt").write_text("v1")
    AuditTrail(tmp_path, ".tether/sessions", "sess", "aaaa7777aaaa")
    dest = Path(make_file_backup(tmp_path, tmp_path / ".tether/backups",
                                 "aaaa7777aaaa"))
    (tmp_path / "data.txt").write_text("clobbered by agent")
    raw = bytearray(dest.read_bytes())
    raw[-10] ^= 0xFF  # flip bits near the end of the gzip stream
    dest.write_bytes(bytes(raw))
    ok, msg = restore_from_backup(tmp_path, "aaaa7777aaaa")
    assert not ok
    assert "sha256" in msg.lower() or "checksum" in msg.lower()
    assert "refus" in msg.lower()
    # nothing was restored
    assert (tmp_path / "data.txt").read_text() == "clobbered by agent"


def test_missing_sidecar_refuses_restore(tmp_path):
    from tether.audit import AuditTrail
    from tether.git_safety import backup_checksum_path, restore_from_backup
    (tmp_path / "data.txt").write_text("v1")
    AuditTrail(tmp_path, ".tether/sessions", "sess", "bbbb8888bbbb")
    dest = Path(make_file_backup(tmp_path, tmp_path / ".tether/backups",
                                 "bbbb8888bbbb"))
    backup_checksum_path(dest).unlink()
    (tmp_path / "data.txt").write_text("clobbered by agent")
    ok, msg = restore_from_backup(tmp_path, "bbbb8888bbbb")
    assert not ok
    assert "sidecar" in msg.lower()
    assert (tmp_path / "data.txt").read_text() == "clobbered by agent"


def test_archive_and_sidecar_excluded_from_own_backup(tmp_path):
    # A custom backup dir inside the project is NOT covered by the default
    # .tether exclusion; the archive/sidecar/temp must still never appear in
    # the backup contents themselves.
    (tmp_path / "backups").mkdir()
    payload = b"x" * (256 * 1024)  # big enough that self-inclusion would show
    for i in range(4):
        (tmp_path / f"blob-{i}.bin").write_bytes(payload)
    dest = Path(make_file_backup(tmp_path, tmp_path / "backups", "selfexcl01"))
    with tarfile.open(dest) as tar:
        names = tar.getnames()
    assert dest.name not in names
    assert (dest.name + ".sha256") not in names
    assert not any(n.startswith(f".{dest.name}.") for n in names)  # temp files
    assert sorted(n for n in names if n.startswith("blob-")) == \
        [f"blob-{i}.bin" for i in range(4)]
    # sidecar exists next to the archive but is not an archive member
    assert Path(str(dest) + ".sha256").exists()


def test_crashed_backup_leaves_no_partial_archive(tmp_path):
    # A failure while writing the temp file must leave neither a truncated
    # archive at the destination nor stray temp files behind.
    import os as _os
    from unittest import mock

    import tether.git_safety as gs

    def boom(*a, **k):
        raise OSError("simulated crash before publish")

    (tmp_path / "data.txt").write_text("v1")
    backup_root = tmp_path / ".tether/backups"
    with mock.patch.object(_os, "replace", boom), \
            pytest.raises(RuntimeError, match="Failed to create file backup"):
        gs.make_file_backup(tmp_path, backup_root, "crashy000001")
    dest = backup_root / "crashy000001.tar.gz"
    assert not dest.exists()
    assert not gs.backup_checksum_path(dest).exists()
    assert list(backup_root.iterdir()) == []  # no truncated archives/temp files


def test_cli_rollback_non_git_restores_backup_from_prefix(tmp_path):
    from tether.audit import AuditTrail
    (tmp_path / "data.txt").write_text("v1")
    audit = AuditTrail(tmp_path, ".tether/sessions", "sess", "eeee5555eeee")
    audit.write_report({"session_id": "eeee5555eeee"})
    make_file_backup(tmp_path, tmp_path / ".tether/backups", "eeee5555eeee")
    (tmp_path / "data.txt").write_text("clobbered")
    r = runner.invoke(app, ["rollback", "eeee5", "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "data.txt").read_text() == "v1"


# ------------------------------------------------ dogfood-06: write sandbox


class _WritingAdapter(AgentAdapter):
    """Creates one file (relative to project dir) on the execute send."""

    name = "writing"
    verified = True

    def __init__(self, relpath):
        super().__init__({})
        self.relpath = relpath
        self._planned = False

    def is_available(self):
        return True, ""

    def start_session(self, project_dir, session_id):
        self.project_dir = project_dir
        return SessionInfo(session_id=session_id, project_dir=project_dir)

    def send(self, prompt, session):
        if not self._planned:
            self._planned = True
            return AgentState(status="completed", logs="plan")
        target = Path(self.project_dir) / self.relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("agent was here")
        return AgentState(status="completed", logs="done")

    def cancel(self, session):
        pass


def _committed_mission(tmp_path, body=None, name="m.yaml"):
    """Write a mission file and commit everything so the tree starts clean."""
    mp = _mission(tmp_path, body=body, name=name)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "mission"],
                   check=True)
    return mp


def test_sandbox_forbidden_path_fails_and_skips_verification(tmp_path):
    _git_repo(tmp_path)
    mp = _committed_mission(tmp_path, body=(
        "mission:\n  name: sbx-f\n  goal: g\n"
        "forbidden_paths:\n  - '*.secret'\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\nadapter: mock\n"
    ))
    adapter = _WritingAdapter("config.secret")
    cfg = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"
    assert {"path": "config.secret",
            "rule": "forbidden_paths: *.secret"} in report["sandbox_violations"]
    assert report["verification_results"] == []  # verification skipped entirely
    assert any("config.secret" in s for s in report["next_steps"])
    assert any("rollback" in s.lower() for s in report["next_steps"])


def test_sandbox_allowed_path_rejects_outside_writes(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / "src").mkdir()
    mp = _committed_mission(tmp_path, body=(
        "mission:\n  name: sbx-a\n  goal: g\n"
        "allowed_paths:\n  - 'src/**'\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\nadapter: mock\n"
    ))
    adapter = _WritingAdapter("README.md")  # outside allowed globs
    cfg = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"
    assert {"path": "README.md",
            "rule": "not matched by allowed_paths"} in report["sandbox_violations"]
    assert report["verification_results"] == []


def test_no_sandbox_fields_behavior_unchanged(tmp_path):
    _git_repo(tmp_path)
    mp = _committed_mission(tmp_path)
    adapter = _WritingAdapter("unrestricted.txt")
    cfg = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "success"
    assert report["sandbox_violations"] == []
    assert "unrestricted.txt" in report["changed_files"]


def test_sandbox_allowed_path_permits_matching_writes(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / "src").mkdir()
    mp = _committed_mission(tmp_path, body=(
        "mission:\n  name: sbx-ok\n  goal: g\n"
        "allowed_paths:\n  - 'src/**'\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\nadapter: mock\n"
    ))
    adapter = _WritingAdapter("src/ok.txt")  # inside allowed globs
    cfg = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "success"
    assert report["sandbox_violations"] == []


@pytest.mark.parametrize("bad", [
    "mission:\n  name: x\n  goal: y\nforbidden_paths: not-a-list\n",
    "mission:\n  name: x\n  goal: y\nallowed_paths: ['a', 3]\n",
])
def test_invalid_sandbox_fields_raise_mission_error(tmp_path, bad):
    p = tmp_path / "bad.yaml"
    p.write_text(bad)
    with pytest.raises(MissionError):
        load_mission(p)


# --------------------------------------------- dogfood-06: audit redaction


def _run_with_audit(tmp_path, cfg_kwargs):
    filler = "F" * 130
    secret_line = filler[:60] + "SECRET-TOKEN" + filler[72:]
    mp = _mission(tmp_path, body=(
        "mission:\n  name: redact\n  goal: g\n"
        f"context:\n  - '{secret_line}'\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\nadapter: mock\n"
    ))
    cfg = TetherConfig(audit_dir=".tether/sessions", **cfg_kwargs)
    report = Orchestrator(SpyAdapter({"scenario": "success"}), cfg, tmp_path).run(
        load_mission(mp))
    assert report["status"] == "success"
    return find_session_dir(tmp_path, ".tether/sessions", report["session_id"])


def test_redact_prompts_false_stores_full_content(tmp_path):
    session = _run_with_audit(tmp_path, {})
    prompt_texts = "".join(
        p.read_text(encoding="utf-8")
        for p in sorted((session / "prompts").glob("*.txt"))
    )
    assert "SECRET-TOKEN" in prompt_texts
    assert "[REDACTED" not in prompt_texts


def test_redact_prompts_true_stores_hash_and_excerpt_only(tmp_path):
    session = _run_with_audit(tmp_path, {"redact_prompts": True})
    prompts = sorted((session / "prompts").glob("*.txt"))
    assert len(prompts) == 2  # plan + execute
    for p in prompts:
        text = p.read_text(encoding="utf-8")
        assert "[REDACTED sha256=" in text
        assert "len=" in text and "head=" in text and "tail=" in text
        assert "SECRET-TOKEN" not in text
    response_texts = "".join(
        p.read_text(encoding="utf-8")
        for p in sorted((session / "responses").glob("*.json"))
    )
    assert "SECRET-TOKEN" not in response_texts


def test_save_response_redaction(tmp_path):
    from tether.audit import AuditTrail
    state = AgentState(
        status="failed",
        logs="X" * 300 + "SECRETDATA" + "Y" * 300,
        error="E" * 300 + "SECRETDATA" + "F" * 300,
    ).model_dump()
    off = AuditTrail(tmp_path, ".tether/sessions", "a", "111111111111")
    on = AuditTrail(tmp_path, ".tether/sessions", "b", "222222222222",
                    redact_prompts=True)
    plain = off.save_response("r", state).read_text(encoding="utf-8")
    redacted = on.save_response("r", state).read_text(encoding="utf-8")
    assert plain.count("SECRETDATA") == 2  # existing behavior unchanged
    assert "SECRETDATA" not in redacted
    assert redacted.count("[REDACTED sha256=") == 2  # logs + error
    # caller's dict is not mutated by redaction
    assert "SECRETDATA" in state["logs"]


def test_redact_body_shape():
    import hashlib
    from tether.audit import redact_body
    body = "A" * 500
    out = redact_body(body)
    digest = hashlib.sha256(body.encode()).hexdigest()
    assert f"sha256={digest}" in out
    assert "len=500" in out
    assert repr("A" * 64) in out  # head/tail excerpts retained


# ------------------------------------------ dogfood-06: content-hash manifest


def test_manifest_detects_same_length_content_change(tmp_path):
    import os
    (tmp_path / "data.bin").write_text("v1!")
    before = snapshot_manifest(tmp_path)
    # small files are fingerprinted by content, not mtime
    assert isinstance(before["data.bin"][1], str)
    assert len(before["data.bin"][1]) == 64  # sha256 hex digest
    stat = (tmp_path / "data.bin").stat()
    (tmp_path / "data.bin").write_text("v2!")  # same length, different content
    os.utime(tmp_path / "data.bin", (stat.st_atime, stat.st_mtime))  # pin mtime
    after = snapshot_manifest(tmp_path)
    assert after["data.bin"][0] == before["data.bin"][0]  # same size...
    assert diff_manifests(before, after)["modified"] == ["data.bin"]  # ...but new hash


def test_manifest_large_file_falls_back_to_size_mtime(tmp_path, monkeypatch):
    import os as _os
    import tether.manifest as manifest_mod
    monkeypatch.setattr(manifest_mod, "HASH_SIZE_LIMIT", 4)
    big = tmp_path / "big.bin"
    big.write_text("12345678")  # above the (lowered) limit

    # same content, same size, shifted mtime -> detected via mtime fallback
    before = snapshot_manifest(tmp_path)
    assert isinstance(before["big.bin"][1], int)  # mtime_ns, not sha256 hex
    stat = big.stat()
    _os.utime(big, (stat.st_atime - 10, stat.st_mtime - 10))
    mid = snapshot_manifest(tmp_path)
    assert mid["big.bin"][1] < before["big.bin"][1]
    assert diff_manifests(before, mid)["modified"] == ["big.bin"]

    # identical size AND mtime -> NOT modified even if bytes differ (fallback
    # is best-effort by design)
    _os.utime(big, (stat.st_atime, stat.st_mtime))
    after_same_stat = snapshot_manifest(tmp_path)
    big.write_text("87654321")  # different content, same length
    _os.utime(big, (stat.st_atime, stat.st_mtime))
    after_diff_bytes = snapshot_manifest(tmp_path)
    assert after_diff_bytes["big.bin"] == after_same_stat["big.bin"]
    assert diff_manifests(after_same_stat, after_diff_bytes)["modified"] == []

    # small files still hash content even when the limit is lowered
    (tmp_path / "small.txt").write_text("aaaa")
    s_before = snapshot_manifest(tmp_path)
    (tmp_path / "small.txt").write_text("bbbb")  # same length
    assert diff_manifests(s_before, snapshot_manifest(tmp_path))["modified"] == \
        ["small.txt"]


# ----------------------------------- dogfood-06: writer-lock stale timeout


def test_writer_lock_stale_seconds_configurable(tmp_path):
    import os
    import time as _time

    def _run():
        mp = _mission(tmp_path)
        cfg = TetherConfig(audit_dir=".tether/sessions",
                           writer_lock_stale_seconds=1)
        adapter = SpyAdapter({"scenario": "success"})
        report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
        return report, adapter.calls

    # stale lock (mtime 2s ago, older than the configured 1s) is taken over
    lock = tmp_path / ".tether" / "tether.lock"
    lock.parent.mkdir(exist_ok=True)
    lock.write_text("oldsess0000\n", encoding="utf-8")
    old = _time.time() - 2
    os.utime(lock, (old, old))
    report, calls = _run()
    assert report["status"] == "success"
    assert "start_session" in calls
    assert not lock.exists()  # taken over and released again

    # fresh lock (mtime now) still blocks even with the short timeout,
    # and fails fast before any adapter interaction
    lock.write_text("freshsess9999\n", encoding="utf-8")
    report2, calls2 = _run()
    assert report2["status"] == "failed"
    assert calls2 == []
    assert any("freshsess9999" in s for s in report2["next_steps"])
    assert lock.read_text().strip() == "freshsess9999"  # not clobbered


# -------------------------- dogfood-07: forensic capture + auto rollback


class _MultiWriterAdapter(AgentAdapter):
    """Writes several files (text or binary) on the execute send."""

    name = "multiwriter"
    verified = True

    def __init__(self, files, fail_execute=False):
        super().__init__({})
        self.files = dict(files)  # relpath -> str | bytes
        self.fail_execute = fail_execute
        self._planned = False

    def is_available(self):
        return True, ""

    def start_session(self, project_dir, session_id):
        self.project_dir = project_dir
        return SessionInfo(session_id=session_id, project_dir=project_dir)

    def send(self, prompt, session):
        if not self._planned:
            self._planned = True
            return AgentState(status="completed", logs="plan")
        for rel, content in self.files.items():
            target = Path(self.project_dir) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content)
        if self.fail_execute:
            return AgentState(status="failed", logs="boom")
        return AgentState(status="completed", logs="done")

    def cancel(self, session):
        pass


def test_git_session_captures_patch_diff_and_untracked(tmp_path):
    _git_repo(tmp_path)
    # a committed binary file so --binary capture can be proven later
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02original")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "binary"],
                   check=True)
    mp = _committed_mission(tmp_path, body=(
        "mission:\n  name: forensics\n  goal: g\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\nadapter: mock\n"
    ))
    adapter = _MultiWriterAdapter({
        "f.txt": "changed by agent\n",               # tracked modification
        "blob.bin": b"\x00\xff\x7fBINARY",           # tracked binary change
        "agent-added.txt": "new file\n",             # untracked (not in diff)
    })
    cfg = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "success"
    session = find_session_dir(tmp_path, ".tether/sessions", report["session_id"])
    patch = session / "patch.diff"
    assert patch.exists()
    raw = patch.read_bytes()
    assert b"diff --git a/f.txt" in raw
    assert b"GIT binary patch" in raw  # binary content captured via --binary
    untracked = (session / "untracked.txt").read_text(encoding="utf-8")
    assert "agent-added.txt" in untracked.splitlines()  # diff misses untracked
    assert ".tether/" not in untracked  # Tether's own files never listed


def test_non_git_session_captures_manifest_diff_json(tmp_path):
    (tmp_path / "seed.txt").write_text("v1")
    adapter = _MultiWriterAdapter({
        "seed.txt": "v2",
        "created-by-agent.txt": "hi",
    })
    cfg = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(_mission(tmp_path)))
    assert report["status"] == "success"
    session = find_session_dir(tmp_path, ".tether/sessions", report["session_id"])
    path = session / "manifest_diff.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["added"] == ["created-by-agent.txt"]
    assert data["modified"] == ["seed.txt"]
    assert data["deleted"] == []
    # before/after fingerprints are recorded and differ for modified files
    assert data["before"]["seed.txt"] != data["after"]["seed.txt"]
    assert "created-by-agent.txt" not in data["before"]
    assert isinstance(data["after"]["created-by-agent.txt"], list)


def test_cli_diff_patch_flag_prints_saved_artifacts(tmp_path):
    # non-git session -> manifest_diff.json is printable via --patch.
    # A real CommandAdapter writes the files during execution so the
    # change capture (which happens pre-verification) sees them.
    (tmp_path / "seed.txt").write_text("v1")
    agent_code = (
        'open("seed.txt", "w").write("v2"); '
        'open("created-by-agent.txt", "w").write("hi")'
    )
    mp = _mission(tmp_path, body=(
        "mission:\n  name: patchcli\n  goal: g\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\n"
        "adapter: command\n"
        "adapters:\n  command:\n    command:\n"
        f"      - {json.dumps(sys.executable)}\n"
        "      - '-c'\n"
        f"      - '{agent_code}'\n"
    ))
    r = runner.invoke(app, ["run", str(mp), "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    sid = r.output.split("Session: ")[1].split()[0]
    # default behavior unchanged: lists changed files
    r = runner.invoke(app, ["diff", sid, "--project-dir", str(tmp_path)])
    assert r.exit_code == 0
    assert "seed.txt" in r.output.splitlines()
    # --patch prints the saved artifact instead
    r = runner.invoke(app, ["diff", sid, "--patch", "--project-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert '"added"' in r.output and "created-by-agent.txt" in r.output

    # git session -> --patch prints the actual git patch text.
    # A real CommandAdapter modifies the TRACKED file during execution so the
    # change capture (which happens pre-verification) sees it.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "a.txt").write_text("v1\n")
    mp = _mission(repo, name="g.yaml", body=(
        "mission:\n  name: gitpatch\n  goal: g\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\n"
        "adapter: command\n"
        "adapters:\n  command:\n    command:\n"
        f"      - {json.dumps(sys.executable)}\n"
        "      - '-c'\n"
        "      - 'open(\"a.txt\", \"w\").write(\"v2\")'\n"
    ))
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    r = runner.invoke(app, ["run", str(mp), "--project-dir", str(repo)])
    assert r.exit_code == 0, r.output
    gid = r.output.split("Session: ")[1].split()[0]
    r = runner.invoke(app, ["diff", gid, "--project-dir", str(repo)])
    assert r.exit_code == 0
    assert "a.txt" in r.output.splitlines()  # default behavior unchanged
    r = runner.invoke(app, ["diff", gid, "--patch", "--project-dir", str(repo)])
    assert r.exit_code == 0, r.output
    assert "diff --git a/a.txt" in r.output


def test_cli_diff_patch_missing_artifact_exits_nonzero(tmp_path):
    # a session directory without change artifacts (e.g. from older versions)
    audit = _git_repo_with_session(tmp_path)
    audit.write_report({"session_id": "aaaa1111aaaa"})
    r = runner.invoke(app, ["diff", "aaaa1111aaaa", "--patch",
                            "--project-dir", str(tmp_path)])
    assert r.exit_code != 0
    assert "No change artifact" in r.output


# -------------------------- dogfood-07: opt-in automatic rollback


def _failing_git_mission(tmp_path, name="arb.yaml"):
    """Committed git mission whose verification clobbers f.txt then fails."""
    clobber_fail = py_cmd(
        'import sys; open("f.txt", "w").write("clobbered\\n"); sys.exit(1)')
    mp = _committed_mission(tmp_path, name=name, body=(
        "mission:\n  name: arb\n  goal: g\n"
        "verification:\n  commands:\n"
        f"    - {clobber_fail}\nadapter: mock\n"
    ))
    return mp


def test_auto_rollback_restores_failed_git_session(tmp_path):
    _git_repo(tmp_path)
    mp = _failing_git_mission(tmp_path)
    base = head_sha(tmp_path)  # HEAD after the mission commit = checkpoint target
    adapter = _MultiWriterAdapter(
        {"f.txt": "clobbered by agent\n", "agent-added.txt": "extra\n"},
        fail_execute=True,
    )
    cfg = TetherConfig(audit_dir=".tether/sessions", auto_rollback=True,
                       max_attempts=1)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"
    ar = report["auto_rollback"]
    assert ar["attempted"] is True
    assert ar["ok"] is True
    assert ar["message"]
    # tracked modification reverted and session-created untracked file removed
    assert (tmp_path / "f.txt").read_text() == "hello\n"
    assert not (tmp_path / "agent-added.txt").exists()
    assert head_sha(tmp_path) == base
    # persisted report carries the same result plus manual guidance
    saved = json.loads((find_session_dir(tmp_path, ".tether/sessions",
                                         report["session_id"]) /
                        "report.json").read_text())
    assert saved["auto_rollback"]["ok"] is True
    assert any("rollback" in s.lower() for s in saved["next_steps"])


def test_auto_rollback_keeps_preexisting_untracked_files(tmp_path):
    _git_repo(tmp_path)
    # commit the mission first so this pre-existing untracked file is NOT
    # part of the committed tree
    mp = _failing_git_mission(tmp_path)
    (tmp_path / "user-notes.txt").write_text("keep me\n")  # pre-existing
    adapter = _MultiWriterAdapter(
        {"f.txt": "clobbered\n", "agent-added.txt": "extra\n"},
        fail_execute=True,
    )
    cfg = TetherConfig(audit_dir=".tether/sessions", auto_rollback=True,
                       max_attempts=1)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp),
                                                      allow_dirty=True)
    assert report["status"] == "failed"
    assert report["auto_rollback"]["ok"] is True
    # session changes are undone...
    assert (tmp_path / "f.txt").read_text() == "hello\n"
    assert not (tmp_path / "agent-added.txt").exists()
    # ...but the pre-existing untracked file survives even though change
    # detection attributed it to the session (it was untracked at detect time)
    assert (tmp_path / "user-notes.txt").read_text() == "keep me\n"


def test_no_auto_rollback_on_success(tmp_path):
    _git_repo(tmp_path)
    mp = _committed_mission(tmp_path)
    adapter = _WritingAdapter("success-output.txt")
    cfg = TetherConfig(audit_dir=".tether/sessions", auto_rollback=True,
                       max_attempts=1)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "success"
    assert "auto_rollback" not in report  # never rolled back a success
    assert (tmp_path / "success-output.txt").exists()  # change kept


def test_no_auto_rollback_in_dry_run(tmp_path):
    _git_repo(tmp_path)
    mp = _committed_mission(tmp_path)
    adapter = SpyAdapter({"scenario": "success"})
    cfg = TetherConfig(audit_dir=".tether/sessions", auto_rollback=True,
                       dry_run=True)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "success"
    assert "auto_rollback" not in report


def test_auto_rollback_disabled_by_default_keeps_changes(tmp_path):
    _git_repo(tmp_path)
    mp = _failing_git_mission(tmp_path)
    adapter = _MultiWriterAdapter({"f.txt": "clobbered\n"}, fail_execute=True)
    cfg = TetherConfig(audit_dir=".tether/sessions", max_attempts=1)
    assert cfg.auto_rollback is False
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"
    assert "auto_rollback" not in report
    assert (tmp_path / "f.txt").read_text() == "clobbered\n"


def test_cli_tri_state_auto_rollback_flag(tmp_path):
    _git_repo(tmp_path)
    mp = _failing_git_mission(tmp_path)

    # default (flag omitted): no rollback, changes remain
    r = runner.invoke(app, ["run", str(mp), "--project-dir", str(tmp_path)])
    assert r.exit_code == EXIT_FAILED
    sid1 = r.output.split("Session: ")[1].split()[0]
    rep1 = json.loads((find_session_dir(tmp_path, ".tether/sessions", sid1) /
                       "report.json").read_text())
    assert "auto_rollback" not in rep1
    assert (tmp_path / "f.txt").read_text() != "hello\n"

    # explicit --no-auto-rollback: same outcome
    r = runner.invoke(app, ["run", str(mp), "--project-dir", str(tmp_path),
                            "--allow-dirty", "--no-auto-rollback"])
    assert r.exit_code == EXIT_FAILED

    # --auto-rollback: failed mission is restored automatically
    r = runner.invoke(app, ["run", str(mp), "--project-dir", str(tmp_path),
                            "--allow-dirty", "--auto-rollback"])
    assert r.exit_code == EXIT_FAILED, r.output  # status stays failed
    sid3 = r.output.split("Session: ")[1].split()[0]
    rep3 = json.loads((find_session_dir(tmp_path, ".tether/sessions", sid3) /
                       "report.json").read_text())
    assert rep3["auto_rollback"]["attempted"] is True
    assert rep3["auto_rollback"]["ok"] is True
    assert (tmp_path / "f.txt").read_text() == "hello\n"


def test_auto_rollback_config_resolution(tmp_path):
    # project config applies; CLI overrides win; default is off
    assert resolve_config(tmp_path).auto_rollback is False
    (tmp_path / "tether.yaml").write_text("auto_rollback: true\n")
    assert resolve_config(tmp_path).auto_rollback is True
    assert resolve_config(
        tmp_path, cli_overrides={"auto_rollback": False}).auto_rollback is False

