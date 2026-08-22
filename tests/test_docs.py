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


def test_security_doc_says_scrub_is_not_erasure():
    security = (REPO_ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8")
    leakage = security.split("### Secret leakage", 1)[1].split("### Verification", 1)[0]
    assert "best-effort" in leakage
    assert "scrub" in leakage
    assert "cryptographic erasure" in leakage
