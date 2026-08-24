"""Documentation truth: docs must describe the modules that actually ship."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_architecture_module_map_lists_agent_tooling_modules():
    text = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    module_map = text.split("## Module map", 1)[1].split("```", 2)[1]
    for module in ("smoke.py", "conformance.py", "certify.py"):
        assert module in module_map, module


def test_readme_non_git_limitation_states_fingerprint_truth():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "HASH_SIZE_LIMIT" in readme
    assert "sha256" in readme


# ------------------- documentation truth (dogfood-19 task 5)


def test_readme_documents_verification_assertions():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    verification = readme.split("## Verification", 1)[1].split("## Dry-run", 1)[0]
    assert "assertions" in verification
    assert "min_occurrences" in verification
    assert '"contains"' in verification or "contains:" in verification
    assert '"matches"' in verification or "matches:" in verification
    # dogfood-20: behavioral probes are documented under Verification
    assert "probes" in verification


def test_readme_documents_probes_and_usage_patterns():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "probes" in readme
    assert "usage_patterns" in readme


def test_readme_documents_secret_scrubbing_and_per_mission_stats():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    audit_section = readme.split("## Audit trail", 1)[1]
    # scrub note lives under the audit trail section
    assert "scrub" in audit_section.split("## Review gate", 1)[0]
    assert "sessions scrub" in readme
    # sessions stats description mentions per-mission baselines
    stats_paragraph = next(line for line in readme.splitlines()
                           if line.startswith("`tether sessions stats`"))
    assert "per-mission baselines" in stats_paragraph


def test_readme_recommends_enforce_for_allowed_paths():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    sandbox = readme.split("## Sandbox modes", 1)[1].split("## Change capture", 1)[0]
    assert "prefer `sandbox_mode: enforce`" in sandbox


# ------------------- documentation truth (dogfood-21 task 5)


def test_readme_documents_budgets():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "budget" in readme.lower()
    budgets = readme.split("## Budgets", 1)[1].split("\n## ", 1)[0]
    assert "max_wall_seconds" in budgets
    assert "max_sends" in budgets
    assert "max_usage" in budgets
    assert "cumulative_usage" in budgets
    assert "EXIT_BUDGET_EXCEEDED" in budgets
    # limitation: metric caps need the adapter to report the metric
    limitations = readme.split("## Current limitations", 1)[1]
    assert "budget" in limitations.lower()
    assert "never reports" in limitations


def test_security_doc_says_scrub_is_not_erasure():
    security = (REPO_ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8")
    leakage = security.split("### Secret leakage", 1)[1].split("### Verification", 1)[0]
    assert "best-effort" in leakage
    assert "scrub" in leakage
    assert "cryptographic erasure" in leakage


# ------------------- documentation truth (dogfood-29)


def _config_keys_sentence() -> str:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Configuration precedence", 1)[1]
    return next(line for line in section.splitlines()
                if line.startswith("Config keys:"))


def test_readme_config_keys_list_is_complete():
    from tether.models import TetherConfig
    sentence = _config_keys_sentence()
    for key in TetherConfig.model_fields:
        assert f"`{key}`" in sentence, f"config key missing from README: {key}"


def test_documented_defaults_match_tether_config():
    from tether.cli import DEFAULT_CONFIG_TEMPLATE
    from tether.models import TetherConfig
    cfg = TetherConfig()
    assert cfg.default_adapter == "mock"
    assert cfg.audit_dir == ".tether/sessions"
    assert cfg.backup_dir == ".tether/backups"
    assert cfg.dry_run is False
    assert cfg.log_level == "INFO"
    assert cfg.command_timeout_seconds == 1800
    assert cfg.verification_timeout_seconds == 600
    assert cfg.max_attempts == 3
    assert cfg.allow_dirty is False
    assert cfg.auto_rollback is False
    assert cfg.redact_prompts is False
    assert cfg.sandbox_mode == "warn"
    assert cfg.writer_lock_stale_seconds == 43200
    assert cfg.retention_days is None
    # `tether init` writes a starter config whose values are real defaults.
    for expected in (
        f"default_adapter: {cfg.default_adapter}",
        f"audit_dir: {cfg.audit_dir}",
        f"dry_run: {cfg.dry_run}".lower(),
        f"log_level: {cfg.log_level}",
        f"command_timeout_seconds: {cfg.command_timeout_seconds}",
        f"verification_timeout_seconds: {cfg.verification_timeout_seconds}",
        f"max_attempts: {cfg.max_attempts}",
    ):
        assert expected in DEFAULT_CONFIG_TEMPLATE


def test_documented_hard_limits_match_constants():
    from tether import orchestrator
    from tether.context_files import (
        BINARY_SNIFF_BYTES,
        CONTEXT_FILES_MAX_COUNT,
        CONTEXT_FILES_MAX_FILE_BYTES,
        CONTEXT_FILES_TOTAL_MAX_BYTES,
    )
    from tether.manifest import HASH_SIZE_LIMIT
    from tether.models import MutationSpec
    from tether.verification import REPAIR_OUTPUT_BUDGET

    # README "Context files" section numbers.
    assert CONTEXT_FILES_MAX_COUNT == 32
    assert CONTEXT_FILES_MAX_FILE_BYTES == 256 * 1024
    assert CONTEXT_FILES_TOTAL_MAX_BYTES == 512 * 1024
    assert BINARY_SNIFF_BYTES == 8192  # NUL sniff window: first 8 KiB
    # README limitations: non-git fingerprint boundary.
    assert HASH_SIZE_LIMIT == 1024 * 1024  # 1 MiB
    # README recovery/repair prompts: ~8KB output budget.
    assert REPAIR_OUTPUT_BUDGET == 8192
    # README review gate: ~4KB excerpt vs full-context 64 KiB caps.
    assert orchestrator.REVIEW_EXCERPT_BUDGET == REPAIR_OUTPUT_BUDGET // 2
    assert orchestrator.REVIEW_FULL_CONTEXT_BUDGET == 64 * 1024
    # README mutation example: per-file cap defaults to 20.
    assert MutationSpec().max_mutants == 20

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "**max 32 files**, **max 256 KiB per file**, **max 512 KiB total context**" in readme
    assert "(~8KB budget)" in readme
    assert "up to 64 KiB" in readme
    assert "~4KB excerpt" in readme
    assert "max_mutants: 20" in readme


def test_recovery_max_attempts_cap_is_20():
    import pytest
    from pydantic import ValidationError
    from tether.models import RecoverySpec
    assert RecoverySpec().strategy == "cumulative"
    with pytest.raises(ValidationError):
        RecoverySpec(max_attempts=21)


def test_review_gate_documented_defaults_match_spec():
    from tether.models import ReviewSpec
    spec = ReviewSpec()
    assert spec.enabled is False
    assert spec.required is True
    assert spec.adapter is None
    assert spec.retry_on_rejection is False
    assert spec.context == "excerpt"
    assert spec.credibility_probe is None
    from tether.orchestrator import REVIEWER_CREDIBILITY_FAILURE
    assert REVIEWER_CREDIBILITY_FAILURE == "reviewer credibility check failed"


def test_smoke_default_prompt_matches_docs():
    from tether.smoke import DEFAULT_PROMPT
    assert DEFAULT_PROMPT == "Reply with the single word OK"
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "`Reply with the single word OK`" in readme


def test_run_exit_codes_match_docs():
    from tether.cli import (
        EXIT_BUDGET_EXCEEDED,
        EXIT_CANCELLED,
        EXIT_FAILED,
        EXIT_REJECTED,
        EXIT_SANDBOX_VIOLATION,
        EXIT_SUCCESS,
    )
    assert (EXIT_SUCCESS, EXIT_FAILED, EXIT_CANCELLED, EXIT_REJECTED,
            EXIT_SANDBOX_VIOLATION, EXIT_BUDGET_EXCEEDED) == (0, 1, 2, 3, 4, 5)
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "exits with code 5 (`EXIT_BUDGET_EXCEEDED`)" in readme
    assert "CLI exit code 2" in readme


def test_adapters_doc_preset_commands_match_code():
    from tether.adapters.experimental import OpencodeAdapter, PiAdapter
    doc = (REPO_ROOT / "docs" / "ADAPTERS.md").read_text(encoding="utf-8")
    lines = doc.splitlines()
    opencode_line = next(line for line in lines if line.startswith("- opencode:"))
    pi_line = next(line for line in lines if line.startswith("- pi:"))
    for part in OpencodeAdapter().command:
        assert f'"{part}"' in opencode_line, part
    for part in PiAdapter().command:
        assert f'"{part}"' in pi_line, part


def test_clean_room_copy_doc_exclusions_match_implementation():
    """The README clean-room 'carries into the room' list must claim exactly
    the exclusions materialize_clean_room enforces (.git/.tether tops,
    gitignored untracked entries, outside-project paths) -- no more."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    clean_room = readme.split("## Verification", 1)[1].split("## Dry-run", 1)[0]
    carries = clean_room.split("What carries into the clean room:", 1)[1] \
                        .split("What deliberately does NOT carry over", 1)[0]
    assert "except gitignored ones" in carries
    for phrase in (".git/", ".tether/", "outside the project directory"):
        assert phrase in carries, phrase
    # The sandbox globs are NOT consulted by the materializer; docs must not
    # claim they are.
    assert "sandbox-forbidden paths are never copied" not in carries


def test_probe_doc_states_marker_self_match_pitfall():
    """The README probe guidance must carry the dogfood-28 authoring
    caveat: a python -c one-liner's AssertionError traceback echoes the
    whole -c source, so a literally-written success marker self-matches
    and the failed probe still passes; markers must be fragment-assembled."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "probe-marker self-match" in readme
    assert "echoes the entire `-c` source line" in readme
    assert "assemble them from string fragments" in readme
