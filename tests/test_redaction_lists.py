"""dogfood-16: explicit secret allow/denylists for redaction."""
import json
from pathlib import Path

from tether.adapters.mock import MockAdapter
from tether.audit import REDACTED, find_session_dir, redact_secrets
from tether.models import MissionContract, TetherConfig
from tether.orchestrator import Orchestrator


def test_denylist_forces_redaction_of_non_marker_key():
    data = {"note": "keep-me-out", "nested": {"Note": "still-hidden"},
            "other": "visible"}
    out = redact_secrets(data, denylist=["note"])
    assert out["note"] == REDACTED
    assert out["nested"]["Note"] == REDACTED  # exact, case-insensitive match
    assert out["other"] == "visible"


def test_allowlist_exempts_marker_key():
    data = {"api_key": "public-by-policy", "password": "s3cret"}
    out = redact_secrets(data, allowlist=["API_KEY"])
    assert out["api_key"] == "public-by-policy"
    assert out["password"] == REDACTED


def test_denylist_beats_allowlist_on_conflict():
    data = {"token": "t"}
    out = redact_secrets(data, denylist=["token"], allowlist=["token"])
    assert out["token"] == REDACTED


def test_empty_and_absent_lists_preserve_existing_behavior():
    data = {"secret": "s", "api_key": "k", "plain": "p",
            "env": {"A": "1", "B": None},
            "nothing": None, "empty_dict": {}, "empty_list": []}
    expected = {
        "secret": REDACTED,
        "api_key": REDACTED,
        "plain": "p",
        "env": {"A": REDACTED, "B": None},
        "nothing": None,
        "empty_dict": {},
        "empty_list": [],
    }
    assert redact_secrets(data) == expected
    assert redact_secrets(data) == redact_secrets(data, denylist=[],
                                                  allowlist=[])


def test_resolved_config_honors_operator_lists(tmp_path):
    """Operator configuration actually takes effect in resolved-config.json."""
    cfg = TetherConfig(
        audit_dir=".tether/sessions", dry_run=True,
        secret_denylist=["default_adapter"],
        secret_allowlist=["secret_denylist"],
    )
    mission = MissionContract(mission={}, name="lists", goal="g")
    report = Orchestrator(MockAdapter({}), cfg, tmp_path).run(mission)
    d = find_session_dir(tmp_path, ".tether/sessions",
                         report["session_id"])
    assert d is not None
    saved = json.loads(
        (d / "resolved-config.json").read_text(encoding="utf-8"))
    # Denylisted non-marker key is forced to redaction...
    assert saved["default_adapter"] == REDACTED
    # ...and the allowlisted marker key keeps its value visible.
    assert saved["secret_denylist"] == ["default_adapter"]
    # A marker key not on the allowlist is still redacted.
    assert saved["secret_allowlist"] == REDACTED


def test_readme_documents_the_redaction_lists():
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "secret_denylist" in readme
    assert "secret_allowlist" in readme
