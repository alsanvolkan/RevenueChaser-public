"""No empty-over-good writes (PL2 / N1).

A provider failure used to arrive as ``[]`` / ``{}`` and be saved over the last
good stored rows, and the job still ended "done". The sync runners now keep the
stored rows and end with a ``JobMessage`` that names what was kept and why.
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.jobs import JobMessage
from RevenueChaser.core import rawstore

_GOOD = [{"ticker": "AAPL", "weight": 0.2}]


def _seed_holdings(tickers=("XLK", "XLE")):
    rawstore.write_rows("etf_holdings", [{"ticker": t, "entry": _GOOD} for t in tickers])
    rawstore.write_rows("store_meta", [{"key": "etf_holdings_generated_at",
                                        "value": "2026-09-01T00:00:00"}])


class TestEtfHoldingsSync:
    def test_failed_fetch_keeps_every_stored_etf(self, rawstore_tmp_db, monkeypatch):
        import RevenueChaser.module_sector_rotation.etf_holdings as eh
        _seed_holdings()
        monkeypatch.setattr(eh, "get_etf_holdings", lambda t, uw: [])
        with pytest.raises(JobMessage, match=r"(?i)kept stored holdings") as exc:
            runners.run_etf_holdings_sync(uw=None)
        assert "XLK" in exc.value.message and "XLE" in exc.value.message
        assert rawstore.get("etf_holdings", "XLK")["entry"] == _GOOD
        assert rawstore.get("etf_holdings", "XLE")["entry"] == _GOOD
        # Nothing was fetched, so the sync time must not claim fresh data.
        assert rawstore.get("store_meta", "etf_holdings_generated_at")["value"] == "2026-09-01T00:00:00"

    def test_partial_fetch_updates_good_rows_and_keeps_the_rest(self, rawstore_tmp_db, monkeypatch):
        import RevenueChaser.module_sector_rotation.etf_holdings as eh
        _seed_holdings()
        fresh = [{"ticker": "XOM", "weight": 0.3}]
        monkeypatch.setattr(eh, "get_etf_holdings", lambda t, uw: fresh if t == "XLE" else [])
        with pytest.raises(JobMessage) as exc:
            runners.run_etf_holdings_sync(uw=None)
        assert "XLK" in exc.value.message
        assert rawstore.get("etf_holdings", "XLE")["entry"] == fresh
        assert rawstore.get("etf_holdings", "XLK")["entry"] == _GOOD
        assert rawstore.get("store_meta", "etf_holdings_generated_at")["value"] != "2026-09-01T00:00:00"

    def test_empty_fetch_with_nothing_stored_is_still_recorded(self, rawstore_tmp_db, monkeypatch):
        import RevenueChaser.module_sector_rotation.etf_holdings as eh
        monkeypatch.setattr(eh, "get_etf_holdings", lambda t, uw: [])
        with pytest.raises(JobMessage):
            runners.run_etf_holdings_sync(uw=None)
        assert rawstore.get("etf_holdings", "XLK")["entry"] == []


class TestMoneyFlow:
    def _patch_paths(self, tmp_path, monkeypatch):
        out = tmp_path / "cache"
        latest = out / "sector_rotation" / "sector_rotation_latest.json"
        monkeypatch.setattr(runners, "OUTPUT_DIR", out)
        monkeypatch.setattr(runners, "MONEY_FLOW_LATEST", latest)
        return latest

    def test_empty_result_keeps_the_stored_leaderboard(self, tmp_path, monkeypatch):
        import RevenueChaser.module_sector_rotation.money_flow as mf
        latest = self._patch_paths(tmp_path, monkeypatch)
        latest.parent.mkdir(parents=True)
        good = {"inflows": [{"ticker": "UP"}], "outflows": [{"ticker": "DN"}],
                "generated_at": "2026-09-01T00:00:00", "as_of": "2026-08-29"}
        latest.write_text(json.dumps(good), encoding="utf-8")
        monkeypatch.setattr(mf, "compute_money_flow", lambda uw, **k: {
            "inflows": [], "outflows": [], "generated_at": None, "as_of": None})
        with pytest.raises(JobMessage, match=r"(?i)kept"):
            runners.run_money_flow_latest(uw=None)
        assert json.loads(latest.read_text(encoding="utf-8")) == good

    def test_non_empty_result_is_written(self, tmp_path, monkeypatch):
        import RevenueChaser.module_sector_rotation.money_flow as mf
        latest = self._patch_paths(tmp_path, monkeypatch)
        monkeypatch.setattr(mf, "compute_money_flow", lambda uw, **k: {
            "inflows": [{"ticker": "NEW"}], "outflows": [], "generated_at": None, "as_of": "2026-09-29"})
        runners.run_money_flow_latest(uw=None)
        assert json.loads(latest.read_text(encoding="utf-8"))["inflows"] == [{"ticker": "NEW"}]


class TestSec13fMeta:
    def test_failed_screener_does_not_blank_stored_names(self, rawstore_tmp_db, monkeypatch):
        from RevenueChaser.module_sec_13f import engine as sec_13f
        import RevenueChaser.module_sec_13f.universe_info as ui
        good = {"name": "Apple Inc", "sector": "Technology", "type": "Stock"}
        sec_13f.save_sec13f_meta({"AAPL": good})
        monkeypatch.setattr(sec_13f, "load_totals", lambda: {"AAPL": {}, "NEWCO": {}})
        monkeypatch.setattr(ui, "fetch_universe_info", lambda uw, tickers: {})
        meta = runners.build_sec13f_meta(uw=None)
        assert meta["AAPL"] == good
        assert sec_13f.load_sec13f_meta()["AAPL"] == good
        # A ticker with nothing stored still gets its placeholder row.
        assert sec_13f.load_sec13f_meta()["NEWCO"]["sector"] == "Unclassified"

    def test_screener_sector_spellings_are_stored_as_morningstar_names(self, rawstore_tmp_db, monkeypatch):
        """#266: the 13F meta step runs the screener's sector through ``canonical_sector``: an old GICS spelling
        or a vendor spelling is stored as the Morningstar name, an override ticker gets its override, an unknown
        sector reads ``Unclassified`` and an asset class passes through."""
        from RevenueChaser.module_sec_13f import engine as sec_13f

        class _UW:
            def screener_batch(self, tickers):
                return {
                    "AAPL": {"full_name": "Apple Inc", "issue_type": "Common Stock", "sector": "Information Technology"},
                    "JNJ": {"full_name": "Johnson & Johnson", "issue_type": "Common Stock", "sector": "Health Care"},
                    "MSFT": {"full_name": "Microsoft", "issue_type": "Common Stock", "sector": "Technology"},
                    "ODD": {"full_name": "Odd Co", "issue_type": "Common Stock", "sector": "Spaceships"},
                }

        monkeypatch.setattr(sec_13f, "load_totals",
                            lambda: {t: {} for t in ("AAPL", "JNJ", "MSFT", "ODD", "BRK.B", "BTC")})
        meta = runners.build_sec13f_meta(uw=_UW())
        assert {t: meta[t]["sector"] for t in meta} == {
            "AAPL": "Technology", "JNJ": "Healthcare", "MSFT": "Technology", "ODD": "Unclassified",
            "BRK.B": "Financial Services", "BTC": "Crypto / Digital Assets"}
        assert sec_13f.load_sec13f_meta()["AAPL"]["sector"] == "Technology"

    def test_an_unknown_screener_sector_is_stored_as_unclassified(self):
        """R3 (#86): the 13F meta stores ``Unclassified`` for an unknown sector (a legacy ``Other`` reads the same),
        and an asset class passes through unchanged."""
        meta = {"AAA": {"name": "", "sector": "Unclassified", "type": "Other"},
                "BBB": {"name": "", "sector": "Unclassified", "type": "Other"},
                "CCC": {"name": "", "sector": "Unclassified", "type": "Other"},
                "OLD": {"name": "Old", "sector": "Energy", "type": "Stock"}}
        runners._apply_universe_info(meta, {
            "AAA": {"name": "Alpha", "sector": "Other", "type": "Stock"},            # named, sector unknown
            "BBB": {"name": "", "sector": "Other", "type": "Stock"},                 # nothing known: skipped
            "CCC": {"name": "Gamma Coin", "sector": "Crypto / Digital Assets", "type": "Other"},
            "OLD": {"name": "", "sector": "Utilities", "type": "Stock"}})            # a known sector wins
        assert meta["AAA"] == {"name": "Alpha", "sector": "Unclassified", "type": "Stock"}
        assert meta["BBB"]["name"] == "" and meta["BBB"]["sector"] == "Unclassified"
        assert meta["CCC"]["sector"] == "Crypto / Digital Assets"
        assert meta["OLD"]["sector"] == "Utilities"
