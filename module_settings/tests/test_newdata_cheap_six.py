"""The six cheap suggestions of the review that avo took (step 5 of the combined plan, 29 Oct 2026).

S2 one status is one colour and one icon, with no second mapping on the page · S6 the store rows say when they were
measured · S8 a report that has not run shows a dash and says so · S9 the limit bar shows its empty part and marks
the cap · S10 a disabled button's reason is readable, not muted · S14 the filter is in the address bar, so "4 late"
can be linked to.
"""
from __future__ import annotations

import re
import pytest


from RevenueChaser.core.tests.board_fixtures import newdata_fixture
from RevenueChaser.module_settings import report
from RevenueChaser.module_settings.tests.test_newdata_findings import _card, _page, _section


# ── S2: one status, one colour, one icon ──

def test_s2_the_statuses_are_one_set_with_one_colour_and_one_icon_each():
    assert set(report.ND_STATUS_ICONS) == set(report.ND_STATUS_WORDS)
    in_css = set(re.findall(r"#newdata \.nd-st-([a-z]+)\{", report.ND_CSS))
    assert in_css == set(report.ND_STATUS_WORDS), "a colour for exactly the statuses, no second set"


def test_s2_the_page_carries_no_second_status_mapping():
    sec = _section(_page())
    used = set(re.findall(r"nd-st-([a-z]+)", sec))
    assert used <= set(report.ND_STATUS_WORDS)
    assert "style=\"color" not in sec, "a status colour is a class, never an inline colour"


# ── S6: measured at ──

def test_s6_a_card_says_the_clock_it_was_measured_at():
    card = _card(_section(_page()), "market_series")
    assert "Measured" in card
    assert re.search(r"Measured</span><span class=\"nd-v\">\d\d:\d\d", card), card[:200]


def test_s6_a_card_with_no_measured_clock_says_nothing_rather_than_a_wrong_time():
    nd = newdata_fixture()
    nd["datasets"] = [{**nd["datasets"][0], "measured_at": None}]
    assert "Measured" not in _card(_section(_page(nd)), "market_series")


def test_s6_the_collector_stamps_the_clock_it_read_the_store_at(monkeypatch):
    from datetime import datetime, timezone
    from RevenueChaser.module_settings import newdata
    now = datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc)
    assert newdata._measured_at(now) == "18:00"


# ── S8: a report that has not run ──

def test_s8_a_report_that_has_not_run_shows_a_dash_and_says_so():
    nd = newdata_fixture()
    nd["datasets"] = [{**nd["datasets"][0], "wrds": {"text": "no WRDS run yet", "produced": False}}]
    row = re.search(r'WRDS difference</span><span class="nd-v">(.*?)</span>',
                    _card(_section(_page(nd)), "market_series"), re.S).group(1)
    assert report.DASH in row and "not produced yet" in row


def test_s8_a_report_that_has_run_shows_its_own_words():
    nd = newdata_fixture()
    nd["datasets"] = [{**nd["datasets"][0], "wrds": {"text": "as of 2026-10-27: all equal", "produced": True}}]
    row = re.search(r'WRDS difference</span><span class="nd-v">(.*?)</span>',
                    _card(_section(_page(nd)), "market_series"), re.S).group(1)
    assert "all equal" in row and "not produced yet" not in row


def test_s8_the_collector_marks_a_slot_that_has_not_run():
    from RevenueChaser.module_settings import newdata
    assert newdata.NO_WRDS == {"text": "no WRDS run yet", "produced": False}


# ── S9: the limit bar ──

def test_s9_the_bar_shows_its_empty_part_and_marks_the_cap():
    rule = re.search(r"#newdata \.nd-bar\{([^}]*)\}", report.ND_CSS).group(1)
    assert "border" in rule, "the empty part of the bar has an edge, so it is visible at all"
    assert re.search(r"#newdata \.nd-bar::after\{[^}]*right:0", report.ND_CSS), "the cap line sits at the right end"


def test_s9_a_full_bar_still_shows_the_cap_line():
    assert 'class="nd-bar"' in report._nd_bar(9, 9)
    assert "width:100.0%" in report._nd_bar(9, 9)


# ── S10: a disabled button's reason ──

def test_s10_the_reason_a_button_is_off_is_not_muted_text():
    nd = newdata_fixture()
    nd["datasets"] = [{**nd["datasets"][0], "backfill": {}}]
    card = _card(_section(_page(nd)), "market_series")
    reason = re.search(r'<div class="([^"]*)">[^<]*backfill view[^<]*', card).group(1)
    assert "nd-k" not in reason, "the reason a control is off is read, so it is not muted"
    assert "nd-off-why" in reason
    assert re.search(r"#newdata \.nd-off-why\{[^}]*var\(--fg\)", report.ND_CSS)


# ── S14: the filter in the address bar ──

def test_s14_the_section_offers_one_link_per_status_carrying_the_filter():
    sec = _section(_page())
    links = dict(re.findall(r'<a class="nd-chip nd-filter[^"]*" href="\?nd_status=([a-z]*)[^"]*"[^>]*>(.*?)</a>',
                            sec, re.S))
    assert set(links) >= set(report.ND_STATUS_WORDS) | {""}, links
    assert all("#newdata" in h for h in re.findall(r'nd-filter[^"]*" href="([^"]+)"', sec))


def test_s14_the_filter_is_read_from_the_address_and_written_back_without_a_reload():
    assert "nd_status" in report.ND_JS
    assert "history.replaceState" in report.ND_JS
    assert "location.reload" not in report.ND_JS
    assert "ndFilter" in report.ND_JS


def test_s14_the_filter_hides_a_row_by_its_own_status_attribute():
    sec = _section(_page())
    assert 'data-status="late"' in sec and 'data-status="fresh"' in sec
    assert "getAttribute('data-status')" in report.ND_JS


# ── The review's m2: the map must not serialise as empty ──

def test_the_screens_map_carries_its_entries_into_a_copy_and_into_json():
    """It used to subclass ``dict`` with the underlying storage left empty, so ``[]``, ``get`` and ``{**x}`` all
    worked while ``.copy()`` returned ``{}`` and ``json.dumps`` wrote ``{}``. A label map that silently serialises
    as empty is exactly the quiet failure the rest of this work goes out of its way to prevent."""
    import json

    from RevenueChaser.module_settings import newdata
    labels = newdata.LABELS
    assert labels, "the map is empty"
    assert dict(labels) == dict(labels.items()) and len(labels.copy()) == len(labels)
    assert json.loads(json.dumps(dict(labels))) == {k: v for k, v in labels.items()}
    with pytest.raises(TypeError):
        labels["spine/security"] = "no"


def test_the_boards_lag_map_is_the_live_number_in_a_copy_too(monkeypatch):
    """m3: the same trap on the other side. ``MAX_LAG`` seeded its storage at import, so ``.copy()`` and
    ``json.dumps`` answered with the import-time snapshot while ``[]`` answered with the catalog's live value."""
    import dataclasses

    from RevenueChaser.module_earnings_radar import store_reads
    from RevenueChaser.rcdata import api
    need = api.catalog.NEEDS["macro.dgs10"]
    monkeypatch.setitem(api.catalog.NEEDS, "macro.dgs10", dataclasses.replace(need, max_lag=99))
    assert store_reads.MAX_LAG["dgs10"] == 99
    assert dict(store_reads.MAX_LAG) == {"dgs10": 99} and store_reads.MAX_LAG.copy() == {"dgs10": 99}
