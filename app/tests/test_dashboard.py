"""Tests for the interactive dashboard (routes + runners)."""
import json
import re
from datetime import date, timedelta

import pytest

from RevenueChaser.app import dashboard, runners
from RevenueChaser.core.tests.panel_client import panel_test_client


class _FakeUW:
    @property
    def active(self):
        return True

    def verify(self):
        return True, None

    def calendar_day_raw(self, d, session):
        return []

    def screener_batch(self, tickers, date=None):
        return {}


class TestDashboardRoutes:
    def _client(self):
        dashboard.app.config["TESTING"] = True
        return panel_test_client(dashboard.app)

    def test_index_renders_tabs(self):
        c = self._client()
        r = c.get("/")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        for label in ("Research", "Earnings Radar",
                      "SPX Gamma", "Financials",
                      "Screeners", "Sector Rotation", "Asymmetric Growth", "Swing Potential",
                      "Fundamentals", "Ownership", "SEC 13F", "LEAPS"):
            assert label in body
        # NRC is merged into the Earnings Radar, no longer a separate tab
        assert "NRC Filter" not in body
        # Earnings Radar v2 and its calibration board are deleted; v1 stays.
        assert "Earnings Radar" in body
        assert "Earnings Radar v2" not in body
        assert "ER v2 Calibration" not in body

    def test_earnings_radar_v2_is_unwired(self):
        keys = {t["key"] for t in dashboard.TABS if t["type"] == "tab"}
        assert "module_earnings_radar_v2" not in keys
        assert "earnings_radar_v2_calibration" not in keys
        for name in ("run_earnings_radar_v2", "run_earnings_radar_v2_calibration"):
            assert not hasattr(runners, name), f"runners.{name} must be removed"
        for name in ("EARNINGS_RADAR_V2_LATEST", "EARNINGS_RADAR_V2_CALIBRATION_LATEST"):
            assert not hasattr(runners, name), f"runners.{name} must be removed"

    def test_index_renders_collapsible_nav(self):
        body = self._client().get("/").get_data(as_text=True)
        assert 'class="nav-group"' in body
        assert 'class="nav-header"' in body and 'aria-expanded="true"' in body
        assert 'class="caret"' in body
        assert 'data-group="Classification"' in body
        assert 'data-tab="module_comovement"' in body

    def test_nav_groups_fold_flat_tabs(self):
        groups = dashboard._nav_groups([
            {"type": "header", "label": "A"},
            {"type": "tab", "key": "x", "label": "X", "url": "/u", "sub": True},
            {"type": "tab", "key": "y", "label": "Y", "url": "/u", "sub": True},
            {"type": "header", "label": "B"},
            {"type": "tab", "key": "z", "label": "Z", "url": "/u", "sub": False},
        ])
        assert [g["label"] for g in groups] == ["A", "B"]
        assert [t["key"] for t in groups[0]["tabs"]] == ["x", "y"]
        assert [t["key"] for t in groups[1]["tabs"]] == ["z"]

    def test_spx_gamma_tab_is_fill_width(self):
        """SPX Gamma is a fluid page: its iframe must fill the pane width (and
        re-fit on resize), not sit at the fixed 1280px default."""
        c = self._client()
        body = c.get("/").get_data(as_text=True)
        assert 'data-tab="module_spx_gamma"' in body
        assert "'module_spx_gamma': true" in body
        assert "'module_earnings_radar': true" in body
        # the fill-width branch must check FILL_WIDTH[tab] before any grow logic
        assert "FILL_WIDTH[tab]" in body
        assert "ResizeObserver" in body

    def test_tradewhisperer_is_removed(self):
        """Removed from RevenueChaser on 2026-09-30 (small-boards decision D3)."""
        from RevenueChaser.app import jobs
        c = self._client()
        assert c.get("/view/module_tradewhisperer").status_code == 404
        assert c.post("/run/tw").status_code == 404
        assert c.get("/api/tw/chart/AAPL").status_code == 404
        assert not any(k.startswith("tw") for k in jobs._RUNNERS)
        assert "TradeWhisperer" not in c.get("/").get_data(as_text=True)

    def test_view_missing_module_placeholder(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runners, "EARNINGS_LATEST", tmp_path / "earnings_latest.html")
        c = self._client()
        r = c.get("/view/module_earnings_radar")
        assert r.status_code == 200
        assert "No Earnings Radar data yet" in r.get_data(as_text=True)

    def test_view_unknown_module_404(self):
        c = self._client()
        r = c.get("/view/nonexistent")
        assert r.status_code == 404

    def test_spxgamma_latest_and_view(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runners, "spxgamma_latest_path", lambda: tmp_path / "latest.json")
        monkeypatch.setattr(dashboard, "_spxgamma_latest_report_file", lambda: None)
        c = self._client()
        r = c.get("/api/spxgamma/latest")
        assert r.status_code == 200
        assert r.get_json()["status"] is None
        r = c.get("/view/module_spx_gamma")
        assert r.status_code == 200

    def test_spxgamma_run_bad_date(self):
        c = self._client()
        r = c.post("/run/spxgamma", data={"date": "not-a-date"})
        assert r.status_code == 400

    def test_spxgamma_run_returns_job_id(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        monkeypatch.setattr(d, "UWClient", lambda: _FakeUW())
        monkeypatch.setattr(d.jobs, "submit", lambda module, params: "stubjob")
        c = self._client()
        r = c.post("/run/spxgamma", data={"date": "2026-08-14"})
        assert r.status_code == 202
        assert r.get_json()["job_id"] == "stubjob"

    def test_spxgamma_registered_requires_uw(self):
        from RevenueChaser.app import jobs
        assert "spxgamma" in jobs._RUNNERS
        assert jobs._REQUIRES_UW.get("spxgamma") is True
        assert jobs._PRODUCES.get("spxgamma") == ["module_spx_gamma"]

    def test_sec13f_edgar_backfill_is_retired(self):
        """13F comes only from the SEC form-13f zip data sets (ownership plan D2)."""
        from RevenueChaser.app import jobs
        assert "module_sec_13f_edgar" not in jobs._RUNNERS
        c = self._client()
        assert c.post("/run/module_sec_13f-edgar", data={"period": "2026-06-30"}).status_code == 404
        assert "sec_13f-edgar" not in c.get("/").get_data(as_text=True)

    def test_sec13f_fetch_needs_no_uw_key(self):
        """#206: the SEC ingest is not gated on Unusual Whales; its names step is best-effort."""
        from RevenueChaser.app import jobs, runners
        assert jobs._RUNNERS["module_sec_13f"] is runners.build_sec13f
        assert jobs._REQUIRES_UW["module_sec_13f"] is False

    def test_sec13f_rebuild_job_is_store_only(self):
        from RevenueChaser.app import jobs, runners
        assert jobs._RUNNERS["sec13f_rebuild"] is runners.run_sec13f_rebuild
        assert jobs._REQUIRES_UW["sec13f_rebuild"] is False
        assert jobs._PRODUCES["sec13f_rebuild"] == ["module_sec_13f"]

    def test_sec13f_rebuild_route_and_button(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        seen = []
        monkeypatch.setattr(d.jobs, "submit", lambda module, params: seen.append(module) or "stubjob")
        c = self._client()
        r = c.post("/run/module_sec_13f-rebuild")
        assert r.status_code == 202 and seen == ["sec13f_rebuild"]
        assert "btn-sec13f-rebuild" in c.get("/").get_data(as_text=True)

    def test_sec13f_check_registered_no_uw(self):
        from RevenueChaser.app import jobs
        assert jobs._REQUIRES_UW.get("sec13f_check") is False

    def test_run_sec13f_check_route_submits(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        seen = []
        monkeypatch.setattr(d.jobs, "submit", lambda module, params: seen.append(module) or "stubjob")
        c = self._client()
        r = c.post("/run/module_sec_13f-check")
        assert r.status_code == 202 and r.get_json()["job_id"] == "stubjob"
        assert seen == ["sec13f_check"]
        assert "btn-sec13f-check" in c.get("/").get_data(as_text=True)

    def test_run_earnings_bad_date(self):
        c = self._client()
        r = c.post("/run/earnings", data={"start": "not-a-date", "end": "not-a-date"})
        assert r.status_code == 400

    def test_run_earnings_bad_dates_list(self):
        c = self._client()
        r = c.post("/run/earnings", data={"dates": "2026-09-07,not-a-date"})
        assert r.status_code == 400

    def test_run_earnings_dates_list_is_submitted(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        captured = {}
        monkeypatch.setattr(d, "UWClient", lambda: _FakeUW())
        def fake_submit(module, params):
            captured.update(module=module, **params)
            return "stubjob"
        monkeypatch.setattr(d.jobs, "submit", fake_submit)
        c = self._client()
        r = c.post("/run/earnings", data={"dates": "2026-09-11,2026-09-07,2026-09-09"})
        assert r.status_code == 202
        assert captured["module"] == "earnings"
        assert captured["dates"] == [date(2026, 9, 11), date(2026, 9, 7), date(2026, 9, 9)]
        assert "start" not in captured

    def test_run_no_api_key(self, monkeypatch):
        c = self._client()
        monkeypatch.setattr(dashboard, "UWClient", lambda: _NoKey())
        r = c.post("/run/module_leaps")  # Fetch = live UW scan -> gated on the key
        assert r.status_code == 400
        data = r.get_json()
        assert data["ok"] is False

    def test_run_returns_job_id(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        monkeypatch.setattr(d, "UWClient", lambda: _FakeUW())
        monkeypatch.setattr(d.jobs, "submit", lambda module, params: "stubjob")
        c = self._client()
        r = c.post("/run/module_leaps")
        assert r.status_code == 202
        data = r.get_json()
        assert data["ok"] is True
        assert data["job_id"] == "stubjob"

    def test_api_jobs_and_job(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        monkeypatch.setattr(d, "UWClient", lambda: _FakeUW())
        monkeypatch.setattr(d.jobs, "submit", lambda module, params: "stubjob")
        c = self._client()
        c.post("/run/leaps")
        jr = c.get("/api/job/stubjob")
        assert jr.status_code == 404  # stub never registered a real job
        jl = c.get("/api/jobs")
        assert jl.status_code == 200
        assert "jobs" in jl.get_json()

    def test_api_job_unknown_404(self):
        c = self._client()
        r = c.get("/api/job/nonexistent")
        assert r.status_code == 404

    def test_api_rotation_dates(self, rawstore_tmp_db):
        from RevenueChaser.core import rawstore
        rawstore.write_rows("rotation_snapshots", [{
            "as_of_date": "2026-08-12", "ticker": "XLK", "name": "IT", "group": "sector",
            "close_price": 1.0, "rs_1w": 0, "rs_1m": 0, "rs_3m": 0, "rs_ratio": 0,
            "rs_momentum": 0, "rs_ratio_z": 0, "rs_momentum_z": 0, "quadrant": "LEADING",
        }])
        c = self._client()
        r = c.get("/api/rotation/dates")
        assert r.status_code == 200
        assert r.get_json()["dates"] == ["2026-08-12"]

    def test_api_rotation_latest(self, rawstore_tmp_db):
        from RevenueChaser.core import rawstore
        for d, gen in [("2026-08-12", "2026-08-12T10:00:00"), ("2026-08-15", "2026-08-15T14:32:05")]:
            rawstore.write_rows("rotation_snapshots", [{
                "as_of_date": d, "ticker": "XLK", "name": "IT", "group": "sector",
                "close_price": 1.0, "rs_1w": 0, "rs_1m": 0, "rs_3m": 0, "rs_ratio": 0,
                "rs_momentum": 0, "rs_ratio_z": 0, "rs_momentum_z": 0, "quadrant": "LEADING",
                "generated_at": gen,
            }])
        c = self._client()
        r = c.get("/api/rotation/latest")
        assert r.status_code == 200
        data = r.get_json()
        assert data["as_of_date"] == "2026-08-15"
        assert data["generated_at"] == "2026-08-15T14:32:05"

    def test_api_rotation_latest_empty(self, rawstore_tmp_db):
        c = self._client()
        r = c.get("/api/rotation/latest")
        assert r.status_code == 200
        assert r.get_json()["as_of_date"] is None

    def test_api_moneyflow_cached(self, tmp_path, monkeypatch):
        import json
        from RevenueChaser.app import runners
        monkeypatch.setattr(runners, "MONEY_FLOW_LATEST", tmp_path / "money_flow_latest.json")
        (tmp_path / "money_flow_latest.json").write_text(
            json.dumps({"inflows": [{"ticker": "UP", "name": "Up", "flow_score": 12.5}],
                        "outflows": [{"ticker": "DOWN", "name": "Down", "flow_score": -9.1}],
                        "generated_at": "2026-08-15T10:00:00"}), encoding="utf-8")
        c = self._client()
        r = c.get("/api/moneyflow")
        assert r.status_code == 200
        data = r.get_json()
        assert data["inflows"][0]["ticker"] == "UP"
        assert data["outflows"][0]["flow_score"] == -9.1

    def test_api_moneyflow_missing(self, tmp_path, monkeypatch):
        from RevenueChaser.app import runners
        monkeypatch.setattr(runners, "MONEY_FLOW_LATEST", tmp_path / "missing.json")
        c = self._client()
        r = c.get("/api/moneyflow")
        assert r.status_code == 200
        assert r.get_json()["inflows"] == []

    def test_run_rotation_spy_only(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        captured = {}
        monkeypatch.setattr(d, "UWClient", lambda: _FakeUW())
        def fake_submit(module, params):
            captured.update(params)
            return "stubjob"
        monkeypatch.setattr(d.jobs, "submit", fake_submit)
        c = self._client()
        r = c.post("/run/rotation", data={"benchmark": "QQQ"})
        assert r.status_code == 202
        assert captured["benchmark"] == "SPY", "benchmark is forced to SPY regardless of input"

    def test_index_has_trail_controls(self):
        c = self._client()
        r = c.get("/")
        body = r.get_data(as_text=True)
        assert "trail-toggle" in body
        assert "range-dd" in body
        assert "btn-refresh" in body
        assert "btn-refresh-universe" in body
        assert "Fetch Universe" in body

    def test_run_module_asymmetric_growth_refresh_universe_flag(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        captured = {}
        monkeypatch.setattr(d, "UWClient", lambda: _FakeUW())
        monkeypatch.setattr(d.jobs, "submit",
                            lambda module, params: captured.update(module=module, params=params) or "stubjob")
        c = self._client()
        r = c.post("/run/module_asymmetric_growth", data={"refresh_universe": "1"})
        assert r.status_code == 202
        assert captured["module"] == "module_asymmetric_growth"
        assert captured["params"].get("refresh_universe") is True

    def test_run_module_asymmetric_growth_no_refresh_by_default(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        captured = {}
        monkeypatch.setattr(d, "UWClient", lambda: _FakeUW())
        monkeypatch.setattr(d.jobs, "submit",
                            lambda module, params: captured.update(params=params) or "stubjob")
        c = self._client()
        c.post("/run/module_asymmetric_growth")
        assert "refresh_universe" not in captured["params"]

    def test_run_swing_refresh_universe_flag(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        captured = {}
        monkeypatch.setattr(d, "UWClient", lambda: _FakeUW())
        monkeypatch.setattr(d.jobs, "submit",
                            lambda module, params: captured.update(module=module, params=params) or "stubjob")
        c = self._client()
        r = c.post("/run/swing", data={"refresh_universe": "1"})
        assert r.status_code == 202
        assert captured["module"] == "swing"
        assert captured["params"].get("refresh_universe") is True

    def test_run_module_fundamentals_no_fetch_flag(self, monkeypatch):
        # 'Refresh Tab' on a universe board = model-only, no network.
        import RevenueChaser.app.dashboard as d
        captured = {}
        monkeypatch.setattr(d, "UWClient", lambda: _FakeUW())
        monkeypatch.setattr(d.jobs, "submit",
                            lambda module, params: captured.update(module=module, params=params) or "stubjob")
        c = self._client()
        r = c.post("/run/module_fundamentals", data={"no_fetch": "1"})
        assert r.status_code == 202
        assert captured["module"] == "module_fundamentals"
        assert captured["params"].get("no_fetch") is True

    def test_run_module_asymmetric_growth_neither_flag(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        captured = {}
        monkeypatch.setattr(d, "UWClient", lambda: _FakeUW())
        monkeypatch.setattr(d.jobs, "submit",
                            lambda module, params: captured.update(params=params) or "stubjob")
        c = self._client()
        c.post("/run/module_asymmetric_growth")
        assert "no_fetch" not in captured["params"]
        assert "refresh_universe" not in captured["params"]

    def test_universe_job_registered_for_all_three_boards(self):
        from RevenueChaser.app import jobs
        assert "universe" in jobs._RUNNERS
        assert jobs._REQUIRES_UW.get("universe") is False
        assert set(jobs._PRODUCES.get("universe")) == {
            "module_asymmetric_growth", "module_swing_potential", "module_fundamentals"}

    def test_run_universe_returns_job_id(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        monkeypatch.setattr(d, "UWClient", lambda: _FakeUW())
        monkeypatch.setattr(d.jobs, "submit", lambda module, params: "stubjob")
        c = self._client()
        r = c.post("/run/universe")
        assert r.status_code == 202
        assert r.get_json()["job_id"] == "stubjob"

    def test_today_button_reloads_current_map(self):
        c = self._client()
        r = c.get("/")
        body = r.get_data(as_text=True)
        # Today must reload the current map view (/view/module_sector_rotation), not the trail
        assert "showToday" in body
        assert "data-src" in body  # uses the frame's data-src (current map)
        # trail URL should NOT be hard-coded in showToday
        idx = body.find("function showToday")
        seg = body[idx:idx + 300]
        assert "/view/module_sector_rotation/trail" not in seg, "Today button must not target the trail view"

    def test_rotation_week_calendar(self):
        c = self._client()
        r = c.get("/")
        body = r.get_data(as_text=True)
        # interactive week calendar (hover/click), not dropdowns
        assert "cal-popover" in body, "week calendar popover should be present"
        assert "cal-week-row" in body, "calendar should render week rows"
        assert "clickWeek" in body, "click-to-select-week JS should be present"
        assert "isoWeekInfo" in body, "ISO week computation should be present"
        assert '<select id="trail-start"' not in body, "old week dropdown should be removed"

    def test_api_rotation_weeks(self, rawstore_tmp_db):
        from RevenueChaser.core import rawstore
        for d in ["2026-08-12", "2026-08-19"]:
            rawstore.write_rows("rotation_snapshots", [{
                "as_of_date": d, "ticker": "XLK", "name": "IT", "group": "sector",
                "close_price": 1.0, "rs_1w": 0, "rs_1m": 0, "rs_3m": 0, "rs_ratio": 0,
                "rs_momentum": 0, "rs_ratio_z": 0, "rs_momentum_z": 0, "quadrant": "LEADING",
            }])
        c = self._client()
        r = c.get("/api/rotation/weeks")
        assert r.status_code == 200
        weeks = r.get_json()["weeks"]

        # The picker offers a fetchable window, not only the weeks already
        # stored — otherwise "Fetch Data Range" cannot reach a new week.
        assert len(weeks) >= dashboard.ROTATION_PICKER_WEEKS
        assert weeks == sorted(weeks, key=lambda w: w["start"])

        # Stored weeks are flagged and resolve to their snapshot date.
        stored = {w["label"]: w for w in weeks if w["has_data"]}
        assert set(stored) == {"W33 2026", "W34 2026"}
        assert stored["W33 2026"]["date"] == "2026-08-12"
        assert stored["W34 2026"]["date"] == "2026-08-19"

        # Every week is a Monday..Sunday span; empty ones address their Monday.
        for w in weeks:
            assert date.fromisoformat(w["start"]).weekday() == 0
            assert date.fromisoformat(w["end"]) - date.fromisoformat(w["start"]) == timedelta(days=6)
            if not w["has_data"]:
                assert w["date"] == w["start"]

    def test_api_rotation_weeks_without_snapshots_still_offers_a_range(self, rawstore_tmp_db):
        """An empty store must not leave the picker with nothing to click."""
        weeks = self._client().get("/api/rotation/weeks").get_json()["weeks"]
        assert len(weeks) == dashboard.ROTATION_PICKER_WEEKS
        assert not any(w["has_data"] for w in weeks)

    def test_holdings_route_missing_ticker(self):
        c = self._client()
        r = c.get("/view/module_sector_rotation/holdings")
        assert r.status_code == 404

    def test_holdings_route_fragment(self, rawstore_tmp_db):
        import RevenueChaser.module_sector_rotation.store as rs
        rs.save_holdings_cache("XLK", [{"ticker": "NVDA", "name": "NVIDIA", "weight": 14.2,
                                        "close": 150.0, "rs_1w": 2.1, "rs_1m": 5.3, "rs_3m": 9.8}])
        c = self._client()
        r = c.get("/view/module_sector_rotation/holdings?ticker=XLK")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "NVDA" in body
        assert "Top holdings" in body

    def test_holdings_legacy_route_alias(self, rawstore_tmp_db):
        # Pre-Phase-2 slug must keep working (the JS falls back to it).
        import RevenueChaser.module_sector_rotation.store as rs
        rs.save_holdings_cache("XLK", [{"ticker": "NVDA", "name": "NVIDIA", "weight": 14.2,
                                        "close": 150.0, "rs_1w": 2.1, "rs_1m": 5.3, "rs_3m": 9.8}])
        c = self._client()
        r = c.get("/view/rotation/holdings?ticker=XLK")
        assert r.status_code == 200
        assert "NVDA" in r.get_data(as_text=True)

    def test_holdings_route_sector_uses_the_industry_tree(self, rawstore_tmp_db):
        from RevenueChaser.core import rawstore
        rawstore.write_rows("equity_universe", [
            {"ticker": "NVDA", "name": "NVIDIA", "sector": "Technology",
             "industry": "Semiconductors", "market_cap": 300.0},
            {"ticker": "AMD", "name": "AMD", "sector": "Technology",
             "industry": "Semiconductors", "market_cap": 100.0},
        ])
        body = self._client().get(
            "/view/module_sector_rotation/holdings?ticker=XLK").get_data(as_text=True)
        assert "Industry holdings" in body and "Semiconductors" in body
        assert "NVDA" in body and "75.00%" in body
        assert "Top holdings" not in body

    def test_holdings_route_every_sector_etf_finds_its_tree(self, rawstore_tmp_db):
        # XLK/XLV/XLF/XLB/XLY/XLP used to fall silently back to the ETF top 10 once the stored sector
        # carried a Morningstar name; every sector row must find its tree.
        from RevenueChaser.core import rawstore
        from RevenueChaser.core.gics_map import MORNINGSTAR_SECTORS
        from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS
        import RevenueChaser.module_sector_rotation.store as rs
        sector_etfs = {i["name"]: t for t, i in ROTATION_ETFS.items() if i["group"] == "sector"}
        assert set(sector_etfs) == set(MORNINGSTAR_SECTORS)
        rawstore.write_rows("equity_universe", [
            {"ticker": f"Q{n:02d}", "name": f"Co {n}", "sector": s, "industry": "Some Industry",
             "market_cap": 10.0} for n, s in enumerate(MORNINGSTAR_SECTORS)])
        for etf in sector_etfs.values():       # a cached ETF top 10 would win if the tree were missed
            rs.save_holdings_cache(etf, [{"ticker": "ETFTOP", "name": "x", "weight": 9.0,
                                          "close": 1.0, "rs_1w": 0.0, "rs_1m": 0.0, "rs_3m": 0.0}])
        c = self._client()
        for sector, etf in sector_etfs.items():
            body = c.get(f"/view/module_sector_rotation/holdings?ticker={etf}").get_data(as_text=True)
            assert "Industry holdings" in body and "Some Industry" in body, (sector, etf)
            assert "ETFTOP" not in body and "Top holdings" not in body, (sector, etf)

    def test_holdings_route_theme_still_uses_etf_holdings(self, rawstore_tmp_db):
        import RevenueChaser.module_sector_rotation.store as rs
        rs.save_holdings_cache("SMH", [{"ticker": "NVDA", "name": "NVIDIA", "weight": 18.0,
                                        "close": 150.0, "rs_1w": 2.1, "rs_1m": 5.3, "rs_3m": 9.8}])
        body = self._client().get(
            "/view/module_sector_rotation/holdings?ticker=SMH").get_data(as_text=True)
        assert "Top holdings" in body and "NVDA" in body

    def test_watchlist_toggle_add_and_remove(self, rawstore_tmp_db):
        from RevenueChaser.core import rawstore
        c = self._client()
        r = c.post("/api/watchlist/toggle/AAA")
        assert r.get_json() == {"ok": True, "ticker": "AAA", "in_watchlist": True}
        assert rawstore.load_watchlist() == {"AAA"}
        # toggle again -> remove
        r = c.post("/api/watchlist/toggle/aaa")
        assert r.get_json()["in_watchlist"] is False
        assert rawstore.load_watchlist() == set()


class _NoKey:
    @property
    def active(self):
        return False

    def verify(self):
        from RevenueChaser.core import credentials
        return False, credentials.missing_message("unusual_whales")


class TestApiTicker:
    def _seed(self, rawstore_tmp_db, cards):
        from RevenueChaser.core import ticker_store
        ticker_store.write_from_cards(cards, asof="2026-08-23")

    def _card(self, ticker):
        return {
            "ticker": ticker, "name": f"{ticker} Inc.", "sector": "Information Technology",
            "industry": "Consumer Electronics", "price": 250.0, "market_cap": 3.7e12,
            "ps_ratio": 9.0, "rev_growth": 0.05, "gross_margin": 0.46, "fcf": 1.0e11,
            "runway_years": 999.0, "rd_intensity": 0.08, "dilution_yoy": -0.02,
            "earnings_date": 1794513600.0, "total_points": 5, "themes": [],
            "tech_breakthrough": False, "dilution_warning": False, "us_listed": True,
            "source": "test", "signals": [],
        }

    def test_api_ticker_list_200(self, rawstore_tmp_db):
        self._seed(rawstore_tmp_db, [self._card("AAPL"), self._card("MSFT")])
        c = panel_test_client(dashboard.app)
        r = c.get("/api/ticker")
        assert r.status_code == 200
        data = r.get_json()
        assert data["count"] == 2
        assert data["asof"] == "2026-08-23"
        assert {t["ticker"] for t in data["tickers"]} == {"AAPL", "MSFT"}

    def test_api_ticker_single_200(self, rawstore_tmp_db):
        self._seed(rawstore_tmp_db, [self._card("AAPL")])
        c = panel_test_client(dashboard.app)
        r = c.get("/api/ticker/AAPL")
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["data"]["ticker"] == "AAPL"
        assert data["data"]["total_points"] == 5

    def test_api_ticker_unknown_404(self, rawstore_tmp_db):
        self._seed(rawstore_tmp_db, [self._card("AAPL")])
        c = panel_test_client(dashboard.app)
        r = c.get("/api/ticker/ZZZZ")
        assert r.status_code == 404


class TestRunners:
    def test_run_universe_refresh_runs_all_three(self, monkeypatch, tmp_path):
        calls = []

        def fake(uw, **kw):
            calls.append(kw)
            return tmp_path / f"{len(calls)}.html"

        monkeypatch.setattr(runners, "run_asymmetric_growth", fake)
        monkeypatch.setattr(runners, "run_swing", fake)
        monkeypatch.setattr(runners, "run_fundamentals", fake)
        runners.run_universe_refresh(None)
        # universe board refreshes the universe; the other two reuse caches
        assert calls == [{"refresh_universe": True}, {}, {}]

    def test_stable_filenames(self):
        assert runners.EARNINGS_LATEST.name == "earnings_radar_latest.html"
        assert runners.LEAPS_LATEST.name == "leaps_latest.html"
        assert runners.ROTATION_LATEST.name == "sector_rotation_latest.html"
        assert runners.SEC_13F_LATEST.name == "sec_13f_latest.html"
        assert runners.ASYMMETRIC_GROWTH_LATEST.name == "asymmetric_growth_latest.html"
        assert runners.FUNDAMENTALS_LATEST.name == "fundamentals_latest.html"

    def test_nrc_results_computed(self):
        from RevenueChaser.module_earnings_radar.models import TradeCard
        cards = [TradeCard(ticker="AAPL", direction_score=20.0, confidence_score=65.0)]
        nrc = runners._compute_nrc_results(cards)
        assert "AAPL" in nrc
        assert "nrc" in nrc["AAPL"]
        assert "tier" in nrc["AAPL"]
        # export_nrc_html requires these keys (regression: KeyError 'growth')
        for k in ("growth", "quality", "beat", "acceleration", "watchlist"):
            assert k in nrc["AAPL"]


class TestQuickTickerCacheState:
    """Last-tick researched on the Financials tab is cached as a per-module JSON
    file under .cache/ (QUICK_STATE), not persisted to the durable store."""

    def _patch_state(self, monkeypatch, tmp_path):
        import RevenueChaser.app.runners as rn
        monkeypatch.setattr(rn, "QUICK_STATE", tmp_path / "financials" / "financials_latest.json")
        return rn

    def test_last_quick_ticker_defaults_when_no_file(self, tmp_path, monkeypatch):
        rn = self._patch_state(monkeypatch, tmp_path)
        assert rn._last_quick_ticker() == "AAPL"

    def test_remember_then_read_roundtrip(self, tmp_path, monkeypatch):
        rn = self._patch_state(monkeypatch, tmp_path)
        rn._remember_quick_ticker("msft")
        assert rn._last_quick_ticker() == "MSFT"

    def test_ticker_box_starts_at_the_last_researched_ticker(self, tmp_path, monkeypatch):
        """DF47: the box was a fixed 'AAPL' while the iframe showed the last report (e.g. MSFT)."""
        rn = self._patch_state(monkeypatch, tmp_path)
        rn._remember_quick_ticker("msft")
        body = dashboard.app.test_client().get("/").get_data(as_text=True)
        assert 'id="quick-ticker" value="MSFT"' in body

    def test_remember_uppercases_ticker_and_stamps_date(self, tmp_path, monkeypatch):
        rn = self._patch_state(monkeypatch, tmp_path)
        rn._remember_quick_ticker("nVdA")
        state = json.loads(rn.QUICK_STATE.read_text(encoding="utf-8"))
        assert state["ticker"] == "NVDA"
        assert state.get("generated_at"), "state must carry a render date"

    def test_last_quick_ticker_defaults_on_missing_key(self, tmp_path, monkeypatch):
        rn = self._patch_state(monkeypatch, tmp_path)
        rn.QUICK_STATE.parent.mkdir(parents=True, exist_ok=True)
        rn.QUICK_STATE.write_text('{"other": 1}', encoding="utf-8")
        assert rn._last_quick_ticker() == "AAPL"

    def test_last_quick_ticker_defaults_on_corrupt_json(self, tmp_path, monkeypatch):
        rn = self._patch_state(monkeypatch, tmp_path)
        rn.QUICK_STATE.parent.mkdir(parents=True, exist_ok=True)
        rn.QUICK_STATE.write_text("{not json", encoding="utf-8")
        assert rn._last_quick_ticker() == "AAPL"

    def test_state_lives_in_cache_as_per_module_json(self):
        # .cache/financials/financials_latest.json — alongside the module's artifact
        p = runners.QUICK_STATE
        assert p.name == "financials_latest.json"
        assert p.parent.name == "financials"
        assert str(runners.OUTPUT_DIR) in str(p.parent)


class TestRefreshReadsStoreFetchReadsNetwork:
    """Refresh Tab == read the store; Fetch == hit the network.

    A board tab is a pre-rendered HTML artifact in .cache/. Two buttons drive it:

      - "Refresh Tab" re-renders the board FROM THE STORE (.store/raw.duckdb).
        When the artifact is missing the /view self-heals from the store — no
        API key, no network (conftest blocks sockets, so any HTTP attempt here
        would raise).
      - "Fetch" runs the live provider scan OVER THE NETWORK (requires the UW
        API key) and writes a fresh artifact.

    These tests pin the split on the LEAPS board.
    """

    def test_leaps_refresh_renders_from_the_store_offline(self, rawstore_tmp_db,
                                                          tmp_path, monkeypatch):
        from RevenueChaser.core import rawstore
        from RevenueChaser.module_leaps.engine import save_cache
        rawstore.save_watchlist({"AAPL"})
        save_cache("AAPL", {"ticker": "AAPL", "label": "Accumulating",
                            "score": 50, "as_of": "2026-09-01"})

        # No artifact on disk -> the view must regenerate from the store.
        import RevenueChaser.app.runners as rn
        import RevenueChaser.module_leaps.report as lr
        monkeypatch.setattr(rn, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(lr, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(rn, "LEAPS_LATEST", tmp_path / "leaps_latest.html")
        monkeypatch.setattr(dashboard, "UWClient", lambda: _NoKey())  # refresh must not need a key

        body = dashboard.app.test_client().get("/view/module_leaps").get_data(as_text=True)
        assert "LEAPS ACCUMULATION RADAR" in body  # board rendered from seeded store data
        assert "AAPL" in body
        assert "No LEAPS data yet" not in body
        # network is globally blocked (conftest) — reaching here proves refresh is offline

    def test_refresh_ignores_stale_artifact_on_any_store_backed_board(
            self, rawstore_tmp_db, tmp_path, monkeypatch):
        """Refresh is a general rule, not a Financials special case: on any
        store-backed board, ?refresh=1 drops the cached html and recomputes from
        .store even when a stale artifact already exists."""
        from RevenueChaser.core import rawstore
        from RevenueChaser.module_leaps.engine import save_cache
        rawstore.save_watchlist({"AAPL"})
        save_cache("AAPL", {"ticker": "AAPL", "label": "Accumulating",
                            "score": 50, "as_of": "2026-09-01"})

        import RevenueChaser.app.runners as rn
        import RevenueChaser.module_leaps.report as lr
        monkeypatch.setattr(rn, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(lr, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(rn, "LEAPS_LATEST", tmp_path / "leaps_latest.html")
        monkeypatch.setattr(dashboard, "UWClient", lambda: _NoKey())

        # Stale artifact for a DIFFERENT state than the store holds.
        (tmp_path / "leaps_latest.html").write_text("<h1>STALE board</h1>", encoding="utf-8")

        body = dashboard.app.test_client().get("/view/module_leaps?refresh=1").get_data(as_text=True)
        assert "LEAPS ACCUMULATION RADAR" in body       # recomputed from the store
        assert "AAPL" in body
        assert "STALE board" not in body                # cached html was ignored
        fresh = (tmp_path / "leaps_latest.html").read_text(encoding="utf-8")
        assert "STALE board" not in fresh               # artifact overwritten too

    def test_leaps_fetch_is_gated_on_the_api_key(self, monkeypatch):
        # "Fetch" is a live UW scan -> without a key it is refused before any network
        monkeypatch.setattr(dashboard, "UWClient", lambda: _NoKey())
        r = panel_test_client(dashboard.app).post("/run/module_leaps")
        assert r.status_code == 400
        assert r.get_json()["ok"] is False
        # The refusal must name the key, the file, and where to get one — the
        # old "UW API key not set." left the user with nothing to act on.
        error = r.get_json()["error"]
        assert "UNUSUAL_WHALES_API_KEY" in error
        assert ".env" in error
        assert "https://unusualwhales.com/api" in error

    def test_financials_refresh_renders_from_the_store_offline(self, rawstore_tmp_db,
                                                               tmp_path, monkeypatch):
        """The Financials tab must re-render from .store when the artifact is missing.

        Data on storage (a cached yfinance payload + a ticker_scores card) must be
        enough for Refresh to produce the report — no network, no empty state.
        """
        from RevenueChaser.core import rawstore
        from RevenueChaser.core import ticker_store
        from RevenueChaser.core.providers import cache as cache_manager

        # seed a ticker_scores card (quick remarks read it first — no network)
        ticker_store.write_from_cards([{
            "ticker": "AAPL", "name": "Apple Inc.", "sector": "Technology",
            "industry": "Consumer Electronics", "price": 250.0, "market_cap": 3.7e12,
            "ps_ratio": 9.0, "rev_growth": 0.05, "gross_margin": 0.46, "fcf": 1.0e11,
            "runway_years": 999.0, "rd_intensity": 0.08, "dilution_yoy": -0.02,
            "earnings_date": 1794513600.0, "total_points": 5, "themes": [],
            "signals": [], "source": "test",
        }], asof="2026-08-23")

        # seed the cached yfinance payload financials.run consumes (ignore_ttl read)
        cache_manager.save("AAPL", {
            "ticker": "AAPL", "sector": "Technology", "shares_outstanding": 15.0e9,
            "market_cap": 3.7e12, "current_price": 250.0, "currency": "USD", "source": "test",
            "info": {"longName": "Apple Inc.", "shortName": "Apple"},
            "annual": [
                {"fiscal_year": "2021", "revenue": 365.0e9, "ebitda": 100.0e9, "ebit": 90.0e9,
                 "depreciation": 11.0e9, "capex": 11.0e9, "net_income": 95.0e9,
                 "total_debt": 120.0e9, "cash": 35.0e9, "total_assets": 350.0e9,
                 "total_equity": 63.0e9, "receivables": 30.0e9, "inventory": 5.0e9,
                 "payables": 50.0e9, "current_assets": 135.0e9, "current_liabilities": 130.0e9,
                 "interest_expense": 2.5e9, "pretax_income": 110.0e9, "tax_provision": 14.0e9},
                {"fiscal_year": "2022", "revenue": 395.0e9, "ebitda": 115.0e9, "ebit": 105.0e9,
                 "depreciation": 11.0e9, "capex": 11.0e9, "net_income": 100.0e9,
                 "total_debt": 110.0e9, "cash": 25.0e9, "total_assets": 350.0e9,
                 "total_equity": 50.0e9, "receivables": 30.0e9, "inventory": 5.0e9,
                 "payables": 50.0e9, "current_assets": 135.0e9, "current_liabilities": 130.0e9,
                 "interest_expense": 2.5e9, "pretax_income": 120.0e9, "tax_provision": 14.0e9},
                {"fiscal_year": "2023", "revenue": 385.0e9, "ebitda": 110.0e9, "ebit": 100.0e9,
                 "depreciation": 11.0e9, "capex": 11.0e9, "net_income": 97.0e9,
                 "total_debt": 110.0e9, "cash": 30.0e9, "total_assets": 350.0e9,
                 "total_equity": 60.0e9, "receivables": 30.0e9, "inventory": 5.0e9,
                 "payables": 50.0e9, "current_assets": 135.0e9, "current_liabilities": 130.0e9,
                 "interest_expense": 2.5e9, "pretax_income": 115.0e9, "tax_provision": 14.0e9},
            ],
        })
        # No artifact on disk -> the view must regenerate from the store.
        import json
        import RevenueChaser.app.runners as rn
        import RevenueChaser.module_financials.run as fm
        monkeypatch.setattr(rn, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(rn, "QUICK_LATEST", tmp_path / "financials_latest.html")
        monkeypatch.setattr(rn, "QUICK_STATE", tmp_path / "financials" / "financials_latest.json")
        rn.QUICK_STATE.parent.mkdir(parents=True, exist_ok=True)
        rn.QUICK_STATE.write_text(json.dumps({"ticker": "AAPL"}), encoding="utf-8")
        monkeypatch.setattr(dashboard, "UWClient", lambda: _NoKey())  # refresh must not need a key

        body = dashboard.app.test_client().get("/view/module_financials").get_data(as_text=True)
        assert "Apple Inc." in body or "AAPL" in body  # report rendered from seeded store data
        assert "No Financials data yet" not in body
        # network is globally blocked (conftest) — reaching here proves refresh is offline

    def test_financials_view_refresh_param_recomputes_over_stale_artifact(
            self, rawstore_tmp_db, tmp_path, monkeypatch):
        """GET /view/module_financials?refresh=1&ticker=... must recompute from
        .store even when a stale .cache artifact exists (the old refresh bug)."""
        import RevenueChaser.app.runners as rn
        import RevenueChaser.module_financials.run as fm
        from RevenueChaser.core.providers import cache as cache_manager

        # Stale artifact on disk for a different ticker + raw payload in store.
        monkeypatch.setattr(rn, "QUICK_LATEST", tmp_path / "financials_latest.html")
        monkeypatch.setattr(rn, "QUICK_STATE", tmp_path / "financials" / "financials_latest.json")
        monkeypatch.setattr(rn, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
        (tmp_path / "financials_latest.html").write_text("<h1>STALE Mongo</h1>", encoding="utf-8")
        cache_manager.save("AAPL", {"ticker": "AAPL", "annual": [], "info": {"longName": "AAPL Inc"}})

        def fake_run(ticker, modules=None, output="html", use_cache=True, no_fetch=False,
                     assumptions=None, peers=None, report_path=None, run_id=None):
            assert no_fetch is True
            path = tmp_path / f"{ticker}_report.html"
            path.write_text(f"<h1>{ticker} Apple</h1>", encoding="utf-8")
            return {"paths": {"html": str(path)}, "results": {}}

        monkeypatch.setattr(fm, "run", fake_run)
        monkeypatch.setattr(dashboard, "UWClient", lambda: _NoKey())

        body = dashboard.app.test_client().get(
            "/view/module_financials?refresh=1&ticker=AAPL").get_data(as_text=True)
        assert "AAPL Apple" in body
        assert "STALE Mongo" not in body
        fresh = (tmp_path / "financials_latest.html").read_text(encoding="utf-8")
        assert "STALE Mongo" not in fresh
        state = json.loads((tmp_path / "financials" / "financials_latest.json").read_text(encoding="utf-8"))
        assert state["ticker"] == "AAPL"

    def test_financials_refresh_updates_state_json_before_serving(
            self, rawstore_tmp_db, tmp_path, monkeypatch):
        """Switching ticker must rewrite financials_latest.json before the HTML
        is served, so Data-as-of / the next load cannot still see the old ticker."""
        import RevenueChaser.app.runners as rn
        import RevenueChaser.module_financials.run as fm
        from RevenueChaser.core.providers import cache as cache_manager

        monkeypatch.setattr(rn, "QUICK_LATEST", tmp_path / "financials_latest.html")
        monkeypatch.setattr(rn, "QUICK_STATE", tmp_path / "financials" / "financials_latest.json")
        monkeypatch.setattr(rn, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
        rn.QUICK_STATE.parent.mkdir(parents=True, exist_ok=True)
        rn.QUICK_STATE.write_text(json.dumps({"ticker": "AAPL"}), encoding="utf-8")
        (tmp_path / "financials_latest.html").write_text("<h1>AAPL</h1>", encoding="utf-8")
        cache_manager.save("AAOI", {"ticker": "AAOI", "annual": [], "info": {"longName": "AAOI Inc"}})

        def fake_run(ticker, modules=None, output="html", use_cache=True, no_fetch=False,
                     assumptions=None, peers=None, report_path=None, run_id=None):
            path = tmp_path / f"{ticker}_report.html"
            path.write_text(f"<h1>{ticker}</h1>", encoding="utf-8")
            return {"paths": {"html": str(path)}, "results": {}}

        monkeypatch.setattr(fm, "run", fake_run)
        monkeypatch.setattr(dashboard, "UWClient", lambda: _NoKey())

        body = dashboard.app.test_client().get(
            "/view/module_financials?refresh=1&ticker=AAOI").get_data(as_text=True)
        assert "<h1>AAOI</h1>" in body
        state = json.loads(rn.QUICK_STATE.read_text(encoding="utf-8"))
        assert state["ticker"] == "AAOI"


    def test_financials_fetch_is_live_and_remembers_ticker(self, rawstore_tmp_db, monkeypatch,
                                                           tmp_path):
        """Fetch = live per-ticker run (no_fetch False); Refresh = store-only (no_fetch True).

        The researched ticker is remembered so Refresh can self-heal offline for it.
        """
        import RevenueChaser.app.runners as rn
        import RevenueChaser.module_financials.run as fm

        calls = {}

        def fake_run(ticker, modules=None, output="html", use_cache=True, no_fetch=False,
                     assumptions=None, peers=None, report_path=None, run_id=None):
            calls["ticker"] = ticker
            calls["no_fetch"] = no_fetch
            path = tmp_path / f"{ticker}_report.html"
            path.write_text(f"<h1>{ticker} report</h1>", encoding="utf-8")
            return {"paths": {"html": str(path)}, "results": {}}

        monkeypatch.setattr(rn, "QUICK_LATEST", tmp_path / "financials_latest.html")
        monkeypatch.setattr(rn, "QUICK_STATE", tmp_path / "financials" / "financials_latest.json")
        monkeypatch.setattr(rn, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(fm, "run", fake_run)

        # Fetch -> live path: no_fetch must be False and ticker remembered
        p = rn.run_quick(None, "MSFT")
        assert p == tmp_path / "financials_latest.html"
        assert calls["ticker"] == "MSFT"
        assert calls["no_fetch"] is False
        import json
        state = json.loads(rn.QUICK_STATE.read_text(encoding="utf-8"))
        assert state["ticker"] == "MSFT"

        # Refresh -> store-only path: no_fetch must be True (same remembered ticker)
        # it first requires a cached payload in the store, else it early-returns
        from RevenueChaser.core.providers import cache as cache_manager
        cache_manager.save("MSFT", {"ticker": "MSFT", "annual": [], "info": {"longName": "MSFT Inc"}})
        p2 = rn.run_quick_cached()
        assert calls["ticker"] == "MSFT"
        assert calls["no_fetch"] is True

    def test_financials_refresh_drops_stale_html_and_recomputes(self, rawstore_tmp_db,
                                                                tmp_path, monkeypatch):
        """Refresh ignores any existing .cache artifact and recomputes from .store.

        Regression: with an MDB report already on disk, Refresh used to serve the
        stale html as-is. It must recompute the box ticker from raw.duckdb instead.
        """
        import RevenueChaser.app.runners as rn
        import RevenueChaser.module_financials.run as fm

        monkeypatch.setattr(rn, "QUICK_LATEST", tmp_path / "financials_latest.html")
        monkeypatch.setattr(rn, "QUICK_STATE", tmp_path / "financials" / "financials_latest.json")
        monkeypatch.setattr(rn, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)

        # A stale artifact exists for a DIFFERENT ticker (the old bug).
        (tmp_path / "financials_latest.html").write_text("<h1>STALE MongoDB</h1>", encoding="utf-8")

        def fake_run(ticker, modules=None, output="html", use_cache=True, no_fetch=False,
                     assumptions=None, peers=None, report_path=None, run_id=None):
            assert no_fetch is True, "Refresh must never touch the network"
            path = tmp_path / f"{ticker}_report.html"
            path.write_text(f"<h1>{ticker} FRESH</h1>", encoding="utf-8")
            return {"paths": {"html": str(path)}, "results": {}}

        monkeypatch.setattr(fm, "run", fake_run)
        from RevenueChaser.core.providers import cache as cache_manager
        cache_manager.save("AAPL", {"ticker": "AAPL", "annual": [], "info": {"longName": "Apple"}})

        # Refresh -> recompute AAPL from the store, replacing the stale html.
        p = rn.run_quick_cached(ticker="AAPL")
        assert p == tmp_path / "financials_latest.html"
        body = (tmp_path / "financials_latest.html").read_text(encoding="utf-8")
        assert "AAPL FRESH" in body
        assert "STALE MongoDB" not in body

    def test_financials_refresh_without_raw_payload_drops_artifact(self, rawstore_tmp_db,
                                                                   tmp_path, monkeypatch):
        """A store-only refresh for a ticker with NO raw payload must drop the
        stale artifact and return None, so the view shows an empty state rather
        than a report for the wrong ticker."""
        import RevenueChaser.app.runners as rn
        monkeypatch.setattr(rn, "QUICK_LATEST", tmp_path / "financials_latest.html")
        monkeypatch.setattr(rn, "QUICK_STATE", tmp_path / "financials" / "financials_latest.json")
        (tmp_path / "financials_latest.html").write_text("<h1>STALE</h1>", encoding="utf-8")

        assert rn.run_quick_cached(ticker="ZZZZ") is None
        assert not (tmp_path / "financials_latest.html").exists()

    def test_financials_refresh_render_error_is_displayed_not_empty(self, rawstore_tmp_db,
                                                                    tmp_path, monkeypatch):
        """A store-only refresh that raises must surface an explicit error board,
        not silently return None (empty state -> network Fetch).

        Regression: run_quick_cached swallowed every fm_run exception with a
        warning and fell back to stale/None, which is how a genuine re-render
        failure (e.g. a cleared .cache write error) emptied the Financials tab
        and pushed the app back over the network even though the data was in the
        store.
        """
        import RevenueChaser.app.runners as rn
        import RevenueChaser.module_financials.run as fm
        from RevenueChaser.core.providers import cache as cache_manager

        monkeypatch.setattr(rn, "QUICK_LATEST", tmp_path / "financials_latest.html")
        monkeypatch.setattr(rn, "QUICK_STATE", tmp_path / "financials" / "financials_latest.json")
        monkeypatch.setattr(rn, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
        cache_manager.save("AAPL", {"ticker": "AAPL", "annual": [], "info": {"longName": "AAPL Inc"}})

        def boom(ticker, modules=None, output="html", use_cache=True, no_fetch=False,
                 assumptions=None, peers=None, report_path=None, run_id=None):
            raise PermissionError("cannot write AAPL_report.html: cache not writable")

        monkeypatch.setattr(fm, "run", boom)

        p = rn.run_quick_cached(ticker="AAPL")
        assert p is not None
        assert p == tmp_path / "financials_latest.html"
        body = p.read_text(encoding="utf-8")
        assert "Refresh failed" in body
        assert "PermissionError" in body
        assert "AAPL_report.html" in body

    def test_financials_refresh_render_error_overwrites_stale_artifact(self, rawstore_tmp_db,
                                                                       tmp_path, monkeypatch):
        """When the store-only render fails the stale artifact must be replaced
        by the error board — never silently served as if it were fresh."""
        import RevenueChaser.app.runners as rn
        import RevenueChaser.module_financials.run as fm
        from RevenueChaser.core.providers import cache as cache_manager

        monkeypatch.setattr(rn, "QUICK_LATEST", tmp_path / "financials_latest.html")
        monkeypatch.setattr(rn, "QUICK_STATE", tmp_path / "financials" / "financials_latest.json")
        monkeypatch.setattr(rn, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
        (tmp_path / "financials_latest.html").write_text("<h1>STALE AAPL report</h1>", encoding="utf-8")
        cache_manager.save("AAPL", {"ticker": "AAPL", "annual": [], "info": {"longName": "AAPL Inc"}})

        def boom(ticker, modules=None, output="html", use_cache=True, no_fetch=False,
                 assumptions=None, peers=None, report_path=None, run_id=None):
            raise OSError("[Errno 2] No such file or directory: '.cache/AAPL_report.html'")

        monkeypatch.setattr(fm, "run", boom)

        p = rn.run_quick_cached(ticker="AAPL")
        assert p is not None
        body = p.read_text(encoding="utf-8")
        assert "STALE AAPL report" not in body
        assert "Refresh failed" in body
        assert "AAPL_report.html" in body


class TestComovementRefresh:
    """Changing Horizon / Remove market on Co-movement is a store-only Refresh.

    The pill click reloads the iframe with ``?refresh=1&horizon=..&benchmark=..``;
    the /view route must forward both through to ``engine.run`` so the board
    reclusters residuals at the requested settings with zero network.
    """

    def test_refresh_honors_horizon_and_benchmark_query(self, monkeypatch, tmp_path):
        import RevenueChaser.app.runners as rn
        import RevenueChaser.module_comovement.engine as eng

        captured = {}

        def fake_run(no_fetch=False, tickers=None, cuts=None,
                     horizon=None, benchmark=None, window=None):
            captured.update(no_fetch=no_fetch, horizon=horizon, benchmark=benchmark,
                            window=window)
            return {"as_of": "2026-09-17T00:00:00+00:00", "generated_at": "x",
                    "universe_total": 0, "eligible": 0, "dropped": [],
                    "cuts": [], "clusters": {}, "fetch": {}, "config_hash": None,
                    "horizon": horizon, "benchmark": benchmark,
                    "benchmark_applied": False, "themes": []}

        monkeypatch.setattr(eng, "run", fake_run)
        monkeypatch.setattr(rn, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(rn, "COMOVEMENT_LATEST", tmp_path / "comovement_latest.html")

        c = panel_test_client(dashboard.app)
        r = c.get("/view/module_comovement?refresh=1&horizon=3&benchmark=qqq")
        assert r.status_code == 200
        assert captured == {"no_fetch": True, "horizon": 3, "benchmark": "qqq",
                            "window": None}

    def test_refresh_forwards_a_valid_window_and_rejects_others(self, monkeypatch, tmp_path):
        import RevenueChaser.app.runners as rn
        import RevenueChaser.module_comovement.engine as eng

        seen = []

        def fake_run(no_fetch=False, tickers=None, cuts=None,
                     horizon=None, benchmark=None, window=None):
            seen.append(window)
            return {"as_of": "x", "generated_at": "x", "universe_total": 0,
                    "eligible": 0, "dropped": [], "cuts": [], "clusters": {},
                    "fetch": {}, "config_hash": None, "horizon": 1,
                    "benchmark": "spy", "benchmark_applied": False, "themes": []}

        monkeypatch.setattr(eng, "run", fake_run)
        monkeypatch.setattr(rn, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(rn, "COMOVEMENT_LATEST", tmp_path / "comovement_latest.html")
        c = panel_test_client(dashboard.app)
        assert c.get("/view/module_comovement?refresh=1&window=42").status_code == 200
        assert seen == [42]
        assert c.get("/view/module_comovement?refresh=1&window=100").status_code == 400
        assert c.get("/view/module_comovement?refresh=1&window=abc").status_code == 400
        assert seen == [42]


class TestOipdParentChrome:
    """Step 7: OIPD's ticker picker lives in the iframe (in-board combobox).

    The parent header box is retired; Fetch / Refresh read the selected value
    from the iframe instead.
    """

    def _body(self):
        dashboard.app.config["TESTING"] = True
        return dashboard.app.test_client().get("/").get_data(as_text=True)

    def test_header_ticker_box_is_gone(self):
        body = self._body()
        assert 'id="oipd-ticker"' not in body
        assert 'id="oipd-ctl"' not in body

    def test_fetch_reads_the_iframe_selected_ticker(self):
        body = self._body()
        assert "contentWindow" in body
        assert "OIPD_TICKER" in body
        assert "getElementById('oipd-ticker')" not in body

    def test_refresh_uses_the_iframe_ticker(self):
        body = self._body()
        assert "refresh=1" in body
        assert "&ticker=" in body
        assert "OIPD_TICKER" in body


class TestEdgarSync:
    """`sync_filings` (new 10-K/20-F detection) is triggered by a dashboard job.

    Value Chain / Relatedness Fetch stay cache-first; this job is the only path
    that recrawls names that already have a filing, so it must be SEC-only
    (``requires_uw=False``) and re-render the Value Chain board from the store
    with zero network once the sync finishes.
    """

    def test_registered_no_uw_and_produces_value_chain(self):
        from RevenueChaser.app import jobs
        assert "edgar_sync" in jobs._RUNNERS
        assert jobs._REQUIRES_UW.get("edgar_sync") is False
        assert jobs._PRODUCES.get("edgar_sync") == ["module_value_chain"]

    def test_run_edgar_sync_route_submits(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        captured = {}
        monkeypatch.setattr(
            d.jobs, "submit",
            lambda module, params: captured.update(module=module, params=params) or "stubjob")
        c = panel_test_client(dashboard.app)
        r = c.post("/run/edgar-sync", data={"limit": "5", "force": "1"})
        assert r.status_code == 202
        assert r.get_json()["job_id"] == "stubjob"
        assert captured["module"] == "edgar_sync"
        # #185: the route no longer accepts a force field (a forced re-slice is a separate dry run).
        assert captured["params"] == {"limit": 5}

    def test_run_edgar_sync_route_bad_limit_400_and_no_submit(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        submitted = []
        monkeypatch.setattr(d.jobs, "submit",
                            lambda module, params: submitted.append(module) or "stubjob")
        c = panel_test_client(dashboard.app)
        r = c.post("/run/edgar-sync", data={"limit": "abc"})
        assert r.status_code == 400
        assert r.get_json()["ok"] is False
        assert submitted == []

    def test_run_edgar_sync_reports_counts_and_rerenders_board(self, monkeypatch, tmp_path):
        from RevenueChaser.app.jobs import JobMessage
        from RevenueChaser.module_value_chain import sync

        monkeypatch.setattr(sync, "sync_filings", lambda **kw: {
            "requested": 3, "updated": 1, "current": 2, "no_filing": 0, "failed": 0})

        vc = tmp_path / "value_chain_latest.html"
        monkeypatch.setattr(runners, "VALUE_CHAIN_LATEST", vc)
        seen = {}

        def fake_vc(uw=None, no_fetch=False):
            seen["no_fetch"] = no_fetch
            vc.write_text("<html>VC</html>", encoding="utf-8")
            return vc

        monkeypatch.setattr(runners, "run_value_chain", fake_vc)

        with pytest.raises(JobMessage) as exc:
            runners.run_edgar_sync(limit=3)

        msg = exc.value.message
        assert "1 filing updated" in msg
        assert "2 already current" in msg
        assert "0 with no 10-K/20-F" in msg
        assert "(3 checked)" in msg
        assert "failed" not in msg
        assert seen["no_fetch"] is True, "the board must re-render from the store"
        assert vc.exists()

    def test_run_edgar_sync_reports_failed_only_when_nonzero(self, monkeypatch, tmp_path):
        from RevenueChaser.app.jobs import JobMessage
        from RevenueChaser.module_value_chain import sync

        monkeypatch.setattr(sync, "sync_filings", lambda **kw: {
            "requested": 4, "updated": 0, "current": 3, "no_filing": 0, "failed": 1})
        monkeypatch.setattr(runners, "run_value_chain",
                            lambda uw=None, no_fetch=False: tmp_path / "vc.html")

        with pytest.raises(JobMessage) as exc:
            runners.run_edgar_sync()

        assert "1 failed" in exc.value.message

    def test_run_edgar_sync_reports_kept_40f_known_miss_and_no_cik(self, monkeypatch, tmp_path):
        from RevenueChaser.app.jobs import JobMessage
        from RevenueChaser.module_value_chain import sync

        monkeypatch.setattr(sync, "sync_filings", lambda **kw: {
            "requested": 20, "updated": 1, "current": 5, "no_filing": 2, "failed": 0,
            "kept": 3, "form_40f": 1, "known_miss": 4, "no_cik": 6})
        monkeypatch.setattr(runners, "run_value_chain",
                            lambda uw=None, no_fetch=False: tmp_path / "vc.html")

        with pytest.raises(JobMessage) as exc:
            runners.run_edgar_sync()

        msg = exc.value.message
        assert "(10 checked)" in msg, "checked = requested - no_cik - known_miss"
        assert "3 kept" in msg
        assert "1 40-F" in msg
        assert "4 recorded misses skipped" in msg
        assert "6 with no CIK" in msg

    def test_run_edgar_sync_omits_absent_counts(self, monkeypatch, tmp_path):
        from RevenueChaser.app.jobs import JobMessage
        from RevenueChaser.module_value_chain import sync

        monkeypatch.setattr(sync, "sync_filings", lambda **kw: {
            "requested": 3, "updated": 1, "current": 2, "no_filing": 0, "failed": 0})
        monkeypatch.setattr(runners, "run_value_chain",
                            lambda uw=None, no_fetch=False: tmp_path / "vc.html")
        with pytest.raises(JobMessage) as exc:
            runners.run_edgar_sync()
        for word in ("kept", "40-F", "skipped", "no CIK"):
            assert word not in exc.value.message

    def test_run_edgar_sync_locked_does_not_rerender(self, monkeypatch):
        from RevenueChaser.app.jobs import JobMessage
        from RevenueChaser.module_value_chain import sync

        monkeypatch.setattr(sync, "sync_filings", lambda **kw: {"locked": True})
        rendered = []
        monkeypatch.setattr(runners, "run_value_chain", lambda *a, **k: rendered.append(1))

        with pytest.raises(JobMessage) as exc:
            runners.run_edgar_sync()

        assert "already running" in exc.value.message
        assert rendered == [], "a locked crawl must not touch the board"


class TestSettingsDataHealthBoard:
    """Settings / Data health: store-only inventory; Refresh never hits a provider."""

    def _client(self):
        dashboard.app.config["TESTING"] = True
        return panel_test_client(dashboard.app)

    def test_settings_tab_is_registered(self):
        keys = {t["key"] for t in dashboard.TABS if t["type"] == "tab"}
        assert "module_settings" in keys
        assert any(t["type"] == "header" and t["label"] == "Ops"
                   for t in dashboard.TABS)
        assert "module_settings" in dashboard._MODEL_ONLY_RENDERERS
        assert "module_settings" in dashboard._STORE_REFRESHABLE
        assert dashboard._module_file("module_settings") == runners.SETTINGS_LATEST
        assert hasattr(runners, "run_settings")

    def test_view_settings_renders_the_inventory(self, rawstore_tmp_db):
        from RevenueChaser.core import rawstore
        rawstore.write_rows("equity_universe",
                            [{"ticker": "AAPL", "market_cap": 3.0e9}])
        r = self._client().get("/view/module_settings")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Settings / Data health" in body
        assert "equity_universe" in body

    def test_refresh_rerenders_from_store_without_network(
            self, rawstore_tmp_db, monkeypatch):
        from RevenueChaser.core import rawstore
        from RevenueChaser.core.providers import sec_edgar

        rawstore.write_rows("equity_universe",
                            [{"ticker": "AAPL", "market_cap": 3.0e9}])
        runners.SETTINGS_LATEST.unlink(missing_ok=True)

        def boom(*args, **kwargs):
            raise AssertionError("settings refresh must not hit the network")

        monkeypatch.setattr(sec_edgar, "get_json", boom)
        monkeypatch.setattr(sec_edgar, "get_text", boom)

        r = self._client().get("/view/module_settings?refresh=1")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Settings / Data health" in body
        assert "equity_universe" in body
        assert runners.SETTINGS_LATEST.exists()


class TestUniverseStats:
    """Standalone universe-stats job: fill null caps only — no screener, no scanners."""

    def _client(self):
        dashboard.app.config["TESTING"] = True
        return panel_test_client(dashboard.app)

    @staticmethod
    def _fake_ticker(calls):
        class _FastInfo:
            shares = 1.0e9
            last_price = 10.0

        class _Ticker:
            def __init__(self, ticker):
                calls.append(ticker)

            @property
            def fast_info(self):
                return _FastInfo()

        return _Ticker

    @staticmethod
    def _counts(**over):
        base = {"requested": 0, "filled": 0, "skipped": 0, "failed": 0,
                "deferred": 0, "rate_limited": False}
        base.update(over)
        return base

    def test_registered_no_uw_and_produces_settings(self):
        from RevenueChaser.app import jobs
        assert "universe_stats" in jobs._RUNNERS
        assert jobs._REQUIRES_UW.get("universe_stats") is False
        assert jobs._PRODUCES.get("universe_stats") == ["module_settings"]

    def test_runs_on_the_long_lane_with_the_universe_refresh(self):
        # Both jobs call backfill_universe_caps (Yahoo) and write equity_universe.
        # The same lane means one worker drains them sequentially, so they can
        # never run concurrently (double request rate, snapshot/replace_all race)
        # and the multi-minute backfill no longer blocks short Fetches.
        from RevenueChaser.app import jobs
        assert jobs._LANE["universe_stats"] == "long"
        assert jobs._LANE["universe"] == "long"

    def test_route_submits(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        captured = {}
        monkeypatch.setattr(
            d.jobs, "submit",
            lambda module, params: captured.update(module=module, params=params) or "stubjob")
        c = self._client()
        r = c.post("/run/universe-stats", data={"limit": "5"})
        assert r.status_code == 202
        assert r.get_json()["job_id"] == "stubjob"
        assert captured["module"] == "universe_stats"
        assert captured["params"] == {"limit": 5}

    def test_route_bad_limit_400_and_no_submit(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        submitted = []
        monkeypatch.setattr(d.jobs, "submit",
                            lambda module, params: submitted.append(module) or "stubjob")
        c = self._client()
        for bad in ("abc", "0"):
            r = c.post("/run/universe-stats", data={"limit": bad})
            assert r.status_code == 400
            assert r.get_json()["ok"] is False
        assert submitted == []

    def test_runner_never_calls_screen_or_scanners(self, monkeypatch, rawstore_tmp_db):
        import yfinance as yf
        from RevenueChaser.app.jobs import JobMessage
        from RevenueChaser.core import rawstore

        rawstore.write_rows("equity_universe", [{"ticker": "MU", "market_cap": None}])
        calls = []
        monkeypatch.setattr(yf, "Ticker", self._fake_ticker(calls))
        monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
        screen_calls = []

        def boom(*_a, **_k):
            raise AssertionError(
                "the stats job must not call the screener or the scanners")

        monkeypatch.setattr(yf, "screen",
                            lambda *_a, **_k: screen_calls.append(1) or boom())
        monkeypatch.setattr(runners, "run_asymmetric_growth", boom)
        monkeypatch.setattr(runners, "run_swing", boom)
        monkeypatch.setattr(runners, "run_fundamentals", boom)

        with pytest.raises(JobMessage) as exc:
            runners.run_universe_stats()

        assert screen_calls == [], "the stats job must not call yf.screen"
        assert "1 caps filled" in exc.value.message
        assert calls == ["MU"]
        assert rawstore.get("equity_universe", "MU")["market_cap"] == pytest.approx(1e10)

    def test_runner_reports_counts_via_jobmessage(self, monkeypatch, rawstore_tmp_db):
        from RevenueChaser.app.jobs import JobMessage
        from RevenueChaser.core import universe_fetch

        monkeypatch.setattr(universe_fetch, "backfill_universe_caps",
                            lambda **kw: self._counts(
                                requested=4, filled=2, skipped=1, deferred=1))
        monkeypatch.setattr(runners, "run_settings", lambda uw=None: None)

        with pytest.raises(JobMessage) as exc:
            runners.run_universe_stats(limit=4)

        msg = exc.value.message
        assert "2 caps filled" in msg
        assert "1 skipped" in msg
        assert "0 failed" in msg
        assert "1 still null" in msg
        assert "(4 checked)" in msg
        assert "rate-limited" not in msg

    def test_runner_reports_rate_limited_suffix(self, monkeypatch, rawstore_tmp_db):
        from RevenueChaser.app.jobs import JobMessage
        from RevenueChaser.core import universe_fetch

        monkeypatch.setattr(universe_fetch, "backfill_universe_caps",
                            lambda **kw: self._counts(requested=2, deferred=2,
                                                      rate_limited=True))
        monkeypatch.setattr(runners, "run_settings", lambda uw=None: None)

        with pytest.raises(JobMessage) as exc:
            runners.run_universe_stats()

        assert "rate-limited, stopped early; run again to resume" in exc.value.message

    def test_runner_reports_locked_and_writes_nothing(self, monkeypatch, rawstore_tmp_db):
        """#359: another writer holds the universe lock -> say so, do not report a run."""
        from RevenueChaser.app.jobs import JobMessage
        from RevenueChaser.core import universe_fetch

        monkeypatch.setattr(universe_fetch, "backfill_universe_caps",
                            lambda **kw: self._counts(locked=4242))
        rerendered = []
        monkeypatch.setattr(runners, "run_settings", lambda uw=None: rerendered.append(1))

        with pytest.raises(JobMessage) as exc:
            runners.run_universe_stats()

        msg = exc.value.message
        assert "locked" in msg and "4242" in msg
        assert "nothing written" in msg
        assert "caps filled" not in msg
        assert rerendered == []

    def test_runner_reports_locked_when_the_owner_is_unknown(self, monkeypatch, rawstore_tmp_db):
        """#359: an unknown lock owner comes back as locked == 0, which is still a lock."""
        from RevenueChaser.app.jobs import JobMessage
        from RevenueChaser.core import universe_fetch

        monkeypatch.setattr(universe_fetch, "backfill_universe_caps",
                            lambda **kw: self._counts(locked=0))
        rerendered = []
        monkeypatch.setattr(runners, "run_settings", lambda uw=None: rerendered.append(1))

        with pytest.raises(JobMessage) as exc:
            runners.run_universe_stats()

        msg = exc.value.message
        assert "locked, nothing written" in msg
        assert "pid" not in msg
        assert "caps filled" not in msg
        assert rerendered == []

    def test_runner_rerenders_settings(self, monkeypatch, rawstore_tmp_db):
        from RevenueChaser.app.jobs import JobMessage
        from RevenueChaser.core import rawstore
        from RevenueChaser.core import universe_fetch

        rawstore.write_rows("equity_universe", [{"ticker": "AAPL", "market_cap": 3.0e9}])
        monkeypatch.setattr(universe_fetch, "backfill_universe_caps", lambda **kw: self._counts())
        runners.SETTINGS_LATEST.unlink(missing_ok=True)

        with pytest.raises(JobMessage):
            runners.run_universe_stats()

        assert runners.SETTINGS_LATEST.exists()


class TestSettingsDrillApi:
    """GET /api/settings/ticker/<t>: the store-only per-ticker drill."""

    def _client(self):
        dashboard.app.config["TESTING"] = True
        return panel_test_client(dashboard.app)

    @staticmethod
    def _seed():
        from RevenueChaser.core import rawstore
        rawstore.write_rows("equity_universe", [
            {"ticker": "MU", "market_cap": None, "name": "Micron Technology"},
        ])
        rawstore.write_rows("chain_filings", [
            {"ticker": "MU", "cik": "0000723125", "form": "10-K",
             "filed": "2026-02-01", "accession": "acc-mu", "quality": "ok"},
        ])

    def test_200_shape(self, rawstore_tmp_db):
        self._seed()
        r = self._client().get("/api/settings/ticker/MU")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["ticker"] == "MU"
        assert body["name"] == "Micron Technology"
        assert body["market_cap"] is None
        assert body["filing"]["form"] == "10-K"
        assert body["presence"]["in_classification_universe"] is False

    def test_unknown_ticker_404(self, rawstore_tmp_db):
        self._seed()
        r = self._client().get("/api/settings/ticker/ZZZZ")
        assert r.status_code == 404
        assert r.get_json() == {"ok": False}

    def test_case_insensitive(self, rawstore_tmp_db):
        self._seed()
        r = self._client().get("/api/settings/ticker/mu")
        assert r.status_code == 200
        assert r.get_json()["ticker"] == "MU"


class TestPerTabOverlay:
    """PL3 / N2: "Running…" covers only the tab a queued/running job produces.

    It used to stay up until no job was active in either lane, so a long-lane
    job (13F ingest, holdings) blocked every board.
    """

    def _body(self):
        dashboard.app.config["TESTING"] = True
        return dashboard.app.test_client().get("/").get_data(as_text=True)

    def test_overlay_is_driven_by_the_active_tab(self):
        body = self._body()
        assert "function jobsForTab(" in body
        assert "function syncOverlay(" in body
        assert "jobsForTab(ACTIVE_JOBS, ACTIVE)" in body

    def test_no_global_overlay_on_any_active_job(self):
        body = self._body()
        # The page-load path follows jobs but no longer blankets every board.
        assert "if(any){ showOverlay(true); startPolling(); }" not in body
        # poll() and showTab() both re-evaluate the overlay for the active tab.
        poll = body[body.index("function poll(){"):body.index("function startPolling(){")]
        assert "syncOverlay()" in poll
        show_tab = body[body.index("function showTab(key){"):body.index("var CAL_WEEKS")]
        assert "syncOverlay()" in show_tab

    def test_jobs_for_tab_logic_under_node(self, tmp_path):
        import re
        import shutil
        import subprocess
        if shutil.which("node") is None:
            pytest.skip("node not on PATH")
        body = self._body()
        fns = [re.search(r"function %s\(.*?\n}\n" % n, body, re.S).group(0)
               for n in ("activeJobs", "jobsForTab")]
        script = "\n".join(fns) + """
var jobs = [
  {id:'a', status:'running', produces:['module_sec_13f']},
  {id:'b', status:'queued',  produces:['module_leaps']},
  {id:'c', status:'done',    produces:['module_oipd']},
];
console.log(JSON.stringify({
  sec: jobsForTab(jobs, 'module_sec_13f').map(function(j){return j.id;}),
  leaps: jobsForTab(jobs, 'module_leaps').map(function(j){return j.id;}),
  oipd: jobsForTab(jobs, 'module_oipd').length,
  earn: jobsForTab(jobs, 'module_earnings_radar').length,
  none: jobsForTab([{status:'running'}], 'module_leaps').length
}));
"""
        p = tmp_path / "overlay.js"
        p.write_text(script, encoding="utf-8")
        out = subprocess.run(["node", str(p)], capture_output=True, text=True,
                             encoding="utf-8", check=True)
        res = json.loads(out.stdout.strip().splitlines()[-1])
        assert res == {"sec": ["a"], "leaps": ["b"], "oipd": 0, "earn": 0, "none": 0}


class TestIdentityJob:
    """``POST /run/identity``: the long-lane identity refresh (#85). The Settings Fetch identity button posts it (#89)."""

    def _client(self):
        dashboard.app.config["TESTING"] = True
        return panel_test_client(dashboard.app)

    def test_registered_long_lane_no_uw(self):
        from RevenueChaser.app import jobs
        assert "identity" in jobs._RUNNERS
        assert jobs._REQUIRES_UW.get("identity") is False
        assert jobs._LANE["identity"] == "long"
        assert jobs._PRODUCES.get("identity") == ["module_settings"]

    def test_route_submits_with_params(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        captured = {}
        monkeypatch.setattr(d.jobs, "submit",
                            lambda module, params: captured.update(module=module, params=params) or "stubjob")
        r = self._client().post("/run/identity", data={"limit": "5"})
        assert r.status_code == 202 and r.get_json()["job_id"] == "stubjob"
        assert captured == {"module": "identity", "params": {"limit": 5}}
        r = self._client().post("/run/identity")
        assert r.status_code == 202 and captured["params"] == {}

    def test_route_bad_limit_400_and_no_submit(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        submitted = []
        monkeypatch.setattr(d.jobs, "submit", lambda module, params: submitted.append(module) or "stubjob")
        for bad in ("abc", "0", "-3"):
            r = self._client().post("/run/identity", data={"limit": bad})
            assert r.status_code == 400 and r.get_json()["ok"] is False
        assert submitted == []

    def test_runner_always_raises_jobmessage_with_the_summary(self, monkeypatch):
        from RevenueChaser.app.jobs import JobMessage
        from RevenueChaser.core.identity import refresh as R
        seen = {}

        def fake(**kw):
            seen.update(kw)
            return {"status": "ok", "error": None, "counts": {
                "tickers": 7366, "by_type": {"etp": 200, "cef": 13, "spac": 116},
                "by_sector_source": {"sp1500": 1501, "provider_uw": 4900, "provider_yf": 325, "sic_explicit": 120},
                "unclassified": 64, "conflicts": 266},
                "sources": {s: {"selected": True, "status": "ok"} for s in R.SOURCES}}
        monkeypatch.setattr(R, "refresh", fake)
        with pytest.raises(JobMessage) as exc:
            runners.run_identity(limit=10)
        assert seen == {"limit": 10}
        msg = exc.value.message
        assert "7,366 tickers" in msg and "213 funds/ETFs" in msg and "116 SPACs" in msg
        assert "S&P 1,501" in msg and "UW 4,900" in msg and "Yahoo 325" in msg and "SIC 120" in msg
        assert "unclassified 64" in msg
        assert "266 conflicts" in msg and "sources ok 7/7" in msg

    def test_runner_raises_jobmessage_even_when_the_run_errored(self, monkeypatch):
        from RevenueChaser.app.jobs import JobMessage
        from RevenueChaser.core.identity import refresh as R
        monkeypatch.setattr(R, "refresh", lambda **kw: {"status": "error", "error": "equity_universe is empty",
                                                        "counts": {}, "sources": {}})
        with pytest.raises(JobMessage) as exc:
            runners.run_identity()
        assert "equity_universe is empty" in exc.value.message


class TestOipdTickerOutsideThePool:
    """#87: the picker pool is the typed universe, but Implied Probability opens any
    valid ticker — a fund the type filter removed, SPY — through the same routes."""

    def _client(self):
        dashboard.app.config["TESTING"] = True
        return panel_test_client(dashboard.app)

    def _seed(self):
        from RevenueChaser.core import rawstore
        rawstore.replace_all("equity_universe", [
            {"ticker": t, "market_cap": 1e9, "name": t, "price": 1.0, "quote": {"symbol": t}}
            for t in ("AAA", "FUND")])
        rawstore.ensure_table("security_master")
        rawstore.write_rows("security_master", [
            {"ticker": "AAA", "security_type": "common"},
            {"ticker": "FUND", "security_type": "cef"}])

    def test_view_refresh_opens_the_excluded_ticker(self, rawstore_tmp_db, monkeypatch, tmp_path):
        from RevenueChaser.module_oipd import engine, report
        self._seed()
        seen = {}

        def fake_run_oipd_cached(ticker=None):
            seen["ticker"] = ticker
            path = tmp_path / "oipd.html"
            path.write_text(report.render_oipd_dashboard(
                engine._empty_payload(ticker, "No cached chain")), encoding="utf-8")
            return path

        monkeypatch.setattr(runners, "run_oipd_cached", fake_run_oipd_cached)
        r = self._client().get("/view/module_oipd", query_string={"refresh": "1", "ticker": "fund"})
        assert r.status_code == 200
        assert seen["ticker"] == "FUND"
        page = r.get_data(as_text=True)
        assert 'var DEFAULT_TICKER = "FUND";' in page      # the focal ticker, not the pool's first
        pool = json.loads(re.search(r"var DATA = (.*?);\s*var META", page, re.S).group(1))
        assert [row["ticker"] for row in pool] == ["AAA"]   # ... and not in the picker's pool

    def test_run_accepts_the_excluded_ticker(self, rawstore_tmp_db, monkeypatch):
        self._seed()
        captured = {}
        monkeypatch.setattr(dashboard, "UWClient", lambda: _FakeUW())
        monkeypatch.setattr(dashboard.jobs, "submit",
                            lambda module, params: captured.update(module=module, params=params)
                            or "stubjob")
        r = self._client().post("/run/module_oipd", data={"ticker": "fund"})
        assert r.status_code == 202
        assert captured == {"module": "module_oipd", "params": {"ticker": "FUND"}}

    def test_the_combobox_rule_is_the_servers_ticker_rule(self):
        from RevenueChaser.core import reporting_common
        assert dashboard._TICKER_RE.pattern in reporting_common.FILTERS_JS
        for ok in ("SPY", "BRK.B", "BF-B", "A", "ABCDEFGHIJ"):
            assert dashboard._valid_ticker(ok)
        for bad in ("APPLE INC", "ABCDEFGHIJK", ""):
            assert not dashboard._valid_ticker(bad)


def test_api_universe_reports_count_and_provenance(rawstore_tmp_db):
    from RevenueChaser.core import rawstore
    rawstore.replace_all("equity_universe", [
        {"ticker": "AAA", "market_cap": 1e9, "sector": "Energy", "quote": {}},
        {"ticker": "BBB", "market_cap": 2e9, "sector": "Energy", "quote": {}},
        {"ticker": "CCC", "market_cap": 1e8, "sector": "Energy", "quote": {}},
        {"ticker": "DDD", "market_cap": None, "sector": "Energy", "quote": {}}])
    rawstore.replace_all("equity_universe_provenance", [{
        "key": "__meta__", "generated_at": "2026-10-02T10:00:00+00:00",
        "refreshed_by": "Swing Potential"}])
    dashboard.app.config["TESTING"] = True
    body = dashboard.app.test_client().get("/api/universe").get_json()
    assert body == {"count": 4, "generated_at": "2026-10-02T10:00:00+00:00",
                    "refreshed_by": "Swing Potential"}


class TestDailyBarsRoute:
    """#156: the daily bars job is on the long lane, needs UW, and validates its tickers."""

    def _client(self):
        return panel_test_client(dashboard.app)

    def _fake_uw(self, monkeypatch):
        import RevenueChaser.app.dashboard as d

        class _UW:
            def verify(self):
                return (True, None)

            def close(self):
                pass

        monkeypatch.setattr(d, "_uw", lambda: _UW())

    def test_registered_long_lane_and_requires_uw(self):
        from RevenueChaser.app import jobs
        assert jobs._LANE["daily_bars"] == "long"
        assert jobs._REQUIRES_UW["daily_bars"] is True

    def test_route_submits_with_no_params(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        self._fake_uw(monkeypatch)
        captured = {}
        monkeypatch.setattr(d.jobs, "submit",
                            lambda module, params: captured.update(module=module, params=params)
                            or "stubjob")
        r = self._client().post("/run/daily-bars")
        assert r.status_code == 202 and r.get_json()["job_id"] == "stubjob"
        assert captured == {"module": "daily_bars", "params": {}}

    def test_route_parses_and_upper_cases_tickers(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        self._fake_uw(monkeypatch)
        captured = {}
        monkeypatch.setattr(d.jobs, "submit", lambda module, params: captured.update(params=params) or "x")
        r = self._client().post("/run/daily-bars", data={"tickers": "aapl, msft"})
        assert r.status_code == 202
        assert captured["params"]["tickers"] == ["AAPL", "MSFT"]

    def test_bad_ticker_is_400_and_no_submit(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        self._fake_uw(monkeypatch)              # so a 400 can only come from the ticker check
        submitted = []
        monkeypatch.setattr(d.jobs, "submit", lambda module, params: submitted.append(module) or "x")
        r = self._client().post("/run/daily-bars", data={"tickers": "AA PL"})
        assert r.status_code == 400 and r.get_json()["ok"] is False
        assert submitted == []

    def test_an_index_symbol_is_400(self, monkeypatch):
        """N6: ^GSPC is an index level, never a bar of price_bars_daily (#293 stores index levels)."""
        import RevenueChaser.app.dashboard as d
        self._fake_uw(monkeypatch)              # so a 400 can only come from the ticker check
        submitted = []
        monkeypatch.setattr(d.jobs, "submit", lambda module, params: submitted.append(module) or "x")
        r = self._client().post("/run/daily-bars", data={"tickers": "SPY,^GSPC"})
        assert r.status_code == 400 and r.get_json()["error"] == "bad ticker" and submitted == []

    def test_more_than_200_tickers_is_400(self, monkeypatch):
        import RevenueChaser.app.dashboard as d
        self._fake_uw(monkeypatch)              # so a 400 can only come from the ticker check
        submitted = []
        monkeypatch.setattr(d.jobs, "submit", lambda module, params: submitted.append(module) or "x")
        r = self._client().post("/run/daily-bars", data={"tickers": ",".join(f"T{i}" for i in range(201))})
        assert r.status_code == 400 and submitted == []

class TestRunValueChainEmptyFetch:
    def _stub(self, monkeypatch, tmp_path, fetch):
        from RevenueChaser.module_value_chain import engine, report

        monkeypatch.setattr(engine, "run", lambda no_fetch=False, **kw: {"fetch": fetch})
        monkeypatch.setattr(report, "render_value_chain_dashboard", lambda p: "<html></html>")
        monkeypatch.setattr(runners, "_write_output", lambda *a, **k: tmp_path / "x")

    def test_an_empty_fetch_raises_a_job_message_with_the_reasons(self, monkeypatch, tmp_path):
        from RevenueChaser.app.jobs import JobMessage

        self._stub(monkeypatch, tmp_path, {"fetched": 0, "cached": 7, "no_filing": 2,
                                           "no_cik": 1, "failed": 0})
        with pytest.raises(JobMessage) as exc:
            runners.run_value_chain(None, no_fetch=False)
        assert exc.value.message == (
            "No new filings fetched (7 already cached, 2 no 10-K/20-F, 1 no CIK)")

    def test_a_fetch_that_wrote_rows_and_a_refresh_return_the_path(self, monkeypatch, tmp_path):
        self._stub(monkeypatch, tmp_path, {"fetched": 3, "cached": 0})
        assert runners.run_value_chain(None, no_fetch=False) == runners.VALUE_CHAIN_LATEST
        self._stub(monkeypatch, tmp_path, {"fetched": 0, "cached": 7})
        assert runners.run_value_chain(None, no_fetch=True) == runners.VALUE_CHAIN_LATEST
