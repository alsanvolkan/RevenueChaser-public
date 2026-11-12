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
from RevenueChaser.module_settings.tests.legacy_fetch import old_fetch

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


def _sources(view, name) -> str:
    return control_render._sources(_file(view, name), control_render._ready(view))


def _box(html: str, ds: str) -> str:
    a = html.index(f'data-dataset="{ds}" style="border-left-color')
    a = html.rindex('<div class="dset sbox"', 0, a)
    nxt = html.find('<div class="dset sbox"', a + 1)
    return html[a:nxt if nxt > 0 else len(html)]


# ── Stage 1: Sources ─────────────────────────────────────────────────────────────────────────────────────────

def test_the_sources_head_counts_and_the_file_state_line():
    html = _sources(_view(), "spine.duckdb")
    assert "<span class=\"stn\">1</span> Sources · spine.duckdb</h3>" in html
    assert "plan step" not in html and '<span class="dim">the spine</span>' in html
    assert ">Collapse all</button>" in html and ">Open all</button>" in html
    assert ("🦆 spine.duckdb · on the disk · 9.8 MB · written 5 Nov 19:43 · 7 of 9 tables hold rows · empty: "
            "cik_map, lei") in html
    assert "One button for each dataset: <b>Fetch</b> asks for a whole run" in html
    assert "stops a running one before its next provider call and holds the dataset" in html
    top = html[:html.index('class="sgrid"')]
    assert "stops" in top and "cancels" in top, "#492 PR (ii): a Pause now stops and cancels, and the legend says so"


def test_a_missing_file_says_no_file_yet():
    assert "🦆 prices.duckdb · no file yet" in _sources(_view(), "prices.duckdb")


def test_a_source_box_has_its_five_slots_in_order():
    box = _box(_sources(_view(), "prices.duckdb"), "alpaca/bars")
    at = [box.index(f'class="{c}"') for c in ("sb1", "sb2", "sbody", "sb3", "sb4", "sb5")]
    assert at == sorted(at)
    head = box[:box.index('class="sb2"')]
    assert 'class="sbtog"' in head and 'class="sdot' in head and 'class="srcb"' in head and "Alpaca" in head
    assert 'data-job="one"' in head
    state = box[box.index('class="sb2"'):box.index('class="sbody"')]
    assert ">late</span>" in state and "⏱ daily" in state and "next in 34 min" in state and "checked 2 h ago" in state
    assert "meter cov" in box[box.index('class="sb3"'):box.index('class="sb4"')]
    assert "last run" in box[box.index('class="sb4"'):box.index('class="sb5"')]
    assert "Backfill…" in box[box.index('class="sb5"'):]


def test_a_box_drops_the_reason_the_cost_line_and_the_no_tier_line():
    v = _view()
    for f in v["files"]:
        for d in f["datasets"]:
            d["status_words"] += " · the reason"
    html = "".join(_sources(v, f["name"]) for f in v["files"])
    assert "the reason" not in html
    assert "cannot be taken back" not in html and "puts one request in the queue" not in html
    assert "No tier choice" not in html


def test_the_one_button_reads_fetch_pause_or_resume_and_no_separate_pause_is_left():
    v = _view()
    v["queue"] = {"active": [], "finished": []}
    idle = _box(_sources(v, "prices.duckdb"), "alpaca/bars")
    assert 'data-job="one" data-state="fetch" data-dataset="alpaca/bars" data-scope="1">Fetch</button>' in idle
    v["queue"] = {"active": [{"id": 4, "dataset": "alpaca/bars", "status": "running"}], "finished": []}
    busy = _box(_sources(v, "prices.duckdb"), "alpaca/bars")
    assert '<span class="lg3 st-r">running</span>' in busy
    assert 'data-state="pause" data-dataset="alpaca/bars"' in busy and ">Pause</button>" in busy
    v["queue"] = {"active": [{"id": 4, "dataset": "alpaca/bars", "status": "queued"}], "finished": []}
    assert '<span class="lg3 st-q">queued</span>' in _box(_sources(v, "prices.duckdb"), "alpaca/bars")
    next(d for d in _file(v, "prices.duckdb")["datasets"] if d["dataset"] == "alpaca/bars")["paused"] = True
    held = _box(_sources(v, "prices.duckdb"), "alpaca/bars")
    assert 'data-state="resume"' in held and ">Resume</button>" in held
    every = "".join(_sources(v, f["name"]) for f in v["files"])
    assert 'data-job="pause"' not in every and 'data-job="fetch" data-dataset="alpaca/bars"' not in every


