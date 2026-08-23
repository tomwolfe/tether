"""dogfood-28 live-fire fixture: mutually exclusive assertions."""
from tether import _oscillation_fixture


def test_alpha_mode() -> None:
    assert _oscillation_fixture.MODE == "alpha"


def test_beta_mode() -> None:
    assert _oscillation_fixture.MODE == "beta"
