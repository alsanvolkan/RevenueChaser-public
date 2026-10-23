"""Ownership per session (#300, D20): the mappers, the one writer and the 13F UW shares step.

Offline: ``.info`` dicts are hand-built in the shapes yfinance ``Ticker.info`` and UW ``/stock/{t}/info`` return,
the UW client is a double, and the clock is passed as ``run_at``.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import job_control, ownership, rawstore

UTC = timezone.utc
COMPLETE = datetime(2026, 10, 8, 20, 35, tzinfo=UTC).timestamp()      # Thu 16:35 EDT: session 10-08, complete
INTRADAY = datetime(2026, 10, 8, 15, 0, tzinfo=UTC).timestamp()       # Thu 11:00 EDT: session 10-08, intraday
LATER_SAME = datetime(2026, 10, 8, 23, 0, tzinfo=UTC).timestamp()     # Thu 19:00 EDT: session 10-08 again
SATURDAY = datetime(2026, 10, 10, 14, 0, tzinfo=UTC).timestamp()      # Sat 10:00 EDT: maps to Fri 10-09
OUTSIDE = datetime(2040, 1, 10, 22, 0, tzinfo=UTC).timestamp()        # past the holiday table

_YF_INFO = {"sharesOutstanding": 1.5e9, "heldPercentInsiders": 0.0521, "heldPercentInstitutions": 0.734,
            "marketCap": 4.5e9, "currency": "USD"}


def _rows(ticker=None):
    sql = "SELECT * FROM ownership_snapshot" + (" WHERE ticker = ?" if ticker else "") + " ORDER BY ticker, source"
    return rawstore.query(sql, [ticker] if ticker else None)


class _UW:
    """``ticker_info`` as the real client gives it: the ``data`` object, ``{}`` on a failed request."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def ticker_info(self, ticker):
        self.calls.append(ticker)
        a = self.answers.get(ticker, {})
        if isinstance(a, Exception):
            raise a
        return a


# ── the mappers ──────────────────────────────────────────────────────────────

def test_yfinance_row_keeps_the_fractions_as_served():
    r = ownership.row_yfinance("aaa", _YF_INFO)
    assert r == {"ticker": "AAA", "source": "yfinance", "shares_outstanding": 1.5e9,
                 "insider_pct": 0.0521, "institutional_pct": 0.734}


def test_yfinance_row_leaves_a_missing_field_null():
    r = ownership.row_yfinance("AAA", {"heldPercentInsiders": 0.1, "sharesOutstanding": "nan"})
    assert (r["shares_outstanding"], r["insider_pct"], r["institutional_pct"]) == (None, 0.1, None)


@pytest.mark.parametrize("info", [None, {}, {"marketCap": 1e9}, {"heldPercentInsiders": None}, "x"])
def test_no_yfinance_row_without_an_ownership_field(info):
    assert ownership.row_yfinance("AAA", info) is None


def test_uw_row_is_the_outstanding_shares_only():
    r = ownership.row_uw("bbb", {"outstanding": "2500000000", "marketcap": "9e9", "full_name": "B Corp"})
    assert r == {"ticker": "BBB", "source": "uw_info", "shares_outstanding": 2.5e9,
                 "insider_pct": None, "institutional_pct": None}


@pytest.mark.parametrize("info", [None, {}, {"outstanding": None}, {"outstanding": ""}, {"outstanding": "x"}])
def test_no_uw_row_without_outstanding(info):
    assert ownership.row_uw("BBB", info) is None


# ── the writer (rule B: complete sessions only, first write per key) ─────────

def test_a_complete_run_writes_its_session_with_the_stamps(rawstore_tmp_db):
    res = ownership.record([ownership.row_yfinance("AAA", _YF_INFO)], run_id="r1", run_at=COMPLETE,
                           fetched_at=COMPLETE - 5)
    assert res["written"] == 1 and res["kept"] == 0 and res["skip_reason"] is None
    (row,) = _rows()
    assert row["session_date"] == date(2026, 10, 8)
    assert (row["run_id"], row["run_at"], row["fetched_at"]) == ("r1", COMPLETE, COMPLETE - 5)
    assert (row["insider_pct"], row["institutional_pct"], row["shares_outstanding"]) == (0.0521, 0.734, 1.5e9)


