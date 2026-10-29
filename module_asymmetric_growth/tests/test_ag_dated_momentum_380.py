"""#380 AG S4 (scope A): the momentum signal reads stored daily bars, never a per-name Yahoo fetch.

Spec: ``.hermes/plans_claude/2026-10-16_r8_specs/380-ag-s4.md``, decisions 2-4. The momentum inputs come from the
stored bars of one ticker up to the newest session SPY has, so a Fetch spends no yfinance request on a technical
fallback, and the board says which price session it used. Since PR 11 (G3) those bars are the prices store's
(``rcdata.read.ticker_bars``, split basis: D01's old "nominal"), and since PR 10 (G4) the freeze anchor reads SPY's
bar there too.

The three D01 rules (decision 2, the sign of ``dist_52w_pct`` kept as ``technical_from_quote`` has it):

- ``dist_52w_pct = (high - price) / high`` over the last 252 sessions' **High** (``DIST_52W_MAX`` reads ``<=``);
- ``sma50`` = the last 50 closes' mean; ``above_sma50 = price > sma50``;
- ``vol_ratio`` = the last 10 sessions' mean volume / the last 63's (Yahoo's "3-month" average, the "90d" label).

Fewer bars than a window make that input unknown: 252 for the high, 63 for the volume, 50 for the mean, 10 for the
short volume. A name with a price but no bars keeps its card with unknown momentum (decision 3); only a name with no
price in any source (D01 close, ``financials.current_price``, the quote's ``regularMarketPrice``) drops as
``no_price``. Offline: a temp store.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from RevenueChaser.core import calendar as cal
from RevenueChaser.core import rawstore
from RevenueChaser.module_asymmetric_growth import engine, freeze
from RevenueChaser.rcdata.tests import prices_store

SESSION = date(2026, 10, 15)                                    # a Thursday
CLOSE = cal.session_close(SESSION).astimezone(timezone.utc)
EVENING = (CLOSE + timedelta(minutes=45)).timestamp()           # written after the close + 30 min

_QUOTE = {"regularMarketPrice": 100.0, "fiftyTwoWeekHigh": 105.0, "fiftyDayAverage": 90.0,
          "averageDailyVolume10Day": 160.0, "averageDailyVolume3Month": 100.0}
_ANNUAL = [{"fiscal_year": y, "revenue": r, "gross_profit": r * 0.6, "operating_cash_flow": r * 0.3,
            "capex": r * 0.05, "total_debt": 10.0, "cash": 200.0, "total_assets": 1000.0,
            "rd_expense": r * 0.2, "diluted_shares": 100.0 + i}
           for i, (y, r) in enumerate([(2021, 100.0), (2022, 130.0), (2023, 190.0)])]


# ── fixtures that other AG tests reuse (they fed momentum through ``quote`` before S4) ────────────────────────────

def sessions(end: date = SESSION, n: int = 260) -> list[date]:
    """The last ``n`` trading days up to ``end``, oldest first."""
    out, d = [], end
    while len(out) < n:
        if cal.is_trading_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def bar(ticker: str, day, *, close: float = 100.0, high: float = 105.0, low: float = 95.0,
        volume: float = 100.0, fetched_at: float | None = None) -> dict:
    """One ``price_bars_daily`` row as the writer stores it."""
    stamp = EVENING if fetched_at is None else fetched_at
    return {"ticker": ticker, "session_date": day, "open": 100.0, "high": high, "low": low, "close": close,
            "adj_close": None, "volume": volume, "split_ratio": None, "adj_source": None, "source": "uw",
            "fetched_at": stamp, "run_id": "seed", "run_at": stamp}


def store_bars(rows: list[dict]) -> None:
    """``bar`` rows in the prices store: the momentum's source since PR 11 and the freeze anchor's since PR 10."""
    prices_store.put_bars(rows, replace=False, overwrite=True)


def put_action(ticker: str, ex: date, action: str, value: float) -> None:
    prices_store.put_action(ticker, ex, action, value, fetched_at=EVENING)


def seed_momentum(tickers, *, session: date = SESSION, n: int = 260) -> list[date]:
    """Stored bars that pass all three momentum rules for ``tickers`` (and SPY, the session's anchor).

    Each series: 250 sessions at close 100 and 10 at 102 (price above its own 50-day mean, 100.4); every high 105
    (2.9% off the high) and the last 10 sessions' volume 160 against the 63-session mean 109.5 (1.46x).
    """
    days = sessions(session, n)
    rows = []
    for ticker in {*tickers, "SPY"}:
        for k, day in enumerate(days):
            tail = k >= len(days) - 10
            rows.append(bar(ticker, day, close=102.0 if tail else 100.0, volume=160.0 if tail else 100.0))
    store_bars(rows)
    return days


def edgar(ticker: str, annual=None, is_us: bool = True) -> dict:
    return {"ticker": ticker, "annual": annual if annual is not None else list(_ANNUAL), "quarterly": [],
            "meta": {"source": "sec_edgar", "entity_name": f"{ticker} Corp", "is_us": is_us, "available": True}}


def universe(monkeypatch, rows) -> None:
    """A scan over exactly ``rows`` (no network): the universe, 13F totals and the spine are stubbed."""
    monkeypatch.setattr(engine, "fetch_universe_stats", lambda *a, **k: (rows, {}))
    import RevenueChaser.module_sec_13f.engine as s13
    monkeypatch.setattr(s13, "load_totals", lambda: {})
    monkeypatch.setattr(engine._spine, "spine_gics", lambda *a, **k: {})
    monkeypatch.setattr(engine._spine, "excluded_tickers", lambda: set())


def row(ticker: str, quote=None) -> dict:
    return {"ticker": ticker, "quote": dict(_QUOTE if quote is None else quote), "market_cap": 5e9}


def steps(result: dict) -> dict:
    return {s["key"]: s for s in result["funnel_steps"]}


def momentum(card: dict) -> dict:
    return next(s for s in card["signals"] if s["key"] == "momentum")


# ── 1. a >= 252-bar fixture, hand-computed ───────────────────────────────────────────────────────────────────────

def _hand_rows(days: list[date]) -> list[dict]:
    """260 bars whose windows are told apart:

    - the high is 200 for the 8 bars before the last 252 and 120 at the 252nd session from the end (a 251-session
      window would read 110), 110 for the rest;
    - closes are 100 except the 50th session from the end (101) and the last one (103), so the 50-close mean is
      100.08 and the price is above it;
    - volume is 60 for the 27 sessions between the last 63 and the last 90 (a 90-session mean would read 94.67),
      100 in the last 63 but the last 10, and 160 in the last 10.
    """
    rows = []
    for k, day in enumerate(days):
        high, close, volume = 110.0, 100.0, 100.0
        if k <= 7:
            high = 200.0
        elif k == 8:
            high = 120.0
        if k == len(days) - 50:
            close = 101.0
        if k == len(days) - 1:
            close = 103.0
        if 170 <= k <= 196:
            volume = 60.0
        if k >= len(days) - 10:
            volume = 160.0
        rows.append(bar("AAA", day, close=close, high=high, volume=volume))
    return rows


def test_a_year_of_bars_gives_the_momentum_inputs():
    days = sessions()
    rows = _hand_rows(days)
    t = engine.technical_from_bars(rows, SESSION)

    assert t["price"] == 103.0                                     # the last close
    assert t["high_52w"] == 120.0                                  # the 200s are older than 252 sessions
    assert t["dist_52w_pct"] == pytest.approx((120.0 - 103.0) / 120.0)      # (high - price) / high, >= 0
    assert t["sma50"] == pytest.approx((48 * 100.0 + 101.0 + 103.0) / 50)
    assert t["above_sma50"] is True
    assert t["vol_10d"] == 160.0
    assert t["vol_90d"] == pytest.approx((53 * 100.0 + 10 * 160.0) / 63)     # 3-month = 63 sessions, not 90
    assert t["vol_ratio"] == pytest.approx(160.0 * 63 / (53 * 100.0 + 10 * 160.0))


def test_a_bar_after_the_scored_session_is_ignored():
    days = sessions()
    rows = _hand_rows(days)
    later = bar("AAA", days[-1] + timedelta(days=1), close=1.0, high=999.0, volume=99999.0)
    assert engine.technical_from_bars(rows + [later], SESSION) == engine.technical_from_bars(rows, SESSION)


def test_fewer_bars_than_a_window_make_only_that_input_unknown():
    days = sessions()
    rows = _hand_rows(days)
    assert engine.technical_from_bars(rows[-251:], SESSION)["high_52w"] is None
    assert engine.technical_from_bars(rows[-251:], SESSION)["dist_52w_pct"] is None
    short = engine.technical_from_bars(rows[-62:], SESSION)
    assert short["vol_90d"] is None and short["vol_ratio"] is None
    assert short["vol_10d"] is not None and short["sma50"] is not None
    mean = engine.technical_from_bars(rows[-49:], SESSION)
    assert mean["sma50"] is None and mean["above_sma50"] is None
    assert engine.technical_from_bars(rows[-9:], SESSION)["vol_10d"] is None
    empty = engine.technical_from_bars([], SESSION)
    assert set(empty) == {"price", "bar_date", "high_52w", "dist_52w_pct", "sma50", "above_sma50",
                          "vol_10d", "vol_90d", "vol_ratio"}                # bar_date: the card's session (PR 6)
    assert all(v is None for v in empty.values())


# ── 2. the per-name Yahoo fallback is gone ───────────────────────────────────────────────────────────────────────

def test_a_fetch_never_calls_fetch_technical(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.core.providers import technical as technical_provider
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("fetch_technical must not run on an AG Fetch")
    monkeypatch.setattr(technical_provider, "fetch_technical", boom)
    assert not hasattr(engine, "fetch_technical"), "the fallback import must be gone, not just unused"

    seed_momentum(["D01ONLY"])
    rawstore.write_rows("edgar_financials", [edgar("D01ONLY"), edgar("NOPRICE")])
    universe(monkeypatch, [row("D01ONLY", quote={}), row("NOPRICE", quote={})])
    res = engine.run_asymmetric_growth_hunter(None, max_workers=1, no_fetch=True)

    assert calls == []
    assert [c["ticker"] for c in res["cards"]] == ["D01ONLY"]
    assert res["summary"]["score_errors"] == 0                     # a restored fallback would raise here
    assert steps(res)["no_price"]["tickers"] == ["NOPRICE"]


# ── 3. a name with a price but no bars: a card with unknown momentum (decision 3) ────────────────────────────────

def test_a_name_with_a_price_but_no_bars_is_a_card_with_unknown_momentum(rawstore_tmp_db, monkeypatch):
    seed_momentum(["HASBARS"])                                     # SPY too: the session exists
    rawstore.write_rows("edgar_financials", [edgar("HASBARS"), edgar("QUOTEONLY"), edgar("NOPRICE")])
    universe(monkeypatch, [row("HASBARS", quote={}),
                           row("QUOTEONLY", quote={"regularMarketPrice": 42.0}),
                           row("NOPRICE", quote={})])
    res = engine.run_asymmetric_growth_hunter(None, max_workers=1, no_fetch=True)

    cards = {c["ticker"]: c for c in res["cards"]}
    assert sorted(cards) == ["HASBARS", "QUOTEONLY"]               # no bars is not a drop
    assert steps(res).get("no_price", {}).get("tickers") == ["NOPRICE"]   # no price in any source is
    q = cards["QUOTEONLY"]
    assert q["price"] == 42.0                                      # the quote's price, the only source
    assert momentum(q)["passed"] == 0 and momentum(q)["known"] == 0       # unknown, never a fail
    assert [s["passed"] for s in momentum(q)["subs"]] == [None, None, None]
    assert momentum(cards["HASBARS"])["passed"] == 3              # bars: the momentum the fixture passes
    assert [p["subject"] for p in freeze.picks(res["cards"])] == ["HASBARS", "QUOTEONLY"]
    assert res["data_as_of"] == SESSION.isoformat()                # the newest D01 session among the cards


# ── 4. a 2:1 split inside the window reads nominal (split-continuous) bars ───────────────────────────────────────

def test_a_split_inside_the_window_reads_nominal_bars(rawstore_tmp_db):
    """``prices.daily_bars``'s nominal basis divides the bars written before a split (``d < ex``, fetched before the
    ex-date's open) by its ratio, so a pre-split bar stored at 100 reads as 50 after a 2:1 split: the series is
    split-continuous, and the momentum facts equal those of the same series stored already split."""
    days = sessions()
    ex = days[-100]                                                # the split's ex-date, inside the window
    rows = [bar("SPL", d, close=100.0, high=105.0, volume=100.0, fetched_at=1.0) for d in days if d < ex]
    rows += [bar("SPL", d, close=50.0, high=52.5, volume=200.0) for d in days if d >= ex]
    rows += [bar("SPY", d) for d in days[-5:]]
    store_bars(rows)
    put_action("SPL", ex, "split", 2.0)

    preload = engine._preload(["SPL"], True)
    assert set(preload["bars"]) == {"SPL"} and preload["session"] == SESSION
    got = engine.technical_from_bars(preload["bars"]["SPL"], preload["session"])

    no_split = [bar("SPL", d, close=50.0, high=52.5, volume=200.0) for d in days]
    assert got == engine.technical_from_bars(no_split, SESSION)
    assert (got["price"], got["high_52w"]) == (50.0, 52.5)
    assert got["dist_52w_pct"] == pytest.approx((52.5 - 50.0) / 52.5)
    assert got["vol_ratio"] == 1.0 and got["above_sma50"] is False


def test_the_window_starts_380_days_before_the_session(rawstore_tmp_db):
    days = sessions(n=300)                                         # ~420 calendar days: some bars fall outside
    store_bars([bar("OLD", d) for d in days] + [bar("SPY", d) for d in days[-5:]])
    preload = engine._preload(["OLD"], True)
    kept = [str(r["session_date"])[:10] for r in preload["bars"]["OLD"]]
    assert min(kept) >= (SESSION - timedelta(days=380)).isoformat() and len(kept) < len(days)


def test_a_dividend_in_the_window_does_not_move_the_nominal_momentum(rawstore_tmp_db):
    """Nominal applies splits only; an amount (the adjusted basis) would move the highs and so the distance."""
    days = sessions()
    ex = days[-50]
    store_bars([bar("DIV", d) for d in days] + [bar("SPY", d) for d in days[-5:]])
    put_action("DIV", ex, "dividend", 1.0)

    preload = engine._preload(["DIV"], True)
    got = engine.technical_from_bars(preload["bars"]["DIV"], preload["session"])
    assert (got["price"], got["high_52w"]) == (100.0, 105.0)        # the nominal levels, no amount applied
    assert got["dist_52w_pct"] == pytest.approx((105.0 - 100.0) / 105.0)


# ── 5. a passing quote with failing bars follows the bars ───────────────────────────────────────────────────────

def test_a_passing_quote_with_failing_bars_follows_the_bars(rawstore_tmp_db, monkeypatch):
    seed_momentum([])                                              # SPY's session only
    store_bars([bar("SLIDE", d, close=50.0, high=100.0, volume=100.0) for d in sessions()])
    rawstore.write_rows("edgar_financials", [edgar("SLIDE")])
    universe(monkeypatch, [row("SLIDE")])                          # the quote passes all three rules
    res = engine.run_asymmetric_growth_hunter(None, max_workers=1, no_fetch=True)

    (card,) = res["cards"]
    assert card["price"] == 50.0                                   # the D01 close, not the quote's 100
    assert momentum(card)["known"] == 3 and momentum(card)["passed"] == 0
    assert [s["passed"] for s in momentum(card)["subs"]] == [False, False, False]


# ── 10. no D01 rows at all: the 247 research script's path still scores ─────────────────────────────────────────

def test_a_store_without_d01_rows_still_scores(rawstore_tmp_db):
    empty = engine._preload(["AAA"], True)
    assert empty["bars"] == {} and empty["session"] is None
    rawstore.write_rows("edgar_financials", [edgar("AAA")])
    preload = engine._preload(["AAA"], True)
    preload["as_of"] = date.min
    card = engine.enrich_and_score("AAA", row("AAA"), no_fetch=True, spine={}, preload=preload)
    assert card is not None and card["price"] == 100.0             # the quote's price, momentum unknown
    assert momentum(card)["known"] == 0


def test_the_per_ticker_path_reads_the_same_bars(rawstore_tmp_db):
    """``enrich_and_score`` without a batch reads the ticker's D01 window itself (research scripts)."""
    seed_momentum(["ONE"])
    rawstore.write_rows("edgar_financials", [edgar("ONE")])
    direct = engine.enrich_and_score("ONE", row("ONE"), no_fetch=True, spine={})
    batch = engine._preload(["ONE"], True)
    via_preload = engine.enrich_and_score("ONE", row("ONE"), no_fetch=True, spine={}, preload=batch)
    assert batch["session"] == SESSION and direct["price"] == 102.0    # the last close of the fixture
    assert direct["price"] == via_preload["price"] and momentum(direct) == momentum(via_preload)


def test_a_zero_short_volume_is_a_zero_ratio_not_unknown():
    days = sessions()
    rows = [bar("AAA", d, volume=0.0 if k >= len(days) - 10 else 100.0) for k, d in enumerate(days)]
    t = engine.technical_from_bars(rows, SESSION)
    assert t["vol_10d"] == 0.0 and t["vol_ratio"] == 0.0


def test_one_price_session_cutoff_serves_every_preload_batch(rawstore_tmp_db, monkeypatch):
    seen = []
    monkeypatch.setattr(engine, "_bars_by_ticker", lambda tickers, session: seen.append(session) or {})
    monkeypatch.setattr(engine, "_price_session", lambda: date(2026, 10, 1))
    first = engine._preload([], True, session=date(2026, 9, 30))
    assert first["session"] == date(2026, 9, 30)                       # a given cutoff is used, not re-read
    assert engine._preload([], True)["session"] == date(2026, 10, 1)   # none given: the newest SPY session
