"""The watch-list pair is one box with two icon halves, stock first (#770).

The owner asked for a glyph instead of the ``Opt`` / ``Stk`` text and for the two halves to sit in a single box,
stock on the left. The label text stays in the accessible name and the title, so the control is never icon-only
to a screen reader, and the two lists stay distinguishable by shape and position, not only by colour."""
from __future__ import annotations

import re

from RevenueChaser.core import reporting_common as rc


def _buttons(html: str) -> list[str]:
    return re.findall(r"<button\b[^>]*>.*?</button>", html, re.S)


def test_stock_comes_first():
    assert list(rc.WATCH_LABELS) == ["stock", "option"]
    order = [re.search(r'data-rc-watch="(\w+)"', b).group(1) for b in _buttons(rc.watch_buttons_html("NVDA"))]
    assert order == ["stock", "option"]


def test_each_half_is_a_glyph_and_not_a_word():
    for b in _buttons(rc.watch_buttons_html("NVDA")):
        assert "<svg" in b
        assert not re.search(r">\s*[A-Za-z]", re.sub(r"<svg.*?</svg>", "", b, flags=re.S)), "no text label"


def test_the_name_moved_into_the_label_rather_than_being_dropped():
    """The visible word is gone from the button; the same words have to still be its accessible name."""
    html = rc.watch_buttons_html("NVDA", on=("stock",))
    assert 'aria-label="NVDA on the stock watch list"' in html
    assert 'aria-label="NVDA on the option watch list"' in html
    assert 'aria-pressed="true"' in html and 'aria-pressed="false"' in html
    for b in _buttons(html):
        body = re.sub(r"<svg.*?</svg>", "", b, flags=re.S)
        assert re.search(r">\s*</button>$", body), "the glyph is the whole button body"


def test_the_js_twin_keeps_the_same_order_and_icons():
    assert "['stock', 'option']" in rc.WATCHLIST_JS
    assert "RC_WATCH_ICONS" in rc.WATCHLIST_JS
    for glyph in rc.WATCH_ICONS.values():
        assert glyph[:20] in rc.WATCHLIST_JS or glyph[:20].replace('"', '\\"') in rc.WATCHLIST_JS


def test_one_box_two_halves_at_44px():
    css = rc.WATCHLIST_CSS
    assert ".rc-watch{" in css and "border-radius:8px;overflow:hidden" in css
    assert ".rc-watch button + button{border-left:" in css, "a hairline splits the box"
    assert "min-width:44px;min-height:44px" in css


def test_the_two_lists_do_not_share_one_pressed_colour():
    css = rc.WATCHLIST_CSS
    assert 'button[data-rc-watch="stock"][aria-pressed="true"]' in css
    assert 'button[data-rc-watch="option"][aria-pressed="true"]' in css
    assert "--sky" in css and "--amber" in css
