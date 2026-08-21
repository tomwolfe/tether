import json
import subprocess

import pytest

from tether.audit import AuditTrail, find_session_dir
from tether.config import resolve_config
from tether.git_safety import (
    changed_files_since,
    create_checkpoint,
    head_sha,
    is_dirty,
    is_git_repo,
    rollback,
)
from tether.mission import MissionError, load_mission
from tether.models import TetherConfig
from tether.orchestrator import Orchestrator
from tether.adapters import resolve_adapter
from tether.verification import run_verification, summarize


@pytest.fixture
def project_dir(tmp_path):
    return tmp_path


def _write_mission(tmp_path, body):
    p = tmp_path / "mission.yaml"
    p.write_text(body, encoding="utf-8")
    return load_mission(p)


MISSION = """\
mission:
  name: test-mission
  goal: do a thing
verification:
  commands:
    - "true"
recovery:
  max_attempts: 3
adapter: mock
"""


def test_mission_validation_ok(tmp_path):
    m = _write_mission(tmp_path, MISSION)
    assert m.name == "test-mission"
    assert m.verification.commands == ["true"]


@pytest.mark.parametrize("bad", [
    "mission:\n  name: ''\n  goal: x\n",
    "mission:\n  name: x\n  goal: ''\n",
    "goal: no mission block\n",
    "mission:\n  name: x\n  goal: y\nverification:\n  commands: not-a-list\n",
])
def test_mission_validation_fails(tmp_path, bad):
    with pytest.raises(MissionError):
        _write_mission(tmp_path, bad)


def test_config_defaults_and_precedence(project_dir):
    cfg = resolve_config(project_dir)
    assert isinstance(cfg, TetherConfig)
    assert cfg.default_adapter == "mock"
    (project_dir / "tether.yaml").write_text("default_adapter: command\nmax_attempts: 5\n")
    cfg = resolve_config(project_dir)
    assert cfg.default_adapter == "command"
    assert cfg.max_attempts == 5
    # CLI overrides beat project config
    cfg = resolve_config(project_dir, cli_overrides={"max_attempts": 2})
    assert cfg.max_attempts == 2


def test_config_rejects_unknown_keys(project_dir):
    (project_dir / "tether.yaml").write_text("bogus_key: 1\n")
    with pytest.raises(ValueError):
        resolve_config(project_dir)


def _orchestra(tmp_path, scenario, commands=("true",), max_attempts=3):
    adapter = resolve_adapter("mock", {"mock": {"scenario": scenario}})
    cfg = TetherConfig(audit_dir=".tether/sessions", max_attempts=max_attempts)
    mission_text = f"""\
mission:
  name: m
  goal: g
verification:
  commands: {json.dumps(list(commands))}
recovery:
  max_attempts: {max_attempts}
adapter: mock
"""
    mp = tmp_path / "m.yaml"
    mp.write_text(mission_text)
    mission = load_mission(mp)
    return Orchestrator(adapter, cfg, tmp_path).run(mission)


def test_mock_success_path(tmp_path):
    report = _orchestra(tmp_path, "success")
    assert report["status"] == "success"
    assert report["recovery_attempts"] == []
    session = find_session_dir(tmp_path, ".tether/sessions", report["session_id"])
    assert session is not None
    assert (session / "report.json").exists()
    assert (session / "events.jsonl").exists()
    assert list((session / "prompts").glob("*.txt"))
    saved = json.loads((session / "report.json").read_text())
    assert saved["status"] == "success"


def test_mock_always_fails_max_attempts(tmp_path):
    report = _orchestra(tmp_path, "always_fail", max_attempts=2)
    assert report["status"] == "failed"
    assert len(report["recovery_attempts"]) == 1  # attempts 1..2 -> one recovery between


def test_recovery_succeeds_after_retry(tmp_path):
    report = _orchestra(tmp_path, "fail_then_succeed")
    assert report["status"] == "success"
    assert len(report["recovery_attempts"]) == 1


def test_verification_failure_detected(tmp_path):
    results = run_verification(["false"], tmp_path)
    passed, out = summarize(results)
    assert not passed
    assert "exit code 1" in out
    ok, _ = summarize(run_verification(["true"], tmp_path))
    assert ok


def test_verification_timeout_and_missing_binary(tmp_path):
    r = run_verification(["python3 -c \"import time; time.sleep(5)\""], tmp_path, timeout_seconds=1)[0]
    assert r.timed_out
    r = run_verification(["definitely-not-a-binary-xyz"], tmp_path)[0]
    assert not r.passed and "not found" in r.stderr


def test_dry_run_skips_execution(tmp_path):
    results = run_verification(["false"], tmp_path, dry_run=True)
    assert results[0].skipped_dry_run and results[0].passed


def _git_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)


def test_git_checkpoint_and_rollback(tmp_path):
    _git_repo(tmp_path)
    base = head_sha(tmp_path)
    info = create_checkpoint(tmp_path, "sess1234")
    assert info.created and info.ref.endswith("sess1234")
    (tmp_path / "f.txt").write_text("changed\n")
    (tmp_path / "new.txt").write_text("new\n")
    assert is_dirty(tmp_path)
    assert changed_files_since(tmp_path, base) == ["f.txt", "new.txt"]
    # dirty tree blocks destructive rollback
    ok, msg = rollback(tmp_path, "sess1234")
    assert not ok and "dirty" in msg
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "--", "."], check=True)
    (tmp_path / "new.txt").unlink()
    ok, msg = rollback(tmp_path, "sess1234")
    assert ok
    assert head_sha(tmp_path) == base
    assert (tmp_path / "f.txt").read_text() == "hello\n"


def test_git_dirty_requires_allow_dirty(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / "f.txt").write_text("dirty\n")
    info = create_checkpoint(tmp_path, "s1", allow_dirty=False)
    assert not info.created and "dirty" in info.warning
    info = create_checkpoint(tmp_path, "s2", allow_dirty=True)
    assert info.created and info.dirty


def test_non_git_project_warns_and_backs_up(tmp_path):
    info = create_checkpoint(tmp_path, "s3")
    assert not info.is_git_repo and info.warning
    from tether.git_safety import make_file_backup
    (tmp_path / "data.txt").write_text("keep me")
    dest = make_file_backup(tmp_path, tmp_path / ".tether/backups", "s3")
    assert dest and (tmp_path / ".tether/backups/s3.tar.gz").exists()


def test_audit_trail_events(tmp_path):
    audit = AuditTrail(tmp_path, ".tether/sessions", "my/mission:name", "abcdef123456")
    audit.log_event("x", {"a": 1})
    audit.save_prompt("plan", "hello")
    assert (audit.dir / "events.jsonl").exists()
    assert find_session_dir(tmp_path, ".tether/sessions", "abcdef1234") == audit.dir