def test_a_rows_own_fetched_at_wins(rawstore_tmp_db):
    r = {**ownership.row_yfinance("AAA", _YF_INFO), "fetched_at": COMPLETE - 60}
    ownership.record([r], run_id="r1", run_at=COMPLETE)
    assert _rows()[0]["fetched_at"] == COMPLETE - 60


def test_an_intraday_run_writes_nothing(rawstore_tmp_db):
    res = ownership.record([ownership.row_yfinance("AAA", _YF_INFO)], run_id="r1", run_at=INTRADAY)
    assert res["written"] == 0 and res["skipped"] == 1 and res["skip_reason"] == "intraday"
    assert _rows() == []


def test_a_run_outside_the_holiday_table_writes_nothing(rawstore_tmp_db):
    res = ownership.record([ownership.row_yfinance("AAA", _YF_INFO)], run_id="r1", run_at=OUTSIDE)
    assert res["written"] == 0 and res["skip_reason"] == "no session"
    assert _rows() == []


def test_the_first_write_of_a_session_wins(rawstore_tmp_db):
    ownership.record([ownership.row_yfinance("AAA", _YF_INFO)], run_id="r1", run_at=COMPLETE)
    res = ownership.record([ownership.row_yfinance("AAA", {**_YF_INFO, "heldPercentInsiders": 0.9})],
                           run_id="r2", run_at=LATER_SAME)
    assert res["written"] == 0 and res["kept"] == 1
    (row,) = _rows()
    assert (row["run_id"], row["insider_pct"]) == ("r1", 0.0521)


def test_two_sources_of_one_session_are_both_kept(rawstore_tmp_db):
    ownership.record([ownership.row_yfinance("AAA", _YF_INFO)], run_id="r1", run_at=COMPLETE)
    ownership.record([ownership.row_uw("AAA", {"outstanding": 1.4e9})], run_id="r2", run_at=LATER_SAME)
    assert [(r["source"], r["shares_outstanding"]) for r in _rows()] == [("uw_info", 1.4e9), ("yfinance", 1.5e9)]


def test_a_key_twice_in_one_batch_keeps_the_first(rawstore_tmp_db):
    a = ownership.row_yfinance("AAA", _YF_INFO)
    b = ownership.row_yfinance("AAA", {**_YF_INFO, "heldPercentInsiders": 0.9})
    res = ownership.record([a, b], run_id="r1", run_at=COMPLETE)
    assert res["written"] == 1 and _rows()[0]["insider_pct"] == 0.0521


def test_a_saturday_run_maps_to_friday(rawstore_tmp_db):
    ownership.record([ownership.row_uw("AAA", {"outstanding": 10})], run_id="r1", run_at=SATURDAY)
    assert _rows()[0]["session_date"] == date(2026, 10, 9)


def test_nothing_to_write_touches_no_table(rawstore_tmp_db):
    assert ownership.record([None], run_id="r1", run_at=COMPLETE)["written"] == 0


# ── the 13F UW shares step ───────────────────────────────────────────────────

def test_pending_lists_the_tickers_without_a_uw_row_this_session(rawstore_tmp_db):
    ownership.record([ownership.row_uw("AAA", {"outstanding": 10})], run_id="r0", run_at=COMPLETE)
    ownership.record([ownership.row_yfinance("BBB", _YF_INFO)], run_id="r0", run_at=COMPLETE)
    session, todo = ownership.pending_uw(["AAA", "bbb", "CCC", "CCC"], LATER_SAME)
    assert session == date(2026, 10, 8) and todo == ["BBB", "CCC"]     # a yfinance row is not a UW one


def test_pending_is_empty_on_an_intraday_run(rawstore_tmp_db):
    assert ownership.pending_uw(["AAA"], INTRADAY) == (None, [])


