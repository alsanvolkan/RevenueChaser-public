"""Tests for the LEAPS accumulation engine (far-dated OI, growth, classifier)."""
import json
import re
from datetime import date

from RevenueChaser.module_leaps.engine import (
    far_oi_totals, _call_share, _growth_pct, build_snapshot, classify,
    _score, load_cache, save_cache, baseline_prior_day, baseline_prior_quarter,
    aggregate_flow_prints, concentration_multiplier, score_flow_candidates,
    radar_snapshot, bucket_score, store_prints, load_recent_prints,
)


def _oi_row(expiry, call_oi=0, put_oi=0):
    return {"expiry": expiry, "call_oi": call_oi, "put_oi": put_oi}


class TestFarOiTotals:
    def test_filters_by_dte(self):
        rows = [
            _oi_row("2026-08-21", call_oi=100, put_oi=50),    # near (< 270d)
            _oi_row("2027-06-17", call_oi=1000, put_oi=400),   # far (~307d)
            _oi_row("2028-01-21", call_oi=500, put_oi=100),    # far
        ]
        t = far_oi_totals(rows, date(2026, 8, 14))
        assert t["far_call_oi"] == 1500
        assert t["far_put_oi"] == 500

    def test_empty(self):
        assert far_oi_totals([], date(2026, 8, 14)) == {"far_call_oi": 0, "far_put_oi": 0}


class TestCallShare:
    def test_share(self):
        assert _call_share(700, 300) == 0.7

    def test_none_when_zero(self):
        assert _call_share(0, 0) is None


class TestGrowthPct:
    def test_positive(self):
        assert _growth_pct(120, 100) == 20.0

    def test_none_baseline(self):
        assert _growth_pct(100, None) is None
        assert _growth_pct(100, 0) is None


class TestBuildSnapshot:
    def test_na_when_no_far_oi(self):
        s = build_snapshot("X", [_oi_row("2026-08-21", call_oi=10, put_oi=5)], [], [],
                           today=date(2026, 8, 14))
        assert s["label"] == "N/A"

    def test_fields_populated(self):
        s = build_snapshot(
            "X", [_oi_row("2027-06-17", call_oi=1000, put_oi=400)],
            [], [{"iv_rank_1y": "60.0"}], today=date(2026, 8, 14))
        assert s["far_call_oi"] == 1000
        assert s["far_call_share"] == 0.7143
        assert s["iv_rank"] == 60.0


class TestClassify:
    def _base(self):
        return {"far_call_oi": 1000, "far_put_oi": 400, "far_call_share": 0.7143,
                "far_call_sweep_pct": None, "iv_rank": None, "iv_rank_slope": None,
                "near_term_event": False}

    def test_accumulating_on_call_growth(self):
        s = self._base()
        prev_1d = {"far_call_oi": 900}
        s = classify(s, prev_1d, None)
        assert s["label"] == "Accumulating"
        assert s["far_oi_growth_1d_pct"] == 11.11

    def test_accumulating_on_quarter_growth(self):
        s = self._base()
        s = classify(s, None, {"far_call_oi": 800})
        assert s["label"] == "Accumulating"

    def test_not_accumulating_when_call_share_low(self):
        s = self._base()
        s["far_call_share"] = 0.4
        s = classify(s, {"far_call_oi": 800}, None)
        assert s["label"] != "Accumulating"

    def test_hedge_case_not_accumulating(self):
        # far put OI growing, call flat -> put dominance, not accumulation
        s = {"far_call_oi": 100, "far_put_oi": 900, "far_call_share": 0.1,
             "far_call_sweep_pct": None, "iv_rank": None, "iv_rank_slope": None,
             "near_term_event": False}
        s = classify(s, {"far_call_oi": 100}, None)
        assert s["label"] != "Accumulating"

    def test_near_term_event_excluded(self):
        s = self._base()
        s["near_term_event"] = True
        s = classify(s, {"far_call_oi": 800}, None)
        assert s["label"] == "Quiet"

    def test_quiet_when_no_growth(self):
        s = self._base()
        s = classify(s, {"far_call_oi": 1000}, None)
        assert s["label"] == "Quiet"


