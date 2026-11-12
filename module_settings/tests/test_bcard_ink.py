"""A ``.bcard`` on Settings / Data health keeps the page's dark panel in every theme (#727): its ink and its wrap."""
from __future__ import annotations

from RevenueChaser.module_settings import report
from RevenueChaser.module_settings.tests.test_report import _payload


def test_a_card_s_text_stays_light_on_its_dark_panel_under_the_light_theme():
    # Measured in Chromium, 12 Nov 2026: with data-theme="light" the heading of every .bcard on the Health tab was
    # rgb(29,36,48) on the panel's rgb(18,22,30). The panel is dark in every theme, so its ink is pinned.
    html = report.render_settings_dashboard(_payload())
    assert ':root[data-theme="light"] .bcard{color:var(--text)}' in html
    # auto AND a page whose data-theme is not set yet (it is set from JavaScript): needs.css falls back to light there
    assert '@media (prefers-color-scheme: light){:root:not([data-theme="dark"]) .bcard{color:var(--text)}}' in html


def test_provider_text_can_wrap_so_a_long_token_never_widens_a_phone_page():
    html = report.render_settings_dashboard(_payload())
    assert ".bcard .kv{color:var(--muted);font-size:12.5px;margin:2px 0;overflow-wrap:anywhere}" in html


def test_the_news_feeds_are_not_drawn_on_the_page():
    """avo, 12 Nov 2026 (card: "Grafana'ya taşınınca"): the feeds' state lives on the Grafana Fetch/Health board, from
    ``rc_news_feed_state`` and the board head; the page draws no card for it, whatever the payload carries."""
    html = report.render_settings_dashboard(dict(_payload(), news_tape_state={"state": "refused", "feeds": []}))
    assert 'id="news-tape' not in html
