"""dogfood-45: the untracked helper and the test that proves it."""
from tether.untracked_helper import helper


def test_helper() -> None:
    assert helper() == 42