class TestScore:
    def test_bounded_0_100(self):
        for label, share in [("Accumulating", 0.9), ("Distributing", 0.1)]:
            s = {"label": label, "far_call_share": share, "far_oi_growth_1d_pct": 30.0,
                 "far_oi_growth_q_pct": None, "far_call_sweep_pct": 0.8,
                 "iv_rank_slope": 2.0}
            sc = _score(s)
            assert 0 <= sc <= 100


class TestCache:
    def test_roundtrip(self, rawstore_tmp_db):
        save_cache("AAPL", {"ticker": "AAPL", "label": "Accumulating", "as_of": "2026-08-14"})
        save_cache("AAPL", {"ticker": "AAPL", "label": "Quiet", "as_of": "2026-08-15"})
        data = load_cache("AAPL")
        assert data["latest"]["label"] == "Quiet"
        # The list is frozen (#377 L3, D1): carried unchanged, never appended, so no data is deleted.
        assert data["history"] == []

    def test_baseline_prior_day(self):
        h = [{"as_of": "2026-08-13"}, {"as_of": "2026-08-14"}]
        assert baseline_prior_day(h)["as_of"] == "2026-08-14"

    def test_baseline_prior_quarter(self):
        h = [{"as_of": "2026-06-30", "far_call_oi": 800},
             {"as_of": "2026-08-13", "far_call_oi": 1000}]
        assert baseline_prior_quarter(h, "2026-06-30")["far_call_oi"] == 800

    def test_baseline_prior_quarter_none_when_missing(self):
        h = [{"as_of": "2026-08-13", "far_call_oi": 1000}]
        assert baseline_prior_quarter(h, "2026-06-30") is None


class TestDashboard:
    def test_render(self, tmp_path, monkeypatch):
        import RevenueChaser.module_leaps.report as ld
        monkeypatch.setattr(ld, "OUTPUT_DIR", tmp_path)
        watched = [{"ticker": "NOK", "name": "Nokia Corp", "label": "Accumulating", "score": 70,
                    "far_call_oi": 1000, "far_put_oi": 400, "far_call_share": 0.71,
                    "far_oi_growth_1d_pct": 18.0, "far_oi_growth_q_pct": 25.0,
                    "iv_rank": 55.0}]
        unwatched = [{"ticker": "XYZ", "name": "Xyz Inc", "label": "Accumulating", "score": 55,
                      "frequency": 4, "notional": 400000.0, "net_notional": 100000.0,
                      "ask_share": 0.7, "near_term_event": False}]
        path = ld.render_leaps_dashboard(watched, unwatched)
        content = path.read_text(encoding="utf-8")
        assert "NOK" in content
        assert "Nokia Corp" in content
        assert "Accumulating" in content
        assert "Watchlist" in content
        assert "High Conviction" in content
        assert "How to read" in content
        assert "XYZ" in content

    def test_cell_html_well_formed(self, tmp_path, monkeypatch):
        # Regression: table layout must have balanced divs and the grid columns.
        import RevenueChaser.module_leaps.report as ld
        monkeypatch.setattr(ld, "OUTPUT_DIR", tmp_path)
        snap = {"ticker": "ABC", "name": "Abc Corp", "label": "Accumulating", "score": 70,
                "frequency": 4, "notional": 400000.0, "net_notional": 100000.0,
                "ask_share": 0.7, "near_term_event": False}
        path = ld.render_leaps_dashboard([snap], [snap])
        html = path.read_text(encoding="utf-8")
        assert html.count("<div") == html.count("</div>"), "divs must be balanced"
        for token in ("Net Notional", "Prints", "Ask %", "Footprint", "Flow", "Abc Corp"):
            assert token in html, f"missing column: {token}"


