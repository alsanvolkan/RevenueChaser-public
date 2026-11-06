"""#492 C1: the Data tab's top and each file's sources, from views whose new fields are filled in by hand."""
from __future__ import annotations

import copy
import re

from RevenueChaser.module_settings import control, control_render
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
    assert re.search(r"plan step 1–7 · the spine · 1 sources · 1 fresh · 0 late · 0 stale · 0 never ran", html)
    assert ">Collapse all</button>" in html and ">Open all</button>" in html
    assert ("🦆 spine.duckdb · on the disk · 9.8 MB · written 5 Nov 19:43 · 7 of 9 tables hold rows · empty: "
            "cik_map, lei") in html
    assert "One button for each dataset: <b>Fetch</b> asks for a run" in html
    assert "holds the dataset: the hourly pass skips it" in html
    top = html[:html.index('class="sgrid"')]
    assert "stops" not in top and "cancels" not in top


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
    _file(v, "prices.duckdb")["datasets"][0]["paused"] = True
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


def test_the_universe_sits_in_the_spine_box_with_its_new_words():
    spine = _box(_sources(_view(), "spine.duckdb"), "spine/security")
    sb5 = spine[spine.index('class="sb5"'):]
    assert 'id="universe"' in sb5
    assert "hover them, or press and hold" not in sb5
    assert ("A Fetch of only some tier sets also brings each group whose chip is on (a Fetch of the whole universe "
            "brings all three anyway):") in sb5
    assert sb5.count('<span class="uoffl">not offered</span>') == 2


def test_the_funnel_block_with_a_count_not_counted():
    html = _sources(_view(), "spine.duckdb")
    assert "How the list comes down to 5,655 stocks and 558 ETFs" in html
    fun = html[html.index('class="funnel"'):]
    assert "<h4>Stocks</h4>" in fun and "<h4>ETFs</h4>" in fun
    assert re.search(r'US listed, common stock</span><b class="fn">not counted</b><span class="fd">ADRs, units', fun)
    v = _view()
    v["universe"].pop("funnel")
    assert 'class="funnel"' not in _sources(v, "spine.duckdb")


def test_providers_sit_once_under_each_panels_boxes_with_their_source_line():
    html = _sources(_view(), "prices.duckdb")
    assert html.count("Providers and daily limits") == 1
    assert html.index('class="sgrid"') < html.index("Providers and daily limits")
    assert ("day caps and request windows: <code>rcdata/caps.py</code> (<code>DAY_CAPS</code>, <code>WINDOWS</code>)"
            " · what each provider brings, our pace and its rule: <code>rcdata/needs/screen.py</code> "
            "(<code>PROVIDERS</code>) · used today: <code>ops.provider_calls_daily</code>") in html


def test_the_sources_css_block_holds_the_grid_and_the_columns():
    css = control_render.CTL_CSS
    blk = css[css.index("/* #492 C1 */"):]
    assert "minmax(0,1fr) minmax(0,.9fr) max-content minmax(0,.75fr) minmax(0,1.6fr)" in blk
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in blk
    assert "@media(max-width:760px)" in blk
