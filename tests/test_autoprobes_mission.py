"""dogfood-43: auto-generated probes wired through the orchestrator.

Integration contract: contract loading (strict keys/types), off-by-default
inertness, live synthesis over a scripted adapter, the generated-probe tier
inside the verification ladder, the teeth gate routing weak probes into
recovery, and dry-run inertness.
"""
import json
import subprocess
import sys
from pathlib import Path

from tether.adapters.base import AgentAdapter, SessionInfo
from tether.audit import find_session_dir
from tether.mission import MissionError, load_mission
from tether.models import AgentState, TetherConfig
from tether.orchestrator import Orchestrator

PASS_CMD = f"{sys.executable} -c 'import sys; sys.exit(0)'"

CALC_SRC = "def triple(x):\n    return x * 3\n"


def _strong_probe_yaml() -> str:
    py = sys.executable
    return (
        "Sure — here are probes:\n```yaml\nprobes:\n"
        f"  - command: '{py} -c \"import calc; print(calc.triple(5))\"'\n"
        "    contains: \"15\"\n```\n")


def _toothless_probe_yaml() -> str:
    py = sys.executable
    return (
        "```yaml\nprobes:\n"
        f"  - command: {py} -c \"print(chr(112) + chr(121))\"\n"
        "    contains: py\n```\n")


class _SynthAdapter(AgentAdapter):
    """Plan -> execute (writes files); '-auto-probes' sessions reply with
    scripted synthesis logs and record the prompts they received."""

    name = "synth"
    verified = True

    def __init__(self, synth_logs, exec_files=None):
        super().__init__({})
        self.synth_logs = synth_logs
        self.exec_files = dict(exec_files or {})
        self.synth_prompts: list[str] = []
        self.session_ids: list[str] = []
        self._planned = False

    def is_available(self):
        return True, ""

    def start_session(self, project_dir, session_id):
        self.project_dir = project_dir
        self.session_ids.append(session_id)
        return SessionInfo(session_id=session_id, project_dir=project_dir)

    def send(self, prompt, session):
        if session.session_id.endswith("-auto-probes"):
            self.synth_prompts.append(prompt)
            if isinstance(self.synth_logs, list):
                logs = self.synth_logs.pop(0) if self.synth_logs else ""
            else:
                logs = self.synth_logs
            return AgentState(status="completed", logs=logs)
        if not self._planned:
            self._planned = True
            return AgentState(status="completed", logs="plan")
        for rel, text in self.exec_files.items():
            target = Path(self.project_dir) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        return AgentState(status="completed", logs="done")

    def cancel(self, session):
        pass


def _git_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"],
                   cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path,
                   check=True)
    (tmp_path / "f.txt").write_text("original\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"],
                   check=True)


def _mission(tmp_path, verification_extra="", top_extra=""):
    mp = tmp_path / "m.yaml"
    mp.write_text(
        f"mission:\n  name: auto\n  goal: make calc.triple behavioral\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\n{verification_extra}"
        f"adapter: mock\nrecovery:\n  max_attempts: 2\n{top_extra}"
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "mission"],
                   check=True)
    return mp


def _run(tmp_path, adapter, mission_path, **run_kwargs):
    cfg = TetherConfig(audit_dir=".tether/sessions")
    return Orchestrator(adapter, cfg, tmp_path).run(
        load_mission(mission_path), **run_kwargs)


def _events(tmp_path, report):
    d = find_session_dir(tmp_path, ".tether/sessions", report["session_id"])
    return [json.loads(ln) for ln in
            (d / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if ln.strip()], d


AUTO_ON = (
    "  auto_probes:\n    enabled: true\n    max_probes: 4\n")


# ----------------------------------------------------------------- loader


def test_loader_accepts_full_block(tmp_path):
    mp = tmp_path / "m.yaml"
    mp.write_text(
        "mission:\n  name: l\n  goal: g\nadapter: mock\n"
        "verification:\n  auto_probes:\n    enabled: true\n"
        "    adapter: opencode\n    max_probes: 3\n"
        "    min_teeth_rate: 0.25\n    max_mutants: 5\n")
    contract = load_mission(mp)
    ap = contract.verification.auto_probes
    assert ap.enabled is True
    assert ap.adapter == "opencode"
    assert ap.max_probes == 3
    assert ap.min_teeth_rate == 0.25
    assert ap.max_mutants == 5


def test_loader_absent_block_stays_none(tmp_path):
    mp = tmp_path / "m.yaml"
    mp.write_text("mission:\n  name: l\n  goal: g\nadapter: mock\n")
    assert load_mission(mp).verification.auto_probes is None


def test_loader_rejects_unknown_keys(tmp_path):
    mp = tmp_path / "m.yaml"
    mp.write_text(
        "mission:\n  name: l\n  goal: g\nadapter: mock\n"
        "verification:\n  auto_probes:\n    enabled: true\n    bogus: 1\n")
    with pytest_raises(MissionError, "bogus"):
        load_mission(mp)


def test_loader_rejects_bad_types_and_bounds(tmp_path):
    base = ("mission:\n  name: l\n  goal: g\nadapter: mock\n"
            "verification:\n  auto_probes:\n")
    for block, fragment in [
            ("    enabled: yes-please\n", "enabled"),
            ("    adapter: 7\n", "adapter"),
            ("    max_probes: 0\n", "max_probes"),
            ("    max_probes: true\n", "max_probes"),
            ("    max_mutants: -1\n", "max_mutants"),
            ("    min_teeth_rate: 1.5\n", "min_teeth_rate"),
            ("    min_teeth_rate: 'high'\n", "min_teeth_rate")]:
        mp = tmp_path / "m.yaml"
        mp.write_text(base + block)
        with pytest_raises(MissionError, fragment):
            load_mission(mp)


def pytest_raises(exc, fragment):
    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, et, ev, tb):
            if et is None:
                raise AssertionError(f"expected {exc.__name__}")
            assert issubclass(et, exc), ev
            assert fragment in str(ev), ev
            return True
    return _Ctx()


