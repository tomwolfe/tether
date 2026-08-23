"""dogfood-28 live-fire fixture: mutually exclusive assertions."""
from tether import _oscillation_fixture


def test_alpha_mode() -> None:
    value = _oscillation_fixture.MODE
    assert type(value) is str
    assert value == "alpha"


def test_beta_mode() -> None:
    value = _oscillation_fixture.MODE
    assert type(value) is str
    assert value == "beta"
