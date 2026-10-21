"""SEC 13F filter-kit tests (KF, #375 Card E)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from RevenueChaser.core import reporting_common as rc
from RevenueChaser.core.tests.board_js import run_board_js
from RevenueChaser.module_sec_13f import report


def _row(shares: float) -> dict:
    return {"shares": shares, "value": shares * 10, "holders": 2, "new_holders": 1, "exited_holders": 0,
            "top10_pct": 90.0, "hhi": 4000.0, "buy_flow": 1.0, "sell_flow": 0.0}


class TestKitRows:
    """Filter rows ride the payload and match the K1 contract."""

    def test_off_spine_ticker_is_unclassified_with_no_cap(self, monkeypatch):
        """KF (#375): a ticker absent from ``equity_universe`` is ``Unclassified`` with ``market_cap: None``."""
        from RevenueChaser.core import universe
        from RevenueChaser.module_sec_13f.report import board_payload

        def fake_filter_fields(tickers):
            # Real filter_fields returns an off-spine ticker as Unclassified with no cap.
            return {
                "AAA": {"ticker": "AAA", "name": "A", "index": ["S&P 500"], "market_cap": 5e9,
                        "gics": ["Technology", "Software"], "theme_ids": [], "earnings_date": None},
                "ZZZ": {"ticker": "ZZZ", "name": "Z", "index": [], "market_cap": None,
                        "gics": ["Unclassified", ""], "theme_ids": [], "earnings_date": None},
            }

        monkeypatch.setattr(universe, "filter_fields", fake_filter_fields)
        totals = {"AAA": {"2025-06-30": _row(100.0)},
                  "ZZZ": {"2025-06-30": _row(50.0)}}
        payload = board_payload(totals, {"AAA": {"name": "A"}, "ZZZ": {"name": "Z"}}, {})
        fields = payload["fields"]
        assert fields["AAA"]["gics"] == ["Technology", "Software"]
        assert fields["AAA"]["market_cap"] == 5e9
        assert fields["ZZZ"]["gics"] == ["Unclassified", ""]
        assert fields["ZZZ"]["market_cap"] is None

    def test_failed_filter_fields_read_still_mounts_the_bar_with_a_notice(self, monkeypatch):
        """KF (#375): a store failure leaves ticker+name only and shows one notice; the page still renders."""
        from RevenueChaser.core import universe
        from RevenueChaser.module_sec_13f.report import render_sec13f_dashboard

        monkeypatch.setattr(universe, "filter_fields", lambda _t: (_ for _ in ()).throw(RuntimeError("store down")))
        totals = {"AAA": {"2025-06-30": _row(100.0)}}
        h = render_sec13f_dashboard(totals, {"AAA": {"name": "A"}}, {})
        assert '<section class="rcf-bar" id="rc-filters"></section>' in h
        assert "filter fields" in h.lower() or "could not be read" in h.lower()
        # The payload still carries ticker/name rows so the kit can mount.
        payload = report.board_payload(totals, {"AAA": {"name": "A"}}, {})
        assert payload["fields"]["AAA"]["ticker"] == "AAA"


def test_the_kit_script_rides_in_its_own_script_tag_before_the_board_script(monkeypatch):
    """R7 review: ``FILTERS_JS`` once followed the board's ``</script>`` as bare text, so ``RCFilters`` was undefined
    in the browser and no row was drawn. It must sit inside a ``<script>`` and come before ``initKit``."""
    import re

    from RevenueChaser.core import universe
    from RevenueChaser.module_sec_13f.report import render_sec13f_dashboard

    monkeypatch.setattr(universe, "filter_fields", lambda ts: {
        t: {"ticker": t, "name": t, "index": [], "market_cap": None, "gics": ["Unclassified", ""],
            "theme_ids": [], "earnings_date": None} for t in ts})
    h = render_sec13f_dashboard({"AAA": {"2025-06-30": _row(100.0)}}, {"AAA": {"name": "A"}}, {})
    scripts = re.findall(r"<script>(.*?)</script>", h, re.S)
    kit = [i for i, s in enumerate(scripts) if "root.RCFilters = {" in s]
    board = [i for i, s in enumerate(scripts) if "function initKit" in s]
    assert kit and board, "the kit and the board script must each be inside a <script> tag"
    assert kit[0] < board[0], "the kit must load before the board script that mounts it"
    tail = h[h.rfind("</script>") + len("</script>"):]
    assert "RCFilters" not in tail, "no script text may follow the last </script>"


class TestKitJs:
    """Client-side filter + sort behaviour, inspected by running the page scripts under node."""

    def _render(self, monkeypatch):
        from RevenueChaser.core import universe

        def fake_filter_fields(tickers):
            return {
                "AAA": {"ticker": "AAA", "name": "Alpha", "index": ["S&P 500"], "market_cap": 5e9,
                        "gics": ["Technology", "Software"], "theme_ids": ["t1"], "earnings_date": None},
                "BBB": {"ticker": "BBB", "name": "Beta", "index": [], "market_cap": 1e8,
                        "gics": ["Energy", ""], "theme_ids": [], "earnings_date": None},
            }

        monkeypatch.setattr(universe, "filter_fields", fake_filter_fields)
        monkeypatch.setattr(universe, "theme_catalog", lambda: {
            "t1": {"label": "Semiconductors", "level": "coarse", "parent": ""}})
        totals = {"AAA": {"2025-06-30": _row(100.0)},
                  "BBB": {"2025-06-30": _row(50.0)}}
        html = report.render_sec13f_dashboard(totals, {"AAA": {"name": "Alpha"}, "BBB": {"name": "Beta"}}, {})
        payload = report.board_payload(totals, {"AAA": {"name": "Alpha"}, "BBB": {"name": "Beta"}}, {})
        return html, payload

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
    def test_page_scripts_parse_and_contain_filter_logic(self, tmp_path, monkeypatch):
        """KF (#375): the inline scripts parse and expose the filter primitives."""
        html, payload = self._render(monkeypatch)
        tail = r"""
console.log(JSON.stringify({
  hasApplyFilters: typeof applyFilters === 'function',
  hasInitKit: typeof initKit === 'function',
  hasFIELDS: typeof FIELDS === 'object',
  hasMETA: typeof META === 'object',
  facets: META.facets
}));
"""
        got = run_board_js(html, tail, tmp_path, payload=payload)
        assert got["hasApplyFilters"] is True
        assert got["hasInitKit"] is True
        assert got["hasFIELDS"] is True
        assert got["hasMETA"] is True
        assert got["facets"] == ["index", "mc", "gics", "theme"]

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
    def test_sort_table_calls_apply_filters(self, tmp_path, monkeypatch):
        """KF (#375): sorting re-applies filters so hidden rows stay hidden."""
        html, payload = self._render(monkeypatch)
        tail = r"""
var calls = [];
var original = applyFilters;
applyFilters = function(){ calls.push('applyFilters'); };
var th = {dataset: {col: 'ticker'}, classList: {add: function(){}}};
sortTable('ticker', th);
console.log(JSON.stringify({calls: calls}));
"""
        got = run_board_js(html, tail, tmp_path, payload=payload)
        assert "applyFilters" in got["calls"]


_KIT_COUNTS = Path(__file__).resolve().parents[2] / "scripts/research/383_card_e_kit_counts.py"


@pytest.mark.skipif(not _KIT_COUNTS.exists(),
                    reason=f"{_KIT_COUNTS.name} is not in this tree")
def test_counts_script_13f_counts_the_payload_rows(monkeypatch):
    """R7 review: since #429 the 13F page holds no row, so the KF counts card counts the payload's rows (and their
    filter fields), not ``<tr data-ticker>`` in the HTML (which is always 0 now)."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "kit_counts_kf", Path(__file__).resolve().parents[2] / "scripts/research/383_card_e_kit_counts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "13f" in mod.BOARDS

    totals = {"AAA": {"2025-06-30": _row(100.0)}, "BBB": {"2025-06-30": _row(50.0)}}
    monkeypatch.setattr("RevenueChaser.module_sec_13f.engine.load_totals", lambda: totals)
    monkeypatch.setattr("RevenueChaser.module_sec_13f.engine.load_sec13f_meta",
                        lambda: {"AAA": {"name": "A"}, "BBB": {"name": "B"}})
    monkeypatch.setattr("RevenueChaser.module_sec_13f.enrich.load_inputs", lambda t: {})
    monkeypatch.setattr("RevenueChaser.core.universe.filter_fields", lambda ts: {
        t: {"ticker": t, "name": t, "index": [], "market_cap": 5e10 if t == "AAA" else None,
            "gics": ["Technology" if t == "AAA" else "Unclassified", ""], "theme_ids": [], "earnings_date": None}
        for t in ts})
    out = mod.measure_13f()
    assert out["board"] == "13f"
    assert out["rows_before"] == 2
    assert out["rows_after_no_filter"] == 2
    assert out["filtered_universe"]["rows"] == 2
    assert out["filtered_universe"]["sector"] == {"Technology": 1, "Unclassified": 1}