def test_hand_run_and_universe_fetched_keep_their_own_press():
    v = _view()
    html = "".join(_sources(v, f["name"]) for f in v["files"])
    assert 'data-job="copy"' in html and '<button type="button" class="btn" disabled>Fetch</button>' in html
    spine = _box(_sources(v, "spine.duckdb"), "spine/security")
    assert 'data-goto="universe">Fetch the' in spine


def test_the_script_routes_the_one_button():
    js = control_render.CTL_JS
    blk = js[js.index("// #492 C1"):]
    assert "X.queue(btn, ds" in blk, "Fetch goes through today's request route (CTLX.queue posts to C.routes.request)"
    assert "C.routes.schedule" in blk
    assert "'/pause'" in blk and "'/resume'" in blk


def test_the_universe_sits_at_the_top_of_fetch_health_with_its_new_words():
    spine = _box(_sources(_view(), "spine.duckdb"), "spine/security")
    assert 'id="universe"' not in spine
    fetch = old_fetch(_view())
    sb5 = fetch[fetch.index('id="sec-universe"'):fetch.index('class="fgrps"')]
    assert 'id="universe"' in sb5
    assert "hover them, or press and hold" not in sb5
    assert ("A Fetch of only some tier sets also brings each group whose chip is on (a Fetch of the whole universe "
            "brings all three anyway):") in sb5
    assert sb5.count('<span class="uoffl">not offered</span>') == 2


def test_the_funnel_block_with_a_count_not_counted():
    html = control_render._universe(control_render._ready(_view()))
    assert "How the list comes down to 5,655 stocks and 558 ETFs" in html
    fun = html[html.index('class="funnel"'):]
    assert "<h4>Stocks</h4>" in fun and "<h4>ETFs</h4>" in fun
    assert re.search(r'US listed, common stock</span><b class="fn">not counted</b><span class="fd">ADRs, units', fun)
    v = _view()
    v["universe"].pop("funnel")
    assert 'class="funnel"' not in control_render._universe(control_render._ready(v))


def test_providers_sit_once_on_fetch_health_above_the_universe_with_their_source_line():
    html = old_fetch(_view())
    assert html.count("Providers and daily limits") == 1 and "Providers and daily limits" not in _sources(
        _view(), "prices.duckdb")
    assert html.index('id="sec-health"') < html.index('id="sec-providers"') < html.index('id="sec-universe"')
    assert ("day caps and request windows: <code>rcdata/caps.py</code> (<code>DAY_CAPS</code>, <code>WINDOWS</code>)"
            " · what each provider brings, our pace and its rule: <code>rcdata/needs/screen.py</code> "
            "(<code>PROVIDERS</code>) · used today: <code>ops.provider_calls_daily</code>") in html


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


def _health(v) -> str:
    return control_render._health(v)


def test_health_has_three_groups_side_by_side_then_data_at_full_width():
    html = _health(_grouped_view())
    at = [html.index(f'data-group="{g}"') for g in ("sources", "scheduled", "build", "data")]
    assert at == sorted(at)
    assert '<h3 class="hgh">Sources</h3>' in html and '<h3 class="hgh">Scheduled run</h3>' in html
    assert '<h3 class="hgh">Build</h3>' in html
    assert '<h3 class="hgh">Data · 5 datasets</h3>' in html