class TestFlowPrints:
    def test_aggregate(self):
        rows = [
            ("AAPL", 100000.0, "call", 0.80, None, 5, 3),   # ITM call
            ("AAPL", 50000.0, "call", 0.45, None, 1, 1),    # between bands (0.40<δ<0.55)
            ("AAPL", 20000.0, "put", -0.90, None, 2, 6),    # ITM put (not counted as call)
            ("AAPL", 30000.0, "call", 0.20, None, 4, 1),    # OTM call
        ]
        agg = aggregate_flow_prints(rows)
        a = agg["AAPL"]
        assert a["frequency"] == 4
        assert a["notional"] == 200000.0
        assert a["call_prem"] == 180000.0
        assert a["itm_call_prem"] == 100000.0   # only the 0.80 call
        assert a["otm_call_prem"] == 30000.0    # only the 0.20 call
        assert a["ask_share"] is not None
        assert 0.0 <= a["ask_share"] <= 1.0

    def test_ask_share_premium_weighted(self):
        rows = [
            ("AAPL", 100000.0, "call", 0.80, None, 9, 1),   # ask 90%
            ("AAPL", 100000.0, "call", 0.60, None, 1, 9),   # ask 10%
        ]
        a = aggregate_flow_prints(rows)["AAPL"]
        assert a["ask_share"] == 0.5  # premium-weighted average of 0.9 and 0.1

    def test_concentration_multiplier_tiered(self):
        # ITM call share 60% -> +0.2; OTM call share 10% -> no boost
        mult, itm, otm = concentration_multiplier(
            {"notional": 100000.0, "itm_call_prem": 60000.0, "otm_call_prem": 10000.0})
        assert mult == 1.2
        assert itm == 0.6
        # ITM 60% + OTM 40% -> +0.2 + +0.1
        mult, _, _ = concentration_multiplier(
            {"notional": 100000.0, "itm_call_prem": 60000.0, "otm_call_prem": 40000.0})
        assert mult == 1.3
        # neither -> base
        mult, _, _ = concentration_multiplier(
            {"notional": 100000.0, "itm_call_prem": 0.0, "otm_call_prem": 0.0})
        assert mult == 1.0

    def test_score_ranking(self):
        agg = {
            "AAPL": {"frequency": 10, "notional": 1000000.0, "call_prem": 900000.0,
                     "itm_call_prem": 600000.0, "otm_call_prem": 100000.0,
                     "ask_share": 0.7, "near_event": False},
            "MSFT": {"frequency": 5, "notional": 500000.0, "call_prem": 100000.0,
                     "itm_call_prem": 0.0, "otm_call_prem": 0.0,
                     "ask_share": 0.3, "near_event": False},
        }
        rows = score_flow_candidates(agg)
        assert rows[0]["ticker"] == "AAPL"
        assert 0 <= rows[0]["final_score"] <= 100
        assert rows[0]["net_notional"] == 800000.0  # 2*900000 - 1000000

    def test_score_single_candidate(self):
        agg = {"XYZ": {"frequency": 3, "notional": 200000.0, "call_prem": 180000.0,
                       "itm_call_prem": 150000.0, "otm_call_prem": 20000.0,
                       "ask_share": 0.6, "near_event": False}}
        rows = score_flow_candidates(agg)
        assert len(rows) == 1
        assert rows[0]["final_score"] > 0

    def test_radar_snapshot_label(self):
        snap = radar_snapshot({"ticker": "ABC", "final_score": 80.0, "frequency": 4,
                               "notional": 400000.0, "net_notional": 100000.0,
                               "concentration": 0.6, "itm_conc": 0.6, "otm_conc": 0.1,
                               "concentration_mult": 1.2, "call_share": 0.7,
                               "ask_share": 0.7, "near_event": False})
        assert snap["label"] == "Accumulating"
        assert snap["score"] == 80
        assert snap["frequency"] == 4

    def test_radar_snapshot_near_event_is_tag_not_drop(self):
        # earnings within 7 days must NOT force a drop — label still Accumulating
        snap = radar_snapshot({"ticker": "ABC", "final_score": 60.0, "frequency": 2,
                               "notional": 100000.0, "net_notional": 50000.0,
                               "concentration": 0.6, "itm_conc": 0.6, "otm_conc": 0.0,
                               "concentration_mult": 1.2, "call_share": 0.8,
                               "ask_share": 0.6, "near_event": True})
        assert snap["label"] == "Accumulating"
        assert snap["near_term_event"] is True

    def test_bucket_score(self):
        assert bucket_score(70) == "high"
        assert bucket_score(50) == "high"
        assert bucket_score(35) == "emerging"
        assert bucket_score(20) == "emerging"
        assert bucket_score(10) == "quiet"
        assert bucket_score(None) == "quiet"

    def test_store_and_load_prints(self, rawstore_tmp_db):
        import RevenueChaser.module_leaps.engine as la
        new = store_prints([
            {"id": "p1", "ticker": "AAPL", "strike": 200.0, "expiry": "2027-01-15",
             "premium": 100000.0, "size": 10, "option_type": "call", "delta": 0.8,
             "next_earnings_date": None, "executed_at": "2026-08-17T00:00:00Z",
             "ask_vol": 5, "bid_vol": 3},
            {"id": "p1", "ticker": "AAPL", "strike": 200.0, "expiry": "2027-01-15",
             "premium": 100000.0, "size": 10, "option_type": "call", "delta": 0.8,
             "next_earnings_date": None, "executed_at": "2026-08-17T00:00:00Z",
             "ask_vol": 5, "bid_vol": 3},
        ], today=date(2026, 8, 18))
        assert new == 1  # deduped
        rows = load_recent_prints(today=date(2026, 8, 18))
        assert len(rows) == 1
        assert rows[0][0] == "AAPL"


