"""#715 step 3b: the data flow is drawn on a phone too, as one swiped strip of pictures.

avo, 11 Nov 2026: "Telefon icin de ayni flow diagrami yapamaz miyiz? Bir dusunelim", then "Bunlar yan yana
olsa tek bir yerde ve soldan saga dogru kaydirilsa?". The wide diagram is hidden under 760 px and used to be
replaced by a 993 px wall of text; it is now one small vertical diagram per served file -- sources, the file,
the boards that read it -- one per serving kind, side by side in one strip, one picture to a screen.
"""
from __future__ import annotations

import re

from RevenueChaser.module_settings import control, control_render
from RevenueChaser.module_settings.tests.test_control_view import NOW, control_nd
from RevenueChaser.rcdata import serving

#: One picture per serving kind, from the registry: a kind added should not come back here as a count.
FILES = len(serving.KINDS)


def _view() -> dict:
    return control.build(control_nd(), now=NOW)


def _phone(html: str) -> str:
    return html[html.index('<div class="phoneonly">'):]


def test_the_phone_gets_one_picture_for_each_served_file_in_one_strip():
    v = _view()
    phone = _phone(control_render._flow(control_render._ready(v)))
    ids = re.findall(r'<section class="pfslide" id="pfs-(\w+)">', phone)
    assert ids == [f["kind"] for f in v["files"]] and len(ids) == FILES
    assert phone.count('<svg class="pflow"') == FILES, "each file has its own picture, not one wide one"
    assert phone.count('<div class="pfdeck"') == 1, "they sit in one strip"
    assert "wrow" not in phone, "the wall of text is gone"


def test_the_names_above_the_strip_jump_to_a_file_and_the_strip_is_what_scrolls():
    v = _view()
    phone = _phone(control_render._flow(control_render._ready(v)))
    tabs = re.findall(r'<button type="button" class="pftab" data-deck="pfs-(\w+)"', phone)
    assert tabs == [f["kind"] for f in v["files"]]
    assert "swipe sideways for the next file, or press a name" in phone
    css = control_render.CTL_CSS
    deck = css[css.index(".ctl .pfdeck{"):]
    assert "overflow-x:auto" in deck.split("}")[0] and "scroll-snap-type:x mandatory" in deck.split("}")[0]
    assert "scroll-snap-align:start" in css[css.index(".ctl .pfslide{"):].split("}")[0]
    js = control_render.CTL_JS
    blk = js[js.index("/* The phone's flow deck (#715)"):]
    assert "getBoundingClientRect" in blk and "scrollTo" in blk, "it scrolls the strip, never the page"


def test_a_picture_names_its_sources_its_file_and_the_boards_that_read_it():
    v = control_render._ready(_view())
    market = [f for f in v["files"] if f["name"] == "market.duckdb"][0]
    svg = control_render._phone_flow(market, "#58a6ff", {"News": "news.duckdb"})
    assert "yfinance" in svg and "Treasury" in svg
    assert '<text x="179.0"' in svg and ">market.duckdb<" in svg
    assert "News" in svg and "→ news.duckdb" in svg, "a board that writes its own file says which"
    assert 'style="stroke:#58a6ff"' in svg, "the lines keep the file's colour"


def test_a_file_nothing_feeds_says_built_here_and_a_file_no_board_reads_says_so():
    v = control_render._ready(_view())
    spine = [f for f in v["files"] if f["name"] == "spine.duckdb"][0]
    assert "built here" in control_render._phone_flow(spine, "#3fb950", {})
    empty = dict(spine, datasets=[], tables=[])
    svg = control_render._phone_flow(empty, "#3fb950", {})
    assert "no board yet" in svg and "pbd off" in svg


def test_pressing_the_file_in_the_picture_opens_that_file_s_section():
    phone = _phone(control_render._flow(control_render._ready(_view())))
    assert phone.count('class="pfile" data-pick="') == FILES, "the same data-pick the diagram and By file use"
    assert 'data-pick="spine.duckdb"' in phone


def test_a_long_board_name_wraps_instead_of_running_off_the_chip():
    assert control_render._wrap("Unusual Whales Interaction (lab)", 24) == ["Unusual Whales", "Interaction (lab)"]
    assert control_render._wrap("Financials", 24) == ["Financials"]
    assert control_render._wrap("one two three four five six seven eight", 12)[-1].endswith("…")