def test_the_step_stores_one_uw_row_per_answered_ticker(rawstore_tmp_db):
    uw = _UW({"AAA": {"outstanding": "100"}, "BBB": {}, "CCC": {"outstanding": 300.0},
              "DDD": RuntimeError("boom")})
    res = ownership.store_uw_shares(uw, ["AAA", "BBB", "CCC", "DDD"], run_id="job13f", run_at=COMPLETE)
    assert uw.calls == ["AAA", "BBB", "CCC", "DDD"]                    # one info call per ticker
    assert (res["asked"], res["written"], res["no_value"]) == (4, 2, 2)
    rows = _rows()
    assert [(r["ticker"], r["source"], r["shares_outstanding"], r["run_id"]) for r in rows] == \
        [("AAA", "uw_info", 100.0, "job13f"), ("CCC", "uw_info", 300.0, "job13f")]
    assert {r["session_date"] for r in rows} == {date(2026, 10, 8)}


def test_the_step_asks_nothing_on_an_intraday_run(rawstore_tmp_db):
    uw = _UW({"AAA": {"outstanding": 1}})
    res = ownership.store_uw_shares(uw, ["AAA"], run_id="j", run_at=INTRADAY)
    assert uw.calls == [] and res["skip_reason"] == "intraday" and _rows() == []


def test_a_second_run_of_the_session_asks_only_the_missing_tickers(rawstore_tmp_db):
    ownership.store_uw_shares(_UW({"AAA": {"outstanding": 1}}), ["AAA", "BBB"], run_id="j1", run_at=COMPLETE)
    uw = _UW({"AAA": {"outstanding": 2}, "BBB": {"outstanding": 3}})
    ownership.store_uw_shares(uw, ["AAA", "BBB"], run_id="j2", run_at=LATER_SAME)
    assert uw.calls == ["BBB"]
    assert [(r["ticker"], r["shares_outstanding"], r["run_id"]) for r in _rows()] == \
        [("AAA", 1.0, "j1"), ("BBB", 3.0, "j2")]


def test_the_step_writes_per_batch(rawstore_tmp_db, monkeypatch):
    """A cancel between batches keeps the batches already asked (each call cost a UW request)."""
    monkeypatch.setattr(ownership, "UW_BATCH", 2)
    seen = []

    def stop_on_second_batch(next_phase=None):
        seen.append(len(_rows()))
        if len(seen) == 2:
            raise job_control.JobCancelled("cancelled")

    monkeypatch.setattr(job_control, "checkpoint", stop_on_second_batch)
    uw = _UW({t: {"outstanding": 1} for t in ("AAA", "BBB", "CCC")})
    with pytest.raises(job_control.JobCancelled):
        ownership.store_uw_shares(uw, ["AAA", "BBB", "CCC"], run_id="j", run_at=COMPLETE)
    assert seen == [0, 2] and uw.calls == ["AAA", "BBB"]
    assert [r["ticker"] for r in _rows()] == ["AAA", "BBB"]


def test_the_session_is_the_runs_not_the_answers(rawstore_tmp_db, monkeypatch):
    """Every row of one step carries the run's session, even when the clock passes midnight between calls."""
    ticks = [COMPLETE]
    monkeypatch.setattr(ownership, "_clock", lambda: ticks.pop() if ticks else OUTSIDE)
    ownership.store_uw_shares(_UW({"AAA": {"outstanding": 1}, "BBB": {"outstanding": 2}}), ["AAA", "BBB"],
                              run_id="j", run_at=COMPLETE)
    assert {r["session_date"] for r in _rows()} == {date(2026, 10, 8)}


# ── review B1/S2: the per-Fetch cap, the order, a new session ─────────────────

TUESDAY = datetime(2026, 10, 6, 20, 35, tzinfo=UTC).timestamp()       # session 10-06, complete
WEDNESDAY = datetime(2026, 10, 7, 20, 35, tzinfo=UTC).timestamp()     # session 10-07, complete
NEXT_DAY = datetime(2026, 10, 9, 20, 35, tzinfo=UTC).timestamp()      # session 10-09, complete


