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
