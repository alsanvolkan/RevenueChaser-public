"""E5d (#372): R6's rate-event overlap comes from the stored ``economic_calendar`` (D37) and the yield series from
the stored ``macro_series`` (D27) — UW/FRED are asked only for what is missing.

Offline: temp stores seeded with the rows, a fake client with a duration-like price series. The rule itself
(FOMC/CPI only, the ±1 trading-day window, None = no veto) is pinned here; the pipeline wiring is in
``test_pipeline_score.py::TestR6YieldBetaWiring``.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_earnings_radar import stored_inputs
from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline
from RevenueChaser.module_earnings_radar.tests.test_pipeline_score import _FakeUW, _duration_ohlc_and_yields

NY = ZoneInfo("America/New_York")
REPORT = date(2026, 1, 20)                 # a Tuesday (Mon Jan 19 is Martin Luther King Day)
WINDOW = (date(2026, 1, 16), date(2026, 1, 21))     # one trading day each side of REPORT, written out by hand


NOW = datetime(2026, 1, 30, 23, 0, tzinfo=timezone.utc)   # Fri 18:00 New York: the latest complete session is Jan 30


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    """The stored DGS10 must be fresh against the clock (review S3); the fixtures' series end in late January."""
    from RevenueChaser.core import calendar
    monkeypatch.setattr(calendar, "_now", lambda: NOW)


@pytest.fixture(autouse=True)
def _store(rawstore_tmp_db):
    return rawstore_tmp_db


STORE_SESSION = date(2026, 1, 15)          # the one nightly sweep a test stores, unless it names another


def _event(event, kind, when: datetime, session: date = STORE_SESSION):
    """One ``economic_calendar`` row as the writer keys it: the session that swept it, then the event."""
    return {"session_date": session, "event_ts": when.timestamp(), "event": event, "type": kind,
            "source": "uw"}


def _rate_event_in_span(event="CPI", kind="report", at=None):
    """A rate event at the window end of REPORT plus the two rows that make the store span the window."""
    start, end = WINDOW
    when = at if at is not None else datetime(end.year, end.month, end.day, 12, 0, tzinfo=NY)
    return [
        _event("Jobs Report", "report", datetime(start.year, start.month, start.day, 8, 30, tzinfo=NY)),
        _event(event, kind, when),
        _event("GDP", "report", datetime(end.year, end.month, end.day, 8, 30, tzinfo=NY)),
    ]


def test_the_window_is_one_trading_day_each_side():
    """Written out by hand, not from the reader's formula: a holiday and a weekend are skipped, nothing more."""
    assert stored_inputs.rate_event_window(REPORT) == WINDOW
    assert stored_inputs.rate_event_window(date(2026, 10, 19)) == (date(2026, 10, 16), date(2026, 10, 20))  # Mon
    assert stored_inputs.rate_event_window(date(2026, 10, 22)) == (date(2026, 10, 21), date(2026, 10, 23))  # Thu
    assert stored_inputs.rate_event_window(date(2026, 10, 23)) == (date(2026, 10, 22), date(2026, 10, 26))  # Fri


def _october_span(*events):
    """A stored calendar spanning Thu 2026-10-15 .. Tue 2026-10-27, plus ``events``."""
    return [_event("Jobs Report", "report", datetime(2026, 10, 15, 8, 30, tzinfo=NY)),
            *events,
            _event("GDP", "report", datetime(2026, 10, 27, 8, 30, tzinfo=NY))]


def test_fomc_two_trading_days_after_a_monday_report_is_no_overlap():
    rawstore.write_rows("economic_calendar", _october_span(
        _event("FOMC Statement", "fomc", datetime(2026, 10, 21, 14, 0, tzinfo=NY))))
    assert stored_inputs.rate_event_overlap(date(2026, 10, 19)) is False


def test_cpi_on_the_monday_after_a_thursday_report_is_no_overlap():
    rawstore.write_rows("economic_calendar", _october_span(
        _event("CPI", "report", datetime(2026, 10, 26, 8, 30, tzinfo=NY))))
    assert stored_inputs.rate_event_overlap(date(2026, 10, 22)) is False


def test_fomc_on_the_monday_after_a_friday_report_is_an_overlap():
    rawstore.write_rows("economic_calendar", _october_span(
        _event("FOMC Statement", "fomc", datetime(2026, 10, 26, 14, 0, tzinfo=NY))))
    assert stored_inputs.rate_event_overlap(date(2026, 10, 23)) is True