class TestEscaping:
    """L-1: ticker, name and label come from UW / meta strings — never raw markup."""

    _EVIL = '"><img src=x onerror=alert(1)></script><script>alert(1)</script>'

    def test_row_strings_are_escaped(self, tmp_path, monkeypatch):
        import RevenueChaser.module_leaps.report as ld
        monkeypatch.setattr(ld, "OUTPUT_DIR", tmp_path)
        snap = {"ticker": self._EVIL, "name": self._EVIL, "label": self._EVIL, "score": 60,
                "frequency": 4, "net_notional": 1.0, "ask_share": 0.5, "near_term_event": False}
        html = ld.render_leaps_dashboard([snap], [dict(snap)]).read_text(encoding="utf-8")
        assert "<img" not in html
        # Only the page's own three script blocks survive (board script, filter-kit script (KL #377), the
        # watch-list pair (PR 4)); the hostile ticker gets no button pair.
        assert html.count("<script") == 3 and html.count("</script>") == 3
        assert not re.search(r"data-rc-watch=\"(option|stock)\"", html)
        assert 'data-ticker="&quot;&gt;&lt;img' in html


def _print(pid, ticker="ZZZZ", executed="2026-09-30T14:00:00Z", otype="call", delta=0.8,
           ned=None, premium=200000.0):
    return {"id": pid, "ticker": ticker, "strike": 200.0, "expiry": "2027-06-18",
            "premium": premium, "size": 20, "option_type": otype, "delta": delta,
            "next_earnings_date": ned, "executed_at": executed, "ask_vol": 10, "bid_vol": 2}


