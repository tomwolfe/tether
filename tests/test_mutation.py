"""Mutation testing meta-verification (dogfood-22): generator, runner,
orchestration gating, telemetry, and docs."""
import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tether.adapters.base import AgentAdapter, SessionInfo
from tether.audit import find_session_dir
from tether.mission import MissionError, load_mission
from tether.models import AgentState, MutationSpec, TetherConfig
from tether.orchestrator import Orchestrator
from tether.verification import (
    MUTATION_OPERATORS,
    Mutant,
    generate_mutants,
    run_mutation_testing,
)


def py_cmd(code: str) -> str:
    return f"{sys.executable} -c '{code}'"


PASS_CMD = py_cmd("import sys; sys.exit(0)")


# ------------------------------------------- task 2: mutant generation


ALL_OPS_SOURCE = (
    "def f(a, b):\n"
    "    if a == b:\n"
    "        return True\n"
    "    x = a * b\n"
    "    y = not x\n"
    "    z = a < b\n"
    "    w = a - b\n"
    "    return z\n"
)


def test_every_operator_produces_valid_distinct_mutants():
    mutants = generate_mutants(
        ALL_OPS_SOURCE, list(MUTATION_OPERATORS), seed=7, max_mutants=100)
    assert {m.operator for m in mutants} == set(MUTATION_OPERATORS)
    for m in mutants:
        ast.parse(m.source)          # syntactically valid
        assert m.source != ALL_OPS_SOURCE
        assert ":" in m.site         # site points into the original


@pytest.mark.parametrize(("source, needle"), [
    ("x = a == b\n", "!="),
    ("x = a != b\n", "=="),
    ("x = a < b\n", ">="),
    ("x = a >= b\n", "<"),
    ("x = a <= b\n", ">"),
    ("x = a > b\n", "<="),
])
def test_negate_compare_swaps(source, needle):
    (m,) = generate_mutants(source, ["negate_compare"], seed=1, max_mutants=10)
    assert m.operator == "negate_compare"
    assert needle in m.source


@pytest.mark.parametrize(("source, needle"), [
    ("flag = True\n", "flag = False"),
    ("flag = False\n", "flag = True"),
    ("y = not x\n", "y = x"),
])
def test_flip_bool_swaps(source, needle):
    (m,) = generate_mutants(source, ["flip_bool"], seed=1, max_mutants=10)
    assert needle in m.source


@pytest.mark.parametrize(("source, needle"), [
    ("y = a + b\n", "a - b"),
    ("y = a - b\n", "a + b"),
    ("y = a * b\n", "a / b"),
    ("y = a / b\n", "a * b"),
])
def test_arithmetic_swaps(source, needle):
    (m,) = generate_mutants(source, ["arithmetic"], seed=1, max_mutants=10)
    assert needle in m.source


def test_break_return_makes_return_none():
    source = "def f():\n    return 42\n"
    (m,) = generate_mutants(source, ["break_return"], seed=1, max_mutants=10)
    assert "return None" in m.source
    ast.parse(m.source)


def test_determinism_same_seed_same_mutants_and_cap():
    big = "\n".join(f"x{i} = a{i} * b{i}" for i in range(40)) + "\n"
    first = generate_mutants(big, ["arithmetic"], seed=123, max_mutants=5)
    second = generate_mutants(big, ["arithmetic"], seed=123, max_mutants=5)
    assert first == second
    assert len(first) == 5                      # capped deterministically
    uncapped = generate_mutants(big, ["arithmetic"], seed=123, max_mutants=1000)
    assert len(uncapped) == 40                  # one mutant per site
    assert [m.site for m in uncapped] == \
        [m.site for m in generate_mutants(big, ["arithmetic"],
                                          seed=999, max_mutants=1000)]


def test_unparseable_source_yields_no_mutants_and_unknown_op_raises():
    assert generate_mutants("def f(:\n", ["flip_bool"], seed=1,
                            max_mutants=5) == []
    with pytest.raises(ValueError):
        generate_mutants("x = 1\n", ["nope"], seed=1, max_mutants=5)


# -------------------------------------- task 2: classification + kill rate

KILLABLE = "flag = True\nvalue = 1 <= 2\ndef f():\n    return value\n"


def _spec(**kw) -> MutationSpec:
    return MutationSpec(enabled=True, **kw)


def _project(tmp_path):
    p = tmp_path / "mod.py"
    p.write_text(KILLABLE, encoding="utf-8")
    return p


