"""#527: the RC Data Health and RC Fetch Grafana boards live inside the app, at the head of the Fetch/Health tab
(avo, 10 Nov 2026: "iki tabdan daha fazlasi olmasin"; the Live tab is gone).

avo, 8 Nov 2026: "bunlarin ne isi var burada? bunlar revenue chaser app icine gomulu olmaliydi". Grafana runs on
the same host as the dashboard, port 3000, and allows only this app's origin to frame it (home-server units/
grafana-server.service.d/life.conf, CSP frame-ancestors). The frame loads only when the tab opens.
"""
from __future__ import annotations

import re

from RevenueChaser.module_settings import report


def _page() -> str:
    return report.render_settings_dashboard({"newdata": {"error": "not read in this test"}, "tickers": []})


def test_two_tabs_only_and_the_frame_heads_fetch_health_with_rc_fetch_first():
    html = _page()
    strip = html[html.index('id="settings-tabs"'):html.index("</nav>")]
    assert re.findall(r'role="tab" id="(tab-[a-z]+)"', strip) == ["tab-data", "tab-health"]
    assert 'id="panel-live"' not in html and "live" not in report.SETTINGS_TABS["tabs"]
    health = html[html.index('id="panel-health"'):]
    assert health.index('id="live-frame"') < health.index("<h1>Data/Health · Fetch/Health</h1>")
    assert report.LIVE_BOARDS[0][0] == "rc-fetch"
    assert "getElementById('panel-health')" in report.LIVE_JS


def test_the_frame_has_no_source_until_the_tab_opens():
    """A Data/Health visit must not load Grafana: the frame gets its address from the script, on the tab's open."""
    html = _page()
    frame = re.search(r"<iframe[^>]*id=\"live-frame\"[^>]*>", html).group(0)
    assert " src=" not in frame
    assert 'title="' in frame and 'loading="lazy"' in frame


def test_the_boards_and_the_grafana_address():
    html = _page()
    for uid in ("rc-data-health", "rc-fetch", "rc-activity", "rc-logs"):
        assert f'data-board="{uid}"' in html
    js = report.LIVE_JS
    assert "location.hostname" in js and "':3000/d/'" in js, "same host as the page, Grafana's port"
    assert "kiosk" in js and "theme=" in js
    assert "rc-data-health" in report.LIVE_HTML and "rc-fetch" in report.LIVE_HTML



def test_the_health_tab_no_longer_draws_the_live_parts():
    """#527 B4, decision D4 = B (avo, 8 Nov 2026 02:39): the queue, the running jobs and the "right now" strip leave
    the Health tab once Grafana shows them; the Grafana frame now heads the same tab, so no link is needed."""
    from RevenueChaser.module_settings.tests.test_control_view import control_nd
    from RevenueChaser.module_settings.tests.test_report import _panel, _payload
    html = report.render_settings_dashboard(dict(_payload(), newdata=control_nd(),
                                                 ibkr_login_url="https://gateway.example/"))
    health = _panel(html, "panel-health")
    assert 'id="sec-queue"' not in health and 'id="health-queue"' not in health
    # avo, 10 Nov 2026: the switches moved with every control to Fetch/Health, once
    assert health.count('class="topbar"') == 1 and "sw-msg-h" not in health
    assert 'id="health-live-link"' not in health and 'id="live-frame"' in health
    data = _panel(html, "panel-data")
    assert 'class="topbar"' not in data and 'href="https://gateway.example/"' in health
