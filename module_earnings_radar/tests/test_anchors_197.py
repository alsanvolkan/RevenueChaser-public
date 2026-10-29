"""#197 E-2: reaction anchors (E3), New York clock (E11/E22), iv_rv wiring (E17), EM units (E24).

Offline. The clock is the ``core.calendar._now`` seam.
"""
from datetime import datetime, timezone

import pytest

from RevenueChaser.module_earnings_radar.pipeline import (
    EarningsPipeline, _compute_earnings_reactions, _compute_return_since_er,
)


def _bars(*pairs):
    return [{"date": d, "close": c} for d, c in pairs]


_BARS = _bars(("2026-08-07", 100.0), ("2026-08-10", 101.0), ("2026-08-11", 102.0),
              ("2026-08-12", 103.0), ("2026-08-13", 104.0))


@pytest.fixture
def clock(monkeypatch):
    from RevenueChaser.core import calendar

    def set_now(iso_utc):
        now = datetime.fromisoformat(iso_utc).replace(tzinfo=timezone.utc)
        monkeypatch.setattr(calendar, "_now", lambda: now)
    return set_now


# ── E3: the reaction anchor ──────────────────────────────────────────────────

class TestReactionAnchor:
    def _anchor(self, report_date, timing, bars=_BARS):
        from RevenueChaser.core.reaction_anchor import reaction_anchor
        return reaction_anchor(bars, report_date, timing)

    def test_bmo_anchors_on_the_report_date_close(self):
        assert self._anchor("2026-08-11", "premarket") == ("2026-08-11", 102.0)
        assert self._anchor("2026-08-11", "BMO") == ("2026-08-11", 102.0)

    def test_amc_anchors_on_the_next_session_close(self):
        assert self._anchor("2026-08-11", "postmarket") == ("2026-08-12", 103.0)
        assert self._anchor("2026-08-11", "AMC") == ("2026-08-12", 103.0)

    def test_unknown_timing_keeps_the_report_date_close(self):
        assert self._anchor("2026-08-11", "") == ("2026-08-11", 102.0)
        assert self._anchor("2026-08-11", "unconfirmed") == ("2026-08-11", 102.0)

    def test_amc_on_the_last_bar_has_no_reaction_session_yet(self):
        assert self._anchor("2026-08-13", "AMC") is None

    def test_report_older_than_the_window_has_no_anchor(self):
        assert self._anchor("2026-08-05", "BMO") is None
        assert self._anchor("2026-08-05", "AMC") is None

    def test_weekend_report_date_goes_to_the_next_session(self):
        assert self._anchor("2026-08-08", "AMC") == ("2026-08-10", 101.0)
        assert self._anchor("2026-08-08", "BMO") == ("2026-08-10", 101.0)

    def test_bars_without_a_close_are_skipped_and_order_is_free(self):
        bars = list(reversed(_BARS)) + [{"date": "2026-08-12", "close": None}]
        assert self._anchor("2026-08-11", "AMC", bars) == ("2026-08-12", 103.0)

    def test_reaction_session_promoted_from_the_research_script(self):
        import numpy as np
        from RevenueChaser.core.reaction_anchor import reaction_session
        dates = np.array(["2026-08-10", "2026-08-11", "2026-08-12"], dtype="datetime64[D]")
        i, tag, _ = reaction_session("2026-08-11T21:30:00Z", dates)  # 17:30 ET: after close
        assert (i, tag) == (2, "afterclose")


class TestEarningsReactionsAnchor:
    def test_amc_print_measures_from_the_next_session_close(self):
        hist = [{"report_date": "2026-08-11", "actual_eps": 1.0, "report_time": "postmarket"}]
        out = _compute_earnings_reactions(hist, [104.0], _BARS)
        assert out["m1"] == f"{(104.0 / 103.0 - 1) * 100:+.1f}%"   # was 104/102

    def test_bmo_print_measures_from_the_report_date_close(self):
        hist = [{"report_date": "2026-08-11", "actual_eps": 1.0, "report_time": "premarket"}]
        out = _compute_earnings_reactions(hist, [104.0], _BARS)
        assert out["m1"] == f"{(104.0 / 102.0 - 1) * 100:+.1f}%"

    def test_report_older_than_the_window_is_blank_not_the_first_bar(self):
        hist = [{"report_date": "2026-08-11", "actual_eps": 1.0, "report_time": "premarket"},
                {"report_date": "2026-03-01", "actual_eps": 1.0, "report_time": "premarket"}]
        out = _compute_earnings_reactions(hist, [104.0], _BARS)
        assert out["m1"] != ""
        assert out["m2"] == ""      # was anchored on the window's first bar (100.0)
        assert out["m2_eps"] == ""  # no estimate in the row

    def test_future_event_shows_no_current_move(self, clock):
        clock("2026-08-12T15:00:00")
        hist = [{"report_date": "2030-01-01", "actual_eps": None}]
        out = _compute_earnings_reactions(hist, [104.0], _BARS)
        assert out["current"] == ""   # was "+0.0%"


    def test_event_reporting_today_still_shows_the_live_move(self, clock):
        clock("2026-08-12T15:00:00")
        hist = [{"report_date": "2026-08-12", "actual_eps": None}]
        out = _compute_earnings_reactions(hist, [104.0], _BARS)
        assert out["current"] == f"{(104.0 / 102.0 - 1) * 100:+.1f}%"  # vs the 08-11 close