class TestRecentWindow:
    """The "recent prints" window keys on when the trade executed, not when we found it."""

    def test_an_old_print_discovered_today_is_outside_the_window(self, rawstore_tmp_db):
        today = date(2026, 10, 2)
        store_prints([_print("old", executed="2026-08-01T14:00:00Z"),
                      _print("new", ticker="NEWW", executed="2026-09-30T14:00:00Z")], today=today)
        assert [r[0] for r in load_recent_prints(today=today)] == ["NEWW"]

    def test_a_late_discovered_recent_print_is_inside_it(self, rawstore_tmp_db):
        # discovered a month ago (first seen then), executed yesterday: backfilled/updated rows
        store_prints([_print("late", executed="2026-10-01T14:00:00Z")], today=date(2026, 9, 1))
        assert [r[0] for r in load_recent_prints(today=date(2026, 10, 2))] == ["ZZZZ"]

    def test_the_window_edge(self, rawstore_tmp_db):
        today = date(2026, 10, 2)
        store_prints([_print("edge", executed="2026-09-27T00:00:00Z"),
                      _print("out", ticker="OUTT", executed="2026-09-26T23:59:00Z")], today=today)
        assert [r[0] for r in load_recent_prints(days=5, today=today)] == ["ZZZZ"]


class TestPrintCap:
    class _UW:
        def __init__(self, pages, per_page):
            self.pages, self.per_page = pages, per_page

        def option_trades(self, min_dte, min_premium, limit, page):
            if page > self.pages:
                return []
            return [{"id": f"p{page}-{i}", "underlying_symbol": "ABC", "strike": "10",
                     "expiry": "2027-06-18", "premium": "60000", "size": 1, "option_type": "call",
                     "delta": "0.6", "next_earnings_date": None,
                     "executed_at": "2026-10-01T14:00:00Z", "ask_vol": 1, "bid_vol": 0, "tags": []}
                    for i in range(self.per_page)]

    def test_hitting_the_cap_warns(self, rawstore_tmp_db, caplog):
        from RevenueChaser.module_leaps.engine import discover_flow_prints
        notes = []
        with caplog.at_level("WARNING"):
            discover_flow_prints(self._UW(5, 3), limit=3, max_pages=5, today=date(2026, 10, 2),
                                 warnings=notes)
        assert len(notes) == 1 and "15" in notes[0] and "cap" in notes[0].lower()
        assert any("cap" in r.message.lower() for r in caplog.records)

    def test_a_short_last_page_does_not_warn(self, rawstore_tmp_db):
        from RevenueChaser.module_leaps.engine import discover_flow_prints
        notes = []
        discover_flow_prints(self._UW(2, 3), limit=3, max_pages=5, today=date(2026, 10, 2),
                             warnings=notes)
        assert notes == []


class TestInjectedClock:
    def test_scan_leaps_cached_honours_its_argument(self, rawstore_tmp_db):
        """A print of the 2026-08-17 session is inside the window on 08-18 and outside it on 10-02 (G9: the store)."""
        from RevenueChaser.module_leaps.scan import scan_leaps_cached
        from RevenueChaser.module_leaps.tests.options_store import pin, print_row, store_footprint
        session = date(2026, 8, 17)
        pin(option=["ZZZZ"])
        store_footprint("ZZZZ", session, {date(2027, 6, 18): (10, 0)},
                        prints=[print_row("ZZZZ", "z1", session, date(2027, 6, 18))])
        _w, unwatched = scan_leaps_cached(today=date(2026, 8, 18))
        assert [u["ticker"] for u in unwatched] == ["ZZZZ"]
        assert unwatched[0]["as_of"] == "2026-08-18"
        assert scan_leaps_cached(today=date(2026, 10, 2))[1] == []

    def test_near_event_flag_follows_the_injected_date(self):
        from RevenueChaser.module_leaps.engine import aggregate_flow_prints
        rows = [("ZZZZ", 1e5, "call", 0.8, "2026-08-20", 5, 1)]
        assert aggregate_flow_prints(rows, today=date(2026, 8, 18))["ZZZZ"]["near_event"] is True
        assert aggregate_flow_prints(rows, today=date(2026, 9, 30))["ZZZZ"]["near_event"] is False

    def test_the_sweep_share_uses_the_snapshot_date(self):
        from RevenueChaser.module_leaps.engine import build_snapshot
        oi = [{"expiry": "2026-12-01", "call_oi": 10, "put_oi": 5}]
        flow = [{"expiry": "2026-12-01", "call_premium_ask_side": 80.0, "put_premium_ask_side": 20.0}]
        snap = build_snapshot("ZZZZ", oi, flow, [], today=date(2026, 1, 1))  # 334 days out
        assert snap["far_call_sweep_pct"] == 0.8


