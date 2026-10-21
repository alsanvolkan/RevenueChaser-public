"""K8a speed (#383, 2026-10-16): the vectorised event engine and the read bounded at ``end``.

- ``apply_events`` and the frame path (``_bars_frame``) equal the per-row code they replace EXACTLY: the same floats
  (no tolerance), the same None, ``attrs['skipped_events']``, the fetched_at / T(e) rule and the volume rule. The
  per-row code is kept verbatim below (``_old_*``) and both run on seeded random fixtures.
- The frame path never loops ``apply_events`` per ticker.
- With ``end`` the bar read stops at ``end``. An event after ``end`` still adjusts every bar before it (both factors
  use ``d_i < e``), so the actions stay unbounded, and the adjusted basis also reads, for each dividend or capital
  gain after ``end``, the one bar its ``C_prev`` comes from. ``daily_bars(start, end)`` equals the unbounded read
  sliced, on both bases.
Offline; the store tests use a temp store.
"""
from __future__ import annotations

import math
import random
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from RevenueChaser.core import prices, rawstore
from RevenueChaser.core.prices import _BAR_COLUMNS, _f, _placeholders, _read, _threshold_epoch


# ── the per-row implementation before #383 K8a speed (main a81f8de), verbatim but for names ──────────────────

class _OldEventRows(list):
    """A list of bar rows that also carries ``attrs`` (``skipped_events``)."""

    def __init__(self, *args, attrs: dict | None = None):
        super().__init__(*args)
        self.attrs = attrs or {}


def _old_apply_events(bars: list[dict], actions: list[dict], basis: str) -> list[dict]:
    if basis not in ("nominal", "adjusted"):
        raise ValueError(f"unknown basis {basis!r}")
    ordered = sorted(bars, key=lambda b: b["session_date"])
    splits = [a for a in actions if a.get("action") == "split" and _f(a.get("value"))]
    amounts = [a for a in actions if a.get("action") in ("dividend", "capital_gain")
               and _f(a.get("value"))]
    skipped = 0
    nominal: list[dict] = []
    for b in ordered:
        d = b["session_date"]
        fetched = float(b.get("fetched_at") or 0.0)
        k = 1.0
        for s in splits:
            if d < s["ex_date"] and fetched < _threshold_epoch(s["ex_date"]):
                k *= float(s["value"])
        row = dict(b)
        row["open"] = None if b.get("open") is None else b["open"] / k
        row["high"] = None if b.get("high") is None else b["high"] / k
        row["low"] = None if b.get("low") is None else b["low"] / k
        row["close"] = None if b.get("close") is None else b["close"] / k
        row["volume"] = None if b.get("volume") is None else b["volume"] * k
        row["split_ratio"] = None
        nominal.append(row)

    if basis == "nominal":
        return _OldEventRows(nominal, attrs={"skipped_events": 0})

    factors: list[tuple[date, float]] = []
    for a in amounts:
        e = a["ex_date"]
        c_prev = None
        for b in nominal:
            if b["session_date"] < e:
                c_prev = b["close"]
            else:
                break
        if c_prev is None or c_prev <= 0:
            skipped += 1
            continue
        a_prime = float(a["value"])
        fetched = float(a.get("fetched_at") or 0.0)
        for s in splits:
            if e < s["ex_date"] and fetched < _threshold_epoch(s["ex_date"]):
                a_prime /= float(s["value"])
        m = 1.0 - a_prime / c_prev
        if m <= 0.0 or m >= 1.0:
            skipped += 1
            continue
        factors.append((e, m))

    out: list[dict] = []
    for row in nominal:
        d = row["session_date"]
        m = 1.0
        for e, f in factors:
            if d < e:
                m *= f
        adj = dict(row)
        for f in ("open", "high", "low", "close"):
            adj[f] = None if row.get(f) is None else row[f] * m
        out.append(adj)
    return _OldEventRows(out, attrs={"skipped_events": skipped})


