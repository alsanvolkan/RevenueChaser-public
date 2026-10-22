"""core.baselines — the derived ``forecast_baselines`` writer (#25 HR-1). Offline; the store is a temp DuckDB.

The fixture: the universe froze on Tuesday 2026-09-01 (``D``). Every member's 30-day window is the sessions from
2026-09-02 to Thursday 2026-10-01, its 90-day window the sessions to Monday 2026-11-30. The bars start on 2026-07-01,
because the trailing volatility needs the 21 sessions ending at ``D``.
"""
from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta, timezone

import pytest

from RevenueChaser.core import baselines, calendar as cal, ledger, outcomes, prices, rawstore

D = date(2026, 9, 1)                      # the freeze date (a Tuesday)
END30 = date(2026, 10, 1)                 # the last session on or before D + 30 days
LAST_BAR = date(2026, 10, 9)
VOL_START = date(2026, 7, 1)              # enough sessions before D for the 21-bar vol window
NOW = datetime(2026, 10, 9, 23, 0, tzinfo=timezone.utc)     # 19:00 New York: 2026-10-09 is complete
LATER = datetime(2027, 1, 15, 23, 0, tzinfo=timezone.utc)   # past window_end(D, 90) = 2026-11-30


def _sessions(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if cal.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _bar(ticker, d, o, h, low, c):
    return {"ticker": ticker, "session_date": d, "open": o, "high": h, "low": low, "close": c,
            "adj_close": None, "volume": 1000.0, "split_ratio": None, "adj_source": None, "source": "yfinance",
            "fetched_at": 0.0, "run_id": "seed", "run_at": 0.0}


def _series(ticker="AAA", *, mfe=0.10, mae=-0.05, amp=0.0, last=LAST_BAR):
    """Bars from 2026-07-01 to ``last``. Up to ``D`` the close is 100 (or ``100*(1+amp)`` on odd sessions, which
    gives a volatility that rises with ``amp``); every session after ``D`` opens at 100 with a high of
    ``100*(1+mfe)`` and a low of ``100*(1+mae)``, so the outcome is exactly ``mfe`` / ``mae`` from a reference of
    100. Nothing jumps by the split threshold."""
    rows = []
    for i, d in enumerate(_sessions(VOL_START, last)):
        if d <= D:
            c = 100.0 * (1 + (amp if i % 2 else 0.0))
            rows.append(_bar(ticker, d, c, c, c, c))
        else:
            rows.append(_bar(ticker, d, 100.0, 100.0 * (1 + mfe), 100.0 * (1 + mae), 100.0))
    return rows


class Clock:
    """A monotone change-capture clock, so a test says which day's membership the snapshot read sees."""

    def __init__(self, start: float):
        self.t = float(start)

    def tick(self) -> float:
        self.t += 1.0
        return self.t


@pytest.fixture
def clock(monkeypatch):
    """The universe is written an hour before ``D`` ended: ``universe_at(D)`` finds it."""
    c = Clock(baselines.day_end(D) - 3600)
    monkeypatch.setattr(rawstore, "_change_clock", c.tick)
    return c


def _seed(rows, *, tickers=None):
    """The bars, and the universe of those names (or ``tickers``) as ``D``'s membership."""
    rawstore.write_rows("price_bars_daily", list(rows))
    names = tickers if tickers is not None else sorted({r["ticker"] for r in rows})
    rawstore.write_rows("equity_universe", [{"ticker": t, "name": t, "market_cap": 1e9} for t in names])


def _dividend(ticker, day, value):
    return {"ticker": ticker, "ex_date": day, "action": "dividend", "value": value, "session_date": day,
            "source": "yfinance", "fetched_at": 0.0, "run_id": "seed", "run_at": 0.0}


def _vols(names, basis):
    """The trailing vol of each name on ``D``, read on ``basis`` through the window ``baselines`` reads."""
    frame = prices.daily_bars_bases(names, start=D - timedelta(days=baselines.VOL_LOOKBACK_DAYS),
                                    end=cal.latest_complete_session(NOW))[basis]
    by = baselines._by_ticker(frame)
    return [baselines.realised_vol([b["close"] for b in by.get(t, []) if b["session_date"] <= D]) for t in names]


def _freeze(subjects, *, source="swing", day=D):
    ledger.write_picks(source, source, [{"subject": s, "rank": i + 1} for i, s in enumerate(subjects)],
                       model_version="v1+abcd", forecast_date=day, run_id="freeze1")


def _rows(day=D, basis=None):
    sql = "SELECT * FROM forecast_baselines WHERE forecast_date = ?"
    params = [day]
    if basis is not None:
        sql += " AND basis = ?"
        params.append(basis)
    return rawstore.query(sql + " ORDER BY horizon_days, basis", params)


def _row(basis="universe", h=30, day=D):
    got = [r for r in _rows(day, basis) if r["horizon_days"] == h]
    assert len(got) == 1, f"{basis}/{h}: {got}"
    return got[0]


# ── the table ────────────────────────────────────────────────────────────────

def test_manifest_entry_is_a_derived_upsert_keyed_by_date_horizon_and_basis():
    spec = rawstore.MANIFEST["forecast_baselines"]
    assert spec["key"] == ["forecast_date", "horizon_days", "basis"]
    assert spec["write"] == "upsert" and spec["history_mode"] == "latest_cache"
    assert rawstore._db_of("forecast_baselines") == "raw" and not spec.get("history")
    cols = {c.split()[0] for c in spec["columns"] if not c.startswith("PRIMARY KEY")}
    assert {"n", "ge20", "ge10", "ge5", "mfe_mean", "mae_mean", "vol_lo", "vol_hi", "status",
            "computed_at", "model_version", "run_id"} <= cols


# ── red-first rules: the two baselines ───────────────────────────────────────

def test_the_universe_baseline_is_every_name_of_that_universe_at_both_horizons(rawstore_tmp_db, clock):
    """The baseline is not built from the picks: four names are in the universe and only AAA was frozen."""
    _seed(_series("AAA", mfe=0.50) + _series("BBB", mfe=0.10) + _series("CCC", mfe=0.03, mae=-0.20)
          + _series("DDD", mfe=0.22))
    _freeze(["AAA"])
    out = baselines.run(now=NOW, run_id="r1")
    for h in baselines.HORIZONS:
        assert _row(h=h)["n"] == 4
    u = _row(h=30)
    assert u["mfe_mean"] == pytest.approx((0.50 + 0.10 + 0.03 + 0.22) / 4)
    assert u["mae_mean"] == pytest.approx((-0.05 * 3 - 0.20) / 4)
    assert (u["ge20"], u["ge10"], u["ge5"]) == (2, 3, 3)        # BBB is exactly +10%: inclusive
    assert u["model_version"] == baselines.MODEL_VERSION and u["run_id"] == "r1"
    assert u["computed_at"] == NOW.timestamp()
    assert (u["vol_lo"], u["vol_hi"]) == (None, None)           # the universe row is not a decile
    assert out["dates"] == 1 and out["written"] == len(_rows())


def test_the_universe_is_the_membership_of_the_freeze_date_not_today(rawstore_tmp_db, clock):
    """A name that left the next day still counts; one that joined the next day does not."""
    _seed(_series("AAA") + _series("BBB"))                       # both in the universe on D
    _freeze(["AAA"])
    clock.t = baselines.day_end(D) + 86400                       # the next evening's universe pull
    rawstore.replace_all("equity_universe", [{"ticker": "CCC"}])  # BBB left, CCC joined
    baselines.run(now=NOW, run_id="r1")
    assert _row(h=30)["n"] == 2                                 # AAA and BBB, not CCC


def test_a_freeze_date_without_a_universe_snapshot_writes_zero_members(rawstore_tmp_db, clock):
    """Before the store recorded membership there is no snapshot: the rows say n = 0 rather than quietly using
    today's list, and the run counts the date as one without a snapshot."""
    rawstore.write_rows("price_bars_daily", _series("AAA"))
    clock.t = baselines.day_end(D) + 86400                       # membership is first recorded after D
    rawstore.write_rows("equity_universe", [{"ticker": "AAA"}])
    _freeze(["AAA"])
    out = baselines.run(now=NOW, run_id="r1")
    assert out["no_snapshot"] == 1
    assert _rows() and {r["n"] for r in _rows()} == {0}


def test_the_vol_decile_baseline_is_the_mean_of_its_own_decile(rawstore_tmp_db, clock):
    """Twenty names, one volatility each: the ten deciles pair neighbours, so decile 1 is the two calmest names
    and decile 10 the two wildest."""
    names = [f"T{i:02d}" for i in range(1, 21)]
    _seed([b for i, t in enumerate(names, start=1) for b in _series(t, mfe=i / 100, amp=i / 1000)])
    _freeze(["T01"])
    baselines.run(now=NOW, run_id="r1")
    d1, d10 = _row(basis="vol_decile_1"), _row(basis="vol_decile_10")
    assert (d1["n"], d10["n"]) == (2, 2)
    assert d1["mfe_mean"] == pytest.approx((0.01 + 0.02) / 2)
    assert d10["mfe_mean"] == pytest.approx((0.19 + 0.20) / 2)
    chain = [_row(basis=f"vol_decile_{d}") for d in range(1, 11)]
    assert chain[0]["vol_lo"] is None and chain[-1]["vol_hi"] is None
    assert all(chain[k]["vol_hi"] == chain[k + 1]["vol_lo"] for k in range(9))   # one boundary per pair
    assert {r["basis"] for r in _rows() if r["horizon_days"] == 30} == \
        {"universe", *[f"vol_decile_{d}" for d in range(1, 11)]}


def test_fewer_than_ten_members_have_no_deciles_at_all(rawstore_tmp_db, clock):
    """Ten groups cannot be made of fewer than ten names: a "decile" of one name is not a peer group. All eleven
    bases are still written (the key set never changes shape) — the deciles with n = 0 and no boundaries — and the
    names are counted in the universe row alone."""
    names = ("AAA", "BBB", "CCC")
    _seed([b for i, t in enumerate(names, start=1) for b in _series(t, amp=i / 100)], tickers=names)
    _freeze(["AAA"])
    baselines.run(now=NOW, run_id="r1")
    got = {r["basis"]: (r["n"], r["vol_lo"], r["vol_hi"]) for r in _rows() if r["horizon_days"] == 30}
    assert len(got) == 11
    assert got["universe"] == (3, None, None)
    assert {b: v for b, v in got.items() if b != "universe"} == \
        {f"vol_decile_{k}": (0, None, None) for k in range(1, baselines.DECILES + 1)}


def test_a_single_name_universe_is_in_no_decile_not_in_decile_ten(rawstore_tmp_db, clock):
    """The nine boundaries of one value are all that value, and ``vol_lo`` inclusive would read the lone name as
    decile 10 — the wildest. There is no peer to be wilder than: the name is in the universe row and in no decile."""
    _seed(_series("ONLY"))
    _freeze(["ONLY"])
    baselines.run(now=NOW, run_id="r1")
    assert _row(h=30)["n"] == 1
    assert {r["n"] for r in _rows() if r["basis"] != "universe"} == {0}


def test_the_volatility_is_read_adjusted_and_the_outcomes_nominal(rawstore_tmp_db, clock):
    """The stated choice, from both sides. A dividend is a one-day fall of the printed price that is not a move of
    the stock: one ex-date inside the vol window (2026-08-28) must not reach the volatility, which reads adjusted
    closes, while a second one inside the 30-day window (2026-09-15) must not move the MFE, which is the pick's own
    nominal measure. T01's high rises to 130 after that second ex-date, so the two bases really differ on both
    numbers: swap either basis and this test goes red."""
    names = [f"T{i:02d}" for i in range(1, 13)]
    step = date(2026, 9, 15)
    bars = [b for i, t in enumerate(names, start=1) for b in _series(t, mfe=0.05, amp=i / 1000)]
    bars = [{**b, "high": 130.0} if b["ticker"] == "T01" and b["session_date"] > step else b for b in bars]
    _seed(bars)
    rawstore.write_rows("price_actions", [_dividend(t, day, 10.0)
                                         for t in names for day in (date(2026, 8, 28), step)])
    _freeze([names[0]])
    baselines.run(now=NOW, run_id="r1")

    stated = {r["basis"]: (r["vol_lo"], r["vol_hi"]) for r in _rows() if r["horizon_days"] == 30}
    hi = [stated[f"vol_decile_{k}"][1] for k in range(1, baselines.DECILES)]
    lo = [stated[f"vol_decile_{k}"][0] for k in range(2, baselines.DECILES + 1)]
    want = baselines.decile_cuts(_vols(names, "adjusted"))
    assert hi == pytest.approx(want) and lo == pytest.approx(want)          # the nine boundaries, both edges
    assert hi != pytest.approx(baselines.decile_cuts(_vols(names, "nominal")))   # the two bases really differ
    assert _row(h=30)["mfe_mean"] == pytest.approx((0.30 + 11 * 0.05) / 12)   # the outcome stays nominal


# ── red-first rules: what is not measured ────────────────────────────────────

def test_a_name_flagged_split_check_is_left_out_of_the_baseline(rawstore_tmp_db, clock):
    """A split the store has no action for leaves pre- and post-split prices in one nominal series, so the name's MFE
    is not a return (HR-2 lists those records and excludes them; the baseline does the same)."""
    split_day = date(2026, 9, 15)
    jumped = [{**b, "open": b["open"] * 3, "high": b["high"] * 3, "low": b["low"] * 3, "close": b["close"] * 3}
              if b["session_date"] < split_day else b for b in _series("SPL")]
    _seed(_series("AAA") + jumped)
    _freeze(["AAA"])
    flagged = outcomes.outcome(D, 30, [{"session_date": b["session_date"], "open": b["open"], "high": b["high"],
                                        "low": b["low"], "close": b["close"]} for b in jumped],
                               cal.latest_complete_session(NOW))
    assert flagged["status"] == outcomes.SPLIT_CHECK      # the fixture really is flagged, mfe or not
    baselines.run(now=NOW, run_id="r1")
    assert _row(h=30)["n"] == 1


def test_a_name_without_the_first_session_open_is_not_counted(rawstore_tmp_db, clock):
    """Review S0-1's rule applies here too: without the first session's open there is no reference, so no MFE."""
    no_ref = [b for b in _series("NOPE") if b["session_date"] != date(2026, 9, 2)]
    _seed(no_ref + _series("AAA"))
    _freeze(["AAA"])
    baselines.run(now=NOW, run_id="r1")
    assert _row(h=30)["n"] == 1


def test_each_horizon_is_measured_over_its_own_window(rawstore_tmp_db, clock):
    """BBB's high is +30%, but only after the 30-day window: it counts for the 90-day mean alone."""
    bbb = [dict(r, high=130.0) if r["session_date"] > END30 else r for r in _series("BBB", mfe=0.02)]
    _seed(_series("AAA", mfe=0.02) + bbb)
    _freeze(["AAA"])
    baselines.run(now=NOW, run_id="r1")
    assert _row(h=30)["mfe_mean"] == pytest.approx(0.02)
    assert _row(h=90)["mfe_mean"] == pytest.approx((0.02 + 0.30) / 2)
    assert (_row(h=30)["status"], _row(h=90)["status"]) == (baselines.CLOSED, baselines.OPEN)


# ── the nightly ──────────────────────────────────────────────────────────────

def test_a_rerun_upserts_the_same_rows_and_adds_none(rawstore_tmp_db, clock):
    _seed(_series("AAA") + _series("BBB", amp=0.01))
    _freeze(["AAA"])
    first = baselines.run(now=NOW, run_id="r1")
    before = _rows()
    second = baselines.run(now=NOW, run_id="r2", full=True)
    after = _rows()
    assert len(before) == len(after) == first["rows"] == second["rows"]
    assert {r["run_id"] for r in after} == {"r2"}
    assert [{k: v for k, v in r.items() if k != "run_id"} for r in after] == \
           [{k: v for k, v in r.items() if k != "run_id"} for r in before]


def test_the_nightly_skips_a_date_whose_windows_have_ended(rawstore_tmp_db, clock):
    _seed(_series("AAA"))
    _freeze(["AAA"])
    baselines.run(now=NOW, run_id="r1")
    assert _row(h=90)["status"] == baselines.OPEN
    closing = baselines.run(now=LATER, run_id="r2")              # the run that closes both windows
    assert closing["dates"] == 1
    assert {_row(h=h)["status"] for h in baselines.HORIZONS} == {baselines.CLOSED}
    assert baselines.run(now=LATER, run_id="r3")["dates"] == 0    # final: skipped
    # The 30-day rows were already closed by r1, so r2 left them alone and rewrote the 90-day ones.
    assert {r["run_id"] for r in _rows() if r["horizon_days"] == 30} == {"r1"}
    assert {r["run_id"] for r in _rows() if r["horizon_days"] == 90} == {"r2"}
    full = baselines.run(now=LATER, run_id="r4", full=True)
    assert full["dates"] == 1 and {r["run_id"] for r in _rows()} == {"r4"}


def test_the_nightly_recomputes_only_the_horizon_still_open(rawstore_tmp_db, clock):
    """At NOW the 30-day window has ended: its eleven rows are final, and the next nightly rewrites the 90-day
    eleven alone (11 rows, not 22) — a closed row is never rewritten, and its numbers cannot change."""
    late = date(2026, 10, 20)
    _seed(_series("AAA", last=late) + _series("BBB", amp=0.01, last=late))
    _freeze(["AAA"])
    baselines.run(now=NOW, run_id="r1")                          # 30-day rows close here, 90-day rows stay open
    assert {_row(h=30)["run_id"]} == {"r1"}
    second = baselines.run(now=datetime(2026, 10, 20, 23, 0, tzinfo=timezone.utc), run_id="r2")
    assert (second["dates"], second["rows"]) == (1, 11) and second["status"] == {baselines.OPEN: 11}
    assert {r["run_id"] for r in _rows() if r["horizon_days"] == 30} == {"r1"}
    assert {r["run_id"] for r in _rows() if r["horizon_days"] == 90} == {"r2"}


def test_an_empty_price_store_refuses_before_any_write(rawstore_tmp_db, clock):
    _seed(_series("AAA"))
    _freeze(["AAA"])
    baselines.run(now=NOW, run_id="r1")
    rawstore.clear("price_bars_daily")
    for kw in ({"full": True}, {}, {"full": True, "write": False}):
        with pytest.raises(RuntimeError, match="no daily bars"):
            baselines.run(now=NOW, run_id="r2", **kw)
    assert {r["run_id"] for r in _rows()} == {"r1"}


def test_dry_run_computes_and_writes_nothing(rawstore_tmp_db, clock):
    _seed(_series("AAA"))
    _freeze(["AAA"])
    out = baselines.run(now=NOW, run_id="r1", write=False)
    assert out["rows"] >= 1 and out["written"] == 0 and out["dry_run"] is True
    assert rawstore.count("forecast_baselines") == 0


def test_n_counts_the_members_with_an_mfe_and_mae_mean_rests_on_the_ones_with_a_mae(rawstore_tmp_db, clock):
    """A window bar can carry a high with no low (a NaN low is dropped, never read as 0). ``n`` counts the members
    whose MFE is measurable, and ``mae_mean`` averages the MAE of those of them that have one: stated here, not an
    accident of the filter."""
    no_low = [dict(b, low=None) if b["session_date"] > D else b for b in _series("NOLOW", mfe=0.30)]
    _seed(_series("AAA", mfe=0.10, mae=-0.20) + no_low)
    _freeze(["AAA"])
    baselines.run(now=NOW, run_id="r1")
    u = _row(h=30)
    assert (u["n"], u["ge20"]) == (2, 1)
    assert u["mfe_mean"] == pytest.approx((0.10 + 0.30) / 2)
    assert u["mae_mean"] == pytest.approx(-0.20)                 # NOLOW has no MAE, so it adds no drawdown


def test_a_ledger_with_no_pick_has_no_baseline(rawstore_tmp_db, clock):
    _seed(_series("AAA"))
    out = baselines.run(now=NOW, run_id="r1")
    assert (out["dates"], out["rows"], rawstore.count("forecast_baselines")) == (0, 0, 0)


# ── guards (green from the start; each killed by its mutation) ──────────────

def test_the_writer_never_writes_the_ledger(rawstore_tmp_db, clock, monkeypatch):
    _seed(_series("AAA"))
    _freeze(["AAA"])
    written = []
    real = rawstore.write_rows

    def spy(name, rows):
        if name == "forecast_ledger":
            raise AssertionError("forecast_baselines must never write forecast_ledger")
        written.append(name)
        return real(name, rows)

    monkeypatch.setattr(rawstore, "write_rows", spy)
    for name in ("update_columns", "replace_all", "delete", "clear", "execute"):
        monkeypatch.setattr(rawstore, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError(name)))
    monkeypatch.setattr(ledger, "write_picks", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ledger")))
    baselines.run(now=NOW, run_id="r1", full=True)
    assert set(written) == {"forecast_baselines"}


def test_the_writer_makes_no_provider_call(rawstore_tmp_db, clock, monkeypatch):
    _seed(_series("AAA"))
    _freeze(["AAA"])
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("provider call"))  # noqa: E731
    monkeypatch.setattr(prices, "_yf_download", boom)
    monkeypatch.setattr(prices, "ensure_daily_bars", boom)
    monkeypatch.setattr(prices, "fill_history", boom)
    from RevenueChaser.core.providers import unusual_whales
    monkeypatch.setattr(unusual_whales.UWClient, "__init__", boom)
    assert baselines.run(now=NOW, run_id="r1")["rows"] >= 1


