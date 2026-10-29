"""Fable's eight must-fix findings on the Data screen, in code (step 4 of the combined plan, avo 29 Oct 2026).

Each test is named after the finding it holds. The mock had all eight; these are the same eight in the page the
dashboard actually renders.
"""
from __future__ import annotations

import re

from RevenueChaser.core.tests.board_fixtures import newdata_fixture
from RevenueChaser.module_settings import newdata, report


def _page(nd=None) -> str:
    return report.render_settings_dashboard({"newdata": newdata_fixture() if nd is None else nd})


def _section(html: str) -> str:
    start = html.index('<section id="newdata"')
    return html[start:html.index("</section>", start)]


ROW = '<details class="nd-card nd-dataset"'


def _card(sec: str, ds: str) -> str:
    """One dataset's row. A row holds <details> of its own (its checks), so it ends where the next row begins."""
    start = sec.rindex(ROW, 0, sec.index(f'data-dataset="{ds}"'))
    after = sec.find(ROW, start + 1)
    return sec[start:after if after != -1 else len(sec)]


# ── M1: a dataset row is born closed on a phone; only the worst row of the group is open ──

def test_m1_the_worst_dataset_is_the_only_one_born_open():
    sec = _section(_page())
    rows = re.findall(r'<details class="nd-card nd-dataset(?: [^"]*)?"([^>]*)>', sec)
    assert len(rows) == 2, rows
    assert " open" in rows[0] and "late" in rows[0], "the worst status of the group is the row that is open"
    assert " open" not in rows[1], "every other row is born closed"


def test_m1_the_row_opens_from_its_own_title():
    card = _card(_section(_page()), "market_series")
    assert card.index("<summary") < card.index("Market series") < card.index("</summary>")


# ── M2: the Backfill button is disabled, with its reason written ──

def test_m2_a_dataset_with_no_backfill_view_has_a_disabled_button_and_the_reason():
    nd = newdata_fixture()
    nd["datasets"] = [{**nd["datasets"][0], "backfill": {}}]
    card = _card(_section(_page(nd)), "market_series")
    button = re.search(r'<button[^>]*data-nd-act="backfill"[^>]*>', card).group(0)
    assert "disabled" in button
    assert "no backfill view" in card.lower(), "a disabled button says why it is disabled"


def test_m2_a_dataset_with_a_backfill_view_keeps_its_button():
    card = _card(_section(_page()), "market_series")
    button = re.search(r'<button[^>]*data-nd-act="backfill"[^>]*>', card).group(0)
    assert "disabled" not in button


# ── M3: every button says its cost — the call count and what is left today — and that there is no cancel ──

def test_m3_the_cost_of_a_run_is_beside_the_buttons():
    nd = newdata_fixture()
    nd["datasets"] = [{**nd["datasets"][0], "dataset": "uw/spot_expiry_strike",
                       "cost": {"provider": "uw", "calls_per_partition": 3, "remaining": 148_700, "cap": 150_000}}]
    card = _card(_section(_page(nd)), "uw/spot_expiry_strike")
    assert "3 calls" in card and "148,700" in card and "150,000" in card and "uw" in card


def test_m3_a_dataset_whose_provider_has_no_cap_says_so_rather_than_a_number():
    nd = newdata_fixture()
    nd["datasets"] = [{**nd["datasets"][0],
                       "cost": {"provider": "yahoo", "calls_per_partition": None, "remaining": None, "cap": None}}]
    card = _card(_section(_page(nd)), "market_series")
    assert "no cap" in card.lower()


def test_m3_the_section_says_a_queued_request_cannot_be_cancelled():
    assert "cannot be cancelled" in _section(_page()).lower()


def test_m3_the_collector_counts_the_cost_from_the_caps_and_the_days_calls():
    """The day's calls are handed in from the spend read, never read again here: a render must not create
    ops.sqlite (``test_the_collect_never_creates_ops_sqlite_or_a_serving_file``)."""
    cost = newdata._cost("uw/spot_expiry_strike", {"calls_per_partition": 3}, {"uw": 1_300})
    assert cost == {"provider": "uw", "calls_per_partition": 3, "remaining": 148_700, "cap": 150_000}
    assert newdata._cost("uw/x", {}, {})["remaining"] == newdata.caps.DAY_CAPS["uw"]


def test_m3_a_dataset_name_without_a_provider_prefix_names_no_provider():
    assert newdata._cost("market_series", {}, {"uw": 5})["provider"] is None


# ── M4: 44 px touch targets, with the exemption for a dense table written down ──

def test_m4_the_targets_are_44px_with_one_named_exemption():
    assert "min-height:44px" in report.ND_CSS
    assert ".nd-dense" in report.ND_CSS, "the dense-table exemption (A24-B) is declared, not implied"
    assert "nd-dense" not in _section(_page()), "nothing on the page uses the exemption yet"


# ── M5: a faded row is a colour token, never opacity ──