def _old_read_series(tickers: list[str], start: date | None = None):
    bars: dict[str, list[dict]] = {}
    actions: dict[str, list[dict]] = {}
    if not tickers:
        return bars, actions
    marks = _placeholders(len(tickers))
    if start is None:
        bar_sql, bar_params = f"SELECT * FROM price_bars_daily WHERE ticker IN ({marks})", list(tickers)
    else:
        bar_sql = (f"SELECT b.* FROM price_bars_daily b WHERE b.ticker IN ({marks}) AND b.session_date >= "
                   f"COALESCE((SELECT MAX(p.session_date) FROM price_bars_daily p "
                   f"WHERE p.ticker = b.ticker AND p.session_date < ?), ?)")
        bar_params = [*tickers, start, start]
    for r in _read(bar_sql, bar_params):
        bars.setdefault(r["ticker"], []).append(r)
    for r in _read(f"SELECT * FROM price_actions WHERE ticker IN ({marks})", list(tickers)):
        actions.setdefault(r["ticker"], []).append(r)
    return bars, actions


def _old_bars_frame(names, bars, actions, basis, start, end) -> pd.DataFrame:
    empty = pd.DataFrame(columns=_BAR_COLUMNS)
    empty.attrs["skipped_events"] = 0
    rows: list[dict] = []
    skipped = 0
    for t in names:
        if t not in bars:
            continue
        acts = actions.get(t, [])
        if start is not None:
            acts = [a for a in acts if a["ex_date"] > start]
        series = _old_apply_events(bars[t], acts, basis)
        skipped += series.attrs.get("skipped_events", 0)
        rows.extend(series)
    if not rows:
        empty.attrs["skipped_events"] = skipped
        return empty
    df = pd.DataFrame(rows)
    df["session_date"] = pd.to_datetime(df["session_date"])
    if start is not None:
        df = df[df["session_date"] >= pd.Timestamp(start)]
    if end is not None:
        df = df[df["session_date"] <= pd.Timestamp(end)]
    df = df[_BAR_COLUMNS].sort_values(["ticker", "session_date"]).reset_index(drop=True)
    df.attrs["skipped_events"] = skipped
    return df


def _old_daily_bars(tickers, basis, start=None, end=None) -> pd.DataFrame:
    names = prices._norm(tickers)
    bars, actions = _old_read_series(names, start) if names else ({}, {})
    return _old_bars_frame(names, bars, actions, basis, start, end)


# ── seeded random fixtures ─────────────────────────────────────────────────────────────────────────────────────

FIRST = date(2026, 1, 5)


def _day(i: int) -> date:
    return FIRST + timedelta(days=i)


def _stamp(rng: random.Random, span: int) -> float | None:
    """A fetched_at: None, 0, or around some day's T(e) — exactly on it, a second either side, or that evening."""
    pick = rng.random()
    if pick < 0.08:
        return None
    if pick < 0.14:
        return 0.0
    thr = _threshold_epoch(_day(rng.randrange(-5, span + 5)))
    return thr + rng.choice([0.0, -1.0, 1.0, 12 * 3600.0, -30 * 86400.0])


def _price(rng: random.Random, store_like: bool):
    pick = rng.random()
    if pick < 0.08:
        return None
    if not store_like and pick < 0.12:
        return rng.choice([0.0, -3.0, 7])                  # c_prev <= 0, an int
    return rng.uniform(1.0, 120.0)


def _random_bars(rng: random.Random, ticker: str, span: int, store_like: bool) -> list[dict]:
    days = sorted(rng.sample(range(span), rng.randrange(0, min(span, 45))))
    if not store_like and days and rng.random() < 0.15:
        days.append(rng.choice(days))                     # a duplicate session: the stable sort decides C_prev
    rows = []
    for i in days:
        vol = None if rng.random() < 0.08 else rng.choice([rng.uniform(1e3, 1e7), float(rng.randrange(1, 10**7))])
        if not store_like and vol is not None and rng.random() < 0.2:
            vol = int(vol)
        f = _stamp(rng, span)
        rows.append({"ticker": ticker, "session_date": _day(i), "open": _price(rng, store_like),
                     "high": _price(rng, store_like), "low": _price(rng, store_like),
                     "close": _price(rng, store_like), "adj_close": None, "volume": vol, "split_ratio": None,
                     "adj_source": None, "source": rng.choice(["uw", "yfinance", "yfinance_max"]),
                     "fetched_at": f, "run_id": "r", "run_at": 0.0})
    if not store_like:
        rng.shuffle(rows)                                 # apply_events sorts
    return rows


