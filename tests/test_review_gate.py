"""dogfood-15: adversarial review gate + verification-strength lint."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tether.adapters.base import AgentAdapter, SessionInfo
from tether.audit import find_session_dir
from tether.cli import app
from tether.mission import load_mission
from tether.models import AgentState, TetherConfig
from tether.orchestrator import Orchestrator

PASS_CMD = f"{sys.executable} -c 'import sys; sys.exit(0)'"

runner = CliRunner()


class _ReviewingAdapter(AgentAdapter):
    """Scripted adapter: plan/execute complete (execute edits a tracked file);
    the review send on the fresh ``-review`` session replies with scripted
    verdict text and records the prompt it received."""

    name = "reviewable"
    verified = True

    def __init__(self, review_logs):
        super().__init__({})
        self.review_logs = review_logs
        self.review_prompts: list[str] = []
        self.session_ids: list[str] = []
        self._planned = False
        self._executed = False

    def is_available(self):
        return True, ""

    def start_session(self, project_dir, session_id):
        self.project_dir = project_dir
        self.session_ids.append(session_id)
        return SessionInfo(session_id=session_id, project_dir=project_dir)

    def send(self, prompt, session):
        if session.session_id.endswith("-review"):
            self.review_prompts.append(prompt)
            return AgentState(status="completed", logs=self.review_logs)
        if not self._planned:
            self._planned = True
            return AgentState(status="completed", logs="plan")
        self._executed = True
        (Path(self.project_dir) / "f.txt").write_text("changed by agent\n")
        return AgentState(status="completed", logs="done")

    def cancel(self, session):
        pass


def _git_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"],
                   cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("original\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"],
                   check=True)


def _run(tmp_path, review_block="", review_logs=None):
    _git_repo(tmp_path)
    mp = tmp_path / "m.yaml"
    mp.write_text(
        f"mission:\n  name: rev\n  goal: make f.txt say done\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\n"
        f"adapter: mock\n{review_block}"
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "mission"],
                   check=True)
    cfg = TetherConfig(audit_dir=".tether/sessions")
    adapter = _ReviewingAdapter(review_logs or REVIEW_APPROVED)
    return Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))


def _events(tmp_path, report):
    d = find_session_dir(tmp_path, ".tether/sessions", report["session_id"])
    return [json.loads(ln) for ln in
            (d / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if ln.strip()], d


REVIEW_APPROVED = "REVIEW: APPROVE\nthe change accomplishes the goal"
REVIEW_REJECTED = "REVIEW: REQUEST_CHANGES\nthe diff never touches the goal"


# --------------------------------------------- review gate verdict contract


def test_review_approve_reports_success_and_verdict(tmp_path):
    report = _run(tmp_path, "review:\n  enabled: true\n")
    assert report["status"] == "success"
    review = report["review"]
    assert set(review) == {"enabled", "adapter", "verdict", "reason"}
    assert review["verdict"] == "approve"
    assert review["enabled"] is True
    assert review["reason"] == "the change accomplishes the goal"
    assert review["adapter"] == "reviewable"


def test_review_request_changes_fails_mission(tmp_path):
    adapter = None
    _git_repo(tmp_path)
    mp = tmp_path / "m.yaml"
    mp.write_text(
        f"mission:\n  name: rev\n  goal: g\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\n"
        f"adapter: mock\nreview:\n  enabled: true\n"
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "mission"],
                   check=True)
    adapter = _ReviewingAdapter(REVIEW_REJECTED)
    cfg = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"
    assert report["review"]["verdict"] == "request_changes"
    assert any("Review gate rejected the change" in s
               and "the diff never touches the goal" in s
               for s in report["next_steps"])
    # No automatic re-execution: exactly one verification round ran.
    assert len(report["recovery_attempts"]) == 0


def test_review_not_required_rejection_keeps_success(tmp_path):
    report = _run(
        tmp_path, "review:\n  enabled: true\n  required: false\n",
        review_logs=REVIEW_REJECTED)
    assert report["status"] == "success"
    assert report["review"]["verdict"] == "request_changes"


@pytest.mark.parametrize("logs", [
    "looks good to me, ship it",
    "REVIEW: APPROVE\nonce\nREVIEW: APPROVE\ntwice",
    "REVIEW: APPROVE\nok\nREVIEW: REQUEST_CHANGES\nconflicting",
])
def test_review_unparseable_output_fails_safe(tmp_path, logs):
    _git_repo(tmp_path)
    mp = tmp_path / "m.yaml"
    mp.write_text(
        f"mission:\n  name: rev\n  goal: g\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\n"
        f"adapter: mock\nreview:\n  enabled: true\n"
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "mission"],
                   check=True)
    cfg = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(_ReviewingAdapter(logs), cfg, tmp_path)\
        .run(load_mission(mp))
    assert report["status"] == "failed"
    assert report["review"]["verdict"] == "request_changes"


# ------------------------------------------------- gate wiring and evidence


def test_review_disabled_leaves_behavior_unchanged(tmp_path):
    report = _run(tmp_path)
    assert report["status"] == "success"
    assert "review" not in report
    events, _ = _events(tmp_path, report)
    assert not [e for e in events if e.get("kind") == "review"]


def test_review_prompt_carries_goal_and_diff_excerpt(tmp_path):
    _git_repo(tmp_path)
    mp = tmp_path / "m.yaml"
    mp.write_text(
        f"mission:\n  name: rev\n  goal: THE-GOAL-TEXT\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\n"
        f"adapter: mock\nreview:\n  enabled: true\n"
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "mission"],
                   check=True)
    adapter = _ReviewingAdapter(REVIEW_APPROVED)
    cfg = TetherConfig(audit_dir=".tether/sessions")
    Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    # Reviewer ran in a FRESH session on the same adapter instance.
    assert len(adapter.session_ids) == 2
    assert adapter.session_ids[1].endswith("-review")
    assert len(adapter.review_prompts) == 1
    prompt = adapter.review_prompts[0]
    assert "THE-GOAL-TEXT" in prompt          # mission goal included
    assert "adversarial reviewer" in prompt   # role instruction
    assert "patch.diff" in prompt             # names the captured artifact
    assert "+changed by agent" in prompt      # bounded excerpt of the diff


def test_review_event_prompt_response_persisted(tmp_path):
    report = _run(tmp_path, "review:\n  enabled: true\n")
    events, d = _events(tmp_path, report)
    review_events = [e for e in events if e.get("kind") == "review"]
    assert len(review_events) == 1
    assert review_events[0]["verdict"] == "approve"
    prompts = list((d / "prompts").glob("*-review.txt"))
    responses = list((d / "responses").glob("*-review.json"))
    assert len(prompts) == 1 and len(responses) == 1
    assert "adversarial reviewer" in prompts[0].read_text(encoding="utf-8")
    saved = json.loads(responses[0].read_text(encoding="utf-8"))
    assert REVIEW_APPROVED in saved["logs"]


# --------------------------------------- verification-strength lint (--strict)


def _mission_file(tmp_path, verification):
    p = tmp_path / "m.yaml"
    p.write_text(f"mission:\n  name: x\n  goal: y\n{verification}")
    return p


def test_trivial_only_mission_warns_non_strict_fails_strict(tmp_path):
    p = _mission_file(tmp_path, 'verification:\n  commands:\n    - "true"\n'
                                 "    - echo probing nothing\n")
    ok = runner.invoke(app, ["validate-mission", str(p)])
    assert ok.exit_code == 0
    assert "WARNING" in ok.output and "trivial" in ok.output
    strict = runner.invoke(app, ["validate-mission", "--strict", str(p)])
    assert strict.exit_code == 1
    assert "INVALID" in strict.output and "trivial" in strict.output


def test_real_command_passes_lint_strict_included(tmp_path):
    p = _mission_file(tmp_path,
                      f"verification:\n  commands:\n    - {PASS_CMD}\n")
    plain = runner.invoke(app, ["validate-mission", str(p)])
    strict = runner.invoke(app, ["validate-mission", "--strict", str(p)])
    assert plain.exit_code == 0 and strict.exit_code == 0
    assert "WARNING" not in plain.output


def test_artifacts_present_passes_strict_despite_trivial_commands(tmp_path):
    p = _mission_file(tmp_path, 'verification:\n  commands:\n    - "true"\n'
                                "  artifacts:\n    - 'docs/*.md'\n")
    strict = runner.invoke(app, ["validate-mission", "--strict", str(p)])
    assert strict.exit_code == 0
    assert "WARNING" not in strict.output


def test_empty_verification_and_artifacts_fails_strict_only(tmp_path):
    p = _mission_file(tmp_path, "")
    lax = runner.invoke(app, ["validate-mission", str(p)])
    strict = runner.invoke(app, ["validate-mission", "--strict", str(p)])
    assert lax.exit_code == 0
    assert strict.exit_code == 1
    assert "no verification commands" in strict.output


# ------------------------------------------------------------ docs truth (3)


def test_architecture_md_documents_the_review_stage():
    root = Path(__file__).resolve().parents[1]
    lowered = (root / "docs" / "ARCHITECTURE.md")\
        .read_text(encoding="utf-8").lower()
    assert "review gate" in lowered
    assert "after verification" in lowered
