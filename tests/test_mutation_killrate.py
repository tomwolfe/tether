"""Unit tests for tools/mutation_killrate.py (dogfood-40).

The suite runner is injected, so no pytest subprocess is spawned here; the
real end-to-end path is exercised by the dogfood-40 mission's verification
command against src/tether/cleanroom.py x tests/test_cleanroom.py.
"""
import hashlib
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import mutation_killrate  # noqa: E402

# Sites: negate_compare on ==, flip_bool on True, break_return on
# `return True`, flip_bool on False, break_return on `return False`.
SRC = (
    "def f(a):\n"
    "    if a == 1:\n"
    "        return True\n"
    "    return False\n"
)


def _pass_on(indexes):
    """Runner factory whose suite passes only for mutant ordinal positions."""

    def factory(repo_root, suites):
        state = {"n": 0}

        def run():
            i = state["n"]
            state["n"] += 1
            return i in indexes, f"call {i}"

        return run

    return factory


def _write_target(tmp_path):
    rel = Path("pkg") / "mod.py"
    (tmp_path / "pkg").mkdir()
    (tmp_path / rel).write_text(SRC, encoding="utf-8")
    return rel


def test_counts_killed_and_survived_with_restored_bytes(tmp_path):
    rel = _write_target(tmp_path)
    report = mutation_killrate.measure(
        rel, tmp_path, ["tests/x.py"], max_mutants=5,
        runner_factory=_pass_on({1}))
    assert report["generated"] == 5
    assert report["killed"] == 4
    assert report["survived"] == 1
    assert report["kill_rate"] == 0.8
    assert len(report["survivors"]) == 1
    # Mutant ordinal 1: visit() matches the Return node before its child
    # Constant, so it is break_return over `return True` on line 3.
    assert "[break_return]" in report["survivors"][0]
    assert report["survivors"][0].startswith("pkg/mod.py:3:")
    assert (tmp_path / rel).read_bytes() == SRC.encode("utf-8")


def test_crashed_suite_counts_as_killed_and_bytes_restore(tmp_path):
    rel = _write_target(tmp_path)

    def factory(repo_root, suites):
        def run():
            raise RuntimeError("boom")

        return run

    report = mutation_killrate.measure(
        rel, tmp_path, ["tests/x.py"], max_mutants=5,
        runner_factory=factory)
    assert report["killed"] == 5
    assert report["survived"] == 0
    assert report["kill_rate"] == 1.0
    assert (tmp_path / rel).read_bytes() == SRC.encode("utf-8")


def test_max_mutants_cap_is_deterministic_subset(tmp_path):
    rel = _write_target(tmp_path)
    first = mutation_killrate.measure(
        rel, tmp_path, ["tests/x.py"], max_mutants=3,
        runner_factory=_pass_on(set()))
    second = mutation_killrate.measure(
        rel, tmp_path, ["tests/x.py"], max_mutants=3,
        runner_factory=_pass_on(set()))
    assert first["generated"] == 3  # capped below the 5 total sites
    assert first["killed"] == 3 and first["survived"] == 0
    assert first["killed"] == second["killed"]


def test_max_mutants_zero_means_full_enumeration(tmp_path):
    # Task 2 pins "--max-mutants (0 = all sites)": 0 must measure every
    # mutable site (5 here), not merely a deterministic subset of it.
    rel = _write_target(tmp_path)
    capped = mutation_killrate.measure(
        rel, tmp_path, ["tests/x.py"], max_mutants=3,
        runner_factory=_pass_on(set()))
    full = mutation_killrate.measure(
        rel, tmp_path, ["tests/x.py"], max_mutants=0,
        runner_factory=_pass_on(set()))
    assert capped["generated"] == 3
    assert full["generated"] == 5 > capped["generated"]
    assert full["killed"] == 5 and full["survived"] == 0
    assert full["kill_rate"] == 1.0


def test_stable_seed_matches_production_derivation():
    expected = int.from_bytes(
        hashlib.sha256(b"src/tether/cleanroom.py").digest()[:8], "big")
    assert mutation_killrate.stable_seed("src/tether/cleanroom.py") == expected


def test_format_report_lists_survivors_and_none_case():
    base = {"target": "m.py", "suites": ["s.py"], "generated": 2,
            "killed": 1, "survived": 1, "kill_rate": 0.5,
            "survivors": ["m.py:1:4 [flip_bool] x = True"]}
    text = mutation_killrate.format_report(base)
    assert "0.5000" in text and "killed 1/2" in text
    assert "m.py:1:4 [flip_bool]" in text
    none = dict(base, survived=0, killed=2, kill_rate=1.0, survivors=[])
    assert "(none)" in mutation_killrate.format_report(none)


def test_main_gate_boundary_and_exit_codes(tmp_path, capsys, monkeypatch):
    rel = _write_target(tmp_path)
    seen: list[tuple[Path, list[str]]] = []

    def fake_default(repo_root, suites):
        seen.append((repo_root, list(suites)))
        return _pass_on({1})(repo_root, suites)

    monkeypatch.setattr(mutation_killrate, "default_suite_runner",
                        fake_default)
    rc = mutation_killrate.main([
        "--target", str(rel), "--suite", "tests/x.py",
        "--repo-root", str(tmp_path), "--max-mutants", "5",
        "--min-kill-rate", "0.8",
    ])
    assert rc == 0  # kill rate exactly at the gate passes
    rc = mutation_killrate.main([
        "--target", str(rel), "--suite", "tests/x.py",
        "--repo-root", str(tmp_path), "--max-mutants", "5",
        "--min-kill-rate", "0.9",
    ])
    assert rc == 2  # below the gate fails distinctly
    out = capsys.readouterr().out
    assert "0.8000" in out
    assert seen == [(tmp_path, ["tests/x.py"]),
                    (tmp_path, ["tests/x.py"])]


def test_main_missing_target_is_usage_error(tmp_path):
    assert mutation_killrate.main([
        "--target", "no/such.py", "--suite", "tests/x.py",
        "--repo-root", str(tmp_path),
    ]) == 1


def test_main_rejects_non_py_target(tmp_path):
    # The tool mutates ONE --target .py file (Task 2 contract): a non-.py
    # target is a harness error (exit 1), never silently mutated.
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.txt").write_text(SRC, encoding="utf-8")
    assert mutation_killrate.main([
        "--target", "pkg/mod.txt", "--suite", "tests/x.py",
        "--repo-root", str(tmp_path),
    ]) == 1


def test_gate_semantics_are_inclusive():
    assert (0.8 >= 0.8) is True
    assert (0.79 >= 0.8) is False