def _random_actions(rng: random.Random, ticker: str, bars: list[dict], span: int, store_like: bool) -> list[dict]:
    on_bars = [b["session_date"] for b in bars]
    out = []
    used: set[tuple[date, str]] = set()
    for _ in range(rng.randrange(0, 9)):
        kind = rng.choice(["split", "dividend", "dividend", "capital_gain"] + ([] if store_like else ["other"]))
        ex = rng.choice(on_bars) if on_bars and rng.random() < 0.6 else _day(rng.randrange(-5, span + 5))
        if store_like and (ex, kind) in used:
            continue
        used.add((ex, kind))
        if kind == "split":
            value = rng.choice([2.0, 3.0, 4.0, 0.5, 0.25, 0.1, 1.5])
        else:
            value = rng.choice([rng.uniform(0.01, 2.0), rng.uniform(0.01, 2.0), rng.uniform(50, 500),   # m <= 0
                                -rng.uniform(0.01, 2.0)])                                              # m >= 1
        if not store_like and rng.random() < 0.1:
            value = rng.choice([0.0, None, float("nan"), float("inf"), "x", "2.0"])                     # filtered
        if store_like and rng.random() < 0.05:
            value = rng.choice([0.0, None])
        f = _stamp(rng, span)
        out.append({"ticker": ticker, "ex_date": ex, "action": kind, "value": value, "session_date": ex,
                    "source": "yfinance", "fetched_at": f, "run_id": "r", "run_at": 0.0})
    return out


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b and type(a) is type(b)


def _same_rows(got: list[dict], want: list[dict]) -> None:
    assert len(got) == len(want)
    for g, w in zip(got, want):
        assert list(g) == list(w)
        for k in w:
            assert _same(g[k], w[k]), (k, g[k], w[k], g["session_date"])


def _assert_frames_equal(got: pd.DataFrame, want: pd.DataFrame) -> None:
    assert got.attrs == want.attrs
    pd.testing.assert_frame_equal(got, want, check_exact=True)


# ── 1. apply_events ────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("basis", ["nominal", "adjusted"])
def test_apply_events_equals_the_per_row_code_on_random_fixtures(basis):
    rng = random.Random(38301)
    for case in range(600):
        span = rng.randrange(1, 70)
        bars = _random_bars(rng, "T", span, store_like=False)
        actions = _random_actions(rng, "T", bars, span, store_like=False)
        want = _old_apply_events(bars, actions, basis)
        got = prices.apply_events(bars, actions, basis)
        assert got.attrs == want.attrs, case
        _same_rows(got, want)


@pytest.mark.parametrize("basis", ["nominal", "adjusted"])
def test_apply_events_edge_inputs(basis):
    one = [{"ticker": "T", "session_date": _day(3), "close": 10.0, "fetched_at": None}]   # no open/volume keys
    div = {"ticker": "T", "ex_date": _day(5), "action": "dividend", "value": 0.5, "fetched_at": None}
    split = {"ticker": "T", "ex_date": _day(5), "action": "split", "value": 2.0, "fetched_at": None}
    for bars, actions in [([], []), ([], [div, split]), (one, []), (one, [div, split]),
                          (one, [dict(div, ex_date=_day(3))]),                          # ex on the only bar
                          ([dict(one[0], close=None)], [div])]:
        want = _old_apply_events(bars, actions, basis)
        got = prices.apply_events(bars, actions, basis)
        assert got.attrs == want.attrs
        _same_rows(got, want)
    with pytest.raises(ValueError):
        prices.apply_events(one, [], "total")


# ── 2. the frame path ──────────────────────────────────────────────────────────────────────────────────────────