def test_killed_survived_classification_and_kill_rate_math(tmp_path):
    p = _project(tmp_path)
    answers = iter([(True, ""), (False, "boom"), (False, "bang")])
    collected: list[Mutant] = []
    summary = run_mutation_testing(
        _spec(), ["mod.py"], tmp_path,
        lambda: next(answers), 600, collect_results=collected)
    assert p.read_bytes() == KILLABLE.encode()      # restored
    assert summary.total == 3
    assert summary.killed == 2
    assert summary.survived == 1
    assert summary.kill_rate == round(2 / 3, 4)
    assert summary.per_file == {
        "mod.py": {"killed": 2, "survived": 1}}
    assert len(collected) == 3
    assert [r.status for r in collected] == \
        ["survived", "killed", "killed"]
    assert collected[1].detail == "boom"


def test_crashing_suite_counts_as_killed_and_still_restores(tmp_path):
    p = _project(tmp_path)

    def suite():
        raise RuntimeError("suite exploded")

    summary = run_mutation_testing(_spec(), ["mod.py"], tmp_path, suite, 600)
    assert summary.killed == summary.total > 0
    assert summary.kill_rate == 1.0
    assert p.read_bytes() == KILLABLE.encode()


def test_no_targets_gives_zero_summary_without_running_suite(tmp_path):
    calls = []
    summary = run_mutation_testing(
        _spec(), [], tmp_path, lambda: calls.append(1) or (True, ""), 600)
    assert calls == []
    assert summary.model_dump() == {
        "total": 0, "killed": 0, "survived": 0, "skipped": 0,
        "kill_rate": 0.0, "per_file": {}}


def test_only_py_changed_files_mutated_and_tether_excluded(tmp_path):
    p = _project(tmp_path)
    (tmp_path / "notes.txt").write_text("flag = True\n", encoding="utf-8")
    tethered = tmp_path / ".tether" / "sneaky.py"
    tethered.parent.mkdir(exist_ok=True)
    tethered.write_text(KILLABLE, encoding="utf-8")
    before = (p.read_bytes(), (tmp_path / "notes.txt").read_bytes(),
              tethered.read_bytes())
    seen: list[str] = []

    def suite():
        seen.append(p.read_text())
        return False, ""

    summary = run_mutation_testing(
        _spec(), ["mod.py", "notes.txt", ".tether/sneaky.py"],
        tmp_path, suite, 600)
    assert summary.total > 0
    assert set(summary.per_file) == {"mod.py"}
    assert seen and all(s != KILLABLE for s in seen)   # real mutants ran
    assert (p.read_bytes(), (tmp_path / "notes.txt").read_bytes(),
            tethered.read_bytes()) == before           # tree byte-identical


def test_unparseable_py_file_is_skipped(tmp_path):
    bad = tmp_path / "broken.py"
    bad.write_text("def f(:\n", encoding="utf-8")
    summary = run_mutation_testing(
        _spec(), ["broken.py"], tmp_path, lambda: (True, ""), 600)
    assert summary.skipped == 1
    assert summary.total == 1
    assert summary.kill_rate == 0.0


# ------------------------------------------- task 1: mission validation


def _write_mission(tmp_path, body):
    p = tmp_path / "m.yaml"
    p.write_text(body, encoding="utf-8")
    return load_mission(p)


def test_absent_mutation_defaults_to_none(tmp_path):
    m = _write_mission(tmp_path, "mission:\n  name: m\n  goal: g\n")
    assert m.verification.mutation is None


def test_full_mutation_block_parses(tmp_path):
    m = _write_mission(tmp_path, (
        "mission:\n  name: m\n  goal: g\n"
        "verification:\n"
        "  mutation:\n"
        "    enabled: true\n"
        "    operators: [negate_compare, flip_bool]\n"
        "    max_mutants: 5\n"
        "    fail_below: 0.5\n"
    ))
    spec = m.verification.mutation
    assert spec is not None
    assert spec.enabled is True
    assert spec.operators == ["negate_compare", "flip_bool"]
    assert spec.max_mutants == 5
    assert spec.fail_below == 0.5


@pytest.mark.parametrize("block", [
    "  mutation: oops\n",                        # not a mapping
    "  mutation:\n    operators: [nope]\n",      # unknown operator
    "  mutation:\n    operators: flat\n",        # operators not a list
    "  mutation:\n    max_mutants: 0\n",         # non-positive cap
    "  mutation:\n    max_mutants: true\n",      # bool is not an int
    "  mutation:\n    fail_below: 1.5\n",        # out of range
    "  mutation:\n    fail_below: high\n",       # not numeric
    "  mutation:\n    enabled: yes-please\n",    # not a boolean
    "  mutation:\n    unknow: 1\n",              # typo'd key must not no-op
])
def test_invalid_mutation_raises_mission_error(tmp_path, block):
    text = ("mission:\n  name: m\n  goal: g\nverification:\n" + block)
    with pytest.raises(MissionError):
        _write_mission(tmp_path, text)


