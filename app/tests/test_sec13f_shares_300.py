"""#300 (D20): the 13F Fetch stores UW ``info`` shares outstanding of every 13F ticker in ``ownership_snapshot``,
once per complete session (a Saturday run maps to Friday), and no longer runs the retired 13F enrichment.

* an intraday run, or a session whose tickers are all stored, asks UW nothing and opens no client;
* a failed UW answer stores no row (asked again by the next Fetch); a UW key that fails ``verify`` is a job note;
* the store-only Refresh of the board writes nothing.
"""
from datetime import date, datetime, timezone

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.tests.test_sec13f_job import GOOD, _zip, store  # noqa: F401 - the store fixture
from RevenueChaser.core import ledger, ownership, rawstore
from RevenueChaser.module_sec_13f import engine as sec_13f

UTC = timezone.utc
COMPLETE = datetime(2026, 10, 8, 20, 35, tzinfo=UTC).timestamp()      # Thu 16:35 EDT: session 10-08, complete
INTRADAY = datetime(2026, 10, 8, 15, 0, tzinfo=UTC).timestamp()       # Thu 11:00 EDT: intraday
SATURDAY = datetime(2026, 10, 10, 14, 0, tzinfo=UTC).timestamp()      # Sat 10:00 EDT: maps to Fri 10-09


class _UW:
    """A UW client double with ``ticker_info``; counts constructions, closes and info calls."""
    built = closed = 0
    ok = (True, None)
    answers: dict = {}
    calls: list = []

    def __init__(self, *a, **k):
        type(self).built += 1

    def verify(self):
        return type(self).ok

    def screener_batch(self, tickers):
        return {}

    def health(self):
        return None

    def ticker_info(self, ticker):
        type(self).calls.append(ticker)
        return type(self).answers.get(ticker, {})

    def close(self):
        type(self).closed += 1


@pytest.fixture
def uw(monkeypatch, store):  # noqa: F811 - the imported fixture
    _UW.built, _UW.closed, _UW.ok, _UW.calls = 0, 0, (True, None), []
    _UW.answers = {"AAPL": {"outstanding": "15000000000"}}
    monkeypatch.setattr(runners, "UWClient", _UW)
    sec_13f.save_sec13f_meta({"AAPL": GOOD})      # meta covers the totals: the screener step is skipped (F-1)
    return _UW


def _at(monkeypatch, when):
    monkeypatch.setattr(ownership, "_clock", lambda: when)


def _owned():
    return rawstore.query("SELECT * FROM ownership_snapshot ORDER BY ticker, source")


def test_a_complete_fetch_stores_uw_shares_for_every_13f_ticker(uw, monkeypatch):
    _at(monkeypatch, COMPLETE)
    runners.build_sec13f(None)
    assert uw.calls == ["AAPL"]
    (row,) = _owned()
    assert (row["ticker"], row["source"], row["session_date"], row["shares_outstanding"]) == \
        ("AAPL", "uw_info", date(2026, 10, 8), 1.5e10)
    assert row["insider_pct"] is None and row["institutional_pct"] is None
    assert (uw.built, uw.closed) == (1, 1)                 # the client it opened is closed


def test_a_saturday_fetch_stores_fridays_session(uw, monkeypatch):
    _at(monkeypatch, SATURDAY)
    runners.build_sec13f(None)
    assert _owned()[0]["session_date"] == date(2026, 10, 9)


def test_an_intraday_fetch_asks_uw_nothing(uw, monkeypatch):
    _at(monkeypatch, INTRADAY)
    runners.build_sec13f(None)
    assert uw.calls == [] and uw.built == 0 and _owned() == []


def test_a_second_fetch_of_the_session_asks_nothing(uw, monkeypatch):
    _at(monkeypatch, COMPLETE)
    runners.build_sec13f(None)
    uw.calls, uw.built = [], 0
    runners.build_sec13f(None)
    assert uw.calls == [] and uw.built == 0 and len(_owned()) == 1


def test_a_passed_client_is_used_and_not_closed(uw, monkeypatch):
    _at(monkeypatch, COMPLETE)
    client = uw()
    runners.build_sec13f(client)
    assert uw.calls == ["AAPL"] and uw.built == 1 and uw.closed == 0


