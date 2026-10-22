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

    def test_the_weekly_read_follows_a_redirect(self, monkeypatch):
        """FRED answers a redirect for a moved series: without the flag the body is not JSON."""
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
        assert macro_watch._fred_weekly("DGS10", "fakekey") == [4.68]
        assert seen == [True]

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

    def test_fred_daily_map_keys_by_date(self, monkeypatch):
        monkeypatch.setattr(
            macro_watch, "_fred_daily",
            lambda sid, api_key=None, limit=80: [("2026-08-13", 4.70)],
        )
        assert macro_watch.fred_daily_map("DGS10") == {"2026-08-13": 4.70}


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
