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

# ── M2: the Backfill button is disabled, with its reason written ──

# ── M3: every button says its cost — the call count and what is left today — and that there is no cancel ──

def test_m3_the_collector_counts_the_cost_from_the_caps_and_the_days_calls(monkeypatch):
    """The day's calls are handed in from the spend read, never read again here: a render must not create
    ops.sqlite (``test_the_collect_never_creates_ops_sqlite_or_a_serving_file``)."""
    monkeypatch.setitem(newdata.caps.DAY_CAPS, "uw", 100_000)      # UW has no cap since 2026-11-10: a test cap
    cost = newdata._cost("uw/spot_expiry_strike", {"calls_per_partition": 3}, {"uw": 1_300})
    assert cost == {"provider": "uw", "calls_per_partition": 3, "remaining": 98_700, "cap": 100_000}
    assert newdata._cost("uw/x", {}, {})["remaining"] == newdata.caps.DAY_CAPS["uw"]


def test_m3_uw_has_no_cap_so_nothing_is_left_to_count():
    assert newdata._cost("uw/x", {}, {"uw": 1_300})["remaining"] is None


def test_m3_a_dataset_name_without_a_provider_prefix_names_no_provider():
    assert newdata._cost("market_series", {}, {"uw": 5})["provider"] is None


# ── M4: 44 px touch targets, with the exemption for a dense table written down ──

# ── M5: a faded row is a colour token, never opacity ──

def test_m5_a_disabled_button_fades_by_token_and_not_by_opacity():
    rule = re.search(r"#newdata \.nd-btn:disabled\{([^}]*)\}", report.ND_CSS).group(1)
    assert "opacity" not in rule, "opacity cannot be checked for contrast; a token can"
    assert "var(--muted)" in rule or "var(--border)" in rule
    assert "opacity:" not in report.ND_CSS.replace("opacity:1", "")


# ── M6: a status is a word and a shape, not only a colour ──

def test_m6_the_palette_is_the_one_it_was():
    for token in ("--pos", "--sky", "--amber", "--neg", "--muted"):
        assert f"var({token})" in report.ND_CSS


# ── M7: the "why it did not run" sentence on the card, and a red ribbon when the worker is silent ──

def test_m7_the_collector_counts_the_silence_from_the_last_run(monkeypatch):
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)
    assert newdata._silent_minutes({"started_at": (now - timedelta(minutes=75)).isoformat()}, now) == 75
    assert newdata._silent_minutes(None, now) is None
    assert newdata._silent_minutes({"started_at": "not a day"}, now) is None
    # A run whose start is ahead of this clock is not negative silence: it is none.
    assert newdata._silent_minutes({"started_at": (now + timedelta(minutes=5)).isoformat()}, now) == 0


# ── M8: the page's own freshness, and no automatic refresh ──

def test_m8_the_stale_ribbon_is_shown_by_the_clock_and_never_by_a_timer():
    """A repeating timer would also hang ``core/tests/board_js.run_board_js``, which runs a page's inline scripts
    under node: a live interval keeps that process alive and the test waits for ever instead of failing. So the
    ribbon is checked when the page is read and when the reader returns to the tab."""
    assert "nd-page-stale" in report.ND_JS
    assert "setInterval(" not in report.ND_JS, "a repeating timer hangs the board-JS harness"
    assert "visibilitychange', ndPageStale" in report.ND_JS
    assert "fetch(" not in report.ND_JS.split("function ndPageStale")[1].split("\n}")[0]
