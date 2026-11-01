"""``needs.ask(need, *, as_of=None, **params)``: the lag counted against the day a board renders (#330).

SPX Gamma renders a chosen day, often a past one, and judges every input against that day. The envelope counted the
lag against one fixed day, the market's newest complete session, so for a render of 15 October a 14 October VIX row
read "stale by 12" while the board was right to call it fresh. ``as_of`` names the day to judge from, and
``freshness["reference"]`` says which day the answer was judged from, with or without it.

These need no store: the reader is replaced at the HTTP-free boundary the envelope calls (``rcdata.read``).
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.rcdata import needs

REFERENCE = date(2026, 10, 27)          # Tue: the pinned newest complete session


@pytest.fixture
def vix_rows(monkeypatch):
    """``read.market_series`` answering one VIX row per day it is given; the call's keywords are kept."""
    seen: dict = {}

    def serve(*days: date):
        def fake(series_ids=(), start=None, end=None, prefix=None):
            seen.update(series_ids=tuple(series_ids), start=start, end=end, prefix=prefix)
            return [{"series_id": "VIX", "obs_date": d, "close": 16.0, "status": "changed", "source": "cboe"}
                    for d in days]
        monkeypatch.setattr(needs.catalog.read, "market_series", fake)
        return seen
    return serve


@pytest.fixture(autouse=True)
def pinned_reference(monkeypatch):
    calls = []
    monkeypatch.setattr(needs.serve, "reference_session", lambda: (calls.append(1), REFERENCE)[1])
    return calls


def test_without_as_of_the_lag_is_counted_against_the_reference_session_and_named(vix_rows, pinned_reference):
    """The default is unchanged (Earnings Radar asks without ``as_of``): same lag, same state; only the day it was
    judged from is now named."""
    vix_rows(date(2026, 10, 14))
    ans = needs.ask("market.vix")
    assert pinned_reference, "the default must still count from reference_session()"
    assert ans.freshness == {"as_of_session": date(2026, 10, 14), "lag_sessions": 9, "max_lag": 3,
                             "unit": "sessions", "reference": REFERENCE}
    assert ans.state == "stale"


def test_a_past_days_render_counts_the_lag_against_that_day(vix_rows, pinned_reference):
    """The issue's case: a render of Thu 15 Oct with a Wed 14 Oct VIX row is one session behind, fresh."""
    vix_rows(date(2026, 10, 14))
    ans = needs.ask("market.vix", as_of=date(2026, 10, 15))
    assert ans.freshness["reference"] == date(2026, 10, 15)
    assert ans.freshness["lag_sessions"] == 1 and ans.state == "fresh"
    assert not pinned_reference, "with as_of the newest complete session plays no part"


def test_as_of_on_a_weekend_is_judged_from_the_session_on_or_before_it(vix_rows):
    """Sun 25 Oct: the newest session on or before it is Fri 23 Oct, so a Friday row is 0 behind, not 1."""
    vix_rows(date(2026, 10, 23))
    ans = needs.ask("market.vix", as_of=date(2026, 10, 25))
    assert ans.freshness["reference"] == date(2026, 10, 23)
    assert ans.freshness["lag_sessions"] == 0 and ans.state == "fresh"


def test_as_of_past_the_need_max_lag_is_stale(vix_rows):
    """Mon 26 Oct is 3 sessions behind Thu 29 Oct (fresh, max_lag 3) and 4 behind Fri 30 Oct (stale)."""
    vix_rows(date(2026, 10, 26))
    assert needs.ask("market.vix", as_of=date(2026, 10, 29)).state == "fresh"
    late = needs.ask("market.vix", as_of=date(2026, 10, 30))
    assert late.state == "stale" and late.freshness["lag_sessions"] == 4


def test_as_of_may_be_an_iso_string_and_is_never_handed_to_the_reader(vix_rows):
    seen = vix_rows(date(2026, 10, 14))
    ans = needs.ask("market.vix", as_of="2026-10-15", start=date(2026, 10, 1), end=date(2026, 10, 15))
    assert ans.freshness["reference"] == date(2026, 10, 15) and ans.state == "fresh"
    assert seen["end"] == date(2026, 10, 15), "the reader's own window is the caller's, untouched"


