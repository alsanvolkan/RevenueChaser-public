"""#372 E5 reader measurement: synthetic snapshots, read-only, offline.

The script must flag the cards R6 would veto (a stored FOMC/CPI inside the report date ±1 trading day AND
|beta| > 4), count the stored coverage of the run's tickers, and never open a live store.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pytest

RESEARCH = Path(__file__).resolve().parents[1] / "research"

pytestmark = pytest.mark.skipif(not RESEARCH.is_dir(),
                                reason="scripts/research/ is not in this tree")

NY = ZoneInfo("America/New_York")
LATEST = date(2026, 10, 14)                 # the newest session in the fixtures (the pinned clock)
NOW = datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc)
REPORT = date(2026, 10, 20)                 # a Tuesday; the window is 10-19 .. 10-21 (report date ±1 trading day)
SESSIONS = 26


def _load():
    spec = importlib.util.spec_from_file_location("e5_card_counts", RESEARCH / "372_e5_card_counts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def script():
    return _load()


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    """Pin the session clock: the coverage and the window both read ``latest_complete_session``."""
    from RevenueChaser.core import calendar
    monkeypatch.setattr(calendar, "_now", lambda: NOW)


def _sessions(n, end=LATEST):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def _duration_ohlc(n=SESSIONS, duration=10.0):
    """Bars whose return moves −duration per 1bp of yield: ``yield_beta`` reads ≈ −duration."""
    out, price, y = [], 100.0, 4.00
    for i, d in enumerate(_sessions(n)):
        out.append({"date": d.isoformat(), "open": price, "high": price, "low": price, "close": price})
        step = 0.10 * (0.5 + (i % 3) * 0.5)
        price *= 1.0 + (-duration * (step / 100.0))
        y += step
    return out


def _yields_from(bars):
    """The DGS10 rows that pair with ``_duration_ohlc`` (same walk, keyed by the bar dates)."""
    out, y = [], 4.00
    for i, bar in enumerate(bars):
        out.append((bar["date"], y))
        step = 0.10 * (0.5 + (i % 3) * 0.5)
        y += step
    return out


def _raw_snapshot(path: Path, *, bars, tickers=("AAA", "BBB"), events=(), dgs=True):
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE macro_series (series_id TEXT, obs_date DATE, source TEXT, value DOUBLE)")
    con.execute("CREATE TABLE economic_calendar (session_date DATE, event_ts DOUBLE, event TEXT, type TEXT)")
    con.execute("CREATE TABLE earnings_calendar (session_date DATE, earnings_date DATE, ticker TEXT, "
                "report_session TEXT)")
    con.execute("CREATE TABLE short_interest (ticker TEXT, session_date DATE)")
    con.execute("CREATE TABLE estimate_snapshots (ticker TEXT, session_date DATE, period TEXT)")
    con.execute("CREATE TABLE earnings_history (ticker TEXT, endpoint TEXT, report_date DATE, valid_from DATE)")
    if dgs:
        con.executemany("INSERT INTO macro_series VALUES ('DGS10', ?, 'fred', ?)",
                        [(date.fromisoformat(d), v) for d, v in _yields_from(bars)])
    if events:
        con.executemany("INSERT INTO economic_calendar VALUES (?, ?, ?, ?)",       # (ts, event, type[, session])
                        [(e[3] if len(e) > 3 else LATEST, e[0], e[1], e[2]) for e in events])
    for t in tickers:
        con.execute("INSERT INTO earnings_calendar VALUES (?, ?, ?, 'premarket')", [LATEST, REPORT, t])
        con.execute("INSERT INTO earnings_calendar VALUES (?, ?, ?, 'afterhours')", [LATEST, REPORT, t])
        con.execute("INSERT INTO short_interest VALUES (?, ?)", [t, LATEST])
        con.execute("INSERT INTO estimate_snapshots VALUES (?, ?, '0q')", [t, LATEST])
        con.execute("INSERT INTO earnings_history VALUES (?, '/earnings/{ticker}', ?, ?)", [t, REPORT, LATEST])
    con.close()


def _prices_snapshot(path: Path, *, tickers=("AAA",), last=LATEST):
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE price_bars_daily (ticker TEXT, session_date DATE, close DOUBLE)")
    con.executemany("INSERT INTO price_bars_daily VALUES (?, ?, 1.0)", [(t, last) for t in tickers])
    con.close()


def _history_snapshot(path: Path, *, cards, run_id="run-1", run_at=1.0, older=()):
    """``older``: extra ``(run_id, run_at, cards)`` runs recorded before ``run_id``."""
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE result_runs (board TEXT, run_id TEXT, run_at DOUBLE)")
    con.execute("CREATE TABLE earnings_cards_history (run_id TEXT, ticker TEXT, earnings_date DATE, inputs BLOB)")
    for rid, at, rows in older:
        con.execute("INSERT INTO result_runs VALUES ('module_earnings_radar', ?, ?)", [rid, at])
        con.executemany("INSERT INTO earnings_cards_history VALUES (?, ?, ?, ?)",
                        [(rid, t, e, zlib.compress(json.dumps(inputs).encode())) for t, e, inputs in rows])
    if run_id is not None:
        con.execute("INSERT INTO result_runs VALUES ('module_earnings_radar', ?, ?)", [run_id, run_at])
    if cards:
        con.executemany("INSERT INTO earnings_cards_history VALUES (?, ?, ?, ?)",
                        [(run_id, t, e, zlib.compress(json.dumps(inputs).encode())) for t, e, inputs in cards])
    con.close()


@pytest.fixture
def snapshots(tmp_path, script):
    """Three snapshots in a scratch folder, shaped like the backups the script is pointed at."""

    def build(*, bars=None, events=(), cards=None, tickers=("AAA", "BBB"), dgs=True, prices_tickers=("AAA",),
              prices_last=LATEST, run_id="run-1", run_at=NOW.timestamp(), older=(), sub=""):
        bars = _duration_ohlc() if bars is None else bars
        base = tmp_path / sub if sub else tmp_path
        base.mkdir(exist_ok=True)
        raw, hist, prices = base / "raw_x.duckdb", base / "history_x.duckdb", base / "prices_x.duckdb"
        _raw_snapshot(raw, bars=bars, tickers=tickers, events=events, dgs=dgs)
        _prices_snapshot(prices, tickers=prices_tickers, last=prices_last)
        if cards is None:
            cards = [(t, REPORT, {"ohlc_year": bars}) for t in tickers]
        _history_snapshot(hist, cards=cards, run_id=run_id, run_at=run_at, older=older)
        return raw, hist, prices, base

    return build


def _open(script, paths, i):
    return script.cm.open_ro(paths[i])


def _ts(y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=NY).timestamp()


START, END = date(2026, 10, 19), date(2026, 10, 21)         # the report date ±1 trading day


def _events(middle=None):
    """The store must SPAN the window; the middle row is the rate event under test."""
    rows = [(START, "Jobs Report", "report"), (END, "GDP", "report")]
    if middle:
        rows.insert(1, middle)
    return [(_ts(d.year, d.month, d.day), event, kind) for d, event, kind in rows]


CPI_EVENT = (date(2026, 10, 20), "CPI, Y/Y%", "report")
PPI_EVENT = (date(2026, 10, 20), "Ex-Food & Energy PPI", "report")


# ── the guards ───────────────────────────────────────────────────────────────

def test_a_live_store_path_is_refused(script):
    assert "live store" in script.cm.refusal(Path(".store/raw.duckdb"))
    assert script.cm.refusal(Path(".cache/research/372/store/raw_20261016.duckdb")) is None


def test_the_run_reads_a_copy_and_never_the_snapshot(snapshots, script, tmp_path):
    raw, hist, prices, folder = snapshots()
    out = folder / "out"
    script.main(["--raw", str(raw), "--history", str(hist), "--prices", str(prices), "--out", str(out)])
    copies = sorted(p.name for p in (out / "store").iterdir())
    assert copies == ["history_x.duckdb", "prices_x.duckdb", "raw_x.duckdb"]
    assert (out / "e5_card_counts.json").is_file() and (out / "e5_card_counts.txt").is_file()
    src = raw.read_bytes()
    script.main(["--raw", str(raw), "--history", str(hist), "--prices", str(prices), "--out", str(out)])
    assert raw.read_bytes() == src                       # a re-run changes nothing at the source


def test_no_socket_opens_during_the_run(script, snapshots):
    raw, hist, prices, folder = snapshots()
    import socket
    with script.cm.offline_guard():
        with pytest.raises(script.cm.OfflineError):
            socket.create_connection(("example.com", 443))
        script.main(["--raw", str(raw), "--history", str(hist), "--prices", str(prices),
                     "--out", str(folder / "offline")])


# ── what it reports ──────────────────────────────────────────────────────────

def test_the_rate_event_and_the_extreme_beta_flag_the_card(snapshots, script):
    raw, hist, prices, folder = snapshots(events=_events(CPI_EVENT))
    result = script.main(["--raw", str(raw), "--history", str(hist), "--prices", str(prices),
                          "--out", str(folder / "a")])
    rec = result["rate_event_cards"]
    assert rec["n_flagged"] == 2 and rec["flagged"] == ["AAA", "BBB"]
    assert {e["overlap"] for e in rec["extreme_betas"]} == {True}
    assert all(abs(e["beta"]) > 4 for e in rec["extreme_betas"])


def test_a_payrolls_event_is_not_a_rate_event(snapshots, script):
    raw, hist, prices, folder = snapshots(events=_events())
    result = script.main(["--raw", str(raw), "--history", str(hist), "--prices", str(prices),
                          "--out", str(folder / "b")])
    assert result["rate_event_cards"]["n_flagged"] == 0
    assert all(e["overlap"] is False for e in result["rate_event_cards"]["extreme_betas"])


def test_a_store_that_does_not_span_the_window_is_none(snapshots, script):
    """Only the middle row is stored: the store cannot answer. The card is not flagged — and the reason is
    ``None`` ("no answer"), never ``False`` ("no overlap"), because the two mean opposite things to R6."""
    raw, hist, prices, folder = snapshots(events=[(_ts(2026, 10, 20), "CPI", "report")])
    result = script.main(["--raw", str(raw), "--history", str(hist), "--prices", str(prices),
                          "--out", str(folder / "span")])
    assert result["rate_event_cards"]["n_flagged"] == 0
    assert {e["overlap"] for e in result["rate_event_cards"]["extreme_betas"]} == {None}


def test_a_missing_beta_is_reported_not_flagged(snapshots, script):
    """One bar pairs nothing: ``yield_beta`` is None, so the card cannot be flagged either way."""
    raw, hist, prices, folder = snapshots(bars=_duration_ohlc(n=2))
    result = script.main(["--raw", str(raw), "--history", str(hist), "--prices", str(prices),
                          "--out", str(folder / "c")])
    assert result["rate_event_cards"]["n_flagged"] == 0
    assert sorted(result["rate_event_cards"]["no_beta"]) == ["AAA", "BBB"]


def test_a_recorded_overlap_that_does_not_match_is_listed(snapshots, script):
    cards = [("AAA", REPORT, {"ohlc_year": _duration_ohlc(), "rate_event_overlap": False})]
    raw, hist, prices, folder = snapshots(events=_events(CPI_EVENT), cards=cards, tickers=("AAA",))
    result = script.main(["--raw", str(raw), "--history", str(hist), "--prices", str(prices),
                          "--out", str(folder / "d")])
    assert result["rate_event_cards"]["recorded_vs_recomputed_gaps"] == [
        {"ticker": "AAA", "report_date": REPORT.isoformat(), "recomputed": True, "recorded": False}]


def test_the_coverage_counts_what_the_readers_would_use(snapshots, script):
    raw, hist, prices, folder = snapshots(events=_events())
    result = script.main(["--raw", str(raw), "--history", str(hist), "--prices", str(prices),
                          "--out", str(folder / "e")])
    cov = result["coverage"]
    assert cov["tickers"] == 2 and cov["latest_complete_session"] == LATEST.isoformat()
    assert cov["stored_tickers"]["calendar"] == {"n": 2, "of": 2}
    assert cov["stored_tickers"]["short_interest"] == {"n": 2, "of": 2}
    assert cov["stored_tickers"]["estimates"] == {"n": 2, "of": 2}
    assert cov["stored_tickers"]["ohlc"] == {"n": 1, "of": 2}          # only AAA has a current bar
    assert cov["dgs10_rows"] == SESSIONS and cov["economic_calendar_rows"] == 2


def test_a_stale_bar_is_not_coverage(snapshots, script):
    raw, hist, prices, folder = snapshots(prices_last=date(2026, 10, 13))
    result = script.main(["--raw", str(raw), "--history", str(hist), "--prices", str(prices),
                          "--out", str(folder / "f")])
    assert result["coverage"]["stored_tickers"]["ohlc"] == {"n": 0, "of": 2}


def test_the_calls_saved_are_the_arithmetic_the_text_explains(snapshots, script):
    raw, hist, prices, folder = snapshots()
    result = script.main(["--raw", str(raw), "--history", str(hist), "--prices", str(prices),
                          "--out", str(folder / "g")])
    saved = result["calls_saved"]
    assert saved == {"calendar": 2, "short_interest": 1, "ohlc": 1, "fred_dgs10": 1, "total": 5}
    text = (folder / "g" / "e5_card_counts.txt").read_text(encoding="utf-8")
    assert "short_screener 1 (100 tickers a request)" in text
    assert "total 5" in text


def test_an_empty_history_snapshot_reports_no_run(snapshots, script):
    raw, hist, prices, folder = snapshots(cards=[], run_id=None)
    result = script.main(["--raw", str(raw), "--history", str(hist), "--prices", str(prices),
                          "--out", str(folder / "h")])
    assert result["run_id"] is None and result["cards"] == 0


# ── the board's own rule (review S7) ─────────────────────────────────────────

def _run(script, paths, name, *extra):
    raw, hist, prices, folder = paths
    return script.main(["--raw", str(raw), "--history", str(hist), "--prices", str(prices),
                        "--out", str(folder / name), *extra])


def test_an_fomc_two_trading_days_after_the_report_is_not_flagged(snapshots, script):
    """Tue 10-20 report: the window ends Wed 10-21. A Thu 10-22 FOMC is +2 trading days (M1)."""
    events = [(_ts(2026, 10, 19), "Jobs Report", "report"), (_ts(2026, 10, 22, 14), "FOMC Statement", "fomc")]
    result = _run(script, snapshots(events=events), "m1")
    assert result["rate_event_cards"]["n_flagged"] == 0
    assert {e["overlap"] for e in result["rate_event_cards"]["extreme_betas"]} == {False}


def _noisy_bars(n=60, seed=4, true_beta=-8.0):
    """A real but small yield dependence in 2% daily noise: |beta| > 4 and |t| < 2 (the board's noise gate)."""
    import random
    rng = random.Random(seed)
    bars, yields, price, y = [], [], 100.0, 4.00
    for d in _sessions(n):
        bars.append({"date": d.isoformat(), "open": price, "high": price, "low": price, "close": price})
        yields.append((d.isoformat(), y))
        dy = rng.choice((-1, 1)) * rng.uniform(0.02, 0.08)
        price *= 1.0 + true_beta * dy / 100.0 + rng.gauss(0.0, 0.02)
        y += dy
    return bars, yields


def test_a_noisy_extreme_beta_is_a_badge_not_a_flag(snapshots, script, monkeypatch):
    """M4: the board skips the veto when |t| < 2, so the card count must too."""
    bars, yields = _noisy_bars()
    monkeypatch.setattr(sys.modules[__name__], "_yields_from", lambda _bars: yields)
    result = _run(script, snapshots(bars=bars, events=_events(CPI_EVENT), tickers=("AAA",)), "m4")
    rec = result["rate_event_cards"]
    (e,) = rec["extreme_betas"]
    assert abs(e["beta"]) > 4 and abs(e["t"]) < 2 and e["noisy"] is True and e["overlap"] is True
    assert rec["n_flagged"] == 0 and rec["noisy_not_vetoed"] == ["AAA"]


def test_the_beta_is_the_last_60_bars_the_board_reads(snapshots, script):
    """A year of bars whose duration lives only in the old part: the board's 60-bar beta is not extreme."""
    year = _duration_ohlc(n=260)
    year = year[:200] + [{**b, "close": 50.0} for b in year[200:]]       # the last 60 sessions do not move
    result = _run(script, snapshots(bars=year, events=_events(CPI_EVENT), tickers=("AAA",)), "s7")
    rec = result["rate_event_cards"]
    assert rec["n_flagged"] == 0
    assert rec["extreme_betas"] == []


def test_the_last_n_runs_are_each_checked(snapshots, script):
    bars = _duration_ohlc()
    older = [("run-0", 0.5, [("CCC", REPORT, {"ohlc_year": bars})])]
    result = _run(script, snapshots(events=_events(CPI_EVENT), older=older), "runs", "--runs", "2")
    assert [r["run_id"] for r in result["runs"]] == ["run-1", "run-0"]
    assert result["runs"][0]["flagged"] == ["AAA", "BBB"] and result["runs"][1]["flagged"] == ["CCC"]
    assert result["rate_event_cards"] == result["runs"][0]
    one = _run(script, snapshots(events=_events(CPI_EVENT), older=older, sub="one"), "one", "--runs", "1")
    assert [r["run_id"] for r in one["runs"]] == ["run-1"]


def test_the_runs_day_and_next_trading_day_are_not_counted_as_saved(snapshots, script):
    """S4: a Fetch on Mon 10-19 reads Mon's and Tue 10-20's calendar pairs live whatever the store holds."""
    monday_evening = datetime(2026, 10, 19, 21, 0, tzinfo=timezone.utc).timestamp()
    result = _run(script, snapshots(run_at=monday_evening), "s4")
    assert result["calls_saved"]["calendar"] == 0
    friday_before = datetime(2026, 10, 16, 21, 0, tzinfo=timezone.utc).timestamp()   # live days 10-16 and 10-19
    assert _run(script, snapshots(run_at=friday_before, sub="b"), "s4b")["calls_saved"]["calendar"] == 2


# ── review D-3: the counts follow the run, not the script's clock ───────────

def test_cards_without_an_answer_are_counted(snapshots, script):
    """D-3: a card whose overlap is None (the store does not span its window) is counted, not just left out."""
    none = _run(script, snapshots(events=[(_ts(2026, 10, 20), "CPI", "report")]), "none")["rate_event_cards"]
    assert none["n_overlap_none"] == 2
    spans = _run(script, snapshots(events=_events(CPI_EVENT), sub="spans"), "spans")["rate_event_cards"]
    assert spans["n_overlap_none"] == 0


def test_a_rescheduled_cpi_is_read_from_the_newest_sweep(snapshots, script):
    """D-1 through the script: the 10-13 sweep had CPI on 10-20, the 10-14 sweep moved it to 10-27."""
    old, new = date(2026, 10, 13), date(2026, 10, 14)
    events = [(*e, sweep) for sweep in (old, new) for e in _events()]
    events += [(_ts(2026, 10, 20), "CPI", "report", old), (_ts(2026, 10, 27), "CPI", "report", new)]
    rec = _run(script, snapshots(events=events), "moved")["rate_event_cards"]
    assert rec["n_flagged"] == 0
    assert {e["overlap"] for e in rec["extreme_betas"]} == {False}


def test_the_ohlc_saved_uses_the_runs_latest_complete_session(snapshots, script):
    """The run was Tue 10-13 17:00 New York; its current bar is 10-13, whatever the script's own clock says."""
    tuesday_evening = datetime(2026, 10, 13, 21, 0, tzinfo=timezone.utc).timestamp()
    result = _run(script, snapshots(run_at=tuesday_evening, prices_last=date(2026, 10, 13)), "tue")
    assert result["coverage"]["latest_complete_session"] == "2026-10-13"
    assert result["calls_saved"]["ohlc"] == 1


def test_an_in_session_run_reads_its_reporters_live(snapshots, script):
    """M3: at 11:00 New York on Tue 10-20 a name reporting that day, or on Mon 10-19, reads etf_ohlc live; a later
    one does not."""
    in_session = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc).timestamp()
    bars = _duration_ohlc()
    cards = [("AAA", date(2026, 10, 20), {"ohlc_year": bars}), ("BBB", date(2026, 10, 27), {"ohlc_year": bars}),
             ("CCC", date(2026, 10, 19), {"ohlc_year": bars})]
    result = _run(script, snapshots(run_at=in_session, cards=cards, prices_tickers=("AAA", "BBB", "CCC"),
                                    prices_last=date(2026, 10, 19)), "insession")
    assert result["coverage"]["latest_complete_session"] == "2026-10-19"
    assert result["coverage"]["in_session_reporters"] == 2
    assert result["calls_saved"]["ohlc"] == 1


