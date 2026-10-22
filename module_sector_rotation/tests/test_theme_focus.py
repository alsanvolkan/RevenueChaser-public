"""Theme focus: coarse NMF labels map to ROTATION_ETFS dots (not a stock screener)."""
import json
from datetime import date

from RevenueChaser.core import rawstore
from RevenueChaser.module_sector_rotation.models import ETFRotationSnapshot


def _pin_run(run_id: str = "tf-run"):
    rawstore.write_rows("theme_runs", [{
        "id": run_id, "created_at": "2026-01-01", "k_fine": 1, "k_medium": 1,
        "k_coarse": 1, "jaccard": 1.0, "is_current": 1,
    }])
    return run_id


def _coarse(run_id: str, cid: str, label: str, size: int = 10):
    return {
        "theme_id": f"{run_id}:{cid}", "level": "coarse", "parent_id": "",
        "label": label, "size": size,
    }


def _etf_rows(theme_id: str, pairs: list[tuple[str, int]]):
    """(etf, rank) pairs for relatedness_theme_etfs."""
    return [
        {"theme_id": theme_id, "etf": etf, "etf_name": etf, "group_name": "theme",
         "overlap": 0.1, "rank": rank}
        for etf, rank in pairs
    ]


class TestThemeEtfsForCoarse:
    def test_empty_unknown_label(self, rawstore_tmp_db):
        from RevenueChaser.module_sector_rotation.theme_focus import theme_etfs_for_coarse
        assert theme_etfs_for_coarse("") == []
        assert theme_etfs_for_coarse(None) == []  # type: ignore[arg-type]
        assert theme_etfs_for_coarse("no-such-theme") == []

    def test_missing_tables_fail_empty(self, rawstore_tmp_db):
        from RevenueChaser.module_sector_rotation.theme_focus import theme_etfs_for_coarse
        # no pin, no themes
        assert theme_etfs_for_coarse("Semiconductors") == []

    def test_jaccard_ordered_by_rank_intersect_rotation(self, rawstore_tmp_db):
        from RevenueChaser.module_sector_rotation.theme_focus import theme_etfs_for_coarse
        run = _pin_run()
        tid = f"{run}:c0"
        rawstore.write_rows("relatedness_themes", [
            _coarse(run, "c0", "Semiconductors"),
        ])
        rawstore.write_rows("relatedness_theme_etfs", _etf_rows(tid, [
            ("SOXX", 2), ("SMH", 1), ("SEA", 3), ("XLK", 4),
        ]))
        # SEA not in ROTATION_ETFS → dropped; ordered by rank
        assert theme_etfs_for_coarse("Semiconductors") == ["SMH", "SOXX", "XLK"]

    def test_drops_holdings_only_etfs(self, rawstore_tmp_db):
        from RevenueChaser.module_sector_rotation.theme_focus import theme_etfs_for_coarse
        run = _pin_run()
        tid = f"{run}:c0"
        rawstore.write_rows("relatedness_themes", [_coarse(run, "c0", "Shipping")])
        rawstore.write_rows("relatedness_theme_etfs", _etf_rows(tid, [
            ("SEA", 1), ("BIZD", 2),
        ]))
        assert theme_etfs_for_coarse("Shipping") == []

    def test_prefers_jaccard_over_overlay(self, rawstore_tmp_db):
        from RevenueChaser.module_sector_rotation.theme_focus import theme_etfs_for_coarse
        run = _pin_run()
        tid = f"{run}:c0"
        rawstore.write_rows("relatedness_themes", [
            _coarse(run, "c0", "Regional community banks"),
        ])
        # Jaccard maps banks theme to XLF first — must not replace with overlay KRE-only
        rawstore.write_rows("relatedness_theme_etfs", _etf_rows(tid, [
            ("XLF", 1), ("KRE", 2),
        ]))
        assert theme_etfs_for_coarse("Regional community banks") == ["XLF", "KRE"]

    def test_overlay_when_jaccard_empty(self, rawstore_tmp_db):
        from RevenueChaser.module_sector_rotation.theme_focus import theme_etfs_for_coarse
        run = _pin_run()
        rawstore.write_rows("relatedness_themes", [
            _coarse(run, "c0", "Regional / community banks"),
            _coarse(run, "c1", "Semiconductors"),
            _coarse(run, "c2", "Aerospace & defense"),
            _coarse(run, "c3", "Totally unknown niche"),
        ])
        # no relatedness_theme_etfs rows
        assert theme_etfs_for_coarse("Regional / community banks") == ["KRE"]
        assert theme_etfs_for_coarse("Semiconductors") == ["SMH"]
        assert theme_etfs_for_coarse("Aerospace & defense") == ["ITA"]
        assert theme_etfs_for_coarse("Totally unknown niche") == []

    def test_only_current_run_coarse(self, rawstore_tmp_db):
        from RevenueChaser.module_sector_rotation.theme_focus import theme_etfs_for_coarse
        run = _pin_run("cur")
        rawstore.write_rows("theme_runs", [{
            "id": "old", "created_at": "2025-01-01", "k_fine": 1, "k_medium": 1,
            "k_coarse": 1, "jaccard": 1.0, "is_current": 0,
        }])
        rawstore.write_rows("relatedness_themes", [
            _coarse("old", "c0", "Semiconductors"),
            _coarse("cur", "c0", "Semiconductors"),
        ])
        rawstore.write_rows("relatedness_theme_etfs",
                            _etf_rows("old:c0", [("XBI", 1)]) +
                            _etf_rows("cur:c0", [("SMH", 1)]))
        assert theme_etfs_for_coarse("Semiconductors") == ["SMH"]


