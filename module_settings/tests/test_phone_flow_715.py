"""#715 step 3b: the data flow is drawn on a phone too, as one swiped strip of pictures.

avo, 11 Nov 2026: "Telefon icin de ayni flow diagrami yapamaz miyiz? Bir dusunelim", then "Bunlar yan yana
olsa tek bir yerde ve soldan saga dogru kaydirilsa?". The wide diagram is hidden under 760 px and used to be
replaced by a 993 px wall of text; it is now one small vertical diagram per served file -- sources, the file,
the boards that read it -- six of them side by side in one strip, one picture to a screen.
"""
from __future__ import annotations

import re

from RevenueChaser.module_settings import control, control_render
from RevenueChaser.module_settings.tests.test_control_view import NOW, control_nd


def _view() -> dict:
    return control.build(control_nd(), now=NOW)


def _phone(html: str) -> str:
    return html[html.index('<div class="phoneonly">'):]


def test_the_phone_gets_one_picture_for_each_served_file_in_one_strip():
    v = _view()
    phone = _phone(control_render._flow(control_render._ready(v)))
    ids = re.findall(r'<section class="pfslide" id="pfs-(\w+)">', phone)
    assert ids == [f["kind"] for f in v["files"]] and len(ids) == 6
    assert phone.count('<svg class="pflow"') == 6, "each file has its own picture, not one wide one"
    assert phone.count('<div class="pfdeck"') == 1, "the six sit in one strip"
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
    assert phone.count('class="pfile" data-pick="') == 6, "the same data-pick the diagram and By file use"
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
