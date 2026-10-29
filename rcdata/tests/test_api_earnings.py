"""``earnings.next`` (step B): the first need a board really uses, and the end of the short-circuit.

Until this step ``core.earnings_dates`` returned the options layer's calendar **instead of** the stored D26 one
(``_layer_next_dates`` returned early), so a name the layer does not cover lost the date the D26 sweep had for it.
The need is one ordered union: the layer first, the stored calendar for the names it does not name, and Yahoo's own
value for whatever neither holds.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from RevenueChaser.core import earnings_dates
from RevenueChaser.rcdata import api

TODAY = date(2026, 10, 27)
SOON = date(2026, 10, 30)
LATER = date(2026, 11, 5)


@pytest.fixture
def layer(monkeypatch):
    """The options layer's screener rows, as ``rcdata.read.option_screener`` returns them."""
    def set_rows(rows):
        monkeypatch.setattr(api.catalog.read, "option_screener", lambda **kw: list(rows))
    return set_rows


@pytest.fixture
def d26(monkeypatch):
    """The stored D26 calendar, as ``rawstore.query`` returns it."""
    def set_rows(rows):
        monkeypatch.setattr(earnings_dates.rawstore, "query", lambda *a, **k: list(rows))
    return set_rows


def _screener(ticker, report_date, session=TODAY):
    return {"ticker": ticker, "report_date": report_date, "session_date": session,
            "source": "uw/option_screener", "source_as_of": session, "fetched_at": 1.0, "run_id": "r1"}


def test_the_catalog_holds_the_need():
    need = api.catalog.NEEDS["earnings.next"]
    assert need.reader_name == "option_screener" and need.dataset == "uw/option_screener"
    assert need.fallbacks == ("d26_calendar",) and need.key_column == "ticker"


def test_the_layers_names_come_from_the_layer(layer, d26):
    layer([_screener("AAA", SOON)])
    d26([{"ticker": "AAA", "d": LATER}])
    assert earnings_dates.stored_next_dates(TODAY)["AAA"] == SOON


def test_a_name_the_layer_does_not_cover_keeps_its_stored_date(layer, d26):
    """The bug this step closes: BBB used to be dropped because AAA was in the layer."""
    layer([_screener("AAA", SOON)])
    d26([{"ticker": "BBB", "d": LATER}])
    assert earnings_dates.stored_next_dates(TODAY) == {"AAA": SOON, "BBB": LATER}


def test_the_union_is_reported_as_coverage(layer, d26):
    layer([_screener("AAA", SOON)])
    d26([{"ticker": "BBB", "d": LATER}])
    ans = earnings_dates.next_dates_answer(TODAY)
    assert ans.state in ("fresh", "stale") and ans.coverage["served"] == 1
    assert ans.coverage["from_fallback"] == 1


def test_a_stale_layer_session_falls_back_to_the_stored_calendar_alone(layer, d26):
    old = TODAY - timedelta(days=earnings_dates.MAX_AGE_DAYS + 10)
    layer([_screener("AAA", SOON, session=old)])
    d26([{"ticker": "BBB", "d": LATER}])
    assert earnings_dates.stored_next_dates(TODAY) == {"BBB": LATER}


def test_a_layer_session_after_today_is_not_read(layer, d26):
    layer([_screener("AAA", SOON, session=TODAY + timedelta(days=1))])
    d26([{"ticker": "BBB", "d": LATER}])
    assert earnings_dates.stored_next_dates(TODAY) == {"BBB": LATER}


def test_a_report_date_before_today_is_not_a_next_date(layer, d26):
    layer([_screener("AAA", TODAY - timedelta(days=1)), _screener("CCC", SOON)])
    d26([])
    assert earnings_dates.stored_next_dates(TODAY) == {"CCC": SOON}


def test_an_unreadable_stored_calendar_still_leaves_the_layers_dates(layer, monkeypatch):
    layer([_screener("AAA", SOON)])

    def boom(*a, **k):
        raise RuntimeError("no raw store")
    monkeypatch.setattr(earnings_dates.rawstore, "query", boom)
    assert earnings_dates.stored_next_dates(TODAY) == {"AAA": SOON}


def test_an_unreadable_layer_still_leaves_the_stored_dates(monkeypatch, d26):
    def boom(**kw):
        raise RuntimeError("no options file")
    monkeypatch.setattr(api.catalog.read, "option_screener", boom)
    d26([{"ticker": "BBB", "d": LATER}])
    assert earnings_dates.stored_next_dates(TODAY) == {"BBB": LATER}


def test_neither_source_is_an_empty_map_never_an_error(layer, d26):
    layer([])
    d26([])
    assert earnings_dates.stored_next_dates(TODAY) == {}
