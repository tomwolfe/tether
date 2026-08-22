"""dogfood-15: adversarial review gate + verification-strength lint."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import tether.adapters as registry
from tether.adapters.base import AgentAdapter, SessionInfo
from tether.audit import find_session_dir
from tether.cli import app
from tether.mission import MissionError, load_mission
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

    def __init__(self, review_logs, name="reviewable",
                 change_text="changed by agent\n"):
        super().__init__({})
        self.review_logs = review_logs
        self.name = name
        self.change_text = change_text
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
            if isinstance(self.review_logs, list):
                logs = self.review_logs.pop(0) if self.review_logs else ""
                return AgentState(status="completed", logs=logs)
            return AgentState(status="completed", logs=self.review_logs)
        if not self._planned:
            self._planned = True
            return AgentState(status="completed", logs="plan")
        self._executed = True
        (Path(self.project_dir) / "f.txt").write_text(self.change_text)
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
    "",
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


ECHOED_PROMPT = (
    "$ opencode run '...answer with exactly one verdict line — "
    "'REVIEW: APPROVE' or 'REVIEW: REQUEST_CHANGES' — followed by one line "
    "of reasoning.'\n"
)


def test_review_last_marker_decides_over_echoed_prompt(tmp_path):
    # Regression (dogfood-16): command adapters echo the full prompt into
    # logs; both tokens therefore appear before the reviewer's real verdict.
    # The LAST marker must decide.
    logs = ECHOED_PROMPT + "REVIEW: APPROVE\nthe change meets the goal"
    report = _run(tmp_path, "review:\n  enabled: true\n", review_logs=logs)
    assert report["status"] == "success"
    assert report["review"]["verdict"] == "approve"


def test_review_late_request_changes_beats_echoed_approve(tmp_path):
    logs = ECHOED_PROMPT + "REVIEW: REQUEST_CHANGES\nartifact misses task 2"
    report = _run(tmp_path, "review:\n  enabled: true\n", review_logs=logs)
    assert report["status"] == "failed"
    assert report["review"]["verdict"] == "request_changes"


def test_review_tokens_inside_diff_hunks_do_not_decide(tmp_path):
    # Regression (dogfood-17): the captured diff may legitimately contain the
    # marker strings mid-line (e.g. test fixtures). Echoed diff content must
    # never decide the verdict; only lines BEGINNING with a marker count.
    diff_noise = (
        "+    \"REVIEW: REQUEST_CHANGES\\nreason-two\",\n"
        "+ data = \"...REVIEW: APPROVE...\"\n"
    )
    logs = ECHOED_PROMPT + diff_noise + \
        "REVIEW: APPROVE\npayload matches the goal"
    report = _run(tmp_path, "review:\n  enabled: true\n", review_logs=logs)
    assert report["status"] == "success"
    assert report["review"]["verdict"] == "approve"


def test_review_real_rejection_wins_over_tokens_in_hunks(tmp_path):
    diff_noise = (
        "+    \"REVIEW: REQUEST_CHANGES\\nreason-two\",\n"
        "+ data = \"...REVIEW: APPROVE...\"\n"
    )
    logs = ECHOED_PROMPT + diff_noise + \
        "REVIEW: REQUEST_CHANGES\nfunctional payload missing from diff"
    report = _run(tmp_path, "review:\n  enabled: true\n", review_logs=logs)
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


# ------------------------------- dogfood-17: independent reviewer + routing


def test_review_adapter_and_retry_fields_validate(tmp_path):
    # Structural validation: review.adapter must be a string when present.
    p = _mission_file(tmp_path, "review:\n  enabled: true\n  adapter: 5\n")
    with pytest.raises(MissionError):
        load_mission(p)
    ok = _mission_file(
        tmp_path,
        "review:\n  enabled: true\n  adapter: pi\n"
        "  retry_on_rejection: true\n")
    m = load_mission(ok)
    assert m.review is not None
    assert m.review.adapter == "pi"
    assert m.review.retry_on_rejection is True
    plain = _mission_file(tmp_path, "review:\n  enabled: true\n")
    m2 = load_mission(plain)
    assert m2.review is not None
    assert m2.review.adapter is None
    assert m2.review.retry_on_rejection is False


def test_independent_reviewer_receives_review_send_worker_does_not(tmp_path):
    _git_repo(tmp_path)
    mp = tmp_path / "m.yaml"
    mp.write_text(
        f"mission:\n  name: rev\n  goal: make f.txt say done\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\n"
        f"adapter: mock\nreview:\n  enabled: true\n"
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "mission"],
                   check=True)
    worker = _ReviewingAdapter(REVIEW_APPROVED, name="worker")
    reviewer = _ReviewingAdapter(REVIEW_APPROVED, name="independent")
    cfg = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(worker, cfg, tmp_path,
                          reviewer=reviewer).run(load_mission(mp))
    assert report["status"] == "success"
    # The review send went to the independent reviewer, never to the worker.
    assert [s for s in reviewer.session_ids if s.endswith("-review")]
    assert len(reviewer.review_prompts) == 1
    assert worker.review_prompts == []
    # The report records the reviewer's OWN adapter name.
    assert report["review"]["adapter"] == "independent"


def test_unset_reviewer_keeps_self_review_path(tmp_path):
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
    adapter = _ReviewingAdapter(REVIEW_APPROVED)
    orch = Orchestrator(adapter, TetherConfig(audit_dir=".tether/sessions"),
                        tmp_path)
    assert orch.reviewer is orch.adapter
    report = orch.run(load_mission(mp))
    assert report["status"] == "success"
    assert report["review"]["adapter"] == "reviewable"


class _UnavailableReviewer(AgentAdapter):
    name = "unavail-reviewer"

    def __init__(self, settings=None):
        super().__init__(settings)

    def is_available(self):
        return False, "reviewer binary missing"

    def start_session(self, project_dir, session_id):
        return SessionInfo(session_id=session_id, project_dir=project_dir)

    def send(self, prompt, session):
        return AgentState(status="completed", logs=REVIEW_APPROVED)

    def cancel(self, session):
        pass


class _CountingWorker(AgentAdapter):
    """Worker that counts every session/send on the class so the test can
    prove the CLI aborted before any agent ran."""

    name = "counting-worker"
    started: list[str] = []
    sent: list[str] = []

    def __init__(self, settings=None):
        super().__init__(settings)

    def is_available(self):
        return True, ""

    def start_session(self, project_dir, session_id):
        type(self).started.append(session_id)
        return SessionInfo(session_id=session_id, project_dir=project_dir)

    def send(self, prompt, session):
        type(self).sent.append(prompt)
        return AgentState(status="completed", logs=REVIEW_APPROVED)

    def cancel(self, session):
        pass


def test_unavailable_named_reviewer_aborts_before_any_agent_run(
        tmp_path, monkeypatch):
    monkeypatch.setitem(registry._REGISTRY, "unavail-reviewer",
                        _UnavailableReviewer)
    monkeypatch.setitem(registry._REGISTRY, "counting-worker",
                        _CountingWorker)
    _CountingWorker.started.clear()
    _CountingWorker.sent.clear()
    _git_repo(tmp_path)
    mp = tmp_path / "m.yaml"
    mp.write_text(
        f"mission:\n  name: rev\n  goal: g\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\n"
        f"adapter: counting-worker\n"
        f"review:\n  enabled: true\n  adapter: unavail-reviewer\n"
    )
    r = runner.invoke(app, ["run", str(mp),
                            "--project-dir", str(tmp_path)])
    assert r.exit_code != 0
    assert "unavail-reviewer" in r.output
    assert "reviewer binary missing" in r.output
    # Aborted up front: the worker agent never ran at all.
    assert _CountingWorker.started == []
    assert _CountingWorker.sent == []


def test_unknown_named_reviewer_aborts_before_any_agent_run(
        tmp_path, monkeypatch):
    monkeypatch.setitem(registry._REGISTRY, "counting-worker",
                        _CountingWorker)
    _CountingWorker.started.clear()
    _CountingWorker.sent.clear()
    _git_repo(tmp_path)
    mp = tmp_path / "m.yaml"
    mp.write_text(
        f"mission:\n  name: rev\n  goal: g\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\n"
        f"adapter: counting-worker\n"
        f"review:\n  enabled: true\n  adapter: no-such-adapter\n"
    )
    r = runner.invoke(app, ["run", str(mp),
                            "--project-dir", str(tmp_path)])
    assert r.exit_code != 0
    assert "no-such-adapter" in r.output
    assert _CountingWorker.started == []
    assert _CountingWorker.sent == []


def test_review_retry_routes_one_more_execution_then_succeeds(tmp_path):
    _git_repo(tmp_path)
    mp = tmp_path / "m.yaml"
    mp.write_text(
        f"mission:\n  name: rev\n  goal: make f.txt say done\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\n"
        f"adapter: mock\n"
        f"review:\n  enabled: true\n  retry_on_rejection: true\n"
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "mission"],
                   check=True)
    adapter = _ReviewingAdapter([REVIEW_REJECTED, REVIEW_APPROVED])
    cfg = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "success"
    assert report["review"]["verdict"] == "approve"
    # Exactly one review-triggered recovery round ran through the normal
    # recovery machinery.
    assert len(report["recovery_attempts"]) == 1
    assert report["recovery_attempts"][0]["failure_class"] == \
        "review_rejection"
    # The repair prompt carries the goal, the review reason, and the diff.
    d = find_session_dir(tmp_path, ".tether/sessions", report["session_id"])
    repairs = list((d / "prompts").glob("*-repair-1.txt"))
    assert len(repairs) == 1
    repair = repairs[0].read_text(encoding="utf-8")
    assert "make f.txt say done" in repair       # mission goal
    assert "the diff never touches the goal" in repair  # review reason
    assert "+changed by agent" in repair         # captured change excerpt


def test_review_retry_never_exceeds_attempts_budget(tmp_path):
    _git_repo(tmp_path)
    mp = tmp_path / "m.yaml"
    mp.write_text(
        f"mission:\n  name: rev\n  goal: g\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\n"
        f"adapter: mock\nrecovery:\n  max_attempts: 2\n"
        f"review:\n  enabled: true\n  retry_on_rejection: true\n"
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "mission"],
                   check=True)
    adapter = _ReviewingAdapter([
        "REVIEW: REQUEST_CHANGES\nreason-one",
        "REVIEW: REQUEST_CHANGES\nreason-two",
    ])
    cfg = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"
    # Budget of 2 total attempts: initial + exactly one review-triggered
    # extra execution; the second rejection exhausts it and fails.
    assert len(report["recovery_attempts"]) == 1
    d = find_session_dir(tmp_path, ".tether/sessions", report["session_id"])
    assert len(list((d / "verification").glob("attempt-*.json"))) == 2
    # The LAST review reason lands in next_steps, as today.
    assert any("Review gate rejected the change" in s
               and "reason-two" in s for s in report["next_steps"])
    assert not any("reason-one" in s for s in report["next_steps"])


def test_review_retry_default_off_still_fails_immediately(tmp_path):
    _git_repo(tmp_path)
    mp = tmp_path / "m.yaml"
    mp.write_text(
        f"mission:\n  name: rev\n  goal: g\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\n"
        f"adapter: mock\n"
        f"review:\n  enabled: true\n  retry_on_rejection: false\n"
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "mission"],
                   check=True)
    adapter = _ReviewingAdapter(REVIEW_REJECTED)
    cfg = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "failed"
    assert report["review"]["verdict"] == "request_changes"
    assert len(report["recovery_attempts"]) == 0


def test_readme_documents_review_adapter_and_retry():
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "review.adapter" in readme
    assert "retry_on_rejection" in readme


# ------------------------- dogfood-20: full-context review mode

BIG_CHANGE = "".join(
    f"+FULL-CONTEXT-LINE-{i:04d} {'x' * 180}\n" for i in range(40))


def _review_run(tmp_path, review_block, review_logs,
                change_text="changed by agent\n"):
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
    adapter = _ReviewingAdapter(review_logs, change_text=change_text)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    return report, adapter


def test_review_context_defaults_to_excerpt(tmp_path):
    m_path = tmp_path / "m.yaml"
    m_path.write_text("mission:\n  name: x\n  goal: y\n")
    assert load_mission(m_path).review is None
    ok = tmp_path / "ok.yaml"
    ok.write_text("mission:\n  name: x\n  goal: y\n"
                  "review:\n  enabled: true\n")
    m = load_mission(ok)
    assert m.review is not None
    assert m.review.context == "excerpt"


def test_review_context_invalid_value_rejected_at_validation(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("mission:\n  name: x\n  goal: y\n"
                 "review:\n  enabled: true\n  context: everything\n")
    with pytest.raises(MissionError):
        load_mission(p)


def test_review_excerpt_mode_prompt_unchanged_by_context_option(tmp_path):
    # Regression guard: an explicit context: "excerpt" must produce the
    # byte-for-byte same review prompt as no context key at all.
    _, plain = _review_run(tmp_path, "review:\n  enabled: true\n",
                           REVIEW_APPROVED)
    _, explicit = _review_run(
        tmp_path, "review:\n  enabled: true\n  context: excerpt\n",
        REVIEW_APPROVED)
    assert len(plain.review_prompts) == 1
    assert len(explicit.review_prompts) == 1
    assert plain.review_prompts[0] == explicit.review_prompts[0]
    assert "Cite specific hunks" not in plain.review_prompts[0]


def test_review_full_context_embeds_entire_artifact_and_cites_hunks(tmp_path):
    report, adapter = _review_run(
        tmp_path, "review:\n  enabled: true\n  context: full\n",
        REVIEW_APPROVED, change_text=BIG_CHANGE)
    assert report["status"] == "success"
    assert report["review"]["verdict"] == "approve"
    prompt = adapter.review_prompts[0]
    # Substantially more of the artifact than the ~4KB excerpt budget:
    # head, middle, AND tail lines are all present (excerpt mode would
    # clip the middle behind a truncation marker).
    assert "+FULL-CONTEXT-LINE-0000" in prompt
    assert "+FULL-CONTEXT-LINE-0020" in prompt
    assert "+FULL-CONTEXT-LINE-0039" in prompt
    assert "[truncated" not in prompt
    # The reviewer is asked to cite specific hunks/lines.
    assert "Cite specific hunks or lines" in prompt


def test_review_excerpt_mode_still_bounds_a_large_artifact(tmp_path):
    _, adapter = _review_run(tmp_path, "review:\n  enabled: true\n",
                             REVIEW_APPROVED, change_text=BIG_CHANGE)
    prompt = adapter.review_prompts[0]
    assert "+FULL-CONTEXT-LINE-0000" in prompt      # head kept
    assert "+FULL-CONTEXT-LINE-0039" in prompt      # tail kept
    assert "+FULL-CONTEXT-LINE-0020" not in prompt  # middle clipped
    assert "[truncated" in prompt
    assert "Cite specific hunks" not in prompt


@pytest.mark.parametrize("review_block", [
    "review:\n  enabled: true\n",
    "review:\n  enabled: true\n  context: full\n",
])
def test_review_both_modes_fail_safe_on_garbage_output(
        tmp_path, review_block):
    report, _adapter = _review_run(
        tmp_path, review_block, "looks good to me, ship it")
    assert report["status"] == "failed"
    assert report["review"]["verdict"] == "request_changes"
