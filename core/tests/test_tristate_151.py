"""#151: the shared tri-state rule result."""
import pytest

from RevenueChaser.core import tristate as t


def test_rule_is_unknown_when_an_input_is_missing_and_never_calls_the_test():
    def boom(*a):
        raise AssertionError("not called")
    assert t.rule((1, None), boom) is None
    assert t.rule((), lambda: True) is True


@pytest.mark.parametrize("inputs, expected", [((2, 1), True), ((1, 2), False), ((0, 0), False)])
def test_rule_is_a_real_bool_when_known(inputs, expected):
    assert t.rule(inputs, lambda a, b: a > b) is expected


def test_zero_and_false_are_known_values():
    assert t.known(0, False, "") is True
    assert t.known(0, None) is False


@pytest.mark.parametrize("results, expected", [
    ((True, None), True), ((None, True), True), ((False, False), False),
    ((False, None), None), ((None, None), None), ((True, False), True)])
def test_either(results, expected):
    assert t.either(*results) is expected


def test_tally_counts_passes_known_and_total():
    rules = [{"passed": True}, {"passed": False}, {"passed": None}, {"passed": True}]
    assert t.tally(rules) == (2, 3, 4)


def test_score_text_names_the_known_count_only_when_something_is_unknown():
    assert t.score_text(2, 3, 3) == "2/3"
    assert t.score_text(2, 2, 3) == "2/3 of 2 known"
    assert t.score_text(0, 0, 8) == "0/8 of 0 known"