class TestCoarseCatalog:
    def test_empty_when_no_pin(self, rawstore_tmp_db):
        from RevenueChaser.module_sector_rotation.theme_focus import coarse_theme_catalog
        assert coarse_theme_catalog() == {}

    def test_catalog_maps_labels_to_etfs(self, rawstore_tmp_db):
        from RevenueChaser.module_sector_rotation.theme_focus import coarse_theme_catalog
        run = _pin_run()
        rawstore.write_rows("relatedness_themes", [
            _coarse(run, "c0", "Semiconductors"),
            _coarse(run, "c1", "Airlines"),
        ])
        rawstore.write_rows("relatedness_theme_etfs",
                            _etf_rows(f"{run}:c0", [("SMH", 1), ("SOXX", 2)]))
        cat = coarse_theme_catalog()
        assert list(cat.keys()) == ["Semiconductors", "Airlines"]
        assert cat["Semiconductors"] == ["SMH", "SOXX"]
        assert cat["Airlines"] == ["JETS"]  # overlay

    def test_two_coarse_themes_sharing_a_label_get_distinct_keys(self, rawstore_tmp_db):
        """C-F5: the catalog uniquifies in appearance order, and the overlay reads the raw label."""
        from RevenueChaser.module_sector_rotation.theme_focus import coarse_theme_catalog
        run = _pin_run()
        rawstore.write_rows("relatedness_themes", [
            _coarse(run, "c0", "Regional Banks"),
            _coarse(run, "c1", "Regional Banks"),
        ])
        rawstore.write_rows("relatedness_theme_etfs",
                            _etf_rows(f"{run}:c1", [("XLF", 1)]))
        cat = coarse_theme_catalog()
        assert list(cat.keys()) == ["Regional Banks", "Regional Banks (2)"]
        assert cat["Regional Banks"] == ["KRE"]         # overlay on the raw label, not the suffixed one
        assert cat["Regional Banks (2)"] == ["XLF"]     # its own Jaccard rows

    def test_a_suffixed_label_without_jaccard_rows_still_gets_the_overlay(self, rawstore_tmp_db):
        """C-F5: the overlay looks up the raw label, so ``Label (2)`` with no Jaccard rows keeps its ETF."""
        from RevenueChaser.module_sector_rotation.theme_focus import coarse_theme_catalog
        run = _pin_run()
        rawstore.write_rows("relatedness_themes", [
            _coarse(run, "c0", "Regional Banks"),
            _coarse(run, "c1", "Regional Banks"),
        ])
        cat = coarse_theme_catalog()
        assert cat == {"Regional Banks": ["KRE"], "Regional Banks (2)": ["KRE"]}

    def test_a_sector_named_coarse_theme_keeps_its_stored_label_etf(self, rawstore_tmp_db):
        """avo's card on the R2 review (2026-10-14, "Eski etikete bağlı"): the catalog renames "Financials (2)" to its
        largest medium child for display, but the overlay still reads the stored label, so it keeps XLF (not the
        child's KRE)."""
        from RevenueChaser.module_sector_rotation.theme_focus import coarse_theme_catalog, theme_etfs_for_coarse
        run = _pin_run()
        rawstore.write_rows("relatedness_themes", [
            _coarse(run, "c0", "Financials (2)"),
            {"theme_id": f"{run}:m0", "level": "medium", "parent_id": f"{run}:c0",
             "label": "Regional Banks", "size": 8},
        ])
        assert coarse_theme_catalog() == {"Regional Banks": ["XLF"]}
        assert theme_etfs_for_coarse("Regional Banks") == ["XLF"]