class TestTwoScoreColumns:
    """O14 (render only): the footprint score (OI + IV, watchlist) and the flow score (prints)
    are two numbers with two meanings and get two columns."""

    def test_radar_snapshot_carries_only_a_flow_score(self):
        cand = {"ticker": "XYZ", "final_score": 61.4, "frequency": 4, "notional": 4e5,
                "concentration_mult": 1.2, "call_share": 0.8, "near_event": False}
        snap = radar_snapshot(cand)
        assert snap["flow_score"] == 61 and snap["footprint_score"] is None
        assert snap["score"] == 61  # tiering and sorting are unchanged

    def test_merge_radar_adds_the_flow_score_to_a_footprint_snapshot(self):
        from RevenueChaser.module_leaps.scan import _merge_radar
        snap = _merge_radar({"ticker": "NOK", "label": "Accumulating", "score": 70},
                            {"frequency": 3, "notional": 1e5, "net_notional": 2e4, "concentration": 0.5,
                             "concentration_mult": 1.2, "ask_share": 0.6, "itm_conc": 0.5,
                             "otm_conc": 0.1, "final_score": 33.2})
        assert snap["footprint_score"] == 70 and snap["flow_score"] == 33 and snap["score"] == 70

    def test_a_watchlist_name_without_prints_has_no_flow_score(self):
        from RevenueChaser.module_leaps.scan import _merge_radar
        snap = _merge_radar({"ticker": "NOK", "label": "Accumulating", "score": 70}, None)
        assert snap["footprint_score"] == 70 and snap["flow_score"] is None

    def test_the_board_has_a_footprint_and_a_flow_column(self, tmp_path, monkeypatch):
        import re
        import RevenueChaser.module_leaps.report as ld
        monkeypatch.setattr(ld, "OUTPUT_DIR", tmp_path)
        watched = [{"ticker": "NOK", "label": "Accumulating", "score": 70, "footprint_score": 70,
                    "flow_score": 33, "frequency": 3, "net_notional": 2e4, "ask_share": 0.6}]
        unwatched = [{"ticker": "XYZ", "label": "Accumulating", "score": 55, "footprint_score": None,
                      "flow_score": 55, "frequency": 4, "net_notional": 1e5, "ask_share": 0.7}]
        html = ld.render_leaps_dashboard(watched, unwatched).read_text(encoding="utf-8")
        assert "Footprint</th>" in html and "Flow</th>" in html
        assert ">Score</th>" not in html
        nok = re.search(r"<tr[^>]*><td><div class=\"tkr\">NOK</div>.*?</tr>", html).group(0)
        assert '<td class="num"><span class="score">70</span></td>' in nok
        assert '<td class="num"><span class="score">33</span></td>' in nok
        xyz = re.search(r"<tr[^>]*><td><div class=\"tkr\">XYZ</div>.*?</tr>", html).group(0)
        assert '<td class="num"><span class="score">—</span></td>' in xyz  # no footprint for it
        assert '<td class="num"><span class="score">55</span></td>' in xyz


