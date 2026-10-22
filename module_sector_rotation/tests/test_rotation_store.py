"""Tests for rotation snapshot persistence, backfill, and trail rendering."""
from datetime import date, timedelta

from RevenueChaser.core import rawstore
from RevenueChaser.module_sector_rotation.models import ETFRotationSnapshot
from RevenueChaser.module_sector_rotation import store as rotation_store
from RevenueChaser.module_sector_rotation.tests.d01_seed import ny, pin_clock, seed


def _seed_snapshot(asof: str, rows: list[dict]):
    """Write snapshot rows directly into the rawstore rotation_snapshots table."""
    for r in rows:
        r["benchmark"] = r.get("benchmark", "SPY")
        r["generated_at"] = r.get("generated_at", "")
    rawstore.write_rows("rotation_snapshots", rows)


def _snap(ticker="XLK", quadrant="LEADING", rz=1.0, mz=0.5):
    return ETFRotationSnapshot(
        as_of_date=date(2026, 8, 14), ticker=ticker, group_name="sector",
        close_price=100.0, rs_1w=1.0, rs_1m=2.0, rs_3m=3.0,
        rs_ratio=2.0, rs_momentum=1.0, rs_ratio_z=rz, rs_momentum_z=mz,
        rrg_quadrant=quadrant,
    )


def _row(d, ticker="ITA", rz=-0.5, mz=0.4, quadrant="IMPROVING", name="A&D", group="theme"):
    return {"as_of_date": d, "ticker": ticker, "name": name, "group": group,
            "close_price": 100.0, "rs_1w": 1.0, "rs_1m": 2.0, "rs_3m": 3.0,
            "rs_ratio": 1.0, "rs_momentum": 1.0, "rs_ratio_z": rz, "rs_momentum_z": mz,
            "quadrant": quadrant}


class TestSnapshotPersistence:
    def test_save_load_roundtrip(self, rawstore_tmp_db):
        rotation_store.save_snapshot([_snap()], as_of=date(2026, 8, 14))
        data = rotation_store.load_snapshot(date(2026, 8, 14))
        assert data["etfs"][0]["ticker"] == "XLK"
        assert data["etfs"][0]["quadrant"] == "LEADING"

    def test_same_day_overwrites(self, rawstore_tmp_db):
        rotation_store.save_snapshot([_snap("XLK")], as_of=date(2026, 8, 14))
        rotation_store.save_snapshot([_snap("XLF", "LAGGING")], as_of=date(2026, 8, 14))
        rows = rawstore.query("SELECT * FROM rotation_snapshots WHERE as_of_date = '2026-08-14'")
        assert len(rows) == 1
        assert rows[0]["ticker"] == "XLF"

    def test_list_and_range(self, rawstore_tmp_db):
        rotation_store.save_snapshot([_snap("XLK")], as_of=date(2026, 8, 12))
        rotation_store.save_snapshot([_snap("XLF")], as_of=date(2026, 8, 14))
        dates = rotation_store.list_snapshot_dates()
        assert dates == ["2026-08-12", "2026-08-14"]
        rng = rotation_store.load_range(date(2026, 8, 12), date(2026, 8, 14))
        assert set(rng.keys()) == {"2026-08-12", "2026-08-14"}


class _FakeUW:
    """A client the D01 backfill must never call (M-ROT-2)."""

    def __getattr__(self, name):
        raise AssertionError(f"the backfill must not call uw.{name}")


class TestBackfill:
    def test_backfill_writes_snapshots(self, rawstore_tmp_db, monkeypatch):
        pin_clock(monkeypatch, ny(2026, 8, 14, 18))
        base = date(2026, 8, 14)
        seed("SPY", base, closes=lambda i: 100.0 + i)
        for t in ["XLK", "XLF", "XLE", "XLV"]:
            seed(t, base, closes=lambda i: 100.0 + i * 1.01)
        written = rotation_store.backfill_snapshots(_FakeUW(), days=5)
        assert written == 5
        assert rotation_store.list_snapshot_dates() == ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13",
                                                        "2026-08-14"]

    def test_backfill_skips_existing(self, rawstore_tmp_db, monkeypatch):
        pin_clock(monkeypatch, ny(2026, 8, 14, 18))
        base = date(2026, 8, 14)
        seed("SPY", base, closes=lambda i: 100.0 + i)
        seed("XLK", base, closes=lambda i: 100.0 + i)
        rotation_store.backfill_snapshots(_FakeUW(), days=3)
        assert rotation_store.backfill_snapshots(_FakeUW(), days=3) == 0
        assert len(rotation_store.list_snapshot_dates()) == 3


