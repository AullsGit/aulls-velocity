from velocity.evaluate.metrics import match_cuts


def test_match_cuts_one_to_one_within_tolerance():
    assert match_cuts([10, 50, 90], [11, 52, 200], tol=2) == [(0, 0), (1, 1)]


def test_match_cuts_does_not_reuse_a_target():
    assert match_cuts([10, 11], [10], tol=2) == [(0, 0)]