class TestEmptyBoardAndNotice:
    def test_an_empty_run_renders_an_empty_state(self, tmp_path, monkeypatch):
        import RevenueChaser.module_leaps.report as ld
        monkeypatch.setattr(ld, "OUTPUT_DIR", tmp_path)
        html = ld.render_leaps_dashboard([], []).read_text(encoding="utf-8")
        assert "No LEAPS data yet" in html
        assert html.count("<div") == html.count("</div>")

    def test_a_populated_board_has_no_empty_state(self, tmp_path, monkeypatch):
        import RevenueChaser.module_leaps.report as ld
        monkeypatch.setattr(ld, "OUTPUT_DIR", tmp_path)
        snap = {"ticker": "ABC", "label": "Accumulating", "score": 70, "frequency": 4,
                "net_notional": 1.0, "ask_share": 0.5}
        assert "No LEAPS data yet" not in ld.render_leaps_dashboard([snap], []).read_text(encoding="utf-8")

    def test_a_notice_is_shown_escaped(self, tmp_path, monkeypatch):
        import RevenueChaser.module_leaps.report as ld
        monkeypatch.setattr(ld, "OUTPUT_DIR", tmp_path)
        html = ld.render_leaps_dashboard([], [], notice="cap <b>hit</b>").read_text(encoding="utf-8")
        assert "cap &lt;b&gt;hit&lt;/b&gt;" in html and "cap <b>hit</b>" not in html


class TestLeapsRunners:
    def test_an_empty_fetch_writes_the_board_and_says_why(self, rawstore_tmp_db):
        import pytest
        from RevenueChaser.app import runners
        from RevenueChaser.app.jobs import JobMessage
        runners.LEAPS_LATEST.unlink(missing_ok=True)
        with pytest.raises(JobMessage, match="No pinned name and no far-dated large print"):
            runners.run_leaps_latest(None)
        assert "No LEAPS data yet" in runners.LEAPS_LATEST.read_text(encoding="utf-8")

    def test_the_cached_refresh_renders_the_empty_state_without_raising(self, rawstore_tmp_db):
        from RevenueChaser.app import runners
        runners.LEAPS_LATEST.unlink(missing_ok=True)
        assert runners.run_leaps_cached() == runners.LEAPS_LATEST
        assert "No LEAPS data yet" in runners.LEAPS_LATEST.read_text(encoding="utf-8")

    def test_the_cached_board_shows_names_from_the_spine(self, rawstore_tmp_db):
        """The names come from the spine (``store_reads.meta``); a missed repoint of the loader hides behind
        a bare ``except`` and silently blanks them."""
        from RevenueChaser.core import config
        from RevenueChaser.module_leaps.scan import scan_leaps_cached
        from RevenueChaser.module_leaps.tests.options_store import pin, print_row, store_footprint
        from RevenueChaser.module_sec_13f.tests.ownership_store import _write_spine
        config.MARKET_DIR.mkdir(parents=True, exist_ok=True)
        _write_spine(config.MARKET_DIR, {"ZZZZ": ("Zed Corp", "Energy")})
        session = date(2026, 10, 1)
        pin(option=["ZZZZ"])
        store_footprint("ZZZZ", session, {date(2027, 6, 18): (10, 0)},
                        prints=[print_row("ZZZZ", "z1", session, date(2027, 6, 18))])
        _w, unwatched = scan_leaps_cached(today=date(2026, 10, 2))
        assert unwatched[0]["name"] == "Zed Corp"


def test_a_failed_options_store_read_is_not_swallowed_by_the_cached_scan(rawstore_tmp_db, monkeypatch):
    """G9: the stored prints and chains are the scan's store reads. A failure there must reach the dashboard as a
    failed Refresh, not become an empty board (the baselines, the calendar and the names are the reads that warn)."""
    import pytest

    from RevenueChaser.module_leaps import scan
    from RevenueChaser.rcdata import ops, read

    def broken(*a, **k):
        raise RuntimeError("options.duckdb: IO Error")
    monkeypatch.setattr(read, "option_daily", broken)
    ops.watch_add("option", "AAA", by="test")
    with pytest.raises(RuntimeError, match="options.duckdb"):
        scan.scan_leaps_cached()