class _FakeDate(date):
    @classmethod
    def today(cls):
        return date(2026, 8, 14)


class TestEarlyTurn:
    def test_early_turn_two_consecutive(self, rawstore_tmp_db, monkeypatch):
        from RevenueChaser.module_sector_rotation.report import _early_turn_tickers, _rows_to_snapshots
        import RevenueChaser.module_sector_rotation.report as rd
        monkeypatch.setattr(rd, "OUTPUT_DIR", rawstore_tmp_db.parent)
        monkeypatch.setattr(rd, "OUTPUT_DIR", rawstore_tmp_db.parent)
        for d in ["2026-08-12", "2026-08-13"]:
            _seed_snapshot(d, [_row(d, ticker="ITA", rz=-0.5, mz=0.4)])
        snaps = _rows_to_snapshots([_row("2026-08-14", ticker="ITA", rz=-0.3, mz=0.5)])
        assert _early_turn_tickers(date(2026, 8, 14), snaps) == {"ITA"}

    def test_early_turn_requires_two_prior(self, rawstore_tmp_db, monkeypatch):
        from RevenueChaser.module_sector_rotation.report import _early_turn_tickers, _rows_to_snapshots
        _seed_snapshot("2026-08-13", [_row("2026-08-13", ticker="ITA", rz=-0.5, mz=0.4)])
        snaps = _rows_to_snapshots([_row("2026-08-14", ticker="ITA", rz=-0.3, mz=0.5)])
        assert _early_turn_tickers(date(2026, 8, 14), snaps) == set()


class TestAdvRadius:
    def test_bounds(self):
        from RevenueChaser.module_sector_rotation.report import _adv_radius
        assert 3.0 <= _adv_radius(0) <= 9.0
        assert 3.0 <= _adv_radius(1_000_000_000) <= 9.0
        assert _adv_radius(None) == 5.0

    def test_monotonic(self):
        from RevenueChaser.module_sector_rotation.report import _adv_radius
        assert _adv_radius(10_000_000) >= _adv_radius(100_000)


class TestHeatStyle:
    def test_positive_tint(self):
        from RevenueChaser.module_sector_rotation.report import _rs_heat_style
        s = _rs_heat_style(15.0)
        assert "52,211,153" in s and "0.12" in s  # subtle 0.12-opacity green tint
        assert _rs_heat_style(1.0) == s  # no intensity ramp

    def test_negative(self):
        from RevenueChaser.module_sector_rotation.report import _rs_heat_style
        s = _rs_heat_style(-15.0)
        assert "248,113,113" in s and "0.12" in s

    def test_none(self):
        from RevenueChaser.module_sector_rotation.report import _rs_heat_style
        assert _rs_heat_style(None) == ""