def test_each_tile_sits_in_its_group_and_keeps_its_filter():
    html = _health(_grouped_view())

    def grp(g):
        a = html.index(f'data-group="{g}"')
        return html[a:html.find('data-group="', a + 1) if html.find('data-group="', a + 1) > 0 else len(html)]
    assert 'data-f="queue"' in grp("scheduled") and "the hourly timer is on" in grp("scheduled")
    assert 'data-f="cap"' in grp("sources") and 'data-f="builder"' in grp("build")
    assert "the longest run today" in grp("build")
    assert 'data-f="fresh"' in grp("data")
    assert 'data-f="universe"' in grp("data"), "a tile with no group goes in Data"
    for t in _grouped_view()["health"]["tiles"]:
        if t["filter"]:
            assert f'data-f="{t["filter"]}"' in html
        assert f'title="{t["label"]}' in html.replace("&#x27;", "'") or t["label"].replace("'", "&#x27;") in html


def test_by_file_lines_under_data():
    html = _health(_grouped_view())
    by = html[html.index('class="byfile"'):]
    assert f"1 of {FILES} files on the disk" in by
    assert by.count('class="bfl"') == FILES
    assert "🦆 on the disk · 9.8 MB · written 5 Nov 19:43 · 7 of 9 tables hold rows · empty: cik_map, lei" in by
    assert by.count("no file yet") == FILES - 1
    assert 'data-pick="spine.duckdb"' in by
    assert 'class="seg fresh"' in by


def test_health_renders_without_the_new_fields():
    v = control.build(control_nd(), now=NOW)
    html = control_render._health(v)
    assert 'data-group="data"' in html and "Data · 5 datasets" in html


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

def test_render_data_holds_the_parts_in_order():
    html = control_render.render_data(_view())
    marks = ['<h1>Data/Health · Database</h1>', 'id="sec-flow"', 'class="sec fpanel" data-file="spine.duckdb"']
    at = [html.index(m) for m in marks]
    assert at == sorted(at)
    assert 'id="sec-health"' not in html and 'id="sec-crosscheck"' not in html and "data-job=" not in html
    fetch = old_fetch(_view())
    marks = ['<h1>Data/Health · Fetch/Health</h1>', 'class="topbar"', 'id="sec-health"', 'id="sec-providers"',
             'id="sec-universe"', 'class="fgrps"', 'class="pictrow"', 'id="sec-crosscheck"']
    at = [fetch.index(m) for m in marks]
    assert at == sorted(at)
    # #527: the board's own help box folded into the page's one shared box (report.DATA_HELP)
    from RevenueChaser.module_settings import report
    assert ("Pause cancels a queued request, stops a running fetch before its next provider call and holds the "
            "dataset until Resume.") in report.DATA_HELP


def test_one_section_per_file_in_plan_order_only_the_first_open():
    v = _view()
    html = control_render.render_data(v)
    panels = re.findall(r'<details class="sec fpanel" data-file="([^"]+)" id="fp-\w+" style="--fh:#[0-9a-f]{6}"( open)?>',
                        html)
    assert [p[0] for p in panels] == [f["name"] for f in v["files"]]
    assert [bool(p[1]) for p in panels] == [True] + [False] * (FILES - 1)
    one = html[html.index('class="sec fpanel" data-file="prices.duckdb"'):html.index('class="sec fpanel" data-file="fund')]
    assert 'class="journey"' in one and 'class="sources' not in one and 'class="pictrow"' not in one
    fetch = old_fetch(v)
    groups = re.findall(r'<details class="sec fgrp" data-file="([^"]+)"', fetch)
    assert groups == [f["name"] for f in v["files"]], "Fetch/Health: one group a file, in plan order"
    assert fetch.index('class="fgrps"') < fetch.index('class="pictrow"')


def test_the_old_controls_are_gone_and_the_universe_is_on_fetch_health_once():
    html = control_render.render_data(_view())
    assert 'id="openall"' not in html and 'id="shutall"' not in html and 'id="dataempty"' not in html
    assert "No file matches the diagram" not in html
    fetch = old_fetch(_view())
    assert html.count('id="universe"') == 0 and fetch.count('id="universe"') == 1
    assert "WIAL" not in html and "cannot be taken back" not in control_render.WIAL


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
