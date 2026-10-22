"""Tests for weekly macro series resolution (FRED + yfinance)."""
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


class TestFredWeekly:
    def test_fred_no_key(self):
        assert macro_watch._fred_weekly("DGS10", "") == []

    def test_fred_parses_values(self, monkeypatch):
        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"observations": [
                    {"date": "2026-08-14", "value": "4.68"},
                    {"date": "2026-08-07", "value": "4.70"},
                ]}
        monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp())
        vals = macro_watch._fred_weekly("DGS10", "fakekey")
        assert vals == [4.70, 4.68]  # ascending

    def test_fred_skips_dots(self, monkeypatch):
        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"observations": [{"date": "a", "value": "."}]}
        monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp())
        assert macro_watch._fred_weekly("X", "fakekey") == []

    def test_fred_weekly_returns_empty_on_non_json_body(self, monkeypatch, caplog):
        class _Resp:
            status_code = 200
            headers = {"content-type": "text/plain"}
            content = b""

            def raise_for_status(self):
                pass

            def json(self):
                raise ValueError("No JSON object could be decoded")
        monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp())
        with caplog.at_level("WARNING"):
            assert macro_watch._fred_weekly("DGS10", "fakekey") == []
        assert any("not_json HTTP 200 text/plain len=0" in r.message for r in caplog.records)


class TestFredDaily:
    def test_fred_daily_no_key(self):
        assert macro_watch._fred_daily("DGS10", "") == []

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

    def test_fred_daily_map_keys_by_date(self, monkeypatch):
        monkeypatch.setattr(
            macro_watch, "_fred_daily",
            lambda sid, api_key=None, limit=80: [("2026-08-13", 4.70)],
        )
        assert macro_watch.fred_daily_map("DGS10") == {"2026-08-13": 4.70}

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


class TestMacroWeeklyCloses:
    def test_fred_priority(self, monkeypatch):
        monkeypatch.setattr(macro_watch, "_fred_weekly", lambda sid, key, limit=160: [1.0, 2.0, 3.0])
        monkeypatch.setattr(macro_watch, "_yf_weekly", lambda sym, limit=160: [9.0])
        assert macro_watch.macro_weekly_closes("US10Y") == [1.0, 2.0, 3.0]

    def test_yf_fallback(self, monkeypatch):
        monkeypatch.setattr(macro_watch, "_fred_weekly", lambda sid, key, limit=160: [])
        monkeypatch.setattr(macro_watch, "_yf_weekly", lambda sym, limit=160: [50.0, 51.0])
        assert macro_watch.macro_weekly_closes("MOVE") == [50.0, 51.0]


class TestVvixIsNotTheVix:
    def test_vvix_never_reads_a_fred_series(self, monkeypatch):
        # FRED's VIXCLS is the VIX itself, and FRED has no VVIX series: the row must come from ^VVIX.
        asked = []
        monkeypatch.setattr(macro_watch, "FRED_API_KEY", "k")
        monkeypatch.setattr(macro_watch, "_fred_weekly",
                            lambda sid, key, limit=160: asked.append(sid) or [18.0, 19.0])
        monkeypatch.setattr(macro_watch, "_yf_weekly", lambda sym, limit=160: [90.0, 91.0] if sym == "^VVIX" else [])
        assert macro_watch.macro_weekly_closes("VVIX") == [90.0, 91.0]
        assert asked == []
