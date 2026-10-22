"""#374 ROT-4 C3: the macro panel reads stored tables only."""
from datetime import date, timedelta

from RevenueChaser.core import rawstore
from RevenueChaser.module_sector_rotation import engine
from RevenueChaser.module_earnings_radar import macro_watch
from RevenueChaser.module_sector_rotation.universe import MACRO_SERIES

DAY = date(2026, 10, 6)


def _seed_macro_series(series_id: str, values: list[tuple[date, float]], source: str = "fred") -> None:
    rows = []
    for obs, value in values:
        rows.append({
            "series_id": series_id, "obs_date": obs, "source": source, "value": value,
            "frequency": "daily", "proxy_for": None, "session_date": obs,
            "fetched_at": 0.0, "run_id": "seed", "run_at": 0.0,
        })
    rawstore.write_rows("macro_series", rows)


def _seed_vol_index(symbol: str, values: list[tuple[date, float]], source: str = "cboe") -> None:
    rows = []
    for session, close in values:
        rows.append({
            "symbol": symbol, "session_date": session, "source": source,
            "open": close, "high": close, "low": close, "close": close,
            "operator": None, "fetched_at": 0.0, "run_id": "seed", "run_at": 0.0,
        })
    rawstore.write_rows("vol_index_daily", rows)


def _raise_on_network(*a, **k):
    raise AssertionError("macro_panel must not reach the network")


class TestMacroPanelReadsStoredTables:
    def test_fred_series_level_and_week_over_week_change(self, rawstore_tmp_db, monkeypatch):
        monkeypatch.setattr(macro_watch, "_fred_weekly", _raise_on_network)
        monkeypatch.setattr(macro_watch, "_yf_weekly", _raise_on_network)
        _seed_macro_series("DGS10", [
            (DAY - timedelta(days=7), 4.70),
            (DAY - timedelta(days=1), 4.68),
        ])
        rows = {r["ticker"]: r for r in engine.macro_panel(as_of=DAY)}
        us10y = rows["US10Y"]
        assert us10y["level"] == 4.68
        assert us10y["change"] == -0.02
        assert us10y["as_of"] == (DAY - timedelta(days=1)).isoformat()

    def test_vol_index_series_level_and_change(self, rawstore_tmp_db, monkeypatch):
        monkeypatch.setattr(macro_watch, "_fred_weekly", _raise_on_network)
        monkeypatch.setattr(macro_watch, "_yf_weekly", _raise_on_network)
        _seed_vol_index("VVIX", [
            (DAY - timedelta(days=7), 89.5),
            (DAY - timedelta(days=1), 91.0),
        ])
        rows = {r["ticker"]: r for r in engine.macro_panel(as_of=DAY)}
        vvix = rows["VVIX"]
        assert vvix["level"] == 91.0
        assert vvix["change"] == 1.5

    def test_short_history_renders_na(self, rawstore_tmp_db, monkeypatch):
        monkeypatch.setattr(macro_watch, "_fred_weekly", _raise_on_network)
        monkeypatch.setattr(macro_watch, "_yf_weekly", _raise_on_network)
        _seed_macro_series("DGS2", [(DAY - timedelta(days=1), 4.25)])
        rows = {r["ticker"]: r for r in engine.macro_panel(as_of=DAY)}
        assert rows["US02Y"]["level"] is None
        assert rows["US02Y"]["change"] is None
        assert rows["US02Y"]["as_of"] is None

    def test_symbol_with_no_writer_renders_na(self, rawstore_tmp_db, monkeypatch):
        monkeypatch.setattr(macro_watch, "_fred_weekly", _raise_on_network)
        monkeypatch.setattr(macro_watch, "_yf_weekly", _raise_on_network)
        rows = {r["ticker"]: r for r in engine.macro_panel(as_of=DAY)}
        wheatf = rows["WHEATF"]
        assert wheatf["level"] is None and wheatf["change"] is None and wheatf["as_of"] is None

    def test_a_second_source_does_not_leak_into_the_fred_panel(self, rawstore_tmp_db, monkeypatch):
        # SHOULD-4: the read pins the writer's source. Another source's value for the same
        # series and date must not reach the panel (the panel would otherwise take the last row).
        monkeypatch.setattr(macro_watch, "_fred_weekly", _raise_on_network)
        monkeypatch.setattr(macro_watch, "_yf_weekly", _raise_on_network)
        _seed_macro_series("DGS10", [(DAY - timedelta(days=7), 4.70), (DAY - timedelta(days=1), 4.68)])
        _seed_macro_series("DGS10", [(DAY - timedelta(days=1), 9.99)], source="other")
        rows = {r["ticker"]: r for r in engine.macro_panel(as_of=DAY)}
        assert rows["US10Y"]["level"] == 4.68
        assert rows["US10Y"]["change"] == -0.02

    def test_a_second_source_does_not_leak_into_the_vol_index_panel(self, rawstore_tmp_db, monkeypatch):
        # SHOULD-4: both writers' sources (cboe, yfinance) stay readable; a foreign source does not.
        monkeypatch.setattr(macro_watch, "_fred_weekly", _raise_on_network)
        monkeypatch.setattr(macro_watch, "_yf_weekly", _raise_on_network)
        _seed_vol_index("VVIX", [(DAY - timedelta(days=7), 89.5), (DAY - timedelta(days=1), 91.0)])
        _seed_vol_index("VVIX", [(DAY - timedelta(days=1), 9.99)], source="other")
        _seed_vol_index("MOVE", [(DAY - timedelta(days=7), 100.0), (DAY - timedelta(days=1), 104.0)],
                        source="yfinance")
        rows = {r["ticker"]: r for r in engine.macro_panel(as_of=DAY)}
        assert rows["VVIX"]["level"] == 91.0
        assert rows["VVIX"]["change"] == 1.5
        assert rows["MOVE"]["level"] == 104.0