# ── the pieces the board will read ──────────────────────────────────────────

def test_realised_vol_is_the_annualised_stdev_of_the_last_20_log_returns():
    closes = [100.0] * 20 + [110.0]
    want = statistics.stdev([0.0] * 19 + [math.log(1.1)]) * math.sqrt(252)
    assert baselines.realised_vol(closes) == pytest.approx(want)
    assert baselines.realised_vol([5.0] + closes) == pytest.approx(want)      # only the last 21 closes count


@pytest.mark.parametrize("closes", [
    [],
    [100.0] * 20,                                          # 20 closes = 19 returns
    [100.0] * 20 + [0.0],                                  # a zero price has no log return
    [100.0] * 20 + [float("nan")],
])
def test_realised_vol_is_none_when_it_cannot_be_measured(closes):
    assert baselines.realised_vol(closes) is None


def test_a_decile_is_matched_by_its_own_boundaries():
    """``vol_lo`` inclusive, ``vol_hi`` exclusive, NULL at either end: a pick's vol lands in exactly one decile."""
    cuts = [1.0, 2.0, 3.0]
    assert [baselines.basis_of(v, cuts) for v in (0.5, 1.0, 2.5, 3.0, 9.0)] == \
        ["vol_decile_1", "vol_decile_2", "vol_decile_3", "vol_decile_4", "vol_decile_4"]
    assert baselines.vol_range(cuts, 1) == (None, 1.0)
    assert baselines.vol_range(cuts, 2) == (1.0, 2.0)
    assert baselines.vol_range(cuts, 4) == (3.0, None)


def test_decile_cuts_split_a_hundred_evenly_spaced_peers_into_ten_groups_of_ten():
    cuts = baselines.decile_cuts([float(i) for i in range(100)])
    assert len(cuts) == baselines.DECILES - 1
    counts = [0] * 10
    for i in range(100):
        counts[int(baselines.basis_of(float(i), cuts).rsplit("_", 1)[1]) - 1] += 1
    assert counts == [10] * 10
    assert baselines.decile_cuts([]) == []
