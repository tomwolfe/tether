"""dogfood-16: explicit secret_denylist / secret_allowlist for redaction."""
import json
from pathlib import Path

from tether.audit import REDACTED, find_session_dir, redact_secrets
from tether.adapters.mock import MockAdapter
from tether.models import MissionContract, TetherConfig
from tether.orchestrator import Orchestrator


# ------------------------------------------------- unit: redact_secrets lists


def test_denylist_forces_redaction_of_non_marker_key():
    obj = {"vault_path": "/run/secrets/vault", "nested": {"vault_path": "x"}}
    out = redact_secrets(obj, denylist=["vault_path"])
    assert out["vault_path"] == REDACTED
    assert out["nested"]["vault_path"] == REDACTED
    # input untouched
    assert obj["vault_path"] == "/run/secrets/vault"


def test_allowlist_exempts_marker_key():
    obj = {"api_key": "k", "token_label": "keep me"}
    out = redact_secrets(obj, allowlist=["token_label"])
    assert out["api_key"] == REDACTED        # other markers still apply
    assert out["token_label"] == "keep me"   # exempted despite 'token'


def test_denylist_beats_allowlist_on_conflict():
    obj = {"api_key": "k"}
    out = redact_secrets(obj, denylist=["API_KEY"], allowlist=["api_key"])
    assert out["api_key"] == REDACTED


def test_lists_match_exactly_and_case_insensitively():
    obj = {"SecretName": "s", "credentials_file": "c", "note_secret": "n"}
    out = redact_secrets(
        obj,
        denylist=["secretname"],           # exact: must NOT hit note_secret
        allowlist=["CREDENTIALS_FILE"],
    )
    assert out["SecretName"] == REDACTED       # denylist matched caselessly
    assert out["credentials_file"] == "c"      # allowlist matched caselessly
    assert out["note_secret"] == REDACTED      # built-in marker unaffected


def test_env_block_stays_redacted_regardless_of_lists():
    obj = {"env": {"A": "x"}, "auth": "y"}
    out = redact_secrets(obj, allowlist=["env", "auth"])
    assert out["env"]["A"] == REDACTED
    assert out["auth"] == "y"


def test_empty_and_absent_lists_preserve_default_behavior():
    obj = {
        "api_key": "k",
        "name": "n",
        "note": None,
        "empty_dict": {},
        "empty_list": [],
        "env": {"HOME": "/h", "NUL": None},
        "deep": [{"password": "p"}, "plain"],
    }
    expected = {
        "api_key": REDACTED,
        "name": "n",
        "note": None,
        "empty_dict": {},
        "empty_list": [],
        "env": {"HOME": REDACTED, "NUL": None},
        "deep": [{"password": REDACTED}, "plain"],
    }
    assert redact_secrets(obj) == expected
    assert redact_secrets(obj, denylist=[], allowlist=[]) == expected


# ------------------------------------------------ config model + wiring


def test_config_defaults_are_empty_lists():
    cfg = TetherConfig()
    assert cfg.secret_denylist == []
    assert cfg.secret_allowlist == []


def _resolved_config(tmp_path, **config):
    cfg = TetherConfig(audit_dir=".tether/sessions",
                       dry_run=True, **config)
    report = Orchestrator(MockAdapter(), cfg, tmp_path).run(
        MissionContract(mission={}, name="t", goal="g"))
    assert report["status"] == "success"
    session = find_session_dir(tmp_path, ".tether/sessions",
                               report["session_id"])
    return json.loads((session / "resolved-config.json").read_text())


def test_config_denylist_takes_effect_in_resolved_config(tmp_path):
    saved = _resolved_config(tmp_path, secret_denylist=["audit_dir"])
    assert saved["audit_dir"] == REDACTED


def test_config_allowlist_takes_effect_in_resolved_config(tmp_path):
    saved = _resolved_config(
        tmp_path,
        secret_allowlist=["api_key"],
        adapters={"command": {"command": ["agent"], "api_key": "visible"}},
    )
    assert saved["adapters"]["command"]["api_key"] == "visible"
    # denylist still wins over the allowlist in the wired path too
    saved = _resolved_config(
        tmp_path,
        secret_denylist=["api_key"],
        secret_allowlist=["api_key"],
        adapters={"command": {"command": ["agent"], "api_key": "hidden"}},
    )
    assert saved["adapters"]["command"]["api_key"] == REDACTED


# --------------------------------------------------------- docs truth


def test_readme_documents_the_lists():
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "secret_denylist" in readme
    assert "secret_allowlist" in readme
