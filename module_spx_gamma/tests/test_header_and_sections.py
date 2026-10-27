"""The board's top bar and which sections start collapsed (phone pass, commit 2; AGENTS.md section 6)."""
from __future__ import annotations

import re

import pytest

from RevenueChaser.module_spx_gamma import render
from .conftest import make_snapshot


def _header(html: str) -> str:
    return re.search(r'<div class="header">(.*?)</div>', html, re.S).group(1)


def test_the_top_bar_shows_a_friendly_last_sync_and_no_iso_timestamp():
    snap = make_snapshot()
    snap["api_snapshot_time"] = "2026-10-23T21:14:05.123456+00:00"
    header = _header(render.render_html(snap))
    assert "Last sync: 23 Oct" in header and "EOD" in header
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", header), header
    assert "Data retrieved" not in header


def test_the_last_sync_is_the_new_york_day():
    snap = make_snapshot()
    snap["api_snapshot_time"] = "2026-10-24T01:30:00Z"        # 21:30 on the 23rd in New York
    assert "Last sync: 23 Oct" in _header(render.render_html(snap))


@pytest.mark.parametrize("stamp", [None, "", "not a time"])
def test_a_missing_stamp_falls_back_to_the_report_date_not_to_none(stamp):
    snap = make_snapshot()
    snap["api_snapshot_time"] = stamp
    header = _header(render.render_html(snap))
    assert "Last sync: 14 Aug" in header and "None" not in header


COLLAPSED = ["Greek Relations — How They Interact", "Details", "How to read this map (Gamma)",
             "Delta Map — Walkthrough", "Greek Sözlüğü & İlişkiler — Türkçe"]
OPEN = ["At a Glance — Single Source of Truth", "SPX Option Position Map", "Net GEX Tornado — Strike Profile",
        "Liquidity Layer — Dealer Flow", "Scenario Set"]


def _html():
    return render.render_html(make_snapshot())


@pytest.mark.parametrize("title", COLLAPSED)
def test_the_reference_sections_start_collapsed(title):
    html = _html().replace("&amp;", "&")
    m = re.search(r'<details class="collapsible"(?P<open>[^>]*)>\s*<summary><h2[^>]*>' + re.escape(title)
                  + r"</h2></summary>", html)
    assert m, f"{title} is not a <details> whose summary holds its <h2> (heading navigation must still find it)"
    assert "open" not in m.group("open"), f"{title} starts open"


@pytest.mark.parametrize("title", OPEN)
def test_the_working_sections_stay_open(title):
    html = _html().replace("&amp;", "&")
    assert f"<h2>{title}</h2>" in html, f"{title} lost its heading"
    assert "<summary><h2" + f">{title}</h2></summary>" not in html


def test_the_markdown_header_has_the_friendly_last_sync_and_no_iso_timestamp():
    """#195 review NIT: section_position_map.md.j2 still printed 'Data retrieved <ISO>'."""
    snap = make_snapshot()
    snap["api_snapshot_time"] = "2026-10-23T21:14:05+00:00"
    first = render.render_markdown(snap).splitlines()[0]
    assert "Last sync: 23 Oct EOD" in first and "Unusual Whales API" in first
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", first), first
    assert "Data retrieved" not in first


def test_eod_never_wraps_onto_a_line_of_its_own():
    """#195 review NIT: at 390 px 'EOD' wrapped away from its date; a no-break space keeps them together."""
    import html as _html
    snap = make_snapshot()
    snap["api_snapshot_time"] = "2026-10-23T21:14:05+00:00"
    header = _html.unescape(_header(render.render_html(snap)))
    assert "23 Oct EOD" in header, repr(header)