# ── E11 / E22: Since ER on New York time ─────────────────────────────────────

class TestSinceErNewYorkClock:
    def test_future_event_is_blank(self, clock):
        clock("2026-08-12T15:00:00")
        assert _compute_return_since_er([], [104.0], _BARS, "2026-08-14", "AMC") == ("", "", "")

    def test_bmo_today_is_hidden_before_the_open_and_shown_after(self, clock):
        bars = _bars(("2026-08-10", 101.0), ("2026-08-11", 102.0), ("2026-08-12", 103.0))
        clock("2026-08-12T13:00:00")   # 09:00 ET (was gated on local clock hours)
        assert _compute_return_since_er([], [103.0], bars, "2026-08-12", "BMO") == ("", "", "")
        clock("2026-08-12T13:45:00")   # 09:45 ET
        label, _, _ = _compute_return_since_er([], [103.0], bars, "2026-08-12", "BMO")
        assert label != ""

    def test_amc_today_is_hidden_until_the_close(self, clock):
        bars = _bars(("2026-08-10", 101.0), ("2026-08-11", 102.0), ("2026-08-12", 103.0))
        clock("2026-08-12T19:59:00")   # 15:59 ET
        assert _compute_return_since_er([], [103.0], bars, "2026-08-12", "AMC") == ("", "", "")
        clock("2026-08-12T20:01:00")   # 16:01 ET
        label, _, _ = _compute_return_since_er([], [103.0], bars, "2026-08-12", "AMC")
        assert label != ""

    def test_now_label_follows_the_new_york_session(self, clock):
        import json
        bars = _bars(("2026-08-10", 101.0), ("2026-08-11", 102.0), ("2026-08-12", 103.0))
        clock("2026-08-12T14:00:00")   # 10:00 ET: market open
        chart = json.loads(_compute_return_since_er([], [103.0], bars, "2026-08-11", "AMC")[2])
        assert chart["now_label"] == "Market"
        clock("2026-08-12T21:00:00")   # 17:00 ET: post-market
        chart = json.loads(_compute_return_since_er([], [103.0], bars, "2026-08-11", "AMC")[2])
        assert chart["now_label"] == "Post-Mkt"

    def test_today_is_the_new_york_date_not_the_utc_date(self, clock):
        # 01:00 UTC on the 13th is still the evening of the 12th in New York, so an
        # event dated the 13th has not happened yet.
        bars = _bars(("2026-08-11", 102.0), ("2026-08-12", 103.0))
        clock("2026-08-13T01:00:00")
        assert _compute_return_since_er([], [103.0], bars, "2026-08-13", "AMC") == ("", "", "")


# ── E17 / E24 ────────────────────────────────────────────────────────────────

class TestExpectedMoveUnits:
    def test_expected_move_perc_is_always_a_ratio(self):
        from RevenueChaser.module_earnings_radar.pipeline import _expected_move_pct
        assert _expected_move_pct({"expected_move_perc": 0.05}) == pytest.approx(5.0)
        assert _expected_move_pct({"expected_move_perc": "0.08"}) == pytest.approx(8.0)
        assert _expected_move_pct({"expected_move_perc": 1.2}) == pytest.approx(120.0)  # was 1.2
        assert _expected_move_pct({"expected_move_perc": 1.0}) == pytest.approx(100.0)

    def test_missing_or_garbage_is_zero(self):
        from RevenueChaser.module_earnings_radar.pipeline import _expected_move_pct
        assert _expected_move_pct({}) == 0.0
        assert _expected_move_pct({"expected_move_perc": "n/a"}) == 0.0
        assert _expected_move_pct({"expected_move_perc": None}, {"expected_move_perc": 0.04}) == pytest.approx(4.0)

    def test_plain_expected_move_key_keeps_the_legacy_unit_read(self):
        from RevenueChaser.module_earnings_radar.pipeline import _expected_move_pct
        assert _expected_move_pct({}, {"expected_move": 0.06}) == pytest.approx(6.0)
        assert _expected_move_pct({}, {"expected_move": 7.5}) == pytest.approx(7.5)

    def test_binary_name_is_excluded_by_the_em_gate(self):
        p = EarningsPipeline(["BIO"], uw=None, cal_meta={"BIO": {"expected_move_perc": 1.2}})
        p._screener_cache = {"BIO": {"call_volume": "5000", "put_volume": "5000",
                                       "total_open_interest": "50000", "call_premium": "400000",
                                       "put_premium": "300000"}}
        p.preselect()
        assert p._filter_outcome["BIO"] == ("excluded", ["EM>25%"])  # was passed as a 1.2% move


class TestIvRvRatioWiring:
    def test_card_reads_the_ratio_r4_writes(self, rawstore_tmp_db):
        from RevenueChaser.module_earnings_radar.tests.test_pipeline_score import _FakeUW, _make_pipeline
        scr = {"AAPL": {"close": 100.0, "iv30d": 0.60, "realized_volatility": 0.40}}
        cal = {"AAPL": {"earnings_date": "2026-08-14", "full_name": "Aple",
                        "sector": "Technology", "expected_move_perc": 0.05}}
        card = _make_pipeline(_FakeUW(), scr, cal)._score("AAPL")
        assert card.iv_rv_ratio == pytest.approx(1.5)   # was always 0
