"""Shared funnel format: one start, per-rule drops, one result."""
from __future__ import annotations

from RevenueChaser.core import funnel


def _good():
    return [
        funnel.step("scanned", "Scanned", 100, "start"),
        funnel.step("floor", "Below floor", 30, "drop", by_design=True),
        funnel.step("nocap", "No cap", 10, "drop", by_design=False,
                    fix={"route": "/run/universe-stats", "label": "Fetch caps"}),
        funnel.step("out", "Clustered", 60, "result"),
    ]


def test_a_valid_funnel_has_no_problems():
    assert funnel.validate(_good()) == []


def test_step_builder_shape():
    s = funnel.step("x", "X", 3, "drop", by_design=False,
                    fix={"route": "/run/a", "label": "A"}, tickers=["A", "B"])
    assert s == {"key": "x", "label": "X", "count": 3, "kind": "drop",
                 "by_design": False, "fix": {"route": "/run/a", "label": "A"},
                 "tickers": ["A", "B"]}
    start = funnel.step("s", "S", 1, "start")
    assert "by_design" not in start and start["fix"] is None


def test_arithmetic_violation_names_the_numbers():
    steps = _good()
    steps[-1]["count"] = 59
    problems = funnel.validate(steps)
    assert any("100" in p and "40" in p and "59" in p for p in problems), problems


def test_needs_exactly_one_start_first_and_one_result_last():
    steps = _good()
    assert any("start" in p for p in funnel.validate(steps[1:]))
    assert any("result" in p for p in funnel.validate(steps[:-1]))
    two = [funnel.step("s2", "S2", 5, "start")] + steps
    assert any("start" in p for p in funnel.validate(two))
    assert funnel.validate([]) != []


def test_counts_must_be_non_negative_ints():
    steps = _good()
    steps[1]["count"] = -1
    assert any("floor" in p and "-1" in p for p in funnel.validate(steps))
    steps = _good()
    steps[1]["count"] = 30.5
    assert any("floor" in p for p in funnel.validate(steps))
    steps = _good()
    steps[1]["count"] = True
    assert any("floor" in p for p in funnel.validate(steps))


def test_tickers_capped_at_50():
    steps = _good()
    steps[1]["tickers"] = [f"T{i}" for i in range(51)]
    assert any("floor" in p and "51" in p for p in funnel.validate(steps))
    steps[1]["tickers"] = [f"T{i}" for i in range(50)]
    assert funnel.validate(steps) == []


def test_fix_route_must_be_a_run_route():
    steps = _good()
    steps[2]["fix"] = {"route": "/view/x", "label": "x"}
    assert any("nocap" in p and "/run/" in p for p in funnel.validate(steps))


def test_round_trip_and_bad_input():
    steps = _good()
    assert funnel.from_dict(funnel.to_dict(steps)) == steps
    assert funnel.from_dict(None) == []
    assert funnel.validate("nope") != []


# ── a note: shown, never counted (#87) ──────────────────────────────────────
_LINK = {"tab": "module_settings", "anchor": "identity", "label": "Settings → Identity"}


def _with_note():
    steps = _good()
    steps.insert(2, funnel.step("untyped", "Unknown security type — kept", 7, "note",
                                link=_LINK))
    return steps


def test_note_is_a_kind():
    assert "note" in funnel.KINDS


def test_a_note_in_the_middle_validates_and_the_arithmetic_ignores_it():
    steps = _with_note()
    assert funnel.validate(steps) == []          # 100 - 30 - 10 = 60 with the note's 7 ignored
    # a note must not be counted as a drop: counting it would make the sums disagree
    assert sum(s["count"] for s in steps if s["kind"] == "drop") == 40


def test_a_note_cannot_be_first_or_last():
    first = [funnel.step("n", "N", 1, "note")] + _good()
    assert any("start" in p for p in funnel.validate(first))
    last = _good() + [funnel.step("n", "N", 1, "note")]
    assert any("result" in p for p in funnel.validate(last))


def test_step_builder_carries_a_link_only_when_given():
    s = funnel.step("untyped", "U", 3, "note", link=_LINK)
    assert s["link"] == _LINK and s["link"] is not _LINK       # a copy
    assert "link" not in funnel.step("x", "X", 1, "drop", by_design=True)


def test_a_bad_link_is_reported():
    for bad in ({}, {"anchor": "identity"}, {"tab": ""}, {"tab": 5}, "settings"):
        steps = _with_note()
        steps[2]["link"] = bad
        assert any("untyped" in p and "link" in p for p in funnel.validate(steps)), bad
    assert funnel.validate(_with_note()) == []


def test_a_note_survives_the_round_trip():
    steps = _with_note()
    assert funnel.from_dict(funnel.to_dict(steps)) == steps