def test_m5_a_disabled_button_fades_by_token_and_not_by_opacity():
    rule = re.search(r"#newdata \.nd-btn:disabled\{([^}]*)\}", report.ND_CSS).group(1)
    assert "opacity" not in rule, "opacity cannot be checked for contrast; a token can"
    assert "var(--muted)" in rule or "var(--border)" in rule
    assert "opacity:" not in report.ND_CSS.replace("opacity:1", "")


# ── M6: a status is a word and a shape, not only a colour ──

def test_m6_every_status_carries_a_word_and_an_icon():
    for status, word in report.ND_STATUS_WORDS.items():
        assert status in report.ND_STATUS_ICONS, f"{status} has no icon"
    card = _card(_section(_page()), "market_series")
    chip = re.search(r'<span class="nd-chip[^"]*">(.*?)</span>\s*</h3>', card, re.S).group(1)
    assert report.ND_STATUS_ICONS["late"] in chip and "late" in chip
    assert 'aria-hidden="true"' in chip, "the icon is decoration; the word is what a reader hears"


def test_m6_the_palette_is_the_one_it_was():
    for token in ("--pos", "--sky", "--amber", "--neg", "--muted"):
        assert f"var({token})" in report.ND_CSS


# ── M7: the "why it did not run" sentence on the card, and a red ribbon when the worker is silent ──

def test_m7_a_card_that_is_not_fresh_says_why_in_a_sentence_of_its_own():
    card = _card(_section(_page()), "market_series")
    why = re.search(r'<div class="nd-why[^"]*">(.*?)</div>', card, re.S)
    assert why and "one missed" in why.group(1)


def test_m7_a_fresh_card_needs_no_why():
    assert 'class="nd-why' not in _card(_section(_page()), "uw/spot_expiry_strike")


def test_m7_a_worker_silent_longer_than_the_limit_turns_the_ribbon_red():
    nd = newdata_fixture()
    nd["verified"] = {**nd["verified"], "silent_minutes": report.ND_WORKER_SILENT_MINUTES + 1}
    sec = _section(_page(nd))
    ribbon = re.search(r'<div[^>]*id="nd-verified"[^>]*>', sec).group(0)
    assert "nd-bad" in ribbon
    assert f"{report.ND_WORKER_SILENT_MINUTES}" in sec


def test_m7_a_worker_that_ran_within_the_limit_leaves_the_ribbon_alone():
    nd = newdata_fixture()
    nd["verified"] = {**nd["verified"], "silent_minutes": report.ND_WORKER_SILENT_MINUTES - 1}
    ribbon = re.search(r'<div[^>]*id="nd-verified"[^>]*>', _section(_page(nd))).group(0)
    assert "nd-bad" not in ribbon


def test_m7_the_collector_counts_the_silence_from_the_last_run(monkeypatch):
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)
    assert newdata._silent_minutes({"started_at": (now - timedelta(minutes=75)).isoformat()}, now) == 75
    assert newdata._silent_minutes(None, now) is None
    assert newdata._silent_minutes({"started_at": "not a day"}, now) is None
    # A run whose start is ahead of this clock is not negative silence: it is none.
    assert newdata._silent_minutes({"started_at": (now + timedelta(minutes=5)).isoformat()}, now) == 0


# ── M8: the page's own freshness, and no automatic refresh ──

def test_m8_the_page_says_when_it_was_read():
    sec = _section(_page())
    assert re.search(r'id="nd-read-at"[^>]*data-nd-read-at="[^"]+"', sec), "the read time is in the page, machine-readable"
    assert "Read " in sec


def test_m8_a_stale_page_gets_a_ribbon_and_never_refreshes_itself():
    sec = _section(_page())
    assert 'id="nd-page-stale"' in sec and "hidden" in sec[sec.index('id="nd-page-stale"') - 80:]
    assert "Refresh" in sec
    assert f"var ND_PAGE_STALE_MS = {report.ND_PAGE_STALE_MINUTES * 60 * 1000};" in _page()
    assert "ND_PAGE_STALE_MS" in report.ND_JS, "the limit reaches the script as one number, not as a copy"
    assert "location.reload" not in report.ND_JS, "the page never refreshes itself (A26)"


def test_m8_the_stale_ribbon_is_shown_by_the_clock_and_never_by_a_timer():
    """A repeating timer would also hang ``core/tests/board_js.run_board_js``, which runs a page's inline scripts
    under node: a live interval keeps that process alive and the test waits for ever instead of failing. So the
    ribbon is checked when the page is read and when the reader returns to the tab."""
    assert "nd-page-stale" in report.ND_JS
    assert "setInterval(" not in report.ND_JS, "a repeating timer hangs the board-JS harness"
    assert "visibilitychange', ndPageStale" in report.ND_JS
    assert "fetch(" not in report.ND_JS.split("function ndPageStale")[1].split("\n}")[0]