def _random_universe(rng: random.Random, span: int):
    names = ["SPY", "AAA", "BBB", "CCC", "DDD"]
    bars: dict[str, list[dict]] = {}
    actions: dict[str, list[dict]] = {}
    for t in names + ["ZZZ"]:                                  # ZZZ: read but never named
        rows = _random_bars(rng, t, span, store_like=True)
        if rows and rng.random() < 0.85:
            bars[t] = rows
        acts = _random_actions(rng, t, rows, span, store_like=True)
        if acts:
            actions[t] = acts                                  # a ticker with actions and no bars: never counted
    return names, bars, actions


def _edge(rng: random.Random, span: int):
    return None if rng.random() < 0.3 else _day(rng.randrange(-3, span + 3))


@pytest.mark.parametrize("basis", ["nominal", "adjusted"])
def test_the_frame_path_equals_the_per_row_code_on_random_universes(basis):
    rng = random.Random(38302)
    for case in range(250):
        span = rng.randrange(1, 60)
        names, bars, actions = _random_universe(rng, span)
        start, end = _edge(rng, span), _edge(rng, span)
        want = _old_bars_frame(names, bars, actions, basis, start, end)
        got = prices._bars_frame(names, bars, actions, basis, start, end)
        _assert_frames_equal(got, want)


def test_the_frame_path_never_loops_apply_events(monkeypatch):
    """The speed: one vectorised pass over every ticker, not ``apply_events`` per ticker."""
    rng = random.Random(38303)
    names, bars, actions = _random_universe(rng, 40)
    want = _old_bars_frame(names, bars, actions, "adjusted", None, None)

    def per_ticker(*a, **k):
        raise AssertionError("apply_events called per ticker")

    monkeypatch.setattr(prices, "apply_events", per_ticker)
    _assert_frames_equal(prices._bars_frame(names, bars, actions, "adjusted", None, None), want)


# ── 3. the read bounded at end ────────────────────────────────────────────────────────────────────────────────

def _ts(d: date) -> float:
    return datetime(d.year, d.month, d.day, 21, 35, tzinfo=UTC).timestamp()


def _act(t: str, d: date, action: str, value: float, fetched: float) -> dict:
    return {"ticker": t, "ex_date": d, "action": action, "value": value, "session_date": d,
            "source": "yfinance", "fetched_at": fetched, "run_id": "r", "run_at": fetched}


def _bar(t: str, d: date, close: float, fetched: float) -> dict:
    return {"ticker": t, "session_date": d, "open": close, "high": close, "low": close, "close": close,
            "adj_close": None, "volume": 1000.0, "split_ratio": None, "adj_source": None, "source": "uw",
            "fetched_at": fetched, "run_id": "r", "run_at": fetched}


def spy_bar_reads(monkeypatch) -> list[tuple[str, date]]:
    """Record ``(ticker, session_date)`` of every bar the store read returns."""
    seen: list[tuple[str, date]] = []
    real = prices._read_columns

    def spy(sql, params):
        cols = real(sql, params)
        if cols:
            days = np.ma.getdata(cols["session_date"]).astype("datetime64[D]").tolist()
            seen.extend(zip(np.ma.getdata(cols["ticker"]).tolist(), days))
        return cols

    monkeypatch.setattr(prices, "_read_columns", spy)
    return seen


def _sessions(n: int) -> list[date]:
    out, d = [], FIRST
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