class TestTrail:
    def _seed_two_weeks(self):
        for d, rz, mz, q in [("2026-08-12", -0.5, 0.2, "IMPROVING"),
                             ("2026-08-19", 0.5, 0.6, "LEADING")]:
            _seed_snapshot(d, [
                _row(d, ticker="ITA", rz=rz, mz=mz, quadrant=q, name="Aerospace & Defense"),
                _row(d, ticker="XLK", rz=-rz, mz=-mz, quadrant="LAGGING", name="Technology", group="sector"),
            ])

    def test_trail_shows_all_etfs_with_decay(self, rawstore_tmp_db, monkeypatch):
        from RevenueChaser.module_sector_rotation.report import export_rotation_trail
        import RevenueChaser.module_sector_rotation.report as rd
        monkeypatch.setattr(rd, "OUTPUT_DIR", rawstore_tmp_db.parent)
        monkeypatch.setattr(rd, "OUTPUT_DIR", rawstore_tmp_db.parent)
        self._seed_two_weeks()
        p = export_rotation_trail(date(2026, 8, 12), date(2026, 8, 19))
        content = p.read_text(encoding="utf-8")
        assert "ROTATION TRAIL" in content
        assert "ITA" in content
        assert "XLK" in content
        assert "path d=" in content  # faint trail line per ETF (2 weekly points)

    def test_trail_weekly_samples_last_of_week(self, rawstore_tmp_db, monkeypatch):
        from RevenueChaser.module_sector_rotation.report import export_rotation_trail, _weekly_sample
        import RevenueChaser.module_sector_rotation.report as rd
        monkeypatch.setattr(rd, "OUTPUT_DIR", rawstore_tmp_db.parent)
        monkeypatch.setattr(rd, "OUTPUT_DIR", rawstore_tmp_db.parent)
        for d in ["2026-08-10", "2026-08-12", "2026-08-14", "2026-08-17", "2026-08-19", "2026-08-21"]:
            _seed_snapshot(d, [_row(d, ticker="ITA", rz=0.1, mz=0.1, quadrant="LEADING")])
        sampled = _weekly_sample(["2026-08-10", "2026-08-12", "2026-08-14",
                                  "2026-08-17", "2026-08-19", "2026-08-21"])
        assert len(sampled) == 2
        p = export_rotation_trail(date(2026, 8, 10), date(2026, 8, 21))
        content = p.read_text(encoding="utf-8")
        assert content.count("<title>ITA") == 2

    def test_trail_empty_range(self, rawstore_tmp_db, monkeypatch):
        from RevenueChaser.module_sector_rotation.report import export_rotation_trail
        import RevenueChaser.module_sector_rotation.report as rd
        monkeypatch.setattr(rd, "OUTPUT_DIR", rawstore_tmp_db.parent)
        monkeypatch.setattr(rd, "OUTPUT_DIR", rawstore_tmp_db.parent)
        p = export_rotation_trail(date(2026, 8, 12), date(2026, 8, 13))
        content = p.read_text(encoding="utf-8")
        assert "No stored snapshots" in content


class TestBackfillWeekly:
    def _ohlc(self, closes, start):
        return [{"date": (start + timedelta(days=i)).isoformat(), "close": c}
                for i, c in enumerate(closes)]

    def test_wednesdays(self):
        from RevenueChaser.module_sector_rotation.store import _wednesdays
        ws = _wednesdays(date(2026, 1, 1), date(2026, 1, 31))
        assert ws == [date(2026, 1, 7), date(2026, 1, 14), date(2026, 1, 21), date(2026, 1, 28)]

    def test_backfill_weekly_writes_wednesdays(self, rawstore_tmp_db, monkeypatch):
        pin_clock(monkeypatch, ny(2026, 8, 14, 18))
        base = date(2026, 8, 14)
        seed("SPY", base, closes=lambda i: 100.0 + i)
        seed("XLK", base, closes=lambda i: 100.0 + i)
        written = rotation_store.backfill_weekly(_FakeUW(), date(2026, 8, 1), date(2026, 8, 14))
        dates = rotation_store.list_snapshot_dates()
        for d in dates:
            assert date.fromisoformat(d).weekday() == 2, f"{d} is not a Wednesday"
        assert written == len(dates) == 2

    def test_backfill_weekly_skips_existing(self, rawstore_tmp_db, monkeypatch):
        pin_clock(monkeypatch, ny(2026, 8, 14, 18))
        _seed_snapshot("2026-08-12", [_row("2026-08-12", ticker="XLK")])
        base = date(2026, 8, 14)
        seed("SPY", base, closes=lambda i: 100.0 + i)
        seed("XLK", base, closes=lambda i: 100.0 + i)
        rotation_store.backfill_weekly(_FakeUW(), date(2026, 8, 1), date(2026, 8, 14))
        assert rotation_store.load_snapshot(date(2026, 8, 12))["etfs"][0]["rs_ratio_z"] == -0.5   # not rewritten
        assert rotation_store.list_snapshot_dates() == ["2026-08-05", "2026-08-12"]


