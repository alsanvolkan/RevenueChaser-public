"""Tests for the nightly macro sweep's FRED request (``macro_watch``) and the macro series map.

The weekly FRED/yfinance reads the RRG once used are gone: both boards read the market store now
(``module_sector_rotation/engine.macro_points``, ``module_earnings_radar/store_reads.dgs10_map``).
"""
from RevenueChaser.module_earnings_radar import macro_watch
from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS, MACRO_SERIES


class TestSeriesMapping:
    def test_all_rrg_macro_symbols_mapped(self):
        for sym, info in ROTATION_ETFS.items():
            if info["group"] in ("yield", "commodity"):
                assert sym in MACRO_SERIES, f"{sym} must be in MACRO_SERIES"

    def test_each_has_source(self):
        for sym in MACRO_SERIES:
            src = MACRO_SERIES[sym]
            assert src.get("fred") or src.get("yf"), f"{sym} has no data source"


class TestFredDaily:
    def test_fred_daily_no_key(self):
        assert macro_watch._fred_daily("DGS10", "") == []

    def test_the_daily_read_follows_a_redirect(self, monkeypatch):
        """httpx follows no redirect by default: a redirect body is not the observations JSON."""
        seen = []

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"observations": [{"date": "2026-08-14", "value": "4.68"}]}

        def get(url, *a, params=None, timeout=None, follow_redirects=False, **k):
            seen.append(follow_redirects)
            return _Resp()

        monkeypatch.setattr("httpx.get", get)
        res = macro_watch.fred_daily_result("DGS10", "fakekey")
        assert res.status == "ok" and res.data == [("2026-08-14", 4.68)]
        assert seen == [True]

    def test_fred_daily_parses_dated_pairs(self, monkeypatch):
        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"observations": [
                    {"date": "2026-08-14", "value": "4.68"},
                    {"date": "2026-08-13", "value": "4.70"},
                    {"date": "2026-08-12", "value": "."},
                ]}
        monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp())
        rows = macro_watch._fred_daily("DGS10", "fakekey")
        assert rows == [("2026-08-13", 4.70), ("2026-08-14", 4.68)]

    def test_fred_daily_reports_non_json_body_without_url_or_key(self, monkeypatch):
        class _Resp:
            status_code = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            content = b"<html><body>maintenance</body></html>"

            def raise_for_status(self):
                pass

            def json(self):
                raise ValueError("No JSON object could be decoded")
        monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp())
        res = macro_watch.fred_daily_result("DGS10", "fakekey")
        assert res.status == "failed"
        err = res.error.get("error")
        assert err.startswith("not_json HTTP 200 text/html")
        assert "api_key" not in err
        assert "stlouisfed" not in err
        assert res.error.get("attempts") == 1

    def test_fred_daily_reports_empty_non_json_body(self, monkeypatch):
        class _Resp:
            status_code = 200
            headers = {}
            content = b""

            def raise_for_status(self):
                pass

            def json(self):
                raise ValueError("No JSON object could be decoded")
        monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp())
        res = macro_watch.fred_daily_result("DGS10", "fakekey")
        assert res.status == "failed"
        assert res.error.get("error").endswith("len=0")


class TestVvixIsNotTheVix:
    def test_the_vvix_row_is_cboes_not_a_fred_series(self):
        """FRED's VIXCLS is the VIX itself and FRED has no VVIX series, so the row the boards read must be Cboe's.

        The rule used to be enforced on the live weekly read; since the boards read ``market_series`` it is the
        series mapping that has to carry it.
        """
        assert tuple(MACRO_SERIES["VVIX"]["series"]) == ("VVIX", "cboe")
        assert MACRO_SERIES["VVIX"]["fred"] is None