@pytest.fixture
def bounded(rawstore_tmp_db):
    """TTT: a 2:1 split on days[30] fetched by the nightly, bars before it filled early (divided) and after (not);
    a 3:1 split on days[50] whose bars were all fetched after its open (none divided). UUU: a gap on 40-41."""
    days = _sessions(60)
    fill = _ts(days[0])
    bars = []
    for i, d in enumerate(days):
        bars.append(_bar("TTT", d, 100.0 + i, fill if i < 30 else _ts(days[55])))
        if i not in (40, 41):
            bars.append(_bar("UUU", d, 20.0 + 0.1 * i, _ts(d)))
    rawstore.write_rows("price_bars_daily", bars)
    rawstore.write_rows("price_actions", [
        _act("TTT", days[30], "split", 2.0, _ts(days[30])),
        _act("TTT", days[50], "split", 3.0, _ts(days[58])),            # fetched later: bars after its open stay
        _act("TTT", days[10], "dividend", 1.0, _ts(days[10])),
        _act("TTT", days[25], "dividend", 0.5, _ts(days[20])),         # fetched before the 2:1: divided by it
        _act("TTT", days[45], "dividend", 0.25, _ts(days[45])),
        _act("UUU", days[20], "dividend", 0.2, _ts(days[20])),
        _act("UUU", days[42], "dividend", 0.3, _ts(days[42])),         # C_prev is days[39]'s close
        _act("UUU", days[55], "capital_gain", 0.1, _ts(days[55]))])
    return days


def test_an_end_bounded_read_reads_no_bar_after_end_but_the_c_prev_bars(bounded, monkeypatch):
    days = bounded
    seen = spy_bar_reads(monkeypatch)
    prices.daily_bars(["TTT", "UUU"], basis="adjusted", start=days[10], end=days[20])
    window = [(t, d) for t in ("TTT", "UUU") for d in days[9:21]]
    first_after = [("TTT", days[21]), ("UUU", days[21])]      # each ticker's first bar past end: it has bars
    c_prev = [("TTT", days[24]), ("TTT", days[44]), ("UUU", days[39]), ("UUU", days[54])]
    assert sorted(seen) == sorted(window + first_after + c_prev)

    seen.clear()
    prices.daily_bars(["TTT", "UUU"], basis="nominal", start=days[10], end=days[20])
    assert sorted(seen) == sorted(window + first_after), "nominal needs no C_prev bar"


def test_an_end_bounded_read_equals_the_unbounded_read_sliced(bounded):
    days = bounded
    full = {b: prices.daily_bars(["TTT", "UUU"], basis=b) for b in ("nominal", "adjusted")}
    windows = [(None, days[5]), (None, days[29]), (days[10], days[20]), (days[29], days[30]), (days[30], days[30]),
               (days[25], days[44]), (days[40], days[41]), (days[0], days[59]), (days[45], days[49]),
               (days[50], days[59]), (days[20], days[10]), (days[59], days[59])]
    for start, end in windows:
        both = prices.daily_bars_bases(["TTT", "UUU"], start=start, end=end)
        for basis in ("nominal", "adjusted"):
            got = prices.daily_bars(["TTT", "UUU"], basis=basis, start=start, end=end)
            _assert_frames_equal(got, _old_daily_bars(["TTT", "UUU"], basis, start, end))
            _assert_frames_equal(both[basis], got)
            f = full[basis]
            want = f[(f["session_date"] >= pd.Timestamp(start or days[0])) & (f["session_date"] <= pd.Timestamp(end))]
            if want.empty:
                assert got.empty
                continue
            pd.testing.assert_frame_equal(got.reset_index(drop=True), want.reset_index(drop=True), check_exact=True)


def test_an_end_bounded_read_equals_the_per_row_code_on_random_stores(rawstore_tmp_db):
    """Random stores (splits on both sides of the window, fetched before and after T(e), dividends and capital
    gains after ``end``): the bounded read equals the per-row code's unbounded read sliced, exactly."""
    rng = random.Random(38304)
    span = 70
    bars, actions = [], []
    for t in ("AAA", "BBB", "CCC", "DDD", "EEE"):
        rows = _random_bars(rng, t, span, store_like=True)
        bars += rows
        actions += _random_actions(rng, t, rows, span, store_like=True)
    rawstore.write_rows("price_bars_daily", bars)
    rawstore.write_rows("price_actions", actions)
    names = ["AAA", "BBB", "CCC", "DDD", "EEE", "NONE"]
    for _ in range(25):
        start, end = _edge(rng, span), _day(rng.randrange(-3, span + 3))
        for basis in ("nominal", "adjusted"):
            want = _old_daily_bars(names, basis, start, end)
            got = prices.daily_bars(names, basis=basis, start=start, end=end)
            _assert_frames_equal(got, want)
