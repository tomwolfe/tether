"""Deliberately contradictory fixture for dogfood-28 live-fire testing.

test_alpha_mode and test_beta_mode assert mutually exclusive values of
MODE, so no assignment can make the suite pass. This forces a real
fix-A-breaks-B / fix-B-breaks-A oscillation when an agent is told the
only mutable file is this one.
"""
MODE = "alpha"
