"""#383 C15: one cached-info fallback shared by Swing (#259) and Asymmetric Growth (#260).

``refetch_or_keep`` is the pure rule: refetch once, and when the refetch has no answer keep the stored
value marked ``_stale`` (a store-only failure never loses the last good info), or ``None`` with nothing
stored. The caller writes the store only on a fresh answer.
"""
from __future__ import annotations

from RevenueChaser.core.info_fallback import has_fields, refetch_or_keep


def test_a_stub_with_only_a_peg_value_is_not_an_answer():
    assert has_fields({"trailingPegRatio": 2.1}) is False
    assert has_fields({"longName": "Apple Inc."}) is True
    assert has_fields({"longName": ""}) is False
    assert has_fields(None) is False


def test_a_fresh_answer_is_returned_and_marked_fresh():
    fresh = {"longName": "New"}
    got, status = refetch_or_keep({"longName": "Old"}, lambda: fresh, has_fields)
    assert got == fresh and status == "fresh"


def test_a_no_answer_keeps_the_stored_value_marked_stale():
    stored = {"longName": "Old"}
    got, status = refetch_or_keep(stored, lambda: {}, has_fields)
    assert status == "stale"
    assert got == {"longName": "Old", "_stale": True}
    assert "_stale" not in stored            # the stored value is copied, never mutated


def test_a_no_answer_with_nothing_stored_is_none():
    got, status = refetch_or_keep(None, lambda: {}, has_fields)
    assert got is None and status == "none"


def test_a_fetch_that_raises_counts_as_no_answer():
    def boom():
        raise RuntimeError("rate limited")

    got, status = refetch_or_keep({"longName": "Old"}, boom, has_fields)
    assert status == "stale" and got["longName"] == "Old" and got["_stale"] is True

    got, status = refetch_or_keep(None, boom, has_fields)
    assert got is None and status == "none"


def test_the_answer_rule_is_the_callers():
    """Asymmetric Growth answers on ``info`` fields OR non-empty ``annual`` (#260)."""
    rule = lambda fin: bool(fin) and (has_fields(fin.get("info")) or bool(fin.get("annual")))
    got, status = refetch_or_keep(None, lambda: {"info": {}, "annual": [{"fiscal_year": "2024"}]}, rule)
    assert status == "fresh" and got["annual"]
    got, status = refetch_or_keep(None, lambda: {"info": {}, "annual": []}, rule)
    assert got is None and status == "none"


def test_the_caught_errors_are_the_callers():
    """R2B-3: ``catch`` narrows what counts as a failed refetch; anything else propagates."""
    import pytest

    def bug():
        raise TypeError("a bug")

    with pytest.raises(TypeError):
        refetch_or_keep({"a": 1}, bug, has_fields, catch=(OSError, ValueError, KeyError))

    def down():
        raise OSError("down")

    assert refetch_or_keep({"a": 1}, down, has_fields, catch=(OSError,)) == ({"a": 1, "_stale": True}, "stale")