class TestUniverseShape:
    def test_rrg_symbols(self):
        from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS
        assert len(ROTATION_ETFS) == 78, f"expected 78 RRG symbols, got {len(ROTATION_ETFS)}"
        groups = {}
        for t, info in ROTATION_ETFS.items():
            groups.setdefault(info["group"], []).append(t)
        assert len(groups["sector"]) == 11, "11 sectors"
        assert len(groups["theme"]) == 23, "23 themes"
        assert len(groups["bond"]) == 3, "3 bond ETFs"
        assert len(groups["region"]) == 30, "30 region (2 broad + 28 country)"
        assert len(groups["yield"]) == 7, "7 yield-curve symbols"
        assert len(groups["commodity"]) == 4, "4 volatility/commodity symbols"
        assert "benchmark" not in groups, "benchmark group should be removed"

    def test_macro_symbols_mapped(self):
        from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS, MACRO_SERIES
        for sym, info in ROTATION_ETFS.items():
            if info["group"] in ("yield", "commodity"):
                assert sym in MACRO_SERIES, f"{sym} should be in MACRO_SERIES"

    def test_country_etfs_present(self):
        from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS, COUNTRY_FLAGS
        for t in ("EWZ", "EWY", "FXI", "INDA", "EWT", "MCHI", "VNM", "IDX", "EPU"):
            assert t in ROTATION_ETFS, f"{t} should be in the RRG universe"
            assert ROTATION_ETFS[t]["group"] == "region", f"{t} should be region"
            assert t in COUNTRY_FLAGS, f"{t} should have a flag"

    def test_benchmark_removed(self):
        from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS
        assert "MAGS" not in ROTATION_ETFS
        assert "benchmark" not in {v["group"] for v in ROTATION_ETFS.values()}


