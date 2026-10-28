"""E2 (#372 ER-D / ER-1, DF10, DF12): a missing screener row is liquidity N/A, never AVOID.

A name the UW screener did not answer for is unknown liquidity, not thin liquidity, and the run is
partial. Offline: fake UW, ``rawstore_tmp_db`` keeps the history write off the real store.
"""
import json
from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline
from RevenueChaser.module_earnings_radar.tests.test_pipeline_score import _FakeUW

HEALTHY = {"calls": 1, "failed": 0, "failures": [], "auth_error": None, "breaker_open": False,
           "skipped_calls": 0}
COMPLETE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()
_STRONG = {"close": "50", "call_volume": "5000", "put_volume": "5000", "total_open_interest": "50000",
           "call_premium": "400000", "put_premium": "300000"}


class _ScreenerUW(_FakeUW):
    """Fake UW whose screener batch carries the request outcome (the real client's ``*_result``)."""

    def __init__(self, rows, status="ok", error=None, **ov):
        super().__init__(**ov)
        self._rows, self._status, self._error = rows, status, error
        self.result_calls = 0

    def screener_batch_result(self, tickers, date=None):
        self.result_calls += 1
        return ProviderResult(self._status, self._rows, self._error, "/screener/stocks")

    def screener_batch(self, tickers, date=None):
        raise AssertionError("the Earnings pipeline must read screener_batch_result when it exists")


def _pipeline(uw, tickers):
    cal = {t: {"earnings_date": "2026-08-14", "full_name": t, "sector": "Technology",
               "expected_move_perc": 0.05} for t in tickers}
    p = EarningsPipeline(tickers, uw=uw, cal_meta=cal)
    p._dgs10_cache = {}
    return p


def test_a_missing_screener_row_reads_n_a_never_thin():
    uw = _ScreenerUW({"AAA": dict(_STRONG)}, status="partial",
                     error={"failed_chunks": 1, "chunks": 2})
    p = _pipeline(uw, ["AAA", "BBB"])
    cards = {c.ticker: c for c in p.run(use_preselect=False)}
    assert p.screener_result == {"status": "partial", "error": {"failed_chunks": 1, "chunks": 2}}
    assert set(cards) == {"AAA", "BBB"}
    miss = cards["BBB"]
    assert miss.liquidity_label == "N/A"
    assert miss.trade_read != "AVOID"
    badges = json.loads(miss.badges_json)
    assert "NO SCREENER ROW" in badges and "LOW LIQUIDITY" not in badges
    have = cards["AAA"]
    assert have.liquidity_label in ("Excellent", "Tradeable")
    assert "NO SCREENER ROW" not in json.loads(have.badges_json)


def test_a_present_row_with_zero_oi_is_still_thin_and_avoid():
    uw = _ScreenerUW({"AAA": {"close": "50", "call_volume": "0", "put_volume": "0",
                              "total_open_interest": "0"}}, status="ok")
    p = _pipeline(uw, ["AAA"])
    (card,) = p.run(use_preselect=False)
    assert card.liquidity_label == "Thin" and card.trade_read == "AVOID"
    assert "NO SCREENER ROW" not in json.loads(card.badges_json)


def test_preselect_excludes_a_name_with_no_screener_row_before_the_gates():
    ts = [f"T{i:02d}" for i in range(25)]
    missing = {t for i, t in enumerate(ts) if i % 5 == 0}
    rows = {t: dict(_STRONG) for t in ts if t not in missing}
    uw = _ScreenerUW(rows, status="partial", error={"failed_chunks": 1, "chunks": 2})
    p = _pipeline(uw, ts)
    scoring = p.preselect(set())
    assert missing.isdisjoint(set(scoring))
    excluded = {e["ticker"]: e["reasons"] for e in p._filter_stats["excluded"]}
    assert all(excluded[t] == ["no screener row"] for t in missing)
    assert p._filter_stats["by_reason"]["no screener row"] == len(missing)


def test_a_watchlist_name_with_no_screener_row_is_still_bypassed():
    ts = [f"T{i:02d}" for i in range(25)]
    rows = {t: dict(_STRONG) for t in ts if t != "T00"}
    uw = _ScreenerUW(rows, status="partial", error={"failed_chunks": 1, "chunks": 2})
    p = _pipeline(uw, ts)
    assert "T00" in p.preselect({"T00"})
    assert p._filter_outcome["T00"][0] == "bypassed"


def test_a_partial_screener_answer_marks_the_recorded_run_partial(rawstore_tmp_db):
    from RevenueChaser.core import rawstore
    from RevenueChaser.module_earnings_radar import history as er_history
    from RevenueChaser.module_earnings_radar.pipeline import nrc_from_card
    from RevenueChaser.module_earnings_radar.models import TradeCard
    card = TradeCard(ticker="AAA", earnings_date=date(2026, 10, 8), earnings_time="AMC")
    nrc = {"AAA": nrc_from_card(card, set())}
    er_history.record([card], nrc, run_id="r1", run_at=COMPLETE, uw_health=HEALTHY,
                      screener={"status": "partial", "error": {"failed_chunks": 1, "chunks": 2}})
    (head,) = rawstore.query("SELECT * FROM result_runs WHERE board = ?", ["module_earnings_radar"],
                             db="history")
    assert head["quality"] == "partial" and "screener" in head["quality_note"]
    assert "1 of 2 chunks failed" in head["quality_note"]


def test_an_ok_screener_leaves_the_run_note_alone(rawstore_tmp_db):
    from RevenueChaser.core import rawstore
    from RevenueChaser.module_earnings_radar import history as er_history
    from RevenueChaser.module_earnings_radar.pipeline import nrc_from_card
    from RevenueChaser.module_earnings_radar.models import TradeCard
    card = TradeCard(ticker="AAA", earnings_date=date(2026, 10, 8), earnings_time="AMC")
    nrc = {"AAA": nrc_from_card(card, set())}
    er_history.record([card], nrc, run_id="r1", run_at=COMPLETE, uw_health=HEALTHY,
                      screener={"status": "ok", "error": None})
    (head,) = rawstore.query("SELECT * FROM result_runs WHERE board = ?", ["module_earnings_radar"],
                             db="history")
    assert head["quality_note"] is None


def test_the_board_marks_n_a_liquidity_zero_and_carries_the_new_drawer_fields():
    from RevenueChaser.module_earnings_radar.models import TradeCard
    from RevenueChaser.module_earnings_radar.report import export_html
    card = TradeCard(ticker="AAA", name="A Co", cluster="Technology", earnings_date=date(2026, 8, 11),
                     liquidity_label="N/A", trade_read="NEUTRAL", trade_read_reason="Mixed — no clear edge",
                     past_1w="+1.2%", past_1d="+0.3%")
    content = export_html(
        [card], [], {},
        notice="the UW screener answered partial (1 of 2 chunks failed): cards without a screener row "
               "read liquidity N/A").read_text(encoding="utf-8")
    assert 'data-liq="0"' in content
    assert "the UW screener answered partial" in content
    assert '"trade_read_reason": "Mixed — no clear edge"' in content
    assert '"past_1w": "+1.2%"' in content
