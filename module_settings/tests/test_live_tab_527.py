"""#527: the RC Data Health and RC Fetch Grafana boards live inside the app, on a Live tab of Data/Health.

avo, 8 Nov 2026: "bunlarin ne isi var burada? bunlar revenue chaser app icine gomulu olmaliydi". Grafana runs on
the same host as the dashboard, port 3000, and allows only this app's origin to frame it (home-server units/
grafana-server.service.d/life.conf, CSP frame-ancestors). The frame loads only when the tab opens.
"""
from __future__ import annotations

import re

from RevenueChaser.module_settings import report


def _page() -> str:
    return report.render_settings_dashboard({"newdata": {"error": "not read in this test"}, "tickers": []})


def test_live_is_the_third_tab_and_starts_hidden():
    html = _page()
    strip = html[html.index('id="settings-tabs"'):html.index("</nav>")]
    assert strip.index('id="tab-data"') < strip.index('id="tab-health"') < strip.index('id="tab-live"')
    assert re.search(r'<div[^>]*id="panel-live"[^>]*role="tabpanel"[^>]*hidden', html)
    assert report.SETTINGS_TABS["tabs"]["live"] == "panel-live"


def test_the_frame_has_no_source_until_the_tab_opens():
    """A Data/Health visit must not load Grafana: the frame gets its address from the script, on the tab's open."""
    html = _page()
    frame = re.search(r"<iframe[^>]*id=\"live-frame\"[^>]*>", html).group(0)
    assert " src=" not in frame
    assert 'title="' in frame and 'loading="lazy"' in frame


def test_the_two_boards_and_the_grafana_address():
    html = _page()
    for uid in ("rc-data-health", "rc-fetch"):
        assert f'data-board="{uid}"' in html
    js = report.LIVE_JS
    assert "location.hostname" in js and "':3000/d/'" in js, "same host as the page, Grafana's port"
    assert "kiosk" in js and "theme=" in js
    assert "rc-data-health" in report.LIVE_HTML and "rc-fetch" in report.LIVE_HTML