def test_an_unreadable_as_of_is_refused_in_words(vix_rows):
    vix_rows(date(2026, 10, 14))
    with pytest.raises(ValueError, match="as_of"):
        needs.ask("market.vix", as_of="15 Oct")


def test_a_need_without_rows_still_names_the_day_it_was_judged_from(vix_rows):
    vix_rows()
    ans = needs.ask("market.vix", as_of=date(2026, 10, 15))
    assert ans.state == "missing" and ans.freshness["reference"] == date(2026, 10, 15)
    assert ans.freshness["lag_sessions"] is None


@pytest.mark.parametrize("name", sorted(needs.catalog.NEEDS))
def test_no_reader_takes_as_of_so_the_envelope_never_swallows_a_readers_argument(name):
    import inspect
    assert "as_of" not in inspect.signature(needs.catalog.NEEDS[name].reader).parameters


# ── review of 1e1b250 (#334): a day the calendar cannot count is an undecided lag, never a lost answer ──

def test_a_valid_day_before_the_holiday_table_keeps_its_rows_and_leaves_the_lag_undecided(vix_rows):
    """Blocker 1: the calendar's holiday table starts in 1993, Cboe's SPX series in 1975. A 1992 render must keep
    its rows; only the lag cannot be counted, and the answer says so instead of raising."""
    vix_rows(date(1992, 10, 22), date(1992, 10, 23))
    ans = needs.ask("market.vix", as_of=date(1992, 10, 23))
    assert len(ans.rows) == 2 and ans.state == needs.serve.UNDECIDED and not ans.ok
    assert ans.freshness["lag_sessions"] is None and ans.freshness["as_of_session"] == date(1992, 10, 23)
    assert ans.freshness["reference"] == date(1992, 10, 23)
    assert "1992" in ans.freshness["lag_reason"]


def test_a_day_after_the_holiday_table_is_undecided_too(vix_rows):
    vix_rows(date(2029, 3, 1))
    ans = needs.ask("market.vix", as_of=date(2029, 3, 2))
    assert ans.rows and ans.state == needs.serve.UNDECIDED and ans.freshness["lag_sessions"] is None


def test_no_rows_outside_the_table_is_still_missing(vix_rows):
    vix_rows()
    ans = needs.ask("market.vix", as_of=date(1992, 10, 23))
    assert ans.state == "missing" and ans.freshness["lag_sessions"] is None


def test_an_in_table_holiday_is_judged_from_the_session_before_it(vix_rows):
    """Thu 26 Nov 2026 (Thanksgiving): the reference is Wed 25 Nov."""
    vix_rows(date(2026, 11, 25))
    ans = needs.ask("market.vix", as_of=date(2026, 11, 26))
    assert ans.freshness["reference"] == date(2026, 11, 25) and ans.freshness["lag_sessions"] == 0
    assert ans.state == "fresh"


def test_a_future_as_of_is_judged_against_that_future_session(vix_rows):
    vix_rows(date(2026, 10, 26))
    ans = needs.ask("market.vix", as_of=date(2026, 12, 15))
    assert ans.state == "stale" and ans.freshness["reference"] == date(2026, 12, 15)


def test_the_session_count_only_reads_the_calendar_after_the_observation():
    """2025-01-02 is one session after a 2024-12-31 row (1 Jan is a holiday). The old board counted forward from the
    row and never asked the calendar about 2024; the walk back must not either."""
    assert needs.lag_sessions(date(2024, 12, 31), date(2025, 1, 2)) == 1
    assert needs.lag_sessions(date(2026, 10, 26), date(2026, 10, 29)) == 3        # unchanged inside the table
    assert needs.lag_sessions(date(2026, 10, 23), date(2026, 10, 25)) == 1        # a Sunday counts as one, as before


def test_an_unreadable_as_of_is_its_own_error_class(vix_rows):
    vix_rows(date(2026, 10, 14))
    with pytest.raises(needs.BadAsOf):
        needs.ask("market.vix", as_of="15 Oct")
    assert issubclass(needs.BadAsOf, ValueError)