def test_after_the_close_a_reporter_keeps_its_stored_bar(snapshots, script):
    """17:00 New York on Tue 10-20: the stored bar already ends that day, so the reporter is stored coverage."""
    after_close = datetime(2026, 10, 20, 21, 0, tzinfo=timezone.utc).timestamp()
    cards = [("AAA", date(2026, 10, 20), {"ohlc_year": _duration_ohlc()})]
    result = _run(script, snapshots(run_at=after_close, cards=cards, tickers=("AAA",),
                                    prices_last=date(2026, 10, 20)), "afterclose")
    assert result["coverage"]["in_session_reporters"] == 0
    assert result["calls_saved"]["ohlc"] == 1


@pytest.mark.parametrize("run_day,saved", [(19, 1), (20, 0)], ids=["three-sessions-old", "four-sessions-old"])
def test_the_fred_call_is_saved_only_when_the_stored_dgs10_is_fresh(snapshots, script, run_day, saved):
    """S3 freshness: the stored DGS10 ends 10-14; a run on 10-19 is 3 sessions later (read), on 10-20 it is 4."""
    run_at = datetime(2026, 10, run_day, 21, 0, tzinfo=timezone.utc).timestamp()
    assert _run(script, snapshots(run_at=run_at), f"fred{run_day}")["calls_saved"]["fred_dgs10"] == saved
