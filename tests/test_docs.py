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


# ------------------- documentation truth (dogfood-29 audit)


def test_strict_mission_key_lists_in_docs_match_mission_error(tmp_path):
    """README.md and docs/ARCHITECTURE.md both quote the dogfood-25
    strict-mission-block hint as two key lists; those lists must equal the
    keys in the actual MissionError message, so the docs cannot drift."""
    import re

    import pytest

    from tether.mission import MissionError, load_mission
    bad = tmp_path / "misnested.yaml"
    bad.write_text("mission:\n  name: x\n  goal: y\n  verification: {}\n",
                   encoding="utf-8")
    with pytest.raises(MissionError) as excinfo:
        load_mission(bad)
    message = str(excinfo.value)

    pattern = re.compile(
        r"contract-level blocks \(([^)]*)\) and free-form content \(([^)]*)\)")
    documented: set[str] = set()
    for name in ("README.md", "docs/ARCHITECTURE.md"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        m = pattern.search(text)
        assert m, f"strict-mission key-list sentence missing from {name}"
        for group in m.groups():
            documented.update(re.findall(r"`([A-Za-z_]+)`", group))
    assert documented == {
        "verification", "recovery", "review", "budget", "adapter",
        "adapters", "allowed_paths", "forbidden_paths",
        "tasks", "context", "constraints", "context_files",
    }
    for key in sorted(documented):
        assert f"'{key}'" in message, key


def test_allow_dirty_is_config_cli_only_as_documented():
    """docs/ARCHITECTURE.md scopes the dirty-tree abort's allow_dirty to
    config/CLI precedence: the mission contract has no such key."""
    from tether.models import MissionContract, TetherConfig
    assert "allow_dirty" in TetherConfig.model_fields
    assert "allow_dirty" not in MissionContract.model_fields
    arch = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    step = next(line for line in arch.splitlines()
                if "aborts the mission before any adapter call" in line)
    assert "(config/CLI precedence applies" in step
    assert "mission/config/CLI" not in step


def test_recovery_strategy_is_mission_only_as_documented(tmp_path):
    """docs/ARCHITECTURE.md: recovery.strategy is mission-only -- project
    config has no such key (unknown top-level config keys are rejected)."""
    import pytest

    from tether.config import resolve_config
    from tether.models import RecoverySpec, TetherConfig
    assert "recovery" not in TetherConfig.model_fields
    assert RecoverySpec().strategy == "cumulative"
    (tmp_path / "tether.yaml").write_text("recovery:\n  strategy: cumulative\n",
                                          encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown config keys"):
        resolve_config(tmp_path)


def test_security_doc_non_git_restore_bullet_matches_implementation():
    """The SECURITY.md rollback-limits bullet must describe what
    restore_from_backup actually does: post-backup files are kept and
    reported (tests/test_safety.py pins the behavior), never lost."""
    security = (REPO_ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8")
    limits = security.split("## Rollback limits", 1)[1].split("\n## ", 1)[0]
    bullet_start = limits.index("- Non-git restores")
    bullet = limits[bullet_start:limits.index("\n-", bullet_start + 1)]
    assert "sha256" in bullet
    assert "created *after* the backup are kept" in bullet
    assert "are lost" not in bullet
    assert "refuses restore" in bullet


# ------------------- documentation truth (dogfood-35 ops pipeline)


def test_readme_scrub_documents_chain_extension_and_bounded_scope():
    """The ops pipeline docs must tie scrubbing to chain integrity: the
    README scrub section promises the appended scrub event plus strict
    per-session scope, and SECURITY.md's audit-chain section names the
    `logs <id> --verify` command that proves the chain afterwards."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    scrub = readme.split("### Secret scrubbing", 1)[1].split("\n## ", 1)[0]
    assert "appends a `scrub` event to `events.jsonl`" in scrub
    assert "[REDACTED" in scrub
    assert "Without `--confirm` it prints a plan" in scrub
    assert "never reads or writes outside the session directory" in scrub
    security = (REPO_ROOT / "docs" / "SECURITY.md").read_text(
        encoding="utf-8")
    chain = security.split("## Audit chain", 1)[1].split("\n## ", 1)[0]
    assert "`tether logs <session-id> --verify`" in chain
    assert "SHA-256 hash chain" in chain
    assert "tamper-evident, not tamper-proof" in chain


def test_readme_stats_documents_malformed_session_resilience():
    """The stats paragraph must document that malformed/truncated/half-
    written session directories are skipped gracefully and never corrupt
    the aggregates of valid sessions."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    paragraph = next(line for line in readme.splitlines()
                     if line.startswith("`tether sessions stats`"))
    assert "malformed, truncated, or half-written session directories" \
        in paragraph
    assert "skipped gracefully instead of failing the command" in paragraph
    assert "never corrupt the aggregates of valid sessions" in paragraph
    # Pre-existing pins on the same paragraph stay true.
    assert "per-mission baselines" in paragraph


def test_readme_clean_documents_confirm_requirement_and_retention():
    """The clean command's docs must make the --confirm requirement and the
    retention_days fallback unmissable: the quick tour shows the preview
    (nothing deleted) vs --confirm pair, and the config-keys sentence names
    retention_days as the --older-than fallback."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    tour = readme.split("## Quick tour", 1)[1].split("\n## ", 1)[0]
    assert "# preview old-session cleanup (nothing deleted)" in tour
    assert ("tether sessions clean --older-than 30d --confirm   "
            "# delete session dirs older than 30 days") in tour
    keys_line = next(line for line in readme.splitlines()
                     if line.startswith("Config keys:"))
    assert ("`retention_days` (used by `sessions clean` when "
            "`--older-than` is omitted)") in keys_line


# ------------------- documentation truth (dogfood-29/36)


def _click_app(typer_app):
    from typer.main import get_command
    return get_command(typer_app)


def test_documented_cli_commands_exist_in_click_app():
    """Every command/subcommand shown in the README quick tour must exist
    in the real Typer/click app."""
    from tether.cli import adapters_app, app, sessions_app
    root = _click_app(app)
    for name in ("init", "validate-config", "validate-mission", "run",
                 "rollback", "report", "diff", "logs", "adapters",
                 "sessions"):
        assert name in root.commands, name
    assert {"list", "smoke", "conformance", "certify"} \
        <= set(_click_app(adapters_app).commands)
    assert {"list", "show", "stats", "clean", "scrub"} \
        <= set(_click_app(sessions_app).commands)


def test_documented_cli_flags_exist():
    """Flags the README documents on specific commands must exist."""
    from tether.cli import app
    commands = _click_app(app).commands

    def opts(name: str) -> set[str]:
        return {o for p in commands[name].params
                for o in (*p.opts, *p.secondary_opts)}

    run_opts = opts("run")
    for flag in ("--adapter", "--project-dir", "--dry-run", "--no-dry-run",
                 "--max-attempts", "--allow-dirty", "--no-allow-dirty",
                 "--auto-rollback", "--no-auto-rollback", "--strict",
                 "--verbose"):
        assert flag in run_opts, flag
    assert "--patch" in opts("diff")
    assert "--verify" in opts("logs")
    assert "--clean" in opts("rollback")
    assert "--strict" in opts("validate-mission")


def test_documented_session_subcommand_flags_exist():
    from tether.cli import sessions_app
    commands = _click_app(sessions_app).commands

    def opts(name: str) -> set[str]:
        return {o for p in commands[name].params
                for o in (*p.opts, *p.secondary_opts)}

    assert {"--older-than", "--confirm"} <= opts("clean")
    assert "--json" in opts("stats")
    assert "--confirm" in opts("scrub")


def test_documented_adapter_setting_names_match_known_settings():
    """The settings README attributes to each adapter are exactly the keys
    the adapter declares as known (unknown-key warnings rely on this)."""
    from tether.adapters.command import CommandAdapter
    from tether.adapters.mock import MockAdapter
    assert CommandAdapter.known_settings == frozenset(
        {"command", "timeout_seconds", "prompt_via_stdin", "env",
         "usage_patterns"})
    assert MockAdapter.known_settings == frozenset({"scenario"})
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    sentence = next(line for line in readme.splitlines()
                    if "unknown setting" in line)
    for key in sorted(CommandAdapter.known_settings):
        assert f"`{key}`" in sentence, key
    assert "`scenario` for mock" in sentence


def test_retries_spec_defaults_match_docs():
    from tether.models import RetriesSpec, TetherConfig
    spec = RetriesSpec()
    assert spec.max_transient_retries == 2
    assert spec.transient_backoff_seconds == 10
    assert TetherConfig().retries.max_transient_retries == 2
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    retries = readme.split("### Transient provider failures", 1)[1] \
                    .split("\n## ", 1)[0]
    assert "2 => up to 3 total" in retries
    assert "flat wait between retries" in retries
    arch = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert ("`retries.max_transient_retries` default 2 extra attempts / "
            "`retries.transient_backoff_seconds` default 10") in arch


def test_sandbox_mode_values_and_advisory_event_match_code():
    import typing

    from tether.models import TetherConfig
    annotation = TetherConfig.model_fields["sandbox_mode"].annotation
    assert typing.get_args(annotation) == ("warn", "enforce")
    assert TetherConfig().sandbox_mode == "warn"
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "sandbox_mode_advisory" in readme
    arch = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    sandbox = arch.split("## Sandbox modes", 1)[1].split("## Design rules",
                                                         1)[0]
    assert "`warn` (default)" in sandbox and "`enforce`" in sandbox


def test_review_consensus_options_match_review_spec():
    import typing

    from tether.models import ReviewSpec
    spec = ReviewSpec()
    assert spec.reviewers is None
    assert spec.consensus == "all"
    assert typing.get_args(ReviewSpec.model_fields["consensus"].annotation) \
        == ("all", "majority")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    consensus = readme.split("**Multi-reviewer consensus**", 1)[1]
    assert "requires unanimous approval" in consensus
    assert "strictly more approvals than rejections" in consensus


def test_retention_days_contract_matches_clean_fallback():
    import pytest

    from pydantic import ValidationError

    from tether.models import TetherConfig
    assert TetherConfig().retention_days is None
    assert TetherConfig(retention_days=0).retention_days == 0
    with pytest.raises(ValidationError):
        TetherConfig(retention_days=-1)


def test_mutation_operator_names_match_verification_module():
    from tether.verification import MUTATION_OPERATORS
    assert MUTATION_OPERATORS == (
        "negate_compare", "flip_bool", "arithmetic", "break_return")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    mutation = readme.split("### Mutation testing", 1)[1] \
                     .split("### Clean-room verification", 1)[0]
    for operator in MUTATION_OPERATORS:
        assert operator in mutation, operator


def test_conformance_check_names_match_doc():
    from tether import conformance
    names = {
        conformance._AVAILABILITY, conformance._SUCCESS,
        conformance._LOGS, conformance._FAILURE, conformance._TIMEOUT,
        conformance._CANCEL, conformance._SPAWN, conformance._PROJECT_DIR,
    }
    assert len(names) == 8
    doc = (REPO_ROOT / "docs" / "ADAPTERS.md").read_text(encoding="utf-8")
    checks = doc.split("Checks:", 1)[1].split("Notes:", 1)[0]
    for name in names:
        assert f"`{name}`" in checks, name


def test_adapters_capability_table_matches_classes():
    doc = (REPO_ROOT / "docs" / "ADAPTERS.md").read_text(encoding="utf-8")
    table = doc.split("| Adapter | cancel |", 1)[1].split("\n\n", 1)[0]

    def row(name: str) -> list[str]:
        line = next(ln for ln in table.splitlines()
                    if ln.startswith(f"| {name} |"))
        return [c.strip() for c in line.strip("|").split("|")]

    def flag(cell: str) -> bool:
        assert cell.rstrip("*") in ("yes", "no", "opt-in"), cell
        return cell.rstrip("*") != "no"

    from tether.adapters.command import CommandAdapter
    from tether.adapters.experimental import OpencodeAdapter, PiAdapter
    from tether.adapters.mock import MockAdapter
    classes = {
        "mock": MockAdapter,
        "command": CommandAdapter,
        "opencode": OpencodeAdapter,
        "pi": PiAdapter,
    }
    maturity = {"mock": "verified", "command": "verified",
                "opencode": "verified", "pi": "experimental"}
    for name, cls in classes.items():
        cells = row(name)
        assert flag(cells[1]) == cls.supports_cancel, name
        assert flag(cells[2]) == cls.supports_process_tree_kill, name
        assert flag(cells[3]) == cls.supports_usage, name
        assert flag(cells[4]) == cls.supports_streaming, name
        assert flag(cells[5]) == cls.one_shot, name
        assert cells[6] == maturity[name], name
        assert cls.verified == (maturity[name] == "verified"), name


def test_command_adapter_env_vars_and_spawn_flags_documented():
    import inspect

    from tether.adapters import command
    src = inspect.getsource(command)
    doc = (REPO_ROOT / "docs" / "ADAPTERS.md").read_text(encoding="utf-8")
    for var in ("TETHER_SESSION_ID", "TETHER_PROJECT_DIR",
                "TETHER_MISSION"):
        assert var in src and f"`{var}`" in doc, var
    for spawn_flag in ("start_new_session", "CREATE_NEW_PROCESS_GROUP"):
        assert spawn_flag in src, spawn_flag
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    containment = readme.split("## Process containment", 1)[1] \
                        .split("\n## ", 1)[0]
    assert "start_new_session=True" in containment
    assert "CREATE_NEW_PROCESS_GROUP" in containment


def test_limitations_sections_mention_reader_straggler_note():
    """dogfood-33/34: both limitations sections must carry the straggler
    caveat, and the constant the README names must really exist."""
    from tether.adapters.command import READER_JOIN_GRACE_SECONDS
    assert READER_JOIN_GRACE_SECONDS > 0
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    limitations = readme.split("## Current limitations", 1)[1]
    assert "command.READER_JOIN_GRACE_SECONDS" in limitations
    assert "straggler" in limitations
    doc = (REPO_ROOT / "docs" / "ADAPTERS.md").read_text(encoding="utf-8")
    limits = doc.split("Current limitations (accepted-by-design)", 1)[1]
    assert "straggler" in limits
    assert "daemon reader threads" in limits


def test_adapters_doc_pins_exact_streaming_straggler_bound():
    """dogfood-37: ADAPTERS.md must state the precise straggler mechanism
    and numbers -- daemon reader threads, the 2.0s READER_JOIN_GRACE_SECONDS
    join grace, possible truncation after the grace expires, fds that stay
    open until the straggler exits or is reaped -- instead of the vague
    "bounded in practice" phrasing, and the named constant must be 2.0."""
    from tether.adapters.command import READER_JOIN_GRACE_SECONDS
    assert READER_JOIN_GRACE_SECONDS == 2.0
    doc = (REPO_ROOT / "docs" / "ADAPTERS.md").read_text(encoding="utf-8")
    assert "bounded in practice" not in doc
    limits = doc.split("Current limitations (accepted-by-design)", 1)[1]
    assert "daemon reader threads" in limits
    assert "READER_JOIN_GRACE_SECONDS = 2.0" in limits
    assert "may be truncated" in limits
    assert "fds stay open until the straggler exits or is reaped" in limits
    assert "tests/test_reader_straggler.py" in limits


def test_security_writer_lock_claim_matches_implementation():
    import inspect

    from tether import orchestrator
    src = inspect.getsource(orchestrator)
    assert "O_CREAT" in src and "O_EXCL" in src
    security = (REPO_ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8")
    lock = security.split("## Local lock scope", 1)[1].split("\n## ", 1)[0]
    assert "O_CREAT|O_EXCL" in lock
    assert "stale takeover" in lock


def test_readme_dev_extra_matches_pyproject():
    import tomllib

    data = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = " ".join(data["project"]["optional-dependencies"]["dev"])
    assert data["project"]["requires-python"] == ">=3.11"
    for extra in ("pytest>=8", "pytest-cov", "ruff", "mypy",
                  "types-PyYAML"):
        assert extra in dev
    deps = " ".join(data["project"]["dependencies"])
    for dep in ("typer>=", "pydantic>=", "pyyaml>="):
        assert dep in deps


# ------------------- documentation truth (dogfood-40)


def test_mutation_killrate_tool_interface_matches_architecture_doc():
    """docs-truth pin: ARCHITECTURE.md's dogfood-40 section describes the
    real tool interface and the pinned cleanroom kill-rate gate."""
    tool = (REPO_ROOT / "tools" / "mutation_killrate.py").read_text(
        encoding="utf-8")
    for flag in ("--target", "--suite", "--max-mutants",
                 "--min-kill-rate"):
        assert f'"{flag}"' in tool, flag
    arch = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(
        encoding="utf-8")
    section = arch.split("## Quantitative verification strength", 1)[1] \
                  .split("\n## ", 1)[0]
    assert "tools/mutation_killrate.py" in section
    assert "--min-kill-rate" in section
    assert "exit code 2" in section and "exit code 1" in section
    assert "src/tether/cleanroom.py" in section
    assert "tests/test_cleanroom.py" in section
    assert "0.80" in section


def test_cleanroom_survivor_probes_are_justified_and_equivalents_documented():
    """docs-truth pin: every probe names its killed mutant, and the four
    equivalent survivors are documented rather than probed."""
    tests_src = (REPO_ROOT / "tests" / "test_cleanroom.py").read_text(
        encoding="utf-8")
    section = tests_src.split("task 5: mutation-survivor probes", 1)[1]
    for site in ("66:8", "83:15", "91:11", "122:27", "122:42", "191:54",
                 "194:44", "194:59"):
        assert f"Kills {site}" in section or site in section, site
    for equiv in ("74:8", "76:8", "83:8", "85:8"):
        assert equiv in section, equiv


def test_mission_pins_cleanroom_gate_as_verification_command():
    """The quantitative criterion is enforced by the mission itself."""
    mission = (REPO_ROOT / "missions" /
               "dogfood-40-cleanroom-killrate-closure.yaml").read_text(
                   encoding="utf-8")
    assert "mutation_killrate.py --target src/tether/cleanroom.py" \
        in mission
    assert "--min-kill-rate 0.8" in mission


def test_dogfooding_records_the_dogfood40_audit():
    """Task 3 requires the audit to be RECORDED in docs/DOGFOODING.md and
    pinned here alongside the ARCHITECTURE.md gate documentation: the
    record must name the tool, both audit targets, and the measured
    post-mission kill rate."""
    doc = (REPO_ROOT / "docs" / "DOGFOODING.md").read_text(encoding="utf-8")
    section = doc.split(
        "## Clean-room mutation strength audit (dogfood-40)", 1)[1]
    assert "tools/mutation_killrate.py" in section
    assert "src/tether/cleanroom.py" in section
    assert "tests/test_cleanroom.py" in section
    assert "0.92" in section


def test_architecture_step12_documents_ansi_stripping_and_reason_rule():
    """dogfood-40 v2 docs-truth pin: the review-gate step must state that
    verdict scanning strips ANSI escape sequences before scanning, and how
    the recorded reason is extracted (decisive line's post-token remainder
    preferred; otherwise the first substantive line past blank/escape-only
    lines). The fail-safe last-marker contract stays pinned too."""
    arch = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    step12 = next(line for line in arch.splitlines()
                  if line.startswith("12. Review gate")).lower()
    assert "after ansi escape sequences are stripped first" in step12
    assert "last line beginning with `review: approve` or " \
        "`review: request_changes` decides" in step12
    assert "remainder after the verdict token" in step12
    assert "first substantive line after the marker" in step12
    assert "skipping blank/escape-only lines" in step12


def test_architecture_documents_git_state_guard():
    """dogfood-41 docs-truth pin: the sandbox documentation must state the
    new contract key, its audit event, and the byte-identical default."""
    arch = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(
        encoding="utf-8")
    section = arch.split("## Sandbox modes", 1)[1].split("\n## ", 1)[0]
    assert "`git_state_guard: true`" in section
    assert "`git_state_violations`" in section
    assert "byte-identical" in section
    # Strict semantics named explicitly: HEAD drift AND checkpoint-ref
    # integrity, checked after every send.
    assert "HEAD" in section
    assert "checkpoint ref" in section


def test_dogfood_missions_from_42_on_enable_git_state_guard():
    """dogfood-42 adoption policy: every self-hosting dogfood mission from
    number 42 onward must run under the guard it helped prove."""
    import re
    for yml in sorted((REPO_ROOT / "missions").glob("dogfood-*.yaml")):
        m = re.match(r"dogfood-(\d+)-", yml.name)
        if not m or int(m.group(1)) < 42:
            continue
        text = yml.read_text(encoding="utf-8")
        assert re.search(r"(?m)^git_state_guard:\s*true\s*$", text), \
            f"{yml.name} must set git_state_guard: true"


def test_docs_document_hook_integrity_and_guard_adoption_policy():
    """dogfood-42 docs-truth pin: ARCHITECTURE's guard paragraph covers
    .git/hooks and core.hooksPath integrity; DOGFOODING states the
    adoption policy for all future dogfood missions."""
    arch = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(
        encoding="utf-8")
    section = arch.split("## Sandbox modes", 1)[1].split("\n## ", 1)[0]
    assert ".git/hooks" in section
    assert "core.hooksPath" in section
    dog = (REPO_ROOT / "docs" / "DOGFOODING.md").read_text(
        encoding="utf-8")
    assert "every dogfood mission" in dog and \
        "`git_state_guard: true`" in dog