def test_the_phone_picture_is_drawn_inside_the_phone_s_width():
    svg = control_render._phone_flow(control_render._ready(_view())["files"][0], "#3fb950", {})
    box = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
    assert int(box.group(1)) == 358 <= 390, "the canvas fits a 390 px phone with the card's padding"
    assert int(box.group(2)) > 0
    css = control_render.CTL_CSS
    assert ".ctl svg.pflow{display:block;width:100%" in css, "and it scales to its card"
    assert "min-height:44px" in css[css.index(".ctl .pfhead{"):], "the name row is 44 px"
    assert "min-height:44px" in css[css.index(".ctl .pftab{"):], "and a name button is a 44 px target too"


# ── avo, 13 Nov 2026, on the phone: "çok ayrık ve kopuk ... daha kompakt", and nothing under the files ──────────

def _phone_rules(css: str) -> str:
    """Every rule of ``css`` that sits inside a ``max-width:640px`` block."""
    return "".join(m.group(1) for m in re.finditer(r"@media\(max-width:640px\)\{(.*?\})\s*\}", css, re.S))


def test_the_file_in_the_picture_is_a_44_px_touch_target():
    svg = control_render._phone_flow(control_render._ready(_view())["files"][0], "#3fb950", {})
    box = re.search(r'<g class="pfile"[^>]*><rect [^>]*height="(\d+)"', svg)
    assert int(box.group(1)) == 44


def test_a_build_step_is_two_lines_on_a_phone_with_its_parts_in_fixed_columns():
    # avo on the preview, 13 Nov 2026: no raw and Parquet lines on a phone; icon, status, role and how often on one
    # line, each in a column as wide as the widest value, so the steps line up under each other
    phone = _phone_rules(control_render.CTL_CSS)
    assert ".ctl table.jt3 td.jh{display:none}" in phone, "raw and Parquet are not drawn on a phone"
    assert "grid-template-columns:var(--jw0,25px) var(--jw1,9ch) var(--jw2,9ch) minmax(0,1fr)" in phone
    assert ".ctl table.jt3 .jttl,.ctl table.jt3 .jmeta{display:contents}" in phone, "their parts sit in the columns"
    assert ".ctl table.jt3 .jsrc{grid-row:2;grid-column:1" in phone and ".ctl table.jt3 .jmeta>*{grid-row:2" in phone
    assert ".ctl details.sec.fpanel{margin-top:12px}" in phone, "and two file sections sit close"
    assert "#panel-data .rcf-clear::after{" in phone, "the lookup's clear press reaches 44 px"
    assert ".ctl #sec-flow .flowlegend,.ctl #sec-flow .phoneonly .fnote{display:none}" in phone, \
        "no colour legend and no explanation lines on a phone"


def test_the_icon_column_is_as_wide_as_the_step_with_the_most_icons():
    f = control_render._ready(_view())["files"][0]
    most = max(min(3, len(d["providers"])) for d in f["datasets"])
    assert f"--jw0:{max(1, most) * control_render.ICON_COL}px" in control_render._meta_widths(f)
    assert "--jw0:25px" in control_render._meta_widths({"datasets": []})


def test_the_reference_parts_under_the_files_are_not_drawn_on_a_phone():
    from RevenueChaser.module_settings import needs_tab
    assert "#needs details.hfold{display:none}" in _phone_rules(needs_tab.NEEDS_CSS)
    assert "#needs details.hfold{display:none}" not in needs_tab.NEEDS_CSS.replace(
        "@media(max-width:640px){#needs details.hfold{display:none}}", ""), "the desk keeps them"


def test_a_fill_box_on_a_phone_is_its_name_and_one_coverage_line():
    """avo, 12 Nov 2026 (phone, the Fetch/Health leftovers): "biraz daha kompakt": the label, the bar and the share
    of a coverage meter share one line, and the boxes stand closer."""
    from pathlib import Path
    css = (Path(control_render.__file__).parent / "static" / "control.css").read_text(encoding="utf-8")
    phone = _phone_rules(css)
    assert ".ctl .fgroup .meter.cov{grid-template-columns:auto minmax(0,1fr) auto;align-items:center;gap:8px}" in phone
    assert ".ctl .fgroup .sgrid{gap:6px;margin:6px 0}" in phone
    assert ".ctl .fgroup .dset{margin-top:0;padding:6px 10px}" in phone