def test_fomc_inside_the_window_is_a_rate_event():
    _row = _event("FOMC Statement", "fomc", datetime(2026, 1, 21, 14, 0, tzinfo=NY))
    rawstore.write_rows("economic_calendar", [
        _event("Jobs Report", "report", datetime(2026, 1, 16, 8, 30, tzinfo=NY)),
        _row,
        _event("GDP", "report", datetime(2026, 1, 22, 8, 30, tzinfo=NY)),
    ])
    assert stored_inputs.rate_event_overlap(REPORT) is True


@pytest.mark.parametrize("event", ["CPI", "CPI, Y/Y%", "Core CPI, M/M%"])
def test_cpi_named_events_are_rate_events(event):
    rawstore.write_rows("economic_calendar", _rate_event_in_span(event=event))
    assert stored_inputs.rate_event_overlap(REPORT) is True


@pytest.mark.parametrize("event", ["PPI", "Ex-Food & Energy PPI"])
def test_ppi_is_not_a_rate_event(event):
    """The PC printed these PPI strings: "CPI" as a substring must not match them."""
    rawstore.write_rows("economic_calendar", _rate_event_in_span(event=event))
    assert stored_inputs.rate_event_overlap(REPORT) is False


def test_payrolls_gdp_and_fed_speakers_are_not_rate_events():
    rawstore.write_rows("economic_calendar", [
        _event("Jobs Report", "report", datetime(2026, 1, 16, 8, 30, tzinfo=NY)),
        _event("Fed Speaker", "fed-speaker", datetime(2026, 1, 21, 12, 0, tzinfo=NY)),
        _event("GDP", "report", datetime(2026, 1, 22, 8, 30, tzinfo=NY)),
    ])
    assert stored_inputs.rate_event_overlap(REPORT) is False


def test_a_report_outside_the_stored_span_is_none():
    """The store holds this week only: a print three months out has no answer, and None is not a veto."""
    rawstore.write_rows("economic_calendar", _rate_event_in_span())
    assert stored_inputs.rate_event_overlap(date(2026, 4, 21)) is None


def test_an_empty_store_is_none():
    assert stored_inputs.rate_event_overlap(REPORT) is None


def test_a_2330_new_york_event_lands_on_the_new_york_date():
    """23:30 in New York is the next UTC day; the window is the New York one, so it is the window end."""
    start, end = WINDOW
    rawstore.write_rows("economic_calendar", [
        _event("Jobs Report", "report", datetime(start.year, start.month, start.day, 8, 30, tzinfo=NY)),
        _event("CPI", "report", datetime(end.year, end.month, end.day, 23, 30, tzinfo=NY)),
    ])
    assert stored_inputs.rate_event_overlap(REPORT) is True


def test_a_broken_store_is_none(monkeypatch):
    rawstore.write_rows("economic_calendar", _rate_event_in_span())
    monkeypatch.setattr(rawstore, "query", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("busy")))
    assert stored_inputs.rate_event_overlap(REPORT) is None


def _two_sweeps(old_cpi: datetime, new_cpi: datetime, old=date(2026, 10, 13), new=date(2026, 10, 16)):
    """Two nightly sweeps of the same span (Mon 10-12 .. Fri 10-30); the CPI sits on a different day in each."""
    def span(s):
        return [_event("Jobs Report", "report", datetime(2026, 10, 12, 8, 30, tzinfo=NY), s),
                _event("GDP", "report", datetime(2026, 10, 30, 8, 30, tzinfo=NY), s)]
    return [*span(old), _event("CPI", "report", old_cpi, old), *span(new), _event("CPI", "report", new_cpi, new)]


def test_a_rescheduled_cpi_does_not_overlap_on_its_old_date():
    """Review D-1: the 10-13 sweep stored CPI on Thu 10-15, the 10-16 sweep has it moved to Fri 10-23. The writer
    keeps both sessions' rows, but only the newest sweep is the calendar now: a Wed 10-14 report has no CPI near it."""
    rawstore.write_rows("economic_calendar", _two_sweeps(datetime(2026, 10, 15, 8, 30, tzinfo=NY),
                                                         datetime(2026, 10, 23, 8, 30, tzinfo=NY)))
    assert len(stored_inputs.rate_events()) == 6
    assert stored_inputs.rate_event_overlap(date(2026, 10, 14)) is False
    assert stored_inputs.rate_event_overlap(date(2026, 10, 22)) is True       # its new date does overlap


