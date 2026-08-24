"""Suite-wide collection helpers (tests only).

tests/test_oscillation_fixture.py is the dogfood-28 live-fire artifact:
test_alpha_mode and test_beta_mode assert mutually exclusive values of
MODE, so no change can satisfy both and any full-suite run fails. It is
therefore excluded from ordinary runs exactly as the dogfood-29
verification command does
(--ignore=tests/test_oscillation_fixture.py). Set
TETHER_RUN_OSCILLATION_FIXTURE=1 to collect it explicitly.
"""
import os

if not os.environ.get("TETHER_RUN_OSCILLATION_FIXTURE"):
    collect_ignore = ["test_oscillation_fixture.py"]
