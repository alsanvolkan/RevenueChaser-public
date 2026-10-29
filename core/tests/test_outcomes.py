"""core.outcomes — the derived forecast_outcomes writer (S0, #380 / #62 62b-1). Offline.

Since PR 10 (G4) the writer runs on the builder's build file of the ledger kind (``con``) and reads the prices store's
bars: each test has an in-memory ``con`` holding ``forecast_ledger`` and ``forecast_outcomes``, and seeds the prices
store (``prices_seed``). The builder's own run of it is ``rcdata/tests/test_ledger_kind.py``.

The fixture: one Swing pick frozen on Tuesday 2026-09-01. Its 30-day window is the sessions from 2026-09-02 to
Thursday 2026-10-01 (21 sessions: Labor Day 2026-09-07 is closed); its 90-day window ends Monday 2026-11-30.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from RevenueChaser.core import calendar as cal
from RevenueChaser.core import ledger, market_bars, outcomes, prices
from RevenueChaser.core.tests import ledger_build
from RevenueChaser.rcdata.build import ledger as kind
from RevenueChaser.rcdata.tests import prices_seed

D = date(2026, 9, 1)                      # the forecast date (a Tuesday)
END30 = date(2026, 10, 1)                 # the last session on or before D + 30 days
LAST_BAR = date(2026, 10, 9)
NOW = datetime(2026, 10, 9, 23, 0, tzinfo=timezone.utc)     # 19:00 New York: 2026-10-09 is complete


def _sessions(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if cal.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _bar(ticker, d, o, h, low, c, fetched_at=0.0):
    return {"ticker": ticker, "session_date": d, "open": o, "high": h, "low": low, "close": c,
            "adj_close": None, "volume": 1000.0, "split_ratio": None, "adj_source": None, "source": "yfinance",
            "fetched_at": fetched_at, "run_id": "seed", "run_at": 0.0}


def _series(ticker="AAA", *, last=LAST_BAR, scale_before: date | None = None, factor: float = 1.0):
    """Bars from D (the forecast date itself) to ``last``. Open 11, high 12, low 10, close 10.5 by default, with:
    D: low 5 and high 30 (outside every window: the forecast day itself);
    2026-09-02 (first session after D): open 10.0, low 8.0 (the reference open and the 30-day low);
    2026-10-01 (END30, the window's last session): high 15.0 (the 30-day high);
    2026-10-02 (one session past END30): high 20.0, low 6.0 (inside the 90-day window only).
    ``scale_before``/``factor``: bars before that date are stored ``factor`` times higher (an unadjusted 2:1 split)."""
    rows = []
    for d in _sessions(D, last):
        o, h, low, c = 11.0, 12.0, 10.0, 10.5
        if d == D:
            low, h = 5.0, 30.0
        elif d == date(2026, 9, 2):
            o, low = 10.0, 8.0
        elif d == END30:
            h = 15.0
        elif d == date(2026, 10, 2):
            h, low = 20.0, 6.0
        k = factor if scale_before is not None and d < scale_before else 1.0
        rows.append(_bar(ticker, d, o * k, h * k, low * k, c * k))
    return rows


_STORE: dict = {}


@pytest.fixture(autouse=True)
def store():
    """The build file the builder hands ``outcomes.run``: the ledger and the outcomes table, in memory."""
    con = ledger_build.connect()
    _STORE.update(con=con, bars=ledger_build.Bars())
    yield con
    con.close()
    _STORE.clear()


def _q(sql, params=None):
    return ledger_build.query(_STORE["con"], sql, params)


def _run(**kw):
    return outcomes.run(_STORE["con"], **kw)


def _seed_bars(rows, actions=()):
    _STORE["bars"].seed(rows, actions)


def _ledger_row(subject, rank, *, day=D, **kw):
    return ledger_build.ledger_row(subject, rank, day=day, **kw)


def _insert_ledger(rows):
    ledger_build.insert_ledger(_STORE["con"], rows)


def _freeze(subjects, *, source="swing", day=D, shown_price=99.0):
    ledger_build.freeze(_STORE["con"], subjects, source=source, day=day, shown_price=shown_price)


def _count():
    return ledger_build.count(_STORE["con"], "forecast_outcomes")


def _rows(subject="AAA"):
    got = _q("SELECT * FROM forecast_outcomes WHERE subject = ? ORDER BY horizon_days", [subject])
    return {r["horizon_days"]: r for r in got}


# ── the table ────────────────────────────────────────────────────────────────

def test_the_table_is_the_ledger_key_plus_horizon_in_the_ledger_kind():
    assert kind.OUTCOMES_KEY == [*kind.LEDGER_KEY, "horizon_days"]
    cols = [c for c, _ in kind.OUTCOMES_SCHEMA]
    assert cols == outcomes.COLUMNS
    assert {"ref_price", "mfe", "mae", "sessions_seen", "status", "computed_at", "model_version"} <= set(cols)


# ── red-first rules ─────────────────────────────────────────────────────────

def test_mfe_and_mae_are_high_and_low_over_exactly_the_window_from_the_next_open(rawstore_tmp_db):
    _seed_bars(_series())
    _freeze(["AAA"])
    _run(now=NOW, run_id="r1")
    r = _rows()[30]
    assert r["ref_price"] == 10.0                      # the open of 2026-09-02, not the frozen shown_price 99
    assert r["mfe"] == pytest.approx(15.0 / 10.0 - 1)  # the high on END30 counts; 2026-10-02's 20 does not
    assert r["mae"] == pytest.approx(8.0 / 10.0 - 1)   # the low on 2026-09-02 counts; D's own 5 does not
    assert r["sessions_seen"] == 21
    assert r["status"] == outcomes.CLOSED
    assert r["model_version"] == outcomes.MODEL_VERSION and r["run_id"] == "r1"
    assert r["computed_at"] == NOW.timestamp()


def test_a_window_with_fewer_bars_than_its_sessions_is_open(rawstore_tmp_db):
    _seed_bars(_series())
    _freeze(["AAA"])
    _run(now=NOW, run_id="r1")
    r = _rows()[90]                                     # ends 2026-11-30; the bars stop at 2026-10-09
    assert r["status"] == outcomes.OPEN
    assert r["sessions_seen"] == 27
    assert r["mfe"] == pytest.approx(20.0 / 10.0 - 1) and r["mae"] == pytest.approx(6.0 / 10.0 - 1)


def test_a_ticker_whose_bars_stop_inside_the_window_stays_open_after_the_window_ends(rawstore_tmp_db):
    _seed_bars(_series(last=date(2026, 9, 18)))
    _freeze(["AAA"])
    _run(now=datetime(2026, 12, 15, 23, 0, tzinfo=timezone.utc), run_id="r1")
    r = _rows()[30]
    assert r["status"] == outcomes.OPEN and r["sessions_seen"] == 12


def test_a_window_whose_last_session_is_the_last_stored_bar_is_closed(rawstore_tmp_db):
    _seed_bars(_series(last=END30))
    _freeze(["AAA"])
    _run(now=NOW, run_id="r1")
    r = _rows()[30]
    assert (r["status"], r["sessions_seen"]) == (outcomes.CLOSED, 21)


def test_the_forecast_day_stays_out_of_its_window_when_an_earlier_pick_loads_it(rawstore_tmp_db):
    """An earlier pick (another source, Friday 2026-08-28) makes the bar read start before D, so D's own bar is
    loaded; it is still not D's window (its open 11 and low 5 must not count)."""
    _seed_bars([_bar("AAA", date(2026, 8, 28), 11.0, 12.0, 10.0, 10.5),
                _bar("AAA", date(2026, 8, 31), 11.5, 12.0, 10.0, 10.5), *_series()])
    _freeze(["AAA"], source="fundamentals", day=date(2026, 8, 28))
    _freeze(["AAA"], source="swing")
    _run(now=NOW, run_id="r1")
    got = _q("SELECT ref_price, mae FROM forecast_outcomes WHERE source = 'swing' AND horizon_days = 30")
    assert got[0]["ref_price"] == 10.0 and got[0]["mae"] == pytest.approx(-0.2)
    # The earlier pick's reference is Monday 2026-08-31's open: the chunk's bar read starts at its oldest pick.
    got = _q("SELECT ref_price FROM forecast_outcomes WHERE source = 'fundamentals' AND horizon_days = 30")
    assert got[0]["ref_price"] == 11.5


def test_a_missing_first_session_bar_leaves_the_row_open_without_a_reference(rawstore_tmp_db):
    """Review S0-1: the reference is the open of the first session after D. Without that bar, the next stored open
    (2026-09-03) must not stand in for it, and the row must not close as final."""
    _seed_bars([b for b in _series() if b["session_date"] != date(2026, 9, 2)])
    _freeze(["AAA"])
    _run(now=NOW, run_id="r1")
    for h, seen in ((30, 20), (90, 26)):
        r = _rows()[h]
        assert (r["ref_price"], r["mfe"], r["mae"]) == (None, None, None)
        assert (r["status"], r["sessions_seen"]) == (outcomes.OPEN, seen)


@pytest.mark.parametrize("bad_open", [None, float("nan"), 0.0, -1.0])
def test_a_first_session_bar_without_a_usable_open_is_open_not_no_bars(rawstore_tmp_db, bad_open):
    """Bars exist, so the row is not no_bars; with no usable reference it stays open and the nightly retries it."""
    _seed_bars([{**b, "open": bad_open} if b["session_date"] == date(2026, 9, 2) else b for b in _series()])
    _freeze(["AAA"])
    _run(now=NOW, run_id="r1")
    r = _rows()[30]
    assert (r["ref_price"], r["mfe"], r["mae"]) == (None, None, None)
    assert (r["status"], r["sessions_seen"]) == (outcomes.OPEN, 21)


def test_a_missing_window_end_bar_with_a_later_bar_is_closed(rawstore_tmp_db):
    """A bar after the window's last session means the end session's bar is not coming: the row is final."""
    _seed_bars([b for b in _series() if b["session_date"] != END30])
    _freeze(["AAA"])
    _run(now=NOW, run_id="r1")
    r = _rows()[30]
    assert (r["status"], r["sessions_seen"]) == (outcomes.CLOSED, 20)
    assert r["mfe"] == pytest.approx(12.0 / 10.0 - 1)


def test_no_bar_after_the_forecast_date_is_no_bars(rawstore_tmp_db):
    _seed_bars([_bar("AAA", D, 11.0, 12.0, 10.0, 10.5)])      # only the forecast day itself
    _freeze(["AAA"])
    _run(now=NOW, run_id="r1")
    for h in (30, 90):
        r = _rows()[h]
        assert r["status"] == outcomes.NO_BARS
        assert (r["ref_price"], r["mfe"], r["mae"], r["sessions_seen"]) == (None, None, None, 0)


def test_a_pick_with_no_completed_session_since_its_forecast_date_is_open_not_no_bars(rawstore_tmp_db):
    _seed_bars([_bar("AAA", D, 11.0, 12.0, 10.0, 10.5)])
    _freeze(["AAA"])
    _run(now=datetime(2026, 9, 1, 23, 0, tzinfo=timezone.utc), run_id="r1")   # the freeze evening
    assert {h: r["status"] for h, r in _rows().items()} == {30: outcomes.OPEN, 90: outcomes.OPEN}


def test_a_rerun_writes_identical_rows(rawstore_tmp_db):
    _seed_bars(_series())
    _freeze(["AAA", "BBB"])                              # BBB has no bars at all
    first = _run(now=NOW, run_id="r1")
    before = _q("SELECT * FROM forecast_outcomes ORDER BY subject, horizon_days")
    _run(now=NOW, run_id="r1", full=True)
    after = _q("SELECT * FROM forecast_outcomes ORDER BY subject, horizon_days")
    assert len(before) == 4 and before == after
    assert first["rows"] == 4


def test_a_split_inside_the_window_leaves_ref_mfe_and_mae_unchanged(rawstore_tmp_db):
    _seed_bars(_series("AAA"))
    split_day = date(2026, 9, 15)
    _seed_bars(_series("SPL", scale_before=split_day, factor=2.0),
               [{"ticker": "SPL", "ex_date": split_day, "action": "split", "value": 2.0, "session_date": split_day,
                 "source": "yfinance", "fetched_at": 0.0, "run_id": "seed", "run_at": 0.0}])
    _freeze(["AAA", "SPL"])
    _run(now=NOW, run_id="r1")
    plain, split = _rows("AAA"), _rows("SPL")
    for h in (30, 90):
        for col in ("ref_price", "mfe", "mae", "sessions_seen", "status"):
            assert split[h][col] == pytest.approx(plain[h][col]) if isinstance(plain[h][col], float) \
                else split[h][col] == plain[h][col], (h, col)


def _split_action(ticker, day, ratio):
    return {"ticker": ticker, "ex_date": day, "action": "split", "value": ratio, "session_date": day,
            "source": "yfinance", "fetched_at": 0.0, "run_id": "seed", "run_at": 0.0}


def test_an_unrecorded_reverse_split_between_the_forecast_day_and_the_next_open_is_flagged(rawstore_tmp_db):
    """The POAS case (PC store, 2026-10-14): shown 0.14 on D, next open 2.12. A 1:15 reverse split with no action in
    the store leaves D's bar 15 times lower than the window's bars. The row is flagged split_check, not closed."""
    _seed_bars(_series("POS", scale_before=date(2026, 9, 2), factor=1 / 15))
    _freeze(["POS"])
    _run(now=NOW, run_id="r1")
    for h in (30, 90):
        r = _rows("POS")[h]
        assert r["status"] == outcomes.SPLIT_CHECK
        assert r["ref_price"] == 10.0                  # kept for a look; the board reads closed rows only


def test_a_recorded_reverse_split_between_the_forecast_day_and_the_next_open_closes_normally(rawstore_tmp_db):
    _seed_bars(_series("POS", scale_before=date(2026, 9, 2), factor=1 / 15),
               [_split_action("POS", date(2026, 9, 2), 1 / 15)])
    _freeze(["POS"])
    _run(now=NOW, run_id="r1")
    r = _rows("POS")[30]
    assert (r["status"], r["ref_price"]) == (outcomes.CLOSED, 10.0)
    assert r["mfe"] == pytest.approx(0.5) and r["mae"] == pytest.approx(-0.2)


def test_an_unrecorded_split_inside_the_window_is_flagged_and_clears_once_recorded(rawstore_tmp_db):
    split_day = date(2026, 9, 15)
    _seed_bars(_series("SPL", scale_before=split_day, factor=3.0))
    _freeze(["SPL"])
    _run(now=NOW, run_id="r1")
    assert {h: r["status"] for h, r in _rows("SPL").items()} == {30: outcomes.SPLIT_CHECK, 90: outcomes.SPLIT_CHECK}
    _seed_bars([], [_split_action("SPL", split_day, 3.0)])        # the prices store records the split later
    _run(now=NOW, run_id="r2")                             # the nightly retries the flagged rows
    r = _rows("SPL")[30]
    assert (r["status"], r["ref_price"], r["run_id"]) == (outcomes.CLOSED, 10.0, "r2")


@pytest.mark.parametrize("bad", [None, float("nan"), 0.0])
def test_an_unrecorded_split_on_a_bar_without_a_usable_open_is_still_flagged(rawstore_tmp_db, bad):
    """The split day's bar has no usable open: its close (or the next bar's open) still shows the jump."""
    split_day = date(2026, 9, 15)
    _seed_bars([{**b, "open": bad} if b["session_date"] == split_day else b
                for b in _series("SPL", scale_before=split_day, factor=3.0)])
    _freeze(["SPL"])
    _run(now=NOW, run_id="r1")
    assert _rows("SPL")[30]["status"] == outcomes.SPLIT_CHECK


def test_a_bar_with_neither_price_does_not_hide_the_jump(rawstore_tmp_db):
    split_day = date(2026, 9, 15)
    _seed_bars([{**b, "open": None, "close": None} if b["session_date"] == split_day else b
                for b in _series("SPL", scale_before=split_day, factor=3.0)])
    _freeze(["SPL"])
    _run(now=NOW, run_id="r1")
    assert _rows("SPL")[30]["status"] == outcomes.SPLIT_CHECK


@pytest.mark.parametrize("ratio", [2.2, 1 / 2.2])
def test_a_real_gap_of_2_2x_either_way_stays_closed(rawstore_tmp_db, ratio):
    """Pins SPLIT_JUMP between 2.2 and 2.5: a 2.2x gap up or down is a real move, not a split."""
    gap = date(2026, 9, 10)                                         # the previous close is 10.5
    o = round(10.5 * ratio, 4)
    _seed_bars([{**b, "open": o, "high": max(o, 12.0), "low": min(o, 10.0)} if b["session_date"] == gap else b
                for b in _series()])
    _freeze(["AAA"])
    _run(now=NOW, run_id="r1")
    assert _rows()[30]["status"] == outcomes.CLOSED


def test_an_unrecorded_split_after_the_window_leaves_the_30_day_row_closed(rawstore_tmp_db):
    """Only bars up to the window's end are checked: a split on 2026-10-05 flags the 90-day row, not the 30-day one."""
    _seed_bars(_series("SPL", scale_before=date(2026, 10, 5), factor=3.0))
    _freeze(["SPL"])
    _run(now=NOW, run_id="r1")
    assert {h: r["status"] for h, r in _rows("SPL").items()} == {30: outcomes.CLOSED, 90: outcomes.SPLIT_CHECK}


def test_a_large_real_gap_below_the_split_threshold_is_not_flagged(rawstore_tmp_db):
    gap = date(2026, 9, 10)                                         # open 1.8 x the previous close 10.5
    _seed_bars([{**b, "open": 18.9, "high": 19.0} if b["session_date"] == gap else b for b in _series()])
    _freeze(["AAA"])
    _run(now=NOW, run_id="r1")
    assert _rows()[30]["status"] == outcomes.CLOSED


def test_outcomes_read_the_nominal_basis_so_a_dividend_moves_nothing(rawstore_tmp_db):
    _seed_bars(_series("AAA"), [{"ticker": "AAA", "ex_date": date(2026, 9, 21), "action": "dividend", "value": 1.0,
                                 "session_date": date(2026, 9, 21), "source": "yfinance", "fetched_at": 0.0,
                                 "run_id": "seed", "run_at": 0.0}])
    _freeze(["AAA"])
    _run(now=NOW, run_id="r1")
    r = _rows()[30]
    assert r["ref_price"] == 10.0 and r["mae"] == pytest.approx(-0.2) and r["mfe"] == pytest.approx(0.5)


def test_every_pick_source_gets_rows_and_a_non_pick_row_gets_none(rawstore_tmp_db):
    _seed_bars(_series())
    for source in ("asymmetric_growth", "swing", "fundamentals"):
        _freeze(["AAA"], source=source)
    _insert_ledger([_ledger_row("SPX", None, source="spx_gamma", target_type="pin_prob",
                                target_date=date(2026, 9, 2), run_id=None, model_version="v1", score=0.4,
                                shown_price=5700.0)])
    _run(now=NOW, run_id="r1")
    got = _q("SELECT source, horizon_days FROM forecast_outcomes ORDER BY source, horizon_days")
    assert [(r["source"], r["horizon_days"]) for r in got] == [
        ("asymmetric_growth", 30), ("asymmetric_growth", 90), ("fundamentals", 30), ("fundamentals", 90),
        ("swing", 30), ("swing", 90)]


def test_the_nightly_run_does_not_recompute_closed_rows(rawstore_tmp_db, monkeypatch):
    _seed_bars(_series())
    _freeze(["AAA"])
    _run(now=NOW, run_id="r1")
    asked = []
    real = market_bars.daily_bars
    monkeypatch.setattr(outcomes.market_bars, "daily_bars",
                        lambda tickers, **kw: asked.append(list(tickers)) or real(tickers, **kw))
    out = _run(now=NOW, run_id="r2")
    assert out["skipped_closed"] == 1 and out["rows"] == 1      # only the open 90-day row is redone
    assert _rows()[30]["run_id"] == "r1" and _rows()[90]["run_id"] == "r2"
    asked.clear()
    _run(now=NOW, run_id="r3", full=True)
    assert _rows()[30]["run_id"] == "r3" and asked == [["AAA"]]


def test_a_closed_row_of_an_older_model_version_is_recomputed(rawstore_tmp_db, monkeypatch):
    _seed_bars(_series())
    _freeze(["AAA"])
    monkeypatch.setattr(outcomes, "MODEL_VERSION", "o0")
    _run(now=NOW, run_id="r1")
    monkeypatch.undo()
    _run(now=NOW, run_id="r2")
    assert {h: (r["model_version"], r["run_id"]) for h, r in _rows().items()} == {
        30: (outcomes.MODEL_VERSION, "r2"), 90: (outcomes.MODEL_VERSION, "r2")}


def test_the_nightly_reads_only_the_picks_not_yet_closed(rawstore_tmp_db, monkeypatch):
    """Review S0-2: the ledger grows by thousands of picks a day; the nightly must not load every pick and every
    closed key into Python. Here every row is closed, so no read may return more than one row."""
    later = datetime(2026, 12, 2, 23, 0, tzinfo=timezone.utc)
    _seed_bars(_series("AAA", last=date(2026, 12, 1)) + _series("BBB", last=date(2026, 12, 1)))
    _freeze(["AAA", "BBB"])
    first = _run(now=later, run_id="r1")
    assert first["status"] == {outcomes.CLOSED: 4}
    sizes = []
    real = outcomes._query

    def spy(*a, **k):
        rows = real(*a, **k)
        sizes.append(len(rows))
        return rows

    real_bars = market_bars.daily_bars

    def spy_bars(*a, **k):
        frame = real_bars(*a, **k)
        sizes.append(len(frame))
        return frame

    monkeypatch.setattr(outcomes, "_query", spy)
    monkeypatch.setattr(outcomes.market_bars, "daily_bars", spy_bars)
    out = _run(now=later, run_id="r2")
    assert (out["picks"], out["rows"], out["skipped_closed"]) == (2, 0, 4)
    assert sizes and max(sizes) <= 1


def test_the_nightly_still_finds_a_pick_with_one_horizon_closed(rawstore_tmp_db):
    _seed_bars(_series())
    _freeze(["AAA", "BBB"])
    _run(now=NOW, run_id="r1")
    out = _run(now=NOW, run_id="r2")
    assert (out["picks"], out["rows"], out["skipped_closed"]) == (2, 3, 1)


def test_an_empty_price_store_refuses_and_keeps_the_closed_rows(rawstore_tmp_db):
    """Review S0-3: with no bar at all (a missing or empty prices.duckdb), a full run would turn every final row into
    no_bars. The run refuses before any write."""
    _seed_bars(_series())
    _freeze(["AAA"])
    _run(now=NOW, run_id="r1")
    prices_seed.drop_ticker("AAA")
    for kw in ({"full": True}, {}, {"full": True, "write": False}):
        with pytest.raises(RuntimeError, match="no daily bars"):
            _run(now=NOW, run_id="r2", **kw)
    assert {h: (r["status"], r["run_id"]) for h, r in _rows().items()} == {
        30: (outcomes.CLOSED, "r1"), 90: (outcomes.OPEN, "r1")}


def test_an_empty_price_store_is_fine_on_the_freeze_evening(rawstore_tmp_db):
    """A first install: picks frozen tonight and no bar stored yet is not an error (no session to measure)."""
    _freeze(["AAA"])
    out = _run(now=datetime(2026, 9, 1, 23, 0, tzinfo=timezone.utc), run_id="r1")
    assert out["status"] == {outcomes.OPEN: 2}


def test_dry_run_computes_and_writes_nothing(rawstore_tmp_db):
    _seed_bars(_series())
    _freeze(["AAA"])
    out = _run(now=NOW, run_id="r1", write=False)
    assert out["rows"] == 2 and out["status"] == {outcomes.CLOSED: 1, outcomes.OPEN: 1}
    assert _count() == 0


def test_an_empty_ledger_writes_nothing_and_says_so(rawstore_tmp_db):
    out = _run(now=NOW, run_id="r1")
    assert out["picks"] == 0 and out["rows"] == 0
    assert _count() == 0


def test_one_grouped_read_per_pick_writes_the_same_rows_in_the_same_order(rawstore_tmp_db, monkeypatch):
    """Review round 2, nit 3: ``run`` called ``outcome`` once per (pick, horizon), walking each ticker's series twice.
    It now takes one ``outcome_by_horizon`` per pick. With the per-horizon entry point made unusable, the rows must
    still be exactly the ones it returns — and in the order the per-horizon loop wrote them in."""
    _seed_bars(_series("AAA") + _series("BBB", last=date(2026, 9, 18)))
    _freeze(["AAA", "BBB"])
    latest = cal.latest_complete_session(NOW)
    series = outcomes._bars(["AAA", "BBB"], D)          # the same read the run makes (it starts at the oldest pick)
    per_horizon = outcomes.outcome                      # captured before the spy: the rule the grouping must match
    expected = []
    for t in ("AAA", "BBB"):
        for h in outcomes.HORIZONS:
            expected.append({"source": "swing", "forecast_date": D, "subject": t, "target_type": outcomes.PICK,
                             "target_date": D,          # the ledger stamps a pick's target_date with its date
                             "horizon_days": h, **per_horizon(D, h, series[t], latest),
                             "computed_at": NOW.timestamp(), "model_version": outcomes.MODEL_VERSION, "run_id": "r1"})

    reads = []
    real = outcomes.outcome_by_horizon

    def spy(day, horizons, bars, latest_session):
        reads.append((day, tuple(horizons)))
        return real(day, horizons, bars, latest_session)

    written = []
    real_write = outcomes.upsert
    monkeypatch.setattr(outcomes, "outcome_by_horizon", spy)
    monkeypatch.setattr(outcomes, "outcome", lambda *a, **k: (_ for _ in ()).throw(AssertionError("per-horizon read")))
    monkeypatch.setattr(outcomes, "upsert",
                        lambda con, table, cols, key, rows: written.append(list(rows)) or real_write(con, table, cols,
                                                                                                     key, rows))
    out = _run(now=NOW, run_id="r1")
    assert [row for chunk in written for row in chunk] == expected       # same rows, same order
    assert (out["rows"], out["status"]) == (4, {outcomes.CLOSED: 1, outcomes.OPEN: 3})
    assert reads == [(D, outcomes.HORIZONS)] * 2, "one pass per pick, not one per (pick, horizon)"


# ── guards (green from the start; each killed by its mutation) ──────────────

def test_the_writer_never_writes_forecast_ledger(rawstore_tmp_db, monkeypatch):
    _seed_bars(_series())
    _freeze(["AAA"])
    before = _q("SELECT * FROM forecast_ledger ORDER BY subject")
    written = []
    real = outcomes.upsert

    def spy(con, table, *a):
        if table == "forecast_ledger":
            raise AssertionError("forecast_outcomes must never write forecast_ledger")
        written.append(table)
        return real(con, table, *a)

    monkeypatch.setattr(outcomes, "upsert", spy)
    monkeypatch.setattr(ledger, "write_picks", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ledger")))
    _run(now=NOW, run_id="r1", full=True)
    assert set(written) == {"forecast_outcomes"}
    assert _q("SELECT * FROM forecast_ledger ORDER BY subject") == before


def test_the_writer_makes_no_provider_call(rawstore_tmp_db, monkeypatch):
    _seed_bars(_series())
    _freeze(["AAA"])
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("provider call"))  # noqa: E731
    monkeypatch.setattr(prices, "_yf_download", boom)
    monkeypatch.setattr(prices, "ensure_daily_bars", boom)
    monkeypatch.setattr(prices, "fill_history", boom)
    from RevenueChaser.core.providers import unusual_whales
    monkeypatch.setattr(unusual_whales.UWClient, "__init__", boom)
    out = _run(now=NOW, run_id="r1")
    assert out["rows"] == 2


# ── window arithmetic ───────────────────────────────────────────────────────

@pytest.mark.parametrize("day,h,end", [
    (D, 30, END30),
    (D, 90, date(2026, 11, 30)),
    (date(2026, 10, 30), 30, date(2026, 11, 27)),   # day 30 is Sunday 2026-11-29: back to Friday (Thanksgiving is 26)
    (date(2026, 8, 7), 31, date(2026, 9, 4)),       # day 31 is Labor Day 2026-09-07: back to Friday 2026-09-04
])
def test_the_window_ends_on_the_last_session_on_or_before_day_n(day, h, end):
    assert outcomes.window_end(day, h) == end
