"""The R3 review's four SHOULD findings on PR #434 (reviews/2026-10-15_pr434_r3_review.md).

- **S1:** a name whose newest D01 bar is before SPY's newest D01 session (a halt, a Daily Bars ``failed_both`` name)
  drops at the scan as ``stale_bars``, a data-gap step in the funnel, so it can never become a pick and refuse the
  whole board's freeze.
- **S2:** D01 is append-only, so a pick's newest bar always carries its own session's ``fetched_at``. When D01 holds
  SPY's complete session bar (Daily Bars has run), a pick without its own is ``no_session_bar``: a later Fetch will
  not change it. Only before Daily Bars has written the session is it ``needs_refetch``.
- **S3:** the scan reads D01 once per chunk of names (one bars read and one actions read, both bases derived from
  it), not four store reads per name. The frames equal the per-name read.
- **S4:** a D04 row without an average volume (UW, ``fast_info``) falls back to the newest Yahoo screener row that
  carries one; a name with none anywhere is kept and counted (``data_quality["no_avg_volume"]``), not silently
  passed.
Offline: a temp store, the calendar seam as the clock.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from RevenueChaser.core import calendar as cal
from RevenueChaser.core import ledger, prices, rawstore, screen_common
from RevenueChaser.core.tests import screen_parity as scn
from RevenueChaser.module_fundamentals import freeze as fund_freeze
from RevenueChaser.module_fundamentals import pipeline as fp
from RevenueChaser.module_swing_potential import freeze as swing_freeze
from RevenueChaser.module_swing_potential import pipeline as sp

D = date(2026, 10, 14)
PREV = date(2026, 10, 13)
NOW = datetime(2026, 10, 14, 21, 45, tzinfo=timezone.utc)      # 17:45 EDT: the 14 Oct session is complete
CFG = SimpleNamespace(MIN_PRICE=5.0, MIN_DOLLAR_VOLUME=250_000.0)
BOARDS = [pytest.param(sp, swing_freeze, id="swing"), pytest.param(fp, fund_freeze, id="fundamentals")]


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(cal, "_now", lambda: NOW)


def _evening(d: date) -> float:
    return datetime(d.year, d.month, d.day, 20, 40, tzinfo=timezone.utc).timestamp()


def _sessions(end: date, n: int) -> list[date]:
    return [d.date() for d in pd.bdate_range(end=end, periods=n)]


def _seed(ticker: str, days: list[date], actions=()):
    rawstore.write_rows("price_bars_daily", [{
        "ticker": ticker, "session_date": d, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
        "adj_close": None, "volume": 1.0e6, "split_ratio": None, "adj_source": None, "source": "uw",
        "fetched_at": _evening(d), "run_id": "seed", "run_at": _evening(d)}
        for i, d in enumerate(days) for c in [(100.0 if i < len(days) // 2 else 50.0) + 0.01 * i]])
    if actions:
        rawstore.write_rows("price_actions", [
            {"ticker": ticker, "ex_date": ex, "action": kind, "value": v, "session_date": ex,
             "source": "yfinance", "fetched_at": _evening(ex), "run_id": "seed", "run_at": _evening(ex)}
            for ex, kind, v in actions])


def _halt_up1(monkeypatch=None):
    """UP1's D01 series ends on 13 Oct, written that evening; SPY and every other name have 14 Oct. The scenario has
    only nine scored names, so one gap is 11% of them: the drop share is raised to let it drop (``monkeypatch``)."""
    if monkeypatch is not None:
        monkeypatch.setattr(screen_common, "STALE_DROP_MAX_SHARE", 0.2)
    with rawstore.connect(db=rawstore.MANIFEST["price_bars_daily"].get("db", "raw")) as conn:
        conn.execute("DELETE FROM price_bars_daily WHERE ticker = 'UP1'")
    kind, drift = scn.BARS["UP1"]
    scn.seed_d01("UP1", scn._bars(kind, drift, scn.LONG_BARS, end=PREV), fetched_at=_evening(PREV))


# ── S1: a name with no bar of SPY's session drops as stale_bars ──────────────

@pytest.mark.parametrize("pipe,frz", BOARDS)
def test_a_name_without_spys_session_bar_drops_as_stale_bars_and_the_day_freezes(rawstore_tmp_db, monkeypatch,
                                                                                  pipe, frz):
    scn.seed()
    scn.patch(monkeypatch)
    _halt_up1(monkeypatch)
    payload = pipe.run_scan()
    assert "UP1" not in {c["ticker"] for c in payload["candidates"]}
    step = {s["key"]: s for s in payload["funnel_steps"]}["stale_bars"]
    assert step["count"] == 1 and step["tickers"] == ["UP1"] and step["by_design"] is False
    assert step["label"] == "No D01 bar of the latest stored session (a halt or a Daily Bars gap)"
    keys = [s["key"] for s in payload["funnel_steps"]]
    assert keys.index("stale_bars") == keys.index("no_bars") + 1        # a data gap next to its sibling
    assert payload["data_quality"]["dropped"]["stale_bars"] == 1
    out = frz.freeze_payload(payload, run_id="s1", now=scn.NOW)
    assert out.frozen == len(payload["candidates"]) > 0, out.message


def test_before_daily_bars_has_run_no_name_drops_as_stale(rawstore_tmp_db, monkeypatch):
    """SPY's newest D01 session is the reference, not the calendar's: when Daily Bars has not written 14 Oct yet,
    every name ends on 13 Oct like SPY and none drops (the freeze's SPY anchor refuses that day instead)."""
    scn.seed()
    scn.patch(monkeypatch)
    _halt_up1(monkeypatch)
    with rawstore.connect(db=rawstore.MANIFEST["price_bars_daily"].get("db", "raw")) as conn:
        conn.execute("DELETE FROM price_bars_daily WHERE session_date = DATE '2026-10-14'")
    payload = sp.run_scan()
    assert "stale_bars" not in {s["key"] for s in payload["funnel_steps"]}
    assert "stale_bars" not in payload["data_quality"]["dropped"]
    assert payload["data_quality"]["stale_ohlc"] == 8          # every scored name, UP1 too, is stale and scored


def test_one_gap_over_the_drop_share_is_a_daily_bars_failure_and_nothing_drops(rawstore_tmp_db, monkeypatch):
    """One of nine names is over the 5% share: a failed Daily Bars run never empties the board. The name is scored
    (marked stale) and the freeze refuses the day, as before S1."""
    scn.seed()
    scn.patch(monkeypatch)
    _halt_up1()
    payload = sp.run_scan()
    assert "stale_bars" not in {s["key"] for s in payload["funnel_steps"]}
    assert "UP1" in {c["ticker"] for c in payload["candidates"]}
    out = swing_freeze.freeze_payload(payload, run_id="s1", now=scn.NOW)
    assert out.frozen == 0 and "UP1" in out.reason


def test_the_drop_share_is_a_share_of_the_scored_names(rawstore_tmp_db, clock, monkeypatch):
    _seed("SPY", _sessions(D, 3))
    for t in ("A", "B", "C", "D"):
        _seed(t, _sessions(D, 3))
    _seed("H", _sessions(PREV, 3))
    names = ["A", "B", "C", "D", "H"]
    monkeypatch.setattr(screen_common, "STALE_DROP_MAX_SHARE", 0.2)
    assert screen_common.stale_names(names, D) == {"H"}                 # 1 of 5 = 20%: drops
    monkeypatch.setattr(screen_common, "STALE_DROP_MAX_SHARE", 0.19)
    assert screen_common.stale_names(names, D) == set()                # over the share: none drops
    assert screen_common.stale_names(names, None) == set()
    assert screen_common.stale_names(["NONE"], D) == set()               # no bars at all is ``no_bars``, not stale


# ── S2: the cause of a missing pick bar ──────────────────────────────────────

def test_a_pick_without_its_session_bar_after_daily_bars_ran_is_no_session_bar(rawstore_tmp_db, clock):
    _seed("SPY", _sessions(D, 5))
    _seed("FRESH", _sessions(D, 5))
    _seed("HALTED", _sessions(PREV, 5))
    assert ledger.ohlc_gaps(D, ["FRESH", "HALTED"]) == {"HALTED": ledger.NO_SESSION_BAR}


def test_before_daily_bars_has_run_a_missing_pick_bar_still_needs_a_refetch(rawstore_tmp_db, clock):
    _seed("SPY", _sessions(PREV, 5))
    _seed("HALTED", _sessions(PREV, 5))
    assert ledger.ohlc_gaps(D, ["HALTED"]) == {"HALTED": ledger.NEEDS_REFETCH}
    assert ledger.ohlc_gaps(D, ["NONE"]) == {"NONE": ledger.NO_SESSION_BAR}


def test_the_refusal_note_for_a_halted_pick_says_a_fetch_will_not_change_it(rawstore_tmp_db, clock):
    _seed("SPY", _sessions(D, 5))
    _seed("HALTED", _sessions(PREV, 5))
    board = {"candidates": [{"ticker": "HALTED"}]}
    out = ledger.freeze_board("swing", "Swing", board, rows_key="candidates",
                              to_rows=lambda items: [{"subject": i["ticker"], "rank": 1} for i in items],
                              model_version="t", run_id="s2", now=NOW, check_spy=False,
                              bar_gaps=ledger.ohlc_gaps)
    assert out.frozen == 0
    assert "HALTED" in out.reason and "a later Fetch will not change it" in out.reason
    assert "can complete the day" not in out.reason


# ── S3: one D01 read per chunk ───────────────────────────────────────────────

def test_the_batched_read_equals_the_per_name_read(rawstore_tmp_db, clock):
    days = _sessions(D, 900)
    _seed("SPY", days)
    _seed("AAA", days, actions=[(days[700], "split", 2.0), (days[850], "dividend", 1.0)])
    _seed("BBB", _sessions(PREV, 300))
    batch = screen_common.load_bars_many(["AAA", "BBB", "NONE"])
    start = D - pd.Timedelta(days=screen_common.BAR_DAYS).to_pytimedelta()
    for t in ("AAA", "BBB"):            # the read before S3: one ``daily_bars`` call per basis and name
        nominal = screen_common._ohlcv(prices.daily_bars([t], basis="nominal", start=start))
        adjusted = screen_common._ohlcv(prices.daily_bars([t], basis="adjusted", start=start))
        pd.testing.assert_frame_equal(batch[t][0], nominal)
        pd.testing.assert_frame_equal(batch[t][1], adjusted)
        assert batch[t][0].attrs["stale"] == (nominal.index[-1].date() < D)
        assert screen_common.load_bars(t)[0].equals(batch[t][0])
    assert batch["NONE"][0].empty and batch["NONE"][1].empty
    assert batch["BBB"][0].attrs["stale"] is True


def test_both_bases_come_from_one_read(rawstore_tmp_db, clock):
    days = _sessions(D, 300)
    _seed("AAA", days, actions=[(days[150], "split", 2.0), (days[280], "dividend", 1.0)])
    both = prices.daily_bars_bases(["AAA"], start=days[10])
    for basis in ("nominal", "adjusted"):
        pd.testing.assert_frame_equal(both[basis], prices.daily_bars(["AAA"], basis=basis, start=days[10]))
    nominal, adjusted = both["nominal"].set_index("session_date"), both["adjusted"].set_index("session_date")
    assert nominal["close"].iloc[-1] == adjusted["close"].iloc[-1]                   # after the dividend: equal
    before = pd.Timestamp(days[279])
    c_prev = nominal.loc[before, "close"]
    assert adjusted.loc[before, "close"] == pytest.approx(c_prev * (1 - 1.0 / c_prev))     # the dividend: adjusted
    assert nominal.loc[pd.Timestamp(days[100]), "close"] == pytest.approx((100.0 + 0.01 * 100) / 2)   # the split


@pytest.mark.parametrize("pipe", [sp, fp], ids=["swing", "fundamentals"])
def test_a_scan_reads_d01_once_per_chunk_not_per_name(rawstore_tmp_db, monkeypatch, pipe):
    scn.seed()
    scn.patch(monkeypatch)
    calls: list[str] = []

    def counting(real):
        def counted(sql, params=None):
            names = [p for p in params or [] if isinstance(p, str)]
            if ("price_bars_daily b" in sql or "FROM price_actions" in sql) and names != ["SPY"]:   # not the baseline
                calls.append(sql)
            return real(sql, params)
        return counted

    monkeypatch.setattr(prices, "_read", counting(prices._read))
    monkeypatch.setattr(prices, "_read_columns", counting(prices._read_columns))   # the bars, as columns (#383)
    payload = pipe.run_scan()
    assert payload["candidates"]
    assert len(calls) == 2                       # one bars read and one actions read for the whole (single) chunk


def test_the_chunks_cover_every_name(rawstore_tmp_db, monkeypatch):
    scn.seed()
    scn.patch(monkeypatch)
    _halt_up1(monkeypatch)
    monkeypatch.setattr(screen_common, "BARS_CHUNK", 2)
    small = sp.run_scan()
    monkeypatch.setattr(screen_common, "BARS_CHUNK", 1000)
    whole = sp.run_scan()
    assert small["candidates"]
    assert [c["ticker"] for c in small["candidates"]] == [c["ticker"] for c in whole["candidates"]]
    assert small["funnel_steps"] == whole["funnel_steps"]


# ── S4: no average volume is never a silent pass ─────────────────────────────

def _q(ticker, session, price, *, v10=None, source="yfinance", payload=None):
    p = payload if payload is not None else (
        {"regularMarketPrice": price, "averageDailyVolume10Day": v10} if v10 is not None
        else {"regularMarketPrice": price})
    return {"ticker": ticker, "session_date": session, "source": source, "market_cap": 1.0e9, "price": price,
            "payload": p, "fetched_at": 0.0, "run_id": "r", "run_at": 0.0}


def test_a_row_without_volume_falls_back_to_the_newest_yahoo_average(rawstore_tmp_db):
    from RevenueChaser.core import quotes
    rawstore.write_rows("quote_snapshot", [
        _q("THIN", PREV, 10.0, v10=1_000.0), _q("THIN", D, 10.0, source="uw", payload={"close": "10"}),
        _q("FAST", date(2026, 10, 12), 10.0, v10=90_000.0), _q("FAST", PREV, 10.0, v10=50_000.0),
        _q("FAST", D, 10.0, payload={"via": "fast_info", "shares": 1.0, "last_price": 10.0}),
        _q("UWONLY", D, 10.0, source="uw", payload={"close": "10"})])
    got = quotes.prefilter_quotes(["THIN", "FAST", "UWONLY"])
    assert got["THIN"]["avg_volume"] == 1_000.0 and got["THIN"]["session_date"] == D
    assert got["FAST"]["avg_volume"] == 50_000.0                 # the newest Yahoo row that has one
    assert got["UWONLY"]["avg_volume"] is None
    pf = screen_common.prefilter([{"ticker": t} for t in ("THIN", "FAST", "UWONLY")], CFG)
    assert pf.drops == {"low_dollar_volume": ["THIN"]}
    assert [r["ticker"] for r in pf.kept] == ["FAST", "UWONLY"]
    assert pf.no_volume == ["UWONLY"]


@pytest.mark.parametrize("pipe", [sp, fp], ids=["swing", "fundamentals"])
def test_the_payload_counts_names_kept_without_a_volume(rawstore_tmp_db, monkeypatch, pipe):
    scn.seed()
    scn.patch(monkeypatch)
    with rawstore.connect(db=rawstore.MANIFEST["quote_snapshot"].get("db", "raw")) as conn:
        conn.execute("DELETE FROM quote_snapshot WHERE ticker = 'UP2'")
    rawstore.write_rows("quote_snapshot", [_q("UP2", scn.SESSION, 50.0, source="uw", payload={"close": "50"})])
    dq = pipe.run_scan()["data_quality"]
    assert dq["no_avg_volume"] == 1