class TestUiContract:
    """Interface-contract tests: if the rendered UI changes, these fail with a reason."""

    def _render(self, tmp_path, monkeypatch):
        import RevenueChaser.module_sector_rotation.report as rd
        monkeypatch.setattr(rd, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(rd, "OUTPUT_DIR", tmp_path)
        snaps = [ETFRotationSnapshot(as_of_date=date(2026, 8, 14), ticker="XLK",
                                     group_name="sector", close_price=100.0,
                                     rs_1w=1.0, rs_1m=2.0, rs_3m=3.0,
                                     rs_ratio=2.0, rs_momentum=1.0,
                                     rs_ratio_z=1.0, rs_momentum_z=0.5, rrg_quadrant="LEADING"),
                 ETFRotationSnapshot(as_of_date=date(2026, 8, 14), ticker="XLF",
                                     group_name="sector", close_price=30.0,
                                     rs_1w=1.0, rs_1m=2.0, rs_3m=3.0,
                                     rs_ratio=1.0, rs_momentum=1.0,
                                     rs_ratio_z=-0.5, rs_momentum_z=0.2, rrg_quadrant="IMPROVING")]
        p = rd.export_rotation_dashboard(snaps, filename="test_rot.html", uw=None)
        return p.read_text(encoding="utf-8")

    def test_embedded_holdings_self_contained(self, tmp_path, monkeypatch):
        import RevenueChaser.module_sector_rotation.report as rd
        monkeypatch.setattr(rd, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(rd, "OUTPUT_DIR", tmp_path)
        snaps = [_snap("XLK"), _snap("XLV", "IMPROVING")]
        holdings = {"XLK": [{"ticker": "UNH", "name": "UnitedHealth", "weight": 8.5,
                             "close": 500.0, "rs_1w": 1.0, "rs_1m": 2.0, "rs_3m": 3.0}]}
        p = rd.export_rotation_dashboard(snaps, filename="test_hold.html", uw=None, holdings=holdings)
        c = p.read_text(encoding="utf-8")
        assert "var ROTATION_HOLDINGS" in c
        assert "UNH" in c  # embedded fragment contains the holding
        assert "UnitedHealth" in c
        # JS prefers the embedded map before any fetch
        assert "ROTATION_HOLDINGS[ticker]" in c

    def test_embedded_holdings_empty_map_when_none(self, tmp_path, monkeypatch):
        import RevenueChaser.module_sector_rotation.report as rd
        monkeypatch.setattr(rd, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(rd, "OUTPUT_DIR", tmp_path)
        snaps = [_snap("XLK")]
        p = rd.export_rotation_dashboard(snaps, filename="test_nohold.html", uw=None)
        c = p.read_text(encoding="utf-8")
        assert "var ROTATION_HOLDINGS = {};" in c

    def test_no_all_filter(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        assert 'data-group="all"' not in c, "The 'All' filter button should be removed"

    def test_sector_is_default_active(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        assert 'class="filter-btn active" data-group="sector"' in c, \
            "Sector should be the default active filter"

    def test_watermark_transparency(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        # quadrant labels (class qlabel) should be more transparent than 0.9
        assert 'class="qlabel"' in c
        assert 'opacity="0.4" class="qlabel"' in c, "Quadrant labels should use opacity 0.4"
        assert 'opacity="0.12"' in c, "Quadrant background rects should use opacity 0.12"

    def test_axis_units(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        assert 'RS Ratio (z)' in c, "X axis should show 'RS Ratio (z)'"
        assert 'RS Momentum (z)' in c, "Y axis should show 'RS Momentum (z)'"

    def test_zoom_controls_present(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        assert 'setZoom' in c, "Zoom controls (setZoom) should be present"
        assert 'zoom-label' in c, "Zoom label should be present"

    def test_full_name_on_map(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        assert '>Technology</text>' in c, "Map should show full name for sector ETFs"

    def test_all_tickers_use_full_name(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        # sector ETF (XLK) should show its full name "Technology", not just ticker
        assert '>Technology</text>' in c, "Map should show full name for every ticker"
        assert '>XLK</text>' not in c, "Map should not label a sector ETF with just its ticker"

    def test_ticker_picker_present(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        assert 'dd-menu' in c, "Ticker multi-select dropdown should be present"
        assert 'Show tickers' in c, "Ticker picker button label should be present"

    def test_picker_uses_full_name(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        # data-name attribute must be present so the picker can show full names
        assert 'data-name="Technology"' in c, "Map points should carry data-name (full name)"
        assert "it.name" in c, "Picker JS should use the full name"

    def test_ticker_filter_js_present(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        assert 'toggleTicker' in c, "toggleTicker JS should be present"
        assert 'selectAll' in c, "selectAll JS should be present"
        assert 'buildPicker' in c, "buildPicker JS should be present"

    def test_dynamic_axis_range(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        assert 'data-base-limit=' in c, "SVG should carry a dynamic data-base-limit"
        assert 'baseLimit' in c, "JS should read the dynamic base limit"
        assert 'wheelZoom' in c, "Wheel-zoom handler should be present"
        assert 'PAN_X' in c, "Pan state should be present"
        assert 'id="map-layer"' in c, "Map content should be wrapped in a zoom/pan layer"

    def test_table_filtering_js(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        assert "tbody tr[data-ticker]" in c, "Table rows should be filtered by the same group/ticker filter"

    def test_dynamic_axis_expands_to_fit_extreme(self, tmp_path, monkeypatch):
        import RevenueChaser.module_sector_rotation.report as rd
        monkeypatch.setattr(rd, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(rd, "OUTPUT_DIR", tmp_path)
        # a ticker with z = 3.5 should expand the axis (limit ≈ 4.0), not be flagged
        snaps = [ETFRotationSnapshot(as_of_date=date(2026, 8, 14), ticker="XLE",
                                     group_name="sector", close_price=100.0,
                                     rs_1w=1.0, rs_1m=2.0, rs_3m=3.0,
                                     rs_ratio=5.0, rs_momentum=5.0,
                                     rs_ratio_z=3.5, rs_momentum_z=2.8, rrg_quadrant="LEADING")]
        p = rd.export_rotation_dashboard(snaps, filename="test_rot2.html", uw=None)
        c = p.read_text(encoding="utf-8")
        assert 'data-over="1"' not in c, "No ticker should be flagged overflow with a dynamic axis"
        assert 'data-base-limit="4.0"' in c, "Axis should expand to ≈4.0 for z=3.5"

    def test_darken_helper(self):
        from RevenueChaser.module_sector_rotation.report import _darken
        assert _darken("#3fb950", 0.5) == "#1f5c28"
        assert _darken("#58a6ff", 0.55) == "#305b8c"
        assert _darken("bad", 0.5) == "bad"

    def test_hover_tooltip_shows_ratio_and_zscores(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        # tooltip should include raw ratio/momentum and z-scores
        assert 'RS Ratio:' in c, "Hover tooltip should show RS Ratio"
        assert 'RS Momentum:' in c, "Hover tooltip should show RS Momentum"
        assert 'Ratio Z:' in c, "Hover tooltip should show Ratio Z"
        assert 'Momentum Z:' in c, "Hover tooltip should show Momentum Z"
        assert 'RS 1W:' in c, "Hover tooltip should show RS 1W/1M/3M"

    def test_holdings_caret_present(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        assert 'hold-caret' in c, "Expandable rows should have an expand caret (hold-caret)"
        assert 'id="caret-XLK"' in c, "Caret should be keyed by ticker id"

    def test_no_caret_for_non_equity_groups(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        # region (EWZ) must NOT have an expand caret; sector (XLK) must
        assert 'id="caret-EWZ"' not in c, "Region rows should not have an expand caret"
        assert 'id="caret-XLK"' in c, "Sector rows should have an expand caret"
        assert "row.querySelector('.hold-caret')" in c, "Click binding should gate on caret presence"

    def test_toggle_holdings_js_present(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        assert 'toggleHoldings' in c, "toggleHoldings JS should be present"
        assert '/view/module_sector_rotation/holdings?ticker=' in c, \
            "Rows should fetch the holdings fragment route"

    def test_holdings_row_click_binding(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        assert 'holdings-row' in c, "Detail row class (holdings-row) should be referenced"
        assert "row.addEventListener('click'" in c, "Row click should be bound to expand holdings"

    def test_map_labels_use_text_anchor(self, tmp_path, monkeypatch):
        c = self._render_dense(tmp_path, monkeypatch)
        assert 'text-anchor="end"' in c, "Map labels should use text-anchor for left-side placement"
        assert 'text-anchor="middle"' in c, "Map labels should use text-anchor for centered placement"

    def test_map_leader_line_present(self, tmp_path, monkeypatch):
        c = self._render_dense(tmp_path, monkeypatch)
        assert 'class="quadrant-point"' in c
        assert 'stroke-width="0.7"' in c, "Map should render leader lines for displaced labels"
        assert 'stroke-dasharray="3,2"' in c, "Leader lines should be dashed"

    def test_radial_quadrant_bias(self):
        from RevenueChaser.module_sector_rotation.report import _quadrant_bias_offset
        # chart center at (0,0): each quadrant pushes outward diagonally
        assert _quadrant_bias_offset(5, 5, 0, 0, 8) == (8, 8)     # LEADING (x+, y+)
        assert _quadrant_bias_offset(-5, 5, 0, 0, 8) == (-8, 8)   # IMPROVING (x-, y+)
        assert _quadrant_bias_offset(-5, -5, 0, 0, 8) == (-8, -8) # LAGGING (x-, y-)
        assert _quadrant_bias_offset(5, -5, 0, 0, 8) == (8, -8)   # WEAKENING (x+, y-)

    def test_trajectory_columns_present(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        assert 'Trajectory' in c, "Table should have a Trajectory group header"
        assert 'θ°' in c, "Table should have a theta column"
        assert 'data-col="r"' in c, "Rows should carry r values"

    def test_moneyflow_panel_present(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        assert 'id="mf-inflows"' in c, "Money-flow inflow card should be present"
        assert 'id="mf-outflows"' in c, "Money-flow outflow card should be present"
        assert '/api/moneyflow' in c, "Money-flow panel should fetch the cached endpoint"
        assert 'toggleMoneyFlow' in c, "Money-flow panel should be collapsible"
        assert 'id="mf-panel"' in c, "Money-flow panel wrapper should be present"

    def test_summary_cards_replace_raw_transitions(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        assert 'summary-cards' in c, "3-card metric strip should be present"
        assert 'Sector + Commodity Counts' in c, "Card 1 (sector+commodity counts) should be present"
        assert '⚡ Early Turn' in c, "Card 2 (early turn) should be present"
        assert '⚠️ Breakdown' in c, "Card 3 (breakdown) should be present"
        assert 'Quadrant transitions:' not in c, "Raw transitions list should be removed"

    def test_counts_include_sector_commodity_exclude_theme(self, tmp_path, monkeypatch):
        import RevenueChaser.module_sector_rotation.report as rd
        monkeypatch.setattr(rd, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(rd, "OUTPUT_DIR", tmp_path)
        snaps = [
            ETFRotationSnapshot(as_of_date=date(2026, 8, 14), ticker="XLK",
                                group_name="sector", close_price=100.0,
                                rs_1w=1.0, rs_1m=2.0, rs_3m=3.0, rs_ratio=2.0,
                                rs_momentum=1.0, rs_ratio_z=1.0, rs_momentum_z=0.5,
                                rrg_quadrant="LEADING"),
            ETFRotationSnapshot(as_of_date=date(2026, 8, 14), ticker="SMH",
                                group_name="theme", close_price=100.0,
                                rs_1w=1.0, rs_1m=2.0, rs_3m=3.0, rs_ratio=1.0,
                                rs_momentum=1.0, rs_ratio_z=-0.5, rs_momentum_z=-0.4,
                                rrg_quadrant="LAGGING"),
        ]
        p = rd.export_rotation_dashboard(snaps, filename="test_theme.html", uw=None)
        c = p.read_text(encoding="utf-8")
        # sector XLK (LEADING) counted; theme SMH (LAGGING) excluded
        assert 'LEADING: 1' in c, "Sector LEADING should be counted"
        assert 'LAGGING: 0' in c, "Theme LAGGING should be excluded from counts"

    def test_single_top_level_filter_bar(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        assert 'topbar' in c, "Top-level filter bar should be present"
        # filters must appear once, outside the quadrant panel
        assert c.count('class="filters"') == 1, "Filters should be consolidated to a single bar"

    def test_ticker_contrast_and_alignment(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        assert '.ticker{color:#F1F5F9' in c, "Ticker should use off-white #F1F5F9"
        assert '#94A3B8' in c, "Name text should use muted slate #94A3B8"
        assert 'th.num,td.num{text-align:right' in c, "Numeric columns should be right-aligned"
        assert 'ui-monospace' in c, "Numeric columns should use monospace font"

    def test_watchlist_export_removed(self, tmp_path, monkeypatch):
        c = self._render(tmp_path, monkeypatch)
        assert 'copyWatchlist' not in c, "Watchlist export JS should be removed"
        assert 'Copy TV watchlist' not in c, "Watchlist export button should be removed"

    def test_breakdown_badge_rendered(self, rawstore_tmp_db, monkeypatch):
        import RevenueChaser.module_sector_rotation.report as rd
        monkeypatch.setattr(rd, "OUTPUT_DIR", rawstore_tmp_db.parent)
        monkeypatch.setattr(rd, "OUTPUT_DIR", rawstore_tmp_db.parent)
        # prior snapshot: XLK LEADING; current: XLK LAGGING (breakdown)
        _seed_snapshot("2026-08-13", [_row("2026-08-13", ticker="XLK", rz=1.0, mz=0.5,
                                           quadrant="LEADING", group="sector")])
        snaps = [ETFRotationSnapshot(as_of_date=date(2026, 8, 14), ticker="XLK",
                                     group_name="sector", close_price=100.0,
                                     rs_1w=1.0, rs_1m=2.0, rs_3m=3.0, rs_ratio=1.0,
                                     rs_momentum=1.0, rs_ratio_z=-0.5, rs_momentum_z=-0.4,
                                     rrg_quadrant="LAGGING")]
        p = rd.export_rotation_dashboard(snaps, filename="test_bd.html", uw=None)
        c = p.read_text(encoding="utf-8")
        assert '⚠️ Breakdown' in c, "Breakdown badge should appear for a transition into LAGGING"
        assert 'XLK — Technology' in c

    def _render_dense(self, tmp_path, monkeypatch):
        import RevenueChaser.module_sector_rotation.report as rd
        monkeypatch.setattr(rd, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(rd, "OUTPUT_DIR", tmp_path)
        # densely cluster many named points near the origin to force collisions
        snaps = [ETFRotationSnapshot(as_of_date=date(2026, 8, 14), ticker=f"T{i}",
                                     group_name="sector", close_price=100.0,
                                     rs_1w=1.0, rs_1m=2.0, rs_3m=3.0,
                                     rs_ratio=1.0, rs_momentum=1.0,
                                     rs_ratio_z=(i % 5) * 0.05, rs_momentum_z=(i % 4) * 0.05,
                                     rrg_quadrant="LEADING") for i in range(30)]
        p = rd.export_rotation_dashboard(snaps, filename="test_dense.html", uw=None)
        return p.read_text(encoding="utf-8")


class TestTrailUiContract:
    def _trail(self, rawstore_tmp_db, monkeypatch):
        import RevenueChaser.module_sector_rotation.report as rd
        monkeypatch.setattr(rd, "OUTPUT_DIR", rawstore_tmp_db.parent)
        monkeypatch.setattr(rd, "OUTPUT_DIR", rawstore_tmp_db.parent)
        for d in ["2026-08-12", "2026-08-19"]:
            _seed_snapshot(d, [_row(d, ticker="XLK", rz=0.5, mz=0.4, quadrant="LEADING",
                                    name="Technology", group="sector")])
        p = rd.export_rotation_trail(date(2026, 8, 12), date(2026, 8, 19))
        return p.read_text(encoding="utf-8")

    def test_trail_no_all_filter(self, rawstore_tmp_db, monkeypatch):
        c = self._trail(rawstore_tmp_db, monkeypatch)
        assert 'data-group="all"' not in c, "Trail should not have an 'All' filter"

    def test_trail_sector_default(self, rawstore_tmp_db, monkeypatch):
        c = self._trail(rawstore_tmp_db, monkeypatch)
        assert 'class="filter-btn active" data-group="sector"' in c, "Trail sector should be default active"

    def test_trail_watermark_transparent(self, rawstore_tmp_db, monkeypatch):
        c = self._trail(rawstore_tmp_db, monkeypatch)
        assert 'opacity="0.12"' in c, "Trail quadrant rects should use opacity 0.12"

    def test_trail_zoom_present(self, rawstore_tmp_db, monkeypatch):
        c = self._trail(rawstore_tmp_db, monkeypatch)
        assert 'setZoom' in c, "Trail zoom controls should be present"

    def test_trail_week_numbers(self, rawstore_tmp_db, monkeypatch):
        c = self._trail(rawstore_tmp_db, monkeypatch)
        # 2026-08-12 = ISO W33; 2026-08-19 = ISO W34
        assert 'W33' in c, "Trail header should show ISO week number W33"
        assert 'W34' in c, "Trail header should show ISO week number W34"

    def test_trail_ticker_picker(self, rawstore_tmp_db, monkeypatch):
        c = self._trail(rawstore_tmp_db, monkeypatch)
        assert 'Show tickers' in c, "Trail ticker picker should be present"
        assert 'data-ticker="XLK"' in c, "Trail ETF groups should carry data-ticker"

    def test_trail_labels_text_anchor(self, rawstore_tmp_db, monkeypatch):
        c = self._trail(rawstore_tmp_db, monkeypatch)
        assert 'text-anchor="middle"' in c, "Trail labels should use text-anchor"

    def test_trail_velocity_vector(self, rawstore_tmp_db, monkeypatch):
        import RevenueChaser.module_sector_rotation.report as rd
        monkeypatch.setattr(rd, "OUTPUT_DIR", rawstore_tmp_db.parent)
        monkeypatch.setattr(rd, "OUTPUT_DIR", rawstore_tmp_db.parent)
        # two weeks with distinct z-scores -> non-zero velocity at head node
        for d, rz, mz in [("2026-08-12", 0.1, 0.1), ("2026-08-19", 0.9, 0.8)]:
            _seed_snapshot(d, [_row(d, ticker="XLK", rz=rz, mz=mz, quadrant="LEADING",
                                    name="Technology", group="sector")])
        p = rd.export_rotation_trail(date(2026, 8, 12), date(2026, 8, 19))
        c = p.read_text(encoding="utf-8")
        assert 'class="velocity-arrow"' in c, "Trail should draw a velocity arrow at the head node"

    def test_trail_hover_focus(self, rawstore_tmp_db, monkeypatch):
        c = self._trail(rawstore_tmp_db, monkeypatch)
        assert 'focusTrail' in c, "Trail hover-focus JS should be present"
        assert "'0.15'" in c, "Hover focus should dim non-selected trails to 0.15 opacity"
