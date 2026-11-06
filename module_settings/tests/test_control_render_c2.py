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


# ── step 1: the Build table ──

def _two_steps() -> dict:
    f = _file()
    first = f["datasets"][0]
    build = {**first, "dataset": "spine/security", "label": "The security master", "role": "build", "step": 2,
             "cadence": "weekly", "providers": ()}
    f["datasets"] = [first, build]
    return f


def test_the_journey_has_three_columns_and_no_board_column():
    html = control_render._journey(_two_steps())
    assert len(re.findall(r"<col\b", html)) == 3
    assert len(re.findall(r"<th\b", re.search(r"<thead>.*?</thead>", html).group(0))) == 3
    for gone in ("bdh", "bdc", "jbd", "Who reads it today", "btree"):
        assert gone not in html, gone
    assert 'class="jpanel jnarrow"' in html, "the narrow panel keeps its SSD part"


def test_a_step_row_has_its_parts_in_order_with_the_cadence_last():
    html = control_render._journey(_two_steps())
    row = re.search(r'<tr class="jr"[^>]*data-ds="alphavantage/listing_status"[^>]*>(.*?)</tr>', html).group(1)
    cell = re.search(r'<td class="js">(.*?)</td>', row).group(1)
    order = [cell.index(x) for x in ('class="jcar"', 'class="stn"', "Listing status of every US name, active and "
                                     "delisted", 'class="sic', 'class="pill p-ok"', '>first<', "⏱ daily")]
    assert order == sorted(order), order
    assert "jst" not in cell
    assert re.search(r'<span class="jmeta">.*<span class="jcad">⏱ daily</span></span>$', cell), cell


def test_a_build_row_has_from_chips_with_full_names():
    html = control_render._journey(_two_steps())
    row = re.search(r'<tr class="jr"[^>]*data-ds="spine/security"[^>]*>(.*?)</tr>', html).group(1)
    assert ('<span class="chip" data-card="alphavantage/listing_status"><span class="cn">1</span>'
            '<span class="t">alphavantage/listing_status</span></span>') in row
    first = re.search(r'<tr class="jr"[^>]*data-ds="alphavantage/listing_status"[^>]*>(.*?)</tr>', html).group(1)
    assert 'class="chip"' not in first, "only a build row has from chips"


def test_the_heads_carry_the_roots_and_the_rootline_is_gone():
    html = control_render._journey(_two_steps())
    head = re.search(r"<thead>.*?</thead>", html).group(0)
    assert re.search(r"📄 HDD raw · <code[^>]*>/hdd/raw</code>.*📦 Parquet · <code[^>]*>/hdd/parquet</code>"
                     r".*files and size · not walked yet", head)
    assert re.search(r"⚡ SSD live · served.*<code[^>]*>/ssd/live</code>", head)
    assert "rootline" not in html and "Roots ·" not in html
    assert "🦆 fund.duckdb" in html


def test_the_removed_sentences_are_gone():
    html = control_render._journey(_two_steps())
    assert "the file is one piece" not in html
    assert "press a name to pick the table" not in html


def test_the_tree_css_sets_the_widths_and_keeps_paths_on_one_line():
    css = control_render.TREE_CSS
    for w in ("width:42%", "width:38%", "width:max(20%, 245px)", "font-size:9.5px", "white-space:nowrap"):
        assert w in css, w


# ── step 2: the tree ──

def test_the_tree_draws_box_lines_build_lines_and_hit_twins():
    js = control_render.TREE_JS
    for part in ("section.fpanel", ".dset[data-dataset", "tr.jr[data-ds]", "createElementNS", "stroke-dasharray",
                 "pointer-events", "stroke", "--fh", "resize", "toggle", "change", "3.4", "44", "0.45"):
        assert part in js, part
    assert "setInterval" not in js


def test_the_tree_css_hides_it_on_a_phone_and_lets_only_the_twins_take_the_pointer():
    css = control_render.TREE_CSS
    assert re.search(r"@media\(max-width:639\.98px\)\{[^}]*svg\.dtree\{display:none\}", css)
    assert re.search(r"svg\.dtree\{[^}]*position:absolute[^}]*pointer-events:none", css)
    assert re.search(r"svg\.dtree \.hit\{[^}]*pointer-events:stroke", css)
    assert ".dtree-ring" in css and "2px solid" in css
