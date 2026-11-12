"""#492 C1: the Data tab's top and each file's sources, from views whose new fields are filled in by hand."""
from __future__ import annotations

import copy
import re

from RevenueChaser.module_settings import control, control_render
from RevenueChaser.rcdata import serving

#: One section, one "By file" line and one picture per serving kind: read from the registry, so the next kind
#: added does not come back here as a count that is one short.
FILES = len(serving.KINDS)
from RevenueChaser.module_settings.tests.test_control_view import NOW, control_nd

_BASE = None


def _view(**over) -> dict:
    global _BASE
    if _BASE is None:
        _BASE = control.build(control_nd(), now=NOW)
    v = copy.deepcopy(_BASE)
    v["timer"] = {"state": "on", "last": "2026-10-30T16:05:00+00:00", "next": "2026-10-30T17:05:00+00:00",
                  "minute": 5}
    for f in v["files"]:
        for d in f["datasets"]:
            d["next_words"] = "in 34 min"
            d["since_words"] = "checked 2 h ago"
        f["state"] = {"exists": False, "bytes": None, "written_at": None, "tables": None, "tables_with_rows": None,
                      "empty": [], "counts": {"fresh": 0, "late": 0, "stale": 0, "never": 0, "total": 0}}
    spine = _file(v, "spine.duckdb")
    spine["state"] = {"exists": True, "bytes": 10_276_044, "written_at": "2026-11-05T18:43:00+00:00", "tables": 9,
                      "tables_with_rows": 7, "empty": ["cik_map", "lei"],
                      "counts": {"fresh": 1, "late": 0, "stale": 0, "never": 0, "total": 1}}
    v["health"]["files"] = [{"name": f["name"], "state": f["state"]} for f in v["files"]]
    v["universe"]["funnel"] = {"stocks": [{"step": "SEC company list", "n": 10430, "drops": ""},
                                          {"step": "US listed, common stock", "n": None, "drops": "ADRs, units"},
                                          {"step": "by the rules", "n": 5655, "drops": "small caps"}],
                               "etfs": [{"step": "ETF list", "n": 3000, "drops": ""},
                                        {"step": "by AUM", "n": 558, "drops": "small funds"}],
                               "added": 2}
    for k, val in over.items():
        v[k] = val
    return v


def _file(view, name):
    return next(f for f in view["files"] if f["name"] == name)


def _box(html: str, ds: str) -> str:
    a = html.index(f'data-dataset="{ds}" style="border-left-color')
    a = html.rindex('<div class="dset sbox"', 0, a)
    nxt = html.find('<div class="dset sbox"', a + 1)
    return html[a:nxt if nxt > 0 else len(html)]


# ── Stage 1: Sources ─────────────────────────────────────────────────────────────────────────────────────────


def test_the_script_routes_the_one_button():
    js = control_render.CTL_JS
    blk = js[js.index("// #492 C1"):]
    assert "X.queue(btn, ds" in blk, "Fetch goes through today's request route (CTLX.queue posts to C.routes.request)"
    assert "C.routes.schedule" in blk
    assert "'/pause'" in blk and "'/resume'" in blk


def test_the_funnel_block_with_a_count_not_counted():
    html = control_render._universe(control_render._ready(_view()))
    assert "How the list comes down to 5,655 stocks and 558 ETFs" in html
    fun = html[html.index('class="funnel"'):]
    assert "<h4>Stocks</h4>" in fun and "<h4>ETFs</h4>" in fun
    assert re.search(r'US listed, common stock</span><b class="fn">not counted</b><span class="fd">ADRs, units', fun)
    v = _view()
    v["universe"].pop("funnel")
    assert 'class="funnel"' not in control_render._universe(control_render._ready(v))


def test_the_sources_css_block_holds_the_grid_and_the_columns():
    css = control_render.CTL_CSS
    blk = css[css.index("/* #492 C1 */"):]
    assert "minmax(0,1fr) minmax(0,.9fr) max-content minmax(0,.75fr) minmax(0,1.6fr)" in blk
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in blk
    assert "@media(max-width:760px)" in blk