def test_the_cap_is_the_typed_number():
    assert ownership.UW_MAX_CALLS_PER_FETCH == 2_500


def test_pending_asks_never_stored_first_then_oldest(rawstore_tmp_db):
    ownership.record([ownership.row_uw("AAA", {"outstanding": 1})], run_id="r", run_at=WEDNESDAY)
    ownership.record([ownership.row_uw("BBB", {"outstanding": 1})], run_id="r", run_at=TUESDAY)
    ownership.record([ownership.row_yfinance("DDD", _YF_INFO)], run_id="r", run_at=WEDNESDAY)  # not a UW row
    session, todo = ownership.pending_uw(["AAA", "BBB", "CCC", "DDD"], COMPLETE)
    assert session == date(2026, 10, 8) and todo == ["CCC", "DDD", "BBB", "AAA"]


def test_the_cap_stops_asking_and_reports_the_rest(rawstore_tmp_db, monkeypatch):
    monkeypatch.setattr(ownership, "UW_MAX_CALLS_PER_FETCH", 2)
    uw = _UW({t: {"outstanding": 1} for t in ("AAA", "BBB", "CCC")})
    res = ownership.store_uw_shares(uw, ["AAA", "BBB", "CCC"], run_id="j", run_at=COMPLETE)
    assert uw.calls == ["AAA", "BBB"] and (res["asked"], res["written"], res["left"]) == (2, 2, 1)
    assert [r["ticker"] for r in _rows()] == ["AAA", "BBB"]


def test_the_next_fetch_of_the_session_continues_with_the_rest(rawstore_tmp_db, monkeypatch):
    monkeypatch.setattr(ownership, "UW_MAX_CALLS_PER_FETCH", 2)
    ownership.store_uw_shares(_UW({t: {"outstanding": 1} for t in ("AAA", "BBB", "CCC")}),
                              ["AAA", "BBB", "CCC"], run_id="j1", run_at=COMPLETE)
    uw = _UW({t: {"outstanding": 2} for t in ("AAA", "BBB", "CCC")})
    res = ownership.store_uw_shares(uw, ["AAA", "BBB", "CCC"], run_id="j2", run_at=LATER_SAME)
    assert uw.calls == ["CCC"] and res["left"] == 0


def test_a_new_session_asks_again(rawstore_tmp_db):
    ownership.store_uw_shares(_UW({"AAA": {"outstanding": 1}}), ["AAA"], run_id="j1", run_at=COMPLETE)
    uw = _UW({"AAA": {"outstanding": 2}})
    ownership.store_uw_shares(uw, ["AAA"], run_id="j2", run_at=NEXT_DAY)
    assert uw.calls == ["AAA"]
    assert [(r["session_date"], r["shares_outstanding"]) for r in _rows()] == \
        [(date(2026, 10, 8), 1.0), (date(2026, 10, 9), 2.0)]


# ── the reader (Asymmetric Growth, #34 D20) ─────────────────────────────────

def _seed(ticker: str, source: str, pct: float | None, run_at: float, session: date | None = None) -> None:
    """One row written by hand: a source the writer never fills an institutional share for, or a NULL field."""
    rawstore.write_rows(ownership.TABLE, [{
        "ticker": ticker, "source": source, "session_date": session or ownership.session_of(run_at)[0],
        "shares_outstanding": None, "insider_pct": None, "institutional_pct": pct,
        "fetched_at": run_at, "run_id": "seed", "run_at": run_at}])


JANUARY = datetime(2026, 1, 15, 21, 35, tzinfo=UTC).timestamp()     # Thu 16:35 EST: session 01-15, complete


