def test_ci_canary_must_fail():
    """Deliberately failing: proves CI goes red and blocks the merge. Reverted immediately."""
    assert 1 == 2