# ------------------------------------- task 3: orchestration + gating


class WriterAdapter(AgentAdapter):
    """Completed sends that drop files into the project (no git commits)."""

    name = "writer"
    verified = True

    def __init__(self, writes):
        super().__init__({})
        self.writes = writes

    def is_available(self):
        return True, ""

    def start_session(self, project_dir, session_id):
        return SessionInfo(session_id=session_id, project_dir=project_dir)

    def send(self, prompt, session):
        for rel, content in self.writes.items():
            path = Path(session.project_dir) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return AgentState(status="completed", logs="out")

    def cancel(self, session):
        pass


class CommittingAdapter(WriterAdapter):
    """Writer that also commits, so a git fixture ends clean pre-verify."""

    def send(self, prompt, session):
        super().send(prompt, session)
        subprocess.run(["git", "add", "."], cwd=session.project_dir, check=True)
        # Best-effort: identical rewrite => nothing to commit => rc 1.
        subprocess.run(["git", "commit", "-qm", "agent change"],
                       cwd=session.project_dir, check=False)
        return AgentState(status="completed", logs="out")


def _mission_text(commands, mutation="", max_attempts=1):
    mutation_block = f"{mutation}\n" if mutation else ""
    return (
        "mission:\n  name: m\n  goal: g\n"
        f"verification:\n  commands:\n    - {commands}\n{mutation_block}"
        + f"recovery:\n  max_attempts: {max_attempts}\nadapter: mock\n"
    )


def _run(tmp_path, adapter, text):
    mp = tmp_path / "m.yaml"
    mp.write_text(text, encoding="utf-8")
    cfg = TetherConfig(audit_dir=".tether/sessions")
    return Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))


MUTATION_ON = ("  mutation:\n    enabled: true")


def test_fail_below_gate_fails_attempt_on_zero_kill_rate(tmp_path):
    adapter = WriterAdapter({"mod.py": "flag = True\n"})
    report = _run(tmp_path, adapter, _mission_text(
        PASS_CMD, MUTATION_ON + "\n    fail_below: 0.5"))
    assert report["status"] == "failed"
    summary = report["mutation"]
    assert summary["killed"] == 0 and summary["survived"] > 0
    assert summary["kill_rate"] == 0.0
    assert any("kill_rate" in s and "fail_below" in s
               for s in report["next_steps"])
    session = find_session_dir(
        tmp_path, ".tether/sessions", report["session_id"])
    detail = json.loads(
        (session / "verification" / "mutation.json").read_text("utf-8"))
    assert detail and all(d["status"] == "survived" for d in detail)
    events = [json.loads(line) for line
              in (session / "events.jsonl").read_text("utf-8").splitlines()]
    assert any(e.get("kind") == "mutation" for e in events)


def test_advisory_mode_never_fails_despite_low_kill_rate(tmp_path):
    adapter = WriterAdapter({"mod.py": "flag = True\n"})
    report = _run(tmp_path, adapter, _mission_text(PASS_CMD, MUTATION_ON))
    assert report["status"] == "success"
    assert report["mutation"]["survived"] > 0
    assert report["mutation"]["kill_rate"] == 0.0


def test_strong_suite_passes_fail_below_gate(tmp_path):
    # Every mutant breaks the asserted behavior, so all are killed.
    cmd = py_cmd("import mod; assert mod.flag; assert mod.check(0)")
    adapter = WriterAdapter({
        "mod.py": "flag = True\ndef check(v):\n    return v <= 1\n"})
    report = _run(tmp_path, adapter, _mission_text(
        cmd, MUTATION_ON + "\n    fail_below: 0.9"))
    assert report["status"] == "success", report["next_steps"]
    assert report["mutation"]["killed"] > 0
    assert report["mutation"]["kill_rate"] == 1.0


def test_default_off_leaves_report_unchanged(tmp_path):
    adapter = WriterAdapter({"mod.py": "flag = True\n"})
    report = _run(tmp_path, adapter, _mission_text(PASS_CMD))
    assert report["status"] == "success"
    assert "mutation" not in report


def test_dry_run_records_mutation_as_skipped(tmp_path):
    adapter = WriterAdapter({"mod.py": "flag = True\n"})
    mp = tmp_path / "m.yaml"
    mp.write_text(_mission_text(PASS_CMD, MUTATION_ON), encoding="utf-8")
    cfg = TetherConfig(audit_dir=".tether/sessions", dry_run=True)
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "success"
    assert report["mutation"]["total"] == 0
    session = find_session_dir(
        tmp_path, ".tether/sessions", report["session_id"])
    assert not (session / "verification" / "mutation.json").exists()
    events = [json.loads(line) for line
              in (session / "events.jsonl").read_text("utf-8").splitlines()]
    skipped = [e for e in events if e.get("kind") == "mutation"]
    assert skipped and skipped[-1].get("status") == "skipped"


