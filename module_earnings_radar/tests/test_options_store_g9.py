"""G9 (build plan PR 19): Earnings Radar's options inputs come from the options store, three ways per name.

* the gates and the pre-score read the stored screener set (``option_screener_daily``) in the UW screener's shape;
  an old or missing set is "no screener row" for every name, and the board says why;
* R4 (gamma flip, GEX profile, term-structure slope), the R5 lean and R8 (retail call share) read:
  1. a name on the option watch list: its list rows of the newest session before the report;
  2. a name off the list that passed the gates: the earnings set fetched the evening before *this* report;
  3. any other name: no options inputs, "not on the option watch list" on R4/R5/R8, no score on those rules;
* R6 reads the 10-year and the FOMC and CPI days from ``market_series``; no FRED call;
* the Fetch asks UW for none of these inputs.

Fixture serving files in the test's own ``MARKET_DIR``; offline.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.module_earnings_radar import history as er_history
from RevenueChaser.module_earnings_radar import store_reads
from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline
from RevenueChaser.module_earnings_radar.tests.options_store import (
    earnings_name, list_name, screener_row, weekdays, write_series)
from RevenueChaser.module_oipd.tests.options_store import write_options
from RevenueChaser.rcdata import ops

SESSION = calendar.latest_complete_session()
REPORT = calendar.next_trading_day(SESSION)
NOT_ON_LIST = "not on the option watch list"


class _UW:
    """The UW calls the Earnings Fetch still makes (calendar, earnings rows, financials, bars); every options call
    of the old Fetch raises."""
    auth_error = None

    def __getattr__(self, name):
        if name in ("screener_batch", "screener_batch_result", "gex_levels", "flow_recent", "flow_recent_result",
                    "term_structure", "spot_exposures_by_strike", "options_volume"):
            def _refused(*a, **k):
                raise AssertionError(f"the Earnings Fetch must not ask UW for {name}")
            return _refused
        raise AttributeError(name)

    def earnings_rows(self, ticker):
        return []

    def ticker_info(self, ticker):
        return {}

    def etf_ohlc(self, ticker, days=60, candle_size="1d"):
        return []

    def ohlc_latest(self, ticker):
        return {}

    @staticmethod
    def _map_financials(ticker, fin=None):
        return []


def _cal(*tickers, report=REPORT):
    return {t: {"earnings_date": report.isoformat(), "full_name": f"{t} Inc", "expected_move_perc": 0.06}
            for t in tickers}


def _pipeline(*tickers, report=REPORT):
    return EarningsPipeline(list(tickers), uw=_UW(), cal_meta=_cal(*tickers, report=report))


@pytest.fixture
def week(rawstore_tmp_db):
    """LST on the option list, ERN off it with its earnings set for this report, NON off it with none, LOW off it
    and failing the gates; all four in the screener set, reporting on the next session."""
    ops.watch_add("option", "LST", by="test")
    write_options(option_screener_daily=[screener_row("LST", SESSION, REPORT), screener_row("ERN", SESSION, REPORT),
                                         screener_row("NON", SESSION, REPORT),
                                         screener_row("LOW", SESSION, REPORT, passes=False)])
    list_name("LST", SESSION, retail_calls=6)
    earnings_name("ERN", SESSION, REPORT, retail_calls=6)
    return rawstore_tmp_db


def _available(card, rule):
    return (card.rule_audit.get(rule) or {}).get("available") is True


def _not_on_list(card, rule):
    audit = card.rule_audit.get(rule) or {}
    return audit.get("available") is False and NOT_ON_LIST in (audit.get("reasons") or [])


# ── the three-way rule per name ─────────────────────────────────────────────────────────────────────────────────

def test_card_inputs_read_the_list_the_earnings_set_or_nothing(week):
    listed = {"SPX", "LST"}
    lst = store_reads.card_inputs("LST", REPORT, listed)
    ern = store_reads.card_inputs("ERN", REPORT, listed)
    assert (lst["set"], lst["session"]) == ("list", SESSION) and (ern["set"], ern["session"]) == ("earnings", SESSION)
    for got in (lst, ern):
        assert got["gex"]["gamma_flip"] == 95.0 and len(got["strikes"]) == 9 and len(got["term"]) == 4
        assert {p["size"] for p in got["prints"]} == {20, 2}
    assert store_reads.card_inputs("NON", REPORT, listed)["set"] is None
    # The earnings set of ERN was fetched for REPORT: a later report of the same name has none yet.
    assert store_reads.card_inputs("ERN", REPORT + timedelta(days=7), listed)["set"] is None


def test_a_list_name_reads_its_list_rows_even_with_an_earnings_set(week):
    earnings_name("LST", SESSION, REPORT, retail_calls=0)
    assert store_reads.card_inputs("LST", REPORT, {"LST"})["set"] == "list"


def test_r4_r5_and_r8_score_the_list_and_earnings_set_names_only(week):
    """The scored names: LST and ERN get R4, R5 and R8; NON shows the label on all three and gets no score there."""
    p = _pipeline("LST", "ERN", "NON")
    cards = {t: p._score(t) for t in ("LST", "ERN", "NON")}
    scored = sorted(t for t, c in cards.items()
                    if all(_available(c, r) for r in ("rule4_volatility", "rule5_positioning", "rule8_sentiment")))
    assert scored == ["ERN", "LST"]
    non = cards["NON"]
    assert all(_not_on_list(non, r) for r in ("rule4_volatility", "rule5_positioning", "rule8_sentiment"))
    assert non.r4_score == non.r5_score == non.r8_score == 0.0
    assert NOT_ON_LIST in non.inst_flow_label and "NOT ON OPTION LIST" in non.badges_json
    for t in ("LST", "ERN"):
        assert cards[t].gex_gamma_flip == "$95.00" and cards[t].inst_flow_tone == "bull"
        # 6 two-lot call prints of $600 against 2 twenty-lot ones of $150k: a retail share near 1%.
        assert cards[t].rule_audit["rule8_sentiment"]["reasons"] == ["retail share not extreme"]


def test_the_store_inputs_reach_the_rules(week):
    card = _pipeline("LST")._score("LST")
    raw = card.rule_audit["rule4_volatility"]
    assert raw["available"] and card.iv_rv_ratio is not None
    packet = __import__("json").loads(card.raw_packet_json)
    assert packet["options_set"] == "list" and packet["options_session"] == SESSION.isoformat()
    assert packet["gex_levels"]["gamma_flip"] == 95.0 and len(packet["flow_recent"]) == 8


# ── the gates and the pre-score read the stored screener set ────────────────────────────────────────────────────

def test_the_gates_read_the_stored_screener_set(week):
    p = _pipeline("LST", "ERN", "NON", "LOW", "OFF")
    kept = p.preselect()
    assert sorted(kept) == ["ERN", "LST", "NON"]
    assert p._filter_outcome["LOW"][0] == "excluded" and "OptVol" in p._filter_outcome["LOW"][1]
    assert p._filter_outcome["OFF"] == ("excluded", ["no screener row"])
    assert p.screener_result == {"status": "ok", "error": None, "session": SESSION.isoformat()}


@pytest.mark.parametrize("age", [None, 5])
def test_an_old_or_missing_screener_set_is_no_row_for_every_name_and_says_so(rawstore_tmp_db, age):
    if age is not None:
        old = SESSION - timedelta(days=age + 2)
        write_options(option_screener_daily=[screener_row("AAA", old, REPORT)])
    p = _pipeline("AAA")
    assert p.screener_cache == {}
    assert p.screener_result["status"] == ("missing" if age is None else "stale")
    note = er_history.screener_problem(p.screener_result)
    assert note and "uw/option_screener" in note and "liquidity N/A" in note


# ── R6 on market_series ─────────────────────────────────────────────────────────────────────────────────────────

def test_r6_reads_the_ten_year_and_the_rate_days_from_market_series(week, monkeypatch):
    from RevenueChaser.module_earnings_radar import macro_watch
    assert not hasattr(macro_watch, "fred_daily_map")      # the board has no FRED reader to call any more
    days = weekdays(SESSION - timedelta(days=120), SESSION)
    write_series([("DGS10", d, 4.0 + i / 100) for i, d in enumerate(days)]
                 + [("FOMC_DECISION", SESSION - timedelta(days=200), 1.0), ("FOMC_DECISION", REPORT, 1.0),
                    ("FOMC_DECISION", REPORT + timedelta(days=200), 1.0),
                    ("CPI_RELEASE", SESSION - timedelta(days=200), 1.0),
                    ("CPI_RELEASE", REPORT + timedelta(days=200), 1.0)])
    p = _pipeline("LST")
    assert len(p._warm_dgs10()) == min(len(days), store_reads.DGS10_LIMIT) and p.input_sources["dgs10"] == "stored"
    events = p._warm_rate_events()
    assert store_reads.rate_event_overlap(REPORT, events) is True
    assert store_reads.rate_event_overlap(REPORT + timedelta(days=60), events) is False
    assert store_reads.rate_event_overlap(REPORT + timedelta(days=400), events) is None   # past both calendars


def test_a_stale_ten_year_gives_r6_no_beta_and_no_fred_call(rawstore_tmp_db):
    write_series([("DGS10", SESSION - timedelta(days=30), 4.1)])
    p = _pipeline("AAA")
    assert p._warm_dgs10() == {} and p.input_sources["dgs10"] == "none"