# ── Health right now ─────────────────────────────────────────────────────────────────────────────────────────

def _grouped_view():
    v = _view()
    groups = {"fresh": "data", "late": "data", "stale": "data", "never": "data", "problems": "data",
              "session": "build", "queue": "scheduled", "builder": "build", "cap": "sources",
              "backup": "build"}
    for t in v["health"]["tiles"]:
        if t["key"] in groups:
            t["group"] = groups[t["key"]]
    v["health"]["tiles"].append({"key": "timer", "cls": "ok", "n": "on", "label": "the hourly timer is on · next 18:05",
                                 "filter": None, "group": "scheduled"})
    v["health"]["tiles"].append({"key": "longest", "cls": "neu", "n": "41 min", "label": "the longest run today",
                                 "filter": None, "group": "build"})
    return v


# ── The flow diagram and the file picker ─────────────────────────────────────────────────────────────────────

def test_the_flow_loses_its_three_paragraphs_and_hides_the_board_box():
    html = control_render._flow(control_render._ready(_view()))
    assert "Press any box" not in html
    assert "The diagram is drawn from the code" not in html
    assert '<div class="bdinfo empty" id="bdinfo" aria-live="polite" hidden></div>' in html
    assert "Tabler Icons (MIT licence)" in html, "the icons' licence stays named"
    assert "Press a board on the right" not in control_render.CTL_JS


def test_each_served_file_is_its_own_folded_section_not_a_picker_box():
    """#715 step 3 (avo, 11 Nov 2026): no tab under a tab, so the file picker is gone."""
    v = _view()
    html = control_render.render_data(v)
    assert 'id="fpick"' not in html and 'role="tab"' not in html and "aria-selected" not in html
    names = re.findall(r'<details class="sec fpanel" data-file="([^"]+)"', html)
    assert names == [f["name"] for f in v["files"]] and len(names) == FILES
    assert "🦆 9.8 MB" in html and html.count("no file yet") == FILES - 1
    assert "1 line" in html


def test_only_the_first_file_section_is_open_and_none_is_hidden():
    html = control_render.render_data(_view())
    assert html.count('<details class="sec fpanel"') == FILES
    assert len(re.findall(r'<details class="sec fpanel"[^>]* open>', html)) == 1
    opens = re.findall(r'<details class="sec fpanel"[^>]*>', html)
    assert len(opens) == FILES and not [t for t in opens if "hidden" in t], "a section is folded, never hidden"


def test_the_script_opens_a_file_from_the_diagram_and_from_by_file():
    js = control_render.CTL_JS
    blk = js[js.index("/* The served files (#492 C1"):]
    assert "details.fpanel" in blk and "p.open = true" in blk
    assert "data-node^=\"file:\"" in blk or "data-node^='file:'" in blk
    assert "data-pick" in blk and "scrollTo" in blk
    assert "aria-selected" not in blk and ".hidden" not in blk and "removeAttribute" not in blk


# ── One Data panel ───────────────────────────────────────────────────────────────────────────────────────────


def test_render_data_renders_without_the_new_fields():
    html = control_render.render_data(control.build(control_nd(), now=NOW))
    assert html.count('class="sec fpanel"') == FILES and 'id="fpick"' not in html


# ── Layout ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_board_is_left_aligned_and_sections_read_as_sections():
    css = control_render.CTL_CSS
    blk = css[css.index("/* #492 C1 */"):]
    assert re.search(r"\.ctl \.wrap\{[^}]*max-width:1280px[^}]*margin:0[;}]", blk)
    assert re.search(r"\.ctl details\.sec>summary\{[^}]*border-bottom:2px solid", blk)
    assert re.search(r"\.ctl details\.sec>summary\{[^}]*min-height:44px", blk)
    assert ".ctl .scroll{" in blk and "overflow-x:auto" in blk
    assert "overflow-x:hidden" not in blk