def test_mutation_targets_respect_sandbox_globs(tmp_path):
    text = (
        "mission:\n  name: m\n  goal: g\n"
        f"verification:\n  commands:\n    - {PASS_CMD}\n{MUTATION_ON}\n"
        "allowed_paths:\n  - src/**\n"
        "forbidden_paths:\n  - src/secret.py\n"
        "adapter: mock\n"
    )
    mp = tmp_path / "m.yaml"
    mp.write_text(text, encoding="utf-8")
    orch = Orchestrator(
        WriterAdapter({}), TetherConfig(audit_dir=".tether/sessions"),
        tmp_path)
    targets = orch._mutation_targets(
        load_mission(mp),
        ["src/ok.py", "mod.py", "notes.txt",
         "src/secret.py", ".tether/x.py"])
    # Only allowed, non-forbidden .py files are ever mutated.
    assert targets == ["src/ok.py"]


def test_git_fixture_tree_clean_after_mutation_run(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"],
                   cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"],
                   cwd=tmp_path, check=True)
    (tmp_path / "mod.py").write_text("base = 0\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".tether/\n", encoding="utf-8")
    mp = tmp_path / "m.yaml"
    mp.write_text(_mission_text(PASS_CMD, MUTATION_ON), encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"],
                   check=True)
    adapter = CommittingAdapter({"mod.py": "flag = True\n"})
    cfg = TetherConfig(audit_dir=".tether/sessions")
    report = Orchestrator(adapter, cfg, tmp_path).run(load_mission(mp))
    assert report["status"] == "success"
    assert report["mutation"]["total"] > 0
    # Byte-identical tree: the committed agent change is intact and mutation
    # testing restored every mutant it wrote.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path,
        capture_output=True, text=True, check=True).stdout
    assert status.strip() == ""
    assert (tmp_path / "mod.py").read_text(encoding="utf-8") == "flag = True\n"


# ------------------------------------------ task 4: telemetry + docs


def test_stats_and_json_carry_mutation_summary(tmp_path):
    from typer.testing import CliRunner

    from tether.cli import app
    runner = CliRunner()
    adapter = WriterAdapter({"mod.py": "flag = True\n"})
    report = _run(tmp_path, adapter, _mission_text(PASS_CMD, MUTATION_ON))
    assert report["status"] == "success"

    rj = runner.invoke(app, ["sessions", "stats", "--json",
                             "--project-dir", str(tmp_path)])
    assert rj.exit_code == 0, rj.output
    data = json.loads(rj.output)
    assert data["mutation"]["sessions_reporting"] == 1
    assert data["mutation"]["survived"] > 0
    assert data["mutation"]["kill_rate"] == 0.0

    rh = runner.invoke(app, ["sessions", "stats",
                             "--project-dir", str(tmp_path)])
    assert rh.exit_code == 0, rh.output
    assert "Mutation:" in rh.output
    assert "kill_rate" in rh.output


def test_stats_human_output_unchanged_without_mutation_sessions(tmp_path):
    from typer.testing import CliRunner

    from tether.cli import app
    runner = CliRunner()
    report = _run(tmp_path, WriterAdapter({}), _mission_text(PASS_CMD))
    assert report["status"] == "success"

    rj = runner.invoke(app, ["sessions", "stats", "--json",
                             "--project-dir", str(tmp_path)])
    assert rj.exit_code == 0, rj.output
    data = json.loads(rj.output)
    assert data["mutation"] == {
        "sessions_reporting": 0, "killed": 0, "survived": 0,
        "skipped": 0, "kill_rate": 0.0}

    rh = runner.invoke(app, ["sessions", "stats",
                             "--project-dir", str(tmp_path)])
    assert rh.exit_code == 0, rh.output
    assert "Mutation:" not in rh.output
    assert "kill_rate" not in rh.output


def test_readme_documents_mutation_testing():
    repo_root = Path(__file__).resolve().parent.parent
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    verification = readme.split("## Verification", 1)[1].split("## Dry-run")[0]
    assert "Mutation testing" in verification
    assert "mutation" in verification.lower()
    assert "kill_rate" in verification
    assert "negate_compare" in verification
    limitations = readme.split("## Current limitations", 1)[1]
    assert "Python-only" in limitations
    assert "advisory by default" in limitations