# ------------------------------------------------------------ orchestrator


def test_off_by_default_is_fully_inert(tmp_path):
    _git_repo(tmp_path)
    mp = _mission(tmp_path)
    adapter = _SynthAdapter(_strong_probe_yaml(),
                            {"calc.py": CALC_SRC})
    report = _run(tmp_path, adapter, mp)
    assert report["status"] == "success"
    assert "auto_probes" not in report
    events, _ = _events(tmp_path, report)
    assert not [e for e in events
                if e.get("kind") in ("auto_probes", "auto_probe_teeth")]
    assert not adapter.synth_prompts


def test_enabled_synthesizes_runs_tier_and_measures_teeth(tmp_path):
    _git_repo(tmp_path)
    mp = _mission(tmp_path, verification_extra=AUTO_ON)
    adapter = _SynthAdapter(_strong_probe_yaml(), {"calc.py": CALC_SRC})
    report = _run(tmp_path, adapter, mp)
    assert report["status"] == "success"
    auto = report["auto_probes"]
    assert auto["enabled"] is True
    assert auto["status"] == "synthesized"
    assert auto["adapter"] == "synth"
    assert auto["count"] == 1
    assert "calc" in auto["probes"][0]["command"]
    # The synthesis prompt carried the mission goal AND the captured change
    # (untracked calc.py listed even though patch.diff alone would miss it).
    assert len(adapter.synth_prompts) == 1
    assert "make calc.triple behavioral" in adapter.synth_prompts[0]
    assert "calc.py" in adapter.synth_prompts[0]
    # Generated probe ran as part of verification evidence.
    assert any("calc.triple" in r["command"]
               for r in report["verification_results"])
    events, sdir = _events(tmp_path, report)
    teeth = [e for e in events if e.get("kind") == "auto_probe_teeth"]
    assert len(teeth) == 1
    assert teeth[0]["killed"] >= 1
    assert (sdir / "verification" / "autoprobes-teeth.json").exists()


def test_garbage_synthesis_is_advisory_fallback(tmp_path):
    _git_repo(tmp_path)
    mp = _mission(tmp_path, verification_extra=AUTO_ON)
    adapter = _SynthAdapter("I am unable to comply.", {"calc.py": CALC_SRC})
    report = _run(tmp_path, adapter, mp)
    assert report["status"] == "success"
    auto = report["auto_probes"]
    assert auto["status"] == "failed"
    assert "fenced yaml" in auto["reason"]
    events, _ = _events(tmp_path, report)
    assert not [e for e in events if e.get("kind") == "auto_probe_teeth"]
    assert len(report["recovery_attempts"]) == 0


def test_toothless_probes_fail_gate_into_recovery(tmp_path):
    _git_repo(tmp_path)
    extra = AUTO_ON + "    min_teeth_rate: 0.5\n"
    mp = _mission(tmp_path, verification_extra=extra)
    adapter = _SynthAdapter([_toothless_probe_yaml(), _toothless_probe_yaml()],
                            {"calc.py": CALC_SRC})
    report = _run(tmp_path, adapter, mp)
    assert report["status"] == "failed"
    events, _ = _events(tmp_path, report)
    teeth = [e for e in events if e.get("kind") == "auto_probe_teeth"]
    assert len(teeth) == 2  # gate ran on both attempts
    assert teeth[0]["kill_rate"] == 0.0
    assert any("lack teeth" in a["failing_output"]
               for a in report["recovery_attempts"])


def test_non_python_change_leaves_teeth_na_but_green(tmp_path):
    _git_repo(tmp_path)
    mp = _mission(tmp_path, verification_extra=AUTO_ON)
    adapter = _SynthAdapter(_toothless_probe_yaml(), {"f.txt": "changed\n"})
    report = _run(tmp_path, adapter, mp)
    assert report["status"] == "success"
    events, _ = _events(tmp_path, report)
    teeth = [e for e in events if e.get("kind") == "auto_probe_teeth"]
    assert len(teeth) == 1
    assert teeth[0]["total"] == 0


def test_dry_run_never_synthesizes(tmp_path):
    _git_repo(tmp_path)
    mp = _mission(tmp_path, verification_extra=AUTO_ON)
    adapter = _SynthAdapter(_strong_probe_yaml(), {"calc.py": CALC_SRC})
    report = _run(tmp_path, adapter, mp, dry_run=True)
    assert report["status"] == "success"
    events, _ = _events(tmp_path, report)
    skips = [e for e in events
             if e.get("kind") == "auto_probes"]
    assert len(skips) == 1
    assert skips[0]["status"] == "skipped"
    assert skips[0]["reason"] == "dry-run"
    assert not adapter.synth_prompts