def test_a_cpi_moved_into_the_window_overlaps_on_its_new_date():
    rawstore.write_rows("economic_calendar", _two_sweeps(datetime(2026, 10, 23, 8, 30, tzinfo=NY),
                                                         datetime(2026, 10, 15, 8, 30, tzinfo=NY)))
    assert stored_inputs.rate_event_overlap(date(2026, 10, 14)) is True
    assert stored_inputs.rate_event_overlap(date(2026, 10, 22)) is False


def test_an_older_sweep_answers_a_window_the_newest_does_not_span():
    """A past print: the newest sweep starts after its window, so the newest sweep that spans it answers."""
    rawstore.write_rows("economic_calendar", [
        *_rate_event_in_span(),                                                       # session Jan 15, CPI Jan 21
        _event("Jobs Report", "report", datetime(2026, 1, 26, 8, 30, tzinfo=NY), date(2026, 1, 23)),
        _event("GDP", "report", datetime(2026, 2, 6, 8, 30, tzinfo=NY), date(2026, 1, 23)),
    ])
    assert stored_inputs.rate_event_overlap(REPORT) is True


def test_sweeps_that_each_cover_half_the_window_are_none():
    """Two sweeps together span the window but neither does alone: no single calendar answers, so no veto."""
    start, end = WINDOW
    rawstore.write_rows("economic_calendar", [
        _event("Jobs Report", "report", datetime(start.year, start.month, start.day, 8, 30, tzinfo=NY),
               date(2026, 1, 9)),
        _event("CPI", "report", datetime(end.year, end.month, end.day, 8, 30, tzinfo=NY), date(2026, 1, 20)),
        _event("GDP", "report", datetime(2026, 1, 28, 8, 30, tzinfo=NY), date(2026, 1, 20)),
    ])
    assert stored_inputs.rate_event_overlap(REPORT) is None


# ── the stored DGS10 map ─────────────────────────────────────────────────────────────────────────────────────────

def _seed_dgs10(yields: dict):
    rawstore.write_rows("macro_series", [
        {"series_id": "DGS10", "obs_date": date.fromisoformat(d), "source": "fred", "value": v,
         "frequency": "daily"} for d, v in yields.items()])


def test_dgs10_map_reads_the_fred_rows():
    _seed_dgs10({"2026-01-28": 4.0, "2026-01-29": 4.1})
    rawstore.write_rows("macro_series", [{"series_id": "DGS10", "obs_date": date(2026, 1, 30), "source": "yfinance",
                                          "value": 9.9}])
    assert stored_inputs.dgs10_map() == {"2026-01-28": 4.0, "2026-01-29": 4.1}


def test_dgs10_map_skips_null_values():
    rawstore.write_rows("macro_series", [
        {"series_id": "DGS10", "obs_date": date(2026, 1, 29), "source": "fred", "value": None}])
    assert stored_inputs.dgs10_map() == {}


def test_dgs10_map_is_the_newest_80_observations():
    """The window FRED would serve: the 80 newest, not the 80 oldest and not all of them (review S1)."""
    days = [date(2025, 10, 22) + timedelta(days=i) for i in range(100)]          # .. 2026-01-29
    _seed_dgs10({d.isoformat(): 4.0 + i / 100 for i, d in enumerate(days)})
    got = stored_inputs.dgs10_map()
    assert sorted(got) == [d.isoformat() for d in days[-80:]]


def _dgs10_ending(newest: date):
    _seed_dgs10({(newest - timedelta(days=i)).isoformat(): 4.0 for i in range(30)})


def test_dgs10_three_sessions_old_is_still_read():
    """Newest observation Tue Jan 27: three sessions (Wed, Thu, Fri) after it up to Fri Jan 30 = MAX_LAG."""
    _dgs10_ending(date(2026, 1, 27))
    assert stored_inputs.MAX_LAG["macro_series"] == 3
    assert max(stored_inputs.dgs10_map()) == "2026-01-27"


def test_dgs10_four_sessions_old_is_stale():
    """Newest observation Mon Jan 26: four sessions behind, so the run asks FRED instead (review S3)."""
    _dgs10_ending(date(2026, 1, 26))
    assert stored_inputs.dgs10_map() == {}