class TestThemeFocusRender:
    def _snaps(self):
        return [
            ETFRotationSnapshot(
                as_of_date=date(2026, 8, 14), ticker="XLK", group_name="sector",
                close_price=100.0, rs_1w=1.0, rs_1m=2.0, rs_3m=3.0,
                rs_ratio=2.0, rs_momentum=1.0, rs_ratio_z=1.0, rs_momentum_z=0.5,
                rrg_quadrant="LEADING"),
            ETFRotationSnapshot(
                as_of_date=date(2026, 8, 14), ticker="KRE", group_name="theme",
                close_price=50.0, rs_1w=1.0, rs_1m=2.0, rs_3m=3.0,
                rs_ratio=1.0, rs_momentum=1.0, rs_ratio_z=-0.5, rs_momentum_z=0.2,
                rrg_quadrant="IMPROVING"),
        ]

    def test_omits_control_when_no_coarses(self, tmp_path, monkeypatch, rawstore_tmp_db):
        # The no-catalog path hides the Theme row and must not invent one from a hard-coded list
        # (the keyword taxonomy's theme list is gone, #383 C21).
        import RevenueChaser.module_sector_rotation.report as rd
        monkeypatch.setattr(rd, "OUTPUT_DIR", tmp_path)
        p = rd.export_rotation_dashboard(self._snaps(), filename="no_theme.html", uw=None)
        c = p.read_text(encoding="utf-8")
        assert 'id="theme"' not in c
        assert "theme-tabs" not in c
        assert "All themes" not in c
        assert "THEME_ETFS" not in c or "var THEME_ETFS = {}" in c or "var THEME_ETFS={}" in c
        assert 'data-group="all"' not in c
        assert 'class="filter-btn active" data-group="sector"' in c

    def test_theme_control_and_js_map(self, tmp_path, monkeypatch, rawstore_tmp_db):
        import RevenueChaser.module_sector_rotation.report as rd
        monkeypatch.setattr(rd, "OUTPUT_DIR", tmp_path)
        run = _pin_run()
        rawstore.write_rows("relatedness_themes", [
            _coarse(run, "c0", "Regional community banks"),
            _coarse(run, "c1", "Semiconductors"),
        ])
        rawstore.write_rows("relatedness_theme_etfs",
                            _etf_rows(f"{run}:c0", [("KRE", 1), ("XLF", 2)]))
        p = rd.export_rotation_dashboard(self._snaps(), filename="with_theme.html", uw=None)
        c = p.read_text(encoding="utf-8")
        assert "All themes" in c
        assert 'id="theme"' in c or "theme-tabs" in c
        assert "var THEME_ETFS" in c
        assert "Regional community banks" in c
        assert "Semiconductors" in c
        assert '"KRE"' in c or "'KRE'" in c
        # filter JS: theme AND group, with cross-group mapped ETFs still shown
        assert "THEME" in c
        assert "THEME_ETFS" in c
        assert "data-ticker" in c
        assert 'data-group="all"' not in c
        assert 'class="filter-btn active" data-group="sector"' in c
        # default is All (full map for active group)
        assert 'value="all"' in c or "THEME = \"all\"" in c or "THEME = 'all'" in c


def test_catalog_reads_only_the_current_run(rawstore_tmp_db):
    """A run whose id extends the current one (``r1`` vs ``r10``) is not mixed in."""
    from RevenueChaser.module_sector_rotation.theme_focus import coarse_theme_catalog
    _pin_run("r1")
    rawstore.write_rows("relatedness_themes", [
        _coarse("r1", "c0", "Semiconductors"), _coarse("r10", "c0", "Old Banks")])
    assert list(coarse_theme_catalog()) == ["Semiconductors"]


def test_theme_focus_reuses_the_relatedness_reader():
    from RevenueChaser.module_relatedness import themes
    from RevenueChaser.module_sector_rotation import theme_focus
    assert not hasattr(theme_focus, "_in_run") and not hasattr(themes, "_in_run")


def test_theme_control_escapes_a_markup_label(rawstore_tmp_db):
    """A coarse label with ``</script>`` reaches neither the <select> nor THEME_ETFS raw."""
    from RevenueChaser.module_sector_rotation.report import _theme_control_html
    label = "</script><img src=x onerror=alert(1)>"
    _pin_run("xs")
    rawstore.write_rows("relatedness_themes", [_coarse("xs", "c0", label)])
    from RevenueChaser.module_sector_rotation.theme_focus import coarse_theme_catalog
    html, etfs_json = _theme_control_html(coarse_theme_catalog())
    assert "&lt;/script&gt;&lt;img" in html and "</script><img" not in html
    assert "</script><img" not in etfs_json
    assert label in json.loads(etfs_json)             # whichever way < is escaped
