"""C26 (#383, LP-D): Earnings reads the LEAPS label read-only.

The lazy ``get_or_fetch_leaps`` path and ``LEAPS_LAZY_FETCH`` are gone, so
``leaps_cache`` has one writer, the LEAPS Fetch; the card shows the label's
``as_of`` and its age. Offline: fake UW, ``rawstore_tmp_db`` keeps LEAPS reads off
the real store.
"""
from datetime import date

import pytest

from RevenueChaser.module_earnings_radar.models import TradeCard
from RevenueChaser.module_earnings_radar import pipeline as pl
from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline
from RevenueChaser.module_earnings_radar.report import export_html
from RevenueChaser.module_earnings_radar.tests.test_pipeline_score import _FakeUW


@pytest.fixture(autouse=True)
def _isolate(rawstore_tmp_db):
    return rawstore_tmp_db


class _LeapsUW(_FakeUW):
    """Fake UW that records every LEAPS read the pipeline makes."""

    def __init__(self, **ov):
        super().__init__(**ov)
        self.leaps_calls = []

    def oi_per_expiry(self, ticker):
        self.leaps_calls.append(("oi_per_expiry", ticker))
        return []

    def flow_per_expiry(self, ticker):
        self.leaps_calls.append(("flow_per_expiry", ticker))
        return []

    def iv_rank(self, ticker):
        self.leaps_calls.append(("iv_rank", ticker))
        return []


def _pipeline(uw):
    p = EarningsPipeline(["AAPL"], uw=uw, cal_meta={"AAPL": {
        "earnings_date": "2026-08-14", "full_name": "Aple", "sector": "Technology",
        "expected_move_perc": 0.05}})
    p._screener_cache = {"AAPL": {"close": "100", "call_volume": "5000",
                                  "put_volume": "5000", "total_open_interest": "50000"}}
    return p


def test_the_latest_snapshot_is_read_without_a_fetch(monkeypatch):
    uw = _LeapsUW()
    monkeypatch.setattr(pl, "leaps_load_cache", lambda t: {
        "latest": {"label": "Accumulating", "as_of": "2026-10-10", "score": 70},
        "history": [{"as_of": "2026-07-10", "label": "Quiet"}]})
    card = _pipeline(uw)._score("AAPL")
    assert card.leaps_label == "Accumulating"
    assert card.leaps_as_of == "2026-10-10"
    assert card.leaps_score == 70
    assert uw.leaps_calls == []          # never a UW LEAPS call on the Earnings path


def test_a_real_cache_payload_scores_the_same_label():
    """The contract L3 keeps (#377 L3): ``save_cache``'s payload is what Earnings reads, unchanged."""
    from RevenueChaser.module_leaps import engine
    engine.save_cache("AAPL", {"ticker": "AAPL", "label": "Accumulating", "as_of": "2026-10-10", "score": 70,
                               "far_call_oi": 1000, "far_put_oi": 100, "far_call_share": 0.9091,
                               "far_oi_growth_1d_pct": 12.5, "far_oi_growth_q_pct": None, "iv_rank": 55.0})
    uw = _LeapsUW()
    card = _pipeline(uw)._score("AAPL")
    assert (card.leaps_label, card.leaps_as_of, card.leaps_score) == ("Accumulating", "2026-10-10", 70)
    assert uw.leaps_calls == []


def test_no_cache_reads_n_a_and_writes_nothing(monkeypatch):
    uw = _LeapsUW()
    monkeypatch.setattr(pl, "leaps_load_cache", lambda t: None)
    from RevenueChaser.module_leaps import engine as leaps_engine
    monkeypatch.setattr(leaps_engine, "save_cache",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("save_cache called")))
    card = _pipeline(uw)._score("AAPL")
    assert card.leaps_label == "N/A" and card.leaps_as_of == ""
    assert uw.leaps_calls == []


def test_the_drawer_carries_the_as_of_and_age():
    card = TradeCard(ticker="AAPL", name="Apple", cluster="Technology",
                     earnings_date=date(2026, 8, 11), leaps_label="Accumulating",
                     leaps_as_of="2026-10-10")
    content = export_html([card], [], {}).read_text(encoding="utf-8")
    assert '"leaps_as_of": "2026-10-10"' in content
    expected = (date.today() - date(2026, 10, 10)).days
    assert f'"leaps_age_days": {expected}' in content


def test_a_card_without_an_as_of_shows_the_label_alone():
    card = TradeCard(ticker="AAPL", name="Apple", cluster="Technology",
                     earnings_date=date(2026, 8, 11), leaps_label="Accumulating")
    content = export_html([card], [], {}).read_text(encoding="utf-8")
    assert '"leaps_as_of": ""' in content
    assert '"leaps_age_days": null' in content