def test_dgs10_on_a_day_the_stock_market_is_closed_counts_the_sessions_after_it():
    """FRED observes bond-market days; Good Friday 2026-04-03 has no stock session but the lag still counts."""
    _seed_dgs10({"2026-04-02": 4.0, "2026-04-03": 4.1})
    assert stored_inputs.dgs10_map(latest=date(2026, 4, 6)) == {"2026-04-02": 4.0, "2026-04-03": 4.1}


def test_a_stale_stored_dgs10_falls_back_to_fred(monkeypatch):
    from RevenueChaser.module_earnings_radar import macro_watch
    _dgs10_ending(date(2026, 1, 20))
    monkeypatch.setattr(macro_watch, "fred_daily_map", lambda *a, **k: {"2026-01-29": 4.2})
    p = _pipeline([])
    assert p._warm_dgs10() == {"2026-01-29": 4.2}
    assert p.input_sources["dgs10"] == "fred"


def test_dgs10_map_of_an_empty_store_is_empty():
    assert stored_inputs.dgs10_map() == {}


# ── the pipeline: stored macro in, veto out ──────────────────────────────────────────────────────────────────────

def _pipeline(ohlc, report=REPORT):
    cal = {"AAPL": {"earnings_date": report.isoformat(), "full_name": "Aple", "sector": "Technology",
                    "expected_move_perc": 0.05}}
    p = EarningsPipeline(["AAPL"], uw=_FakeUW(etf_ohlc=ohlc), cal_meta=cal)
    p._screener_cache = {"AAPL": {"close": "100", "call_volume": "0", "put_volume": "0",
                                  "total_open_interest": "0"}}
    return p


def test_stored_dgs10_and_a_rate_event_veto_an_extreme_duration():
    ohlc, yields = _duration_ohlc_and_yields(duration=10.0)
    _seed_dgs10(yields)
    rawstore.write_rows("economic_calendar", _rate_event_in_span())
    p = _pipeline(ohlc)

    p._warm_dgs10()
    card = p._score("AAPL")

    assert p.input_sources["dgs10"] == "stored"
    assert p.input_sources["rate_events"] == "stored"
    assert card.macro_veto is True
    assert card.final_action == "NO_TRADE"
    assert "MACRO VETO" in (card.badges_json or "")
    assert p._dgs10_cache == yields


def test_a_report_outside_the_stored_span_keeps_the_badge_only():
    ohlc, yields = _duration_ohlc_and_yields(duration=10.0)
    _seed_dgs10(yields)
    rawstore.write_rows("economic_calendar", _rate_event_in_span())
    p = _pipeline(ohlc, report=date(2026, 6, 16))

    p._warm_dgs10()
    card = p._score("AAPL")

    assert card.macro_veto is False
    audit = (card.rule_audit or {}).get("rule6_macro", {})
    assert any("extreme rate sensitivity" in x for x in audit.get("reasons") or [])
    assert p.input_sources["rate_events"] == "none"


def test_an_empty_macro_series_falls_back_to_fred(monkeypatch):
    from RevenueChaser.module_earnings_radar import macro_watch
    calls = []
    monkeypatch.setattr(macro_watch, "fred_daily_map",
                        lambda series_id="DGS10", **kw: calls.append(series_id) or {"2026-01-05": 4.0})
    p = _pipeline([])

    got = p._warm_dgs10()

    assert calls == ["DGS10"]
    assert got == {"2026-01-05": 4.0}
    assert p.input_sources["dgs10"] == "fred"


def test_a_fred_answer_that_is_empty_is_reported_as_none(monkeypatch):
    from RevenueChaser.module_earnings_radar import macro_watch
    monkeypatch.setattr(macro_watch, "fred_daily_map", lambda *a, **k: {})
    p = _pipeline([])
    assert p._warm_dgs10() == {}
    assert p.input_sources["dgs10"] == "none"


def test_the_raw_packet_carries_the_overlap():
    import json
    ohlc, yields = _duration_ohlc_and_yields(duration=10.0)
    _seed_dgs10(yields)
    rawstore.write_rows("economic_calendar", _rate_event_in_span())
    p = _pipeline(ohlc)
    p._warm_dgs10()
    packet = json.loads(p._score("AAPL").raw_packet_json)
    assert packet["rate_event_overlap"] is True

    p2 = _pipeline(ohlc, report=date(2026, 6, 16))
    p2._warm_dgs10()
    assert json.loads(p2._score("AAPL").raw_packet_json)["rate_event_overlap"] is None


