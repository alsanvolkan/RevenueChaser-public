"""#527, #715: the Grafana boards live inside the app. Fetch/Health is headed by the one board rc-health and Logs
is the board rc-logs (avo, 12 Nov 2026: three tabs, no tab holds another tab; the Live tab stays gone).

avo, 8 Nov 2026: "bunlarin ne isi var burada? bunlar revenue chaser app icine gomulu olmaliydi". Grafana runs on
the same host as the dashboard, port 3000, and allows only this app's origin to frame it (home-server units/
grafana-server.service.d/life.conf, CSP frame-ancestors). The frame loads only when the tab opens.
"""
from __future__ import annotations

import re

from RevenueChaser.module_settings import report


def _page() -> str:
    return report.render_settings_dashboard({"newdata": {"error": "not read in this test"}, "tickers": []})


def test_four_tabs_watch_lists_first_and_each_grafana_tab_frames_one_board():
    """#715 (avo, 12 Nov 2026): Database · Fetch/Health · Logs, and no tab holds another tab, so the board picker
    above the frame is gone: Fetch/Health is the one board rc-health, Logs is rc-logs. The same day: the watch lists
    get a tab of their own, the first one."""
    html = _page()
    strip = html[html.index('id="settings-tabs"'):html.index("</nav>")]
    assert re.findall(r'role="tab" id="(tab-[a-z]+)"', strip) == ["tab-watch", "tab-data", "tab-health", "tab-logs"]
    assert re.findall(r'aria-selected="(true|false)"', strip) == ["true", "false", "false", "false"]
    assert re.findall(r">([^<]+)</button>", strip) == ["Watch lists", "Database", "Fetch/Health", "Logs"]
    watch = html[html.index('id="panel-watch"'):html.index('id="panel-data"')]
    assert "The watch lists could not be read." in watch, "a failed read is one line, never an empty tab"
    assert 'id="panel-live"' not in html and "live" not in report.SETTINGS_TABS["tabs"]
    health = html[html.index('id="panel-health"'):html.index('id="panel-logs"')]
    logs = html[html.index('id="panel-logs"'):html.index("<script>", html.index('id="panel-logs"'))]
    assert health.index('id="live-frame"') < health.index("<h1>Settings · Universe and fills</h1>")
    assert re.findall(r'data-board="([a-z-]+)"', health) == ["rc-health"]
    assert re.findall(r'data-board="([a-z-]+)"', logs) == ["rc-logs"]
    assert "<button" not in health[:health.index('id="live-frame"')], "no picker: a tab holds no other tab"
    assert re.search(r'<div[^>]*id="panel-logs"[^>]*role="tabpanel"[^>]*aria-labelledby="tab-logs"[^>]*hidden', html)
    assert "<button" not in logs and logs.count("<iframe") == 1


def test_the_frames_have_no_source_until_their_tab_opens():
    """A Database visit must not load Grafana: each frame gets its address from the script, on its tab's open."""
    html = _page()
    frames = re.findall(r"<iframe[^>]*>", html)
    assert len(frames) == 2
    for frame in frames:
        assert " src=" not in frame
        assert 'title="' in frame and 'loading="lazy"' in frame


def test_the_grafana_address():
    js = report.LIVE_JS
    assert "location.hostname" in js and "':3000/d/'" in js, "same host as the page, Grafana's port"
    assert "kiosk" in js and "theme=" in js
    assert "panel.hidden" in js and "aria-labelledby" in js, "a frame loads only while its own tab is open"
    assert [(p, uid) for p, uid, _, _ in report.LIVE_BOARDS] == [("panel-health", "rc-health"), ("panel-logs", "rc-logs")]



def test_the_health_tab_no_longer_draws_the_live_parts():
    """#527 B4, decision D4 = B (avo, 8 Nov 2026 02:39): the queue, the running jobs and the "right now" strip leave
    the Health tab once Grafana shows them; the Grafana frame now heads the same tab, so no link is needed.

    #715 step 2 (avo, 11 Nov 2026) puts the queue back, but as part of the Fetch board under its file groups,
    not as the Health tab's own card."""
    from RevenueChaser.module_settings.tests.test_control_view import control_nd
    from RevenueChaser.module_settings.tests.test_report import _panel, _payload
    html = report.render_settings_dashboard(dict(_payload(), newdata=control_nd(),
                                                 ibkr_login_url="https://gateway.example/"))
    health = _panel(html, "panel-health")
    # #715 (avo, 12 Nov 2026, "Karşılığı olanı şimdi kaldır"): the queue, the switches and the reference parts are
    # the framed board's; under the frame stay the gateway's login link, the universe and the fills.
    for gone in ('id="health-queue"', 'id="sec-queue"', 'id="needs"', 'id="sw-msg"', 'id="health-live-link"'):
        assert gone not in health, gone
    assert health.index('id="live-frame"') < health.index('id="sec-universe"') < health.index('class="fgrps"')
    assert health.count('class="topbar"') == 1 and "sw-msg-h" not in health
    data = _panel(html, "panel-data")
    assert 'class="topbar"' not in data and 'href="https://gateway.example/"' in health