def test_a_failed_answer_stores_no_row(uw, monkeypatch):
    _at(monkeypatch, COMPLETE)
    uw.answers = {}
    runners.build_sec13f(None)
    assert uw.calls == ["AAPL"] and _owned() == []


def test_a_key_that_fails_verify_is_a_note_and_asks_nothing(uw, monkeypatch):
    _at(monkeypatch, COMPLETE)
    ledger.take_notes()
    uw.ok = (False, "no API key")
    runners.build_sec13f(None)
    assert uw.calls == [] and _owned() == [] and uw.closed == uw.built == 1
    assert any("13F shares outstanding were not stored: no API key" in n for n in ledger.take_notes())


def test_a_store_failure_is_a_note_and_the_board_still_renders(uw, monkeypatch):
    _at(monkeypatch, COMPLETE)
    ledger.take_notes()
    runners.SEC_13F_LATEST.unlink(missing_ok=True)

    def boom(*a, **k):
        raise rawstore.RawStoreError("locked")

    monkeypatch.setattr(ownership, "record", boom)
    runners.build_sec13f(None)
    assert runners.SEC_13F_LATEST.exists()
    assert any("13F shares outstanding were not stored" in n and "locked" in n for n in ledger.take_notes())
    assert uw.closed == uw.built == 1


def test_the_fetch_no_longer_writes_the_13f_enrichment(uw, monkeypatch):
    _at(monkeypatch, COMPLETE)
    runners.build_sec13f(None)
    assert rawstore.query("SELECT * FROM sec13f_enrichment") == []
    from RevenueChaser.module_sec_13f import enrich
    assert not hasattr(enrich, "build_enrichment") and not hasattr(enrich, "save_enrichment")


def test_the_board_refresh_writes_no_ownership(uw, monkeypatch):
    _at(monkeypatch, COMPLETE)
    sec_13f.recompute_totals()
    runners.run_sec_13f_latest(None)
    assert uw.calls == [] and _owned() == []


def test_a_rejected_key_on_the_meta_client_asks_nothing(uw, monkeypatch):
    """The meta step's client failed ``verify``: the shares step reuses its verdict and spends no call."""
    _at(monkeypatch, COMPLETE)
    sec_13f.save_sec13f_meta({"AAPL": {"name": "", "sector": "Unclassified", "type": "Other"}})   # meta step runs
    uw.ok = (False, "token rejected")
    with pytest.raises(Exception, match="not refreshed"):
        runners.build_sec13f(None)
    assert uw.calls == [] and _owned() == [] and (uw.built, uw.closed) == (1, 1)


def test_uw_is_asked_before_the_board_is_rendered(uw, monkeypatch):
    """#375 F2 (13F-B) reverses review S1: the board's % Out reads the rows this step stores, so the step runs
    before the render and the board file does not exist yet when UW is asked."""
    _at(monkeypatch, COMPLETE)
    runners.SEC_13F_LATEST.unlink(missing_ok=True)
    seen = []
    monkeypatch.setattr(_UW, "ticker_info", lambda self, t: seen.append(runners.SEC_13F_LATEST.exists()) or {})
    runners.build_sec13f(None)
    assert seen == [False] and runners.SEC_13F_LATEST.exists()


def test_a_capped_step_leaves_one_note_with_the_rest(uw, monkeypatch):
    """Review B1: the cap holds in code (the 13F job cannot be cancelled) and says what is left."""
    _at(monkeypatch, COMPLETE)
    ledger.take_notes()
    monkeypatch.setattr(ownership, "UW_MAX_CALLS_PER_FETCH", 2)
    monkeypatch.setattr(sec_13f, "load_totals", lambda: {t: {} for t in ("AAA", "BBB", "CCC")})
    runners._store_13f_shares(uw())
    assert uw.calls == ["AAA", "BBB"]
    notes = [n for n in ledger.take_notes() if "13F shares outstanding" in n]
    assert notes == ["13F shares outstanding: the cap of 2 UW calls per Fetch was reached; "
                     "1 ticker(s) left for the next Fetch."]