# ── the noise gate: |t| < 2 is no veto (docs/rules/redesign.md, "Noisy gate") ───────────────────────────────────

def _noisy_ohlc_and_yields(true_beta=-8.0, n=60, seed=4):
    """A volatile name: a real but small yield dependence buried in 2% daily noise, so |beta| > 4 and |t| < 2."""
    import random
    rng = random.Random(seed)
    ohlc, yields = [], {}
    price, y = 100.0, 4.00
    start = date(2025, 12, 1)                                      # .. 2026-01-29
    for i in range(n):
        d = (start + timedelta(days=i)).isoformat()
        ohlc.append({"date": d, "close": price})
        yields[d] = y
        dy = rng.choice((-1, 1)) * rng.uniform(0.02, 0.08)            # percent points, as FRED serves DGS10
        price *= 1.0 + true_beta * dy / 100.0 + rng.gauss(0.0, 0.02)
        y += dy
    return ohlc, yields


def _ols_t(ohlc, yields):
    """The textbook OLS t-stat of the slope (numpy), the reference for ``yield_beta_tstat``."""
    import numpy as np
    closes = {b["date"]: b["close"] for b in ohlc}
    dates = sorted(set(closes) & set(yields))
    r = np.array([(closes[b] - closes[a]) / closes[a] for a, b in zip(dates, dates[1:])])
    dy = np.array([(yields[b] - yields[a]) / 100.0 for a, b in zip(dates, dates[1:])])
    slope, intercept = np.polyfit(dy, r, 1)
    resid = r - (intercept + slope * dy)
    se = np.sqrt((resid @ resid) / (len(r) - 2) / ((dy - dy.mean()) @ (dy - dy.mean())))
    return slope, slope / se


def test_yield_beta_tstat_is_the_ols_t_stat():
    from RevenueChaser.module_earnings_radar.pipeline import yield_beta_tstat
    from RevenueChaser.module_earnings_radar.rules import rule6_macro
    ohlc, yields = _noisy_ohlc_and_yields()
    closes = {b["date"]: b["close"] for b in ohlc}
    beta = rule6_macro.yield_beta(closes, yields)
    slope, t = _ols_t(ohlc, yields)
    assert beta == pytest.approx(slope)
    assert yield_beta_tstat(closes, yields, beta) == pytest.approx(t)


def test_yield_beta_tstat_without_a_beta_is_none():
    from RevenueChaser.module_earnings_radar.pipeline import yield_beta_tstat
    assert yield_beta_tstat({"2026-01-05": 1.0}, {"2026-01-05": 4.0}, None) is None


def test_a_noisy_extreme_beta_with_a_rate_event_is_a_badge_not_a_veto():
    ohlc, yields = _noisy_ohlc_and_yields()
    beta, t = _ols_t(ohlc, yields)
    assert abs(beta) > 4 and abs(t) < 2                                 # the fixture is what its name says
    _seed_dgs10(yields)
    rawstore.write_rows("economic_calendar", _rate_event_in_span())
    p = _pipeline(ohlc)
    p._warm_dgs10()

    card = p._score("AAPL")

    assert card.macro_veto is False
    assert card.final_action != "NO_TRADE"
    audit = (card.rule_audit or {}).get("rule6_macro", {})
    assert any("extreme rate sensitivity" in x for x in audit.get("reasons") or [])
    assert not any("macro veto" in x for x in audit.get("reasons") or [])


def test_the_economic_calendar_is_read_once_per_run(monkeypatch):
    """Every card's overlap is a lookup in the run's one read, not a table read per card (review S6)."""
    rawstore.write_rows("economic_calendar", _rate_event_in_span())
    reads = []
    real = stored_inputs.rate_events
    monkeypatch.setattr(stored_inputs, "rate_events", lambda: reads.append(1) or real())
    ohlc, yields = _duration_ohlc_and_yields(duration=10.0)
    p = _pipeline(ohlc)
    p._dgs10_cache = yields

    first, second = p._score("AAPL"), p._score("AAPL")

    assert len(reads) == 1
    assert first.macro_veto is True and second.macro_veto is True
