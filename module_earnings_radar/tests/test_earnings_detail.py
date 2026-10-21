"""Tests for the earnings-detail data helper (drawer quarter history)."""
import datetime as dt

from RevenueChaser.module_earnings_radar.pipeline import _build_earnings_detail, _quarter_label, _fmt_price_float


class TestQuarterLabel:
    def test_q1(self):
        assert _quarter_label("2026-03-31") == "Q1 2026"

    def test_q3(self):
        assert _quarter_label("2025-09-30") == "Q3 2025"

    def test_bad_value(self):
        assert _quarter_label("not-a-date") == "not-a-date"


class TestBuildEarningsDetail:
    def test_chronological_order(self):
        hist = [
            {"report_date": "2026-05-12", "actual_eps": 0.30, "street_mean_est": 0.28,
             "ending_fiscal_quarter": "2026-03-31", "post_earnings_move_1d": 0.03, "expected_move_perc": 0.05},
            {"report_date": "2025-11-12", "actual_eps": 0.22, "street_mean_est": 0.18,
             "ending_fiscal_quarter": "2025-09-30", "post_earnings_move_1d": 0.01, "expected_move_perc": 0.10},
        ]
        d = _build_earnings_detail(hist)
        assert [x["q"] for x in d] == ["Q3 2025", "Q1 2026"]

    def test_upcoming_excluded(self):
        hist = [
            {"report_date": "2026-08-13", "actual_eps": None, "street_mean_est": 0.09},
            {"report_date": "2026-05-12", "actual_eps": 0.30, "street_mean_est": 0.28,
             "post_earnings_move_1d": 0.03},
        ]
        d = _build_earnings_detail(hist)
        assert len(d) == 1
        assert d[0]["date"] == "2026-05-12"

    def test_missing_implied_is_none_not_reused(self):
        hist = [
            {"report_date": "2026-05-12", "actual_eps": 0.30, "street_mean_est": 0.28,
             "post_earnings_move_1d": 0.03, "expected_move_perc": None},
        ]
        d = _build_earnings_detail(hist)
        assert d[0]["implied"] is None
        assert d[0]["actual"] == 3.0  # 0.03 * 100

    def test_alternate_field_names(self):
        hist = [
            {"report_date": "2026-05-12", "reported_eps": 0.30, "estimated_eps": 0.28,
             "post_earnings_move_1d": 0.03},
        ]
        d = _build_earnings_detail(hist)
        assert d[0]["eps_actual"] == 0.30
        assert d[0]["eps_est"] == 0.28

    def test_surprise_pct_null_near_zero_estimate(self):
        hist = [
            {"report_date": "2026-05-12", "actual_eps": 0.01, "street_mean_est": 0.005,
             "post_earnings_move_1d": 0.03},
        ]
        d = _build_earnings_detail(hist)
        assert d[0]["surprise_dollar"] is not None
        assert d[0]["surprise_pct"] is None

    def test_8_quarter_cap(self):
        base = dt.date(2020, 1, 1)
        hist = []
        for i in range(10):
            d = (base + dt.timedelta(days=i * 100)).isoformat()
            hist.append({"report_date": d, "actual_eps": 0.1, "street_mean_est": 0.1,
                         "post_earnings_move_1d": 0.01, "ending_fiscal_quarter": d})
        d = _build_earnings_detail(hist)
        assert len(d) == 8

    def test_revenue_actual_mapped_by_fiscal_date(self):
        hist = [
            {"report_date": "2026-05-12", "actual_eps": 0.30, "street_mean_est": 0.28,
             "post_earnings_move_1d": 0.03, "ending_fiscal_quarter": "2026-03-31"},
        ]
        fin = [{"fiscal_date": "2026-03-31", "revenue": 1_250_000_000.0}]
        d = _build_earnings_detail(hist, fin)
        assert d[0]["revenue_actual"] == 1_250_000_000.0

    def test_revenue_actual_none_when_missing(self):
        hist = [
            {"report_date": "2026-05-12", "actual_eps": 0.30, "street_mean_est": 0.28,
             "post_earnings_move_1d": 0.03, "ending_fiscal_quarter": "2026-03-31"},
        ]
        d = _build_earnings_detail(hist, [])
        assert d[0]["revenue_actual"] is None

    def test_revenue_est_mapped_by_fiscal_date(self):
        hist = [
            {"report_date": "2026-05-12", "actual_eps": 0.30, "street_mean_est": 0.28,
             "post_earnings_move_1d": 0.03, "ending_fiscal_quarter": "2026-03-31"},
        ]
        d = _build_earnings_detail(hist, [], rev_est={"2026-03-31": 1_300_000_000.0})
        assert d[0]["revenue_est"] == 1_300_000_000.0

    def test_revenue_est_none_when_no_feed(self):
        hist = [
            {"report_date": "2026-05-12", "actual_eps": 0.30, "street_mean_est": 0.28,
             "post_earnings_move_1d": 0.03, "ending_fiscal_quarter": "2026-03-31"},
        ]
        d = _build_earnings_detail(hist, [], rev_est={})
        assert d[0]["revenue_est"] is None


class TestFmtPriceFloat:
    def test_string_price(self):
        assert _fmt_price_float("6.355") == 6.355

    def test_numeric_price(self):
        assert _fmt_price_float(6.355) == 6.355

    def test_none(self):
        assert _fmt_price_float(None) is None

    def test_empty_string(self):
        assert _fmt_price_float("") is None

    def test_garbage(self):
        assert _fmt_price_float("abc") is None

    def test_int_string(self):
        assert _fmt_price_float("10") == 10.0
