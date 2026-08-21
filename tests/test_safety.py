"""P0/P1 safety, correctness, and configuration behavior tests."""
import json
import subprocess
import sys
import tarfile

import pytest
from typer.testing import CliRunner

from tether.adapters.base import AgentAdapter, SessionInfo
from tether.adapters.mock import MockAdapter
from tether.audit import find_session_dir
from tether.cli import app
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
