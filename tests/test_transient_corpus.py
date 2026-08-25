"""dogfood-38: corpus-driven stress tests for the transient classifier.

Every entry in tests/fixtures/provider_errors.json was harvested from real
past dogfood sessions (.tether/sessions/*) and hand-verified: the corpus
expectations are ground truth. The parameterized tests below are the
acceptance gate — every entry, every case/whitespace/wrapping variant of a
signature-bearing entry, and every adversarial near-miss must classify
exactly as the fixture says.
"""
import json
from pathlib import Path

import pytest

from tether.models import AgentState
from tether.reliability import is_transient_failure

FIXTURE_PATH = (Path(__file__).resolve().parent.parent / "tests" /
                "fixtures" / "provider_errors.json")
DATA = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
ENTRIES = DATA["entries"]
NEAR_MISSES = DATA["near_misses"]


def _state(entry: dict) -> AgentState:
    return AgentState(
        status=entry.get("status", "failed"),
        logs=entry.get("logs") or "",
        error=entry.get("error"),
    )


def _variants(text: str):
    """Deterministic mixed-case / whitespace / prefix-suffix wrappings of a
    signature-bearing string; all must stay equally classifiable."""
    yield "upper", text.upper()
    yield "lower", text.lower()
    yield "mixed", text.swapcase()
    yield "whitespace", "\n\t  " + text + "  \n "
    yield "prefix", "send attempt failed: " + text
    yield "suffix", text + " - see stderr above"
    yield "wrapped", "[opencode stderr] " + text + " (end of output)"


# --------------------------------------------- task 2: corpus acceptance gate


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e["id"])
def test_corpus_entry_classifies_exactly_as_expected(entry):
    actual = "transient" if is_transient_failure(_state(entry)) else "fatal"
    assert actual == entry["expected"]


@pytest.mark.parametrize(
    "case_id,text",
    [(f"{e['id']}::{name}", variant)
     for e in ENTRIES if e["expected"] == "transient"
     for name, variant in _variants(e["error"] or "")],
    ids=str,
)
def test_signature_bearing_variants_stay_transient(case_id, text):
    """Case, whitespace and prefix/suffix wrapping must not change a
    transient verdict for any signature-bearing corpus entry."""
    state = AgentState(status="failed", error=text)
    assert is_transient_failure(state) is True


# ------------------------------------------- task 3: adversarial near-misses


@pytest.mark.parametrize("entry", NEAR_MISSES, ids=lambda e: e["id"])
def test_near_miss_is_never_transient(entry):
    """Strings sharing words with signatures but lacking one must never
    retry — guards against over-broad matching."""
    assert is_transient_failure(_state(entry)) is False
