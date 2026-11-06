"""Slice C2 of #492: the Build table, the tree overlay and the example columns coloured by source."""
from __future__ import annotations

import re

from RevenueChaser.module_settings import control, control_render


def _file(samples=None, **kw) -> dict:
    ds = {"dataset": "alphavantage/listing_status", "label": "Listing status of every US name, active and delisted",
          "attended": False, "checked_on": "2026-10-29", "source_as_of": "2026-10-29", "role": "first", "step": 1,
          "cadence": "daily", "providers": ("alphavantage",), "samples": samples or [], "recent": [],
          "issues_open": 0}
    f = {"kind": "fund", "name": "fund.duckdb", "datasets": [ds], "tables": [], "storage": {}, "walked_at": None,
         "roots": {"raw": "/hdd/raw", "parquet": "/hdd/parquet", "ssd": "/ssd/live"}, "path": "/ssd/live/fund.duckdb",
         "bytes": 1000, "color": "#123456"}
    f.update(kw)
    return f


def _fund_sample(pv=None) -> dict:
    s = {"table": "fund", "built": True,
         "columns": [{"name": f"c{i}", "type": "TEXT"} for i in range(10)], "rows": [[str(i) for i in range(10)]]}
    if pv is not None:
        s["pv"] = pv
    return s


# ── step 3: the example columns by source ──

def test_samples_draw_one_srcrow_with_a_run_per_source(monkeypatch):
    monkeypatch.setattr(control, "PV_LABELS", {"sec": "SEC filings", "rule": "a rule", "build": "the builder"},
                        raising=False)
    html = control_render._samples(_file([_fund_sample(["sec"] * 5 + ["rule"] * 2 + ["build"] * 3)]))
    row = re.search(r'<tr class="srcrow">(.*?)</tr>', html).group(1)
    assert re.findall(r'<th class="pv-(\w+)" colspan="(\d+)">([^<]*)</th>', row) == [
        ("sec", "5", "SEC filings"), ("rule", "2", "a rule"), ("build", "3", "the builder")]
    assert html.index('class="srcrow"') < html.index(">c0<")
    heads = re.findall(r'<th class="(pv-\w+)">c\d', html)
    assert heads == ["pv-sec"] * 5 + ["pv-rule"] * 2 + ["pv-build"] * 3
    cells = re.findall(r'<td class="(pv-\w+)">\d</td>', html)
    assert cells == heads


def test_samples_without_pv_draw_no_srcrow_and_stay_build():
    html = control_render._samples(_file([_fund_sample()]))
    assert "srcrow" not in html
    assert set(re.findall(r'class="(pv-\w+)"', html)) == {"pv-build"}
    assert "Refresh the example rows" in html


def test_the_health_hint_points_at_the_source_box():
    html = control_render._health_table(_file())
    assert "under its source box above" in html and "Fetch section" not in html


def test_the_tree_files_are_loaded_into_the_page():
    from RevenueChaser.module_settings.tests.test_control_view import _build
    tree_css = (control_render._STATIC / "data_tree.css").read_text(encoding="utf-8")
    tree_js = (control_render._STATIC / "data_tree.js").read_text(encoding="utf-8")
    assert tree_css.strip() and tree_css in control_render.CTL_CSS
    assert ".ctl .pv-mix" in tree_css and ':root[data-theme="dark"] .ctl{' in tree_css
    assert "--pv-finra" in tree_css and "font-style:italic" in tree_css
    assert tree_js.strip() and tree_js in control_render.scripts(_build())