def test_the_reader_takes_the_newest_session_and_labels_its_source(rawstore_tmp_db):
    _seed("AAA", ownership.YFINANCE, 0.31, JANUARY)
    _seed("AAA", ownership.YFINANCE, 0.55, COMPLETE)
    assert ownership.latest_institutional(["aaa", "BBB"]) == {
        "AAA": {"institutional_pct": 0.55, "source": "yfinance",
                "session": date(2026, 10, 8), "session_label": "2026-10-08"}}


def test_a_null_institutional_value_is_not_a_usable_row(rawstore_tmp_db):
    _seed("AAA", ownership.YFINANCE, 0.31, JANUARY)
    _seed("AAA", ownership.UW_INFO, None, COMPLETE)          # newer, but a shares-only row
    _seed("BBB", ownership.UW_INFO, None, COMPLETE)          # nothing usable at all
    out = ownership.latest_institutional(["AAA", "BBB"])
    assert set(out) == {"AAA"} and out["AAA"]["institutional_pct"] == 0.31
    assert out["AAA"]["source"] == "yfinance"


def test_inside_one_session_uw_wins_and_a_newer_session_still_beats_it(rawstore_tmp_db):
    _seed("TIE", ownership.YFINANCE, 0.55, COMPLETE)
    _seed("TIE", ownership.UW_INFO, 0.21, LATER_SAME)        # the same session: UW first
    _seed("NEWER", ownership.UW_INFO, 0.21, JANUARY)
    _seed("NEWER", ownership.YFINANCE, 0.55, COMPLETE)       # a newer session: it wins over the older UW row
    out = ownership.latest_institutional(["TIE", "NEWER"])
    assert (out["TIE"]["source"], out["TIE"]["institutional_pct"]) == ("uw_info", 0.21)
    assert (out["NEWER"]["source"], out["NEWER"]["institutional_pct"]) == ("yfinance", 0.55)
    assert out["NEWER"]["session_label"] == "2026-10-08"


def test_the_session_is_the_rows_own_column_the_one_the_ranking_used(rawstore_tmp_db):
    """The write already mapped ``run_at`` through ``session_of``, so the read takes that column and does not
    re-derive a session: ranking and display then answer by the same rule, and a ``run_at`` the holiday table
    cannot place (``session_for`` raises outside 2025-2028) cannot silently drop the date."""
    _seed("AAA", ownership.YFINANCE, 0.31, SATURDAY, session=date(2026, 10, 1))
    (row,) = ownership.latest_institutional(["AAA"]).values()
    assert (row["session"], row["session_label"]) == (date(2026, 10, 1), "2026-10-01")


def test_a_run_stamp_outside_the_holiday_table_still_reports_its_session(rawstore_tmp_db):
    """The case the re-derivation lost: ``run_at`` in 1970, a real ``session_date`` on the row."""
    _seed("AAA", ownership.YFINANCE, 0.31, 0.0, session=date(2026, 10, 8))
    (row,) = ownership.latest_institutional(["AAA"]).values()
    assert row["session_label"] == "2026-10-08"


def test_a_stored_zero_is_a_value_not_a_missing_one(rawstore_tmp_db):
    _seed("AAA", ownership.YFINANCE, 0.0, COMPLETE)          # a real zero is a value, not a missing one
    assert ownership.latest_institutional(["AAA"])["AAA"]["institutional_pct"] == 0.0


def test_an_empty_list_reads_nothing(rawstore_tmp_db):
    _seed("AAA", ownership.YFINANCE, 0.31, COMPLETE)
    assert ownership.latest_institutional([]) == {}
    assert ownership.latest_institutional(["", None]) == {}


def test_a_missing_table_answers_no_rows(rawstore_tmp_db):
    assert rawstore.query("SELECT 1 FROM ownership_snapshot") == []
    assert ownership.latest_institutional(["AAA"]) == {}


def test_an_unreadable_table_answers_no_rows(rawstore_tmp_db, monkeypatch):
    """A locked store must not take a board down: the reader says "nothing stored" and the board falls back."""
    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ownership.rawstore, "query", boom)
    assert ownership.latest_institutional(["AAA"]) == {}
