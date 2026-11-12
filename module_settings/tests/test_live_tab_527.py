"""#527: the RC Grafana boards live inside the app, at the head of the tab that owns them.

avo, 12 Nov 2026 ("bence uc sekme ...", "gomebiliriz"): three tabs. Fetch/Health frames the fetch controls, the
DuckDB health and the RC timers (rc-jobs); Logs frames what flowed (rc-logs) and the runs by the board they feed
(rc-activity).

avo, 8 Nov 2026: "bunlarin ne isi var burada? bunlar revenue chaser app icine gomulu olmaliydi". Grafana runs on
the same host as the dashboard, port 3000, and allows only this app's origin to frame it (home-server units/
grafana-server.service.d/life.conf, CSP frame-ancestors). The frame loads only when the tab opens.
"""
from __future__ import annotations

import re

from RevenueChaser.module_settings import report


def _page() -> str:
    return report.render_settings_dashboard({"newdata": {"error": "not read in this test"}, "tickers": []})


def test_three_tabs_and_the_frame_heads_fetch_health_with_rc_fetch_first():
    html = _page()
    strip = html[html.index('id="settings-tabs"'):html.index("</nav>")]
    assert re.findall(r'role="tab" id="(tab-[a-z]+)"', strip) == ["tab-data", "tab-health", "tab-logs"]
    assert 'id="panel-live"' not in html and "live" not in report.SETTINGS_TABS["tabs"]
    health = html[html.index('id="panel-health"'):]
    assert health.index('id="live-frame"') < health.index("<h1>Data/Health · Fetch/Health</h1>")
    assert report.LIVE_BOARDS[0][0] == "rc-fetch"
    # one script drives every strip: it finds each one's panel, so a third tab needs no second copy
    assert "querySelectorAll('.live-board')" in report.LIVE_JS
    assert "aria-labelledby" in report.LIVE_JS


def test_the_logs_tab_frames_what_flowed_and_the_runs():
    """avo, 12 Nov 2026: "logs ise hem internal hem external requestlerin aktigi tablo"."""
    html = _page()
    logs = html[html.index('id="panel-logs"'):]
    assert re.search(r'<div id="panel-logs" role="tabpanel" aria-labelledby="tab-logs" hidden', html), \
        "the Logs panel is a tab panel and starts hidden"
    assert [uid for uid, _ in report.LOGS_BOARDS] == ["rc-logs", "rc-activity"]
    frame = re.search(r"<iframe[^>]*id=\"logs-frame\"[^>]*>", logs).group(0)
    assert " src=" not in frame and 'loading="lazy"' in frame
    # the runs and the log lines leave the Fetch/Health strip
    assert "rc-activity" not in report.LIVE_HTML and "rc-logs" not in report.LIVE_HTML


def test_the_frame_has_no_source_until_the_tab_opens():
    """A Data/Health visit must not load Grafana: the frame gets its address from the script, on the tab's open."""
    html = _page()
    frame = re.search(r"<iframe[^>]*id=\"live-frame\"[^>]*>", html).group(0)
    assert " src=" not in frame
    assert 'title="' in frame and 'loading="lazy"' in frame


def test_the_boards_and_the_grafana_address():
    html = _page()
    for uid in ("rc-data-health", "rc-fetch", "rc-jobs", "rc-activity", "rc-logs"):
        assert f'data-board="{uid}"' in html
    js = report.LIVE_JS
    assert "location.hostname" in js and "':3000/d/'" in js, "same host as the page, Grafana's port"
    assert "kiosk" in js and "theme=" in js
    assert "rc-data-health" in report.LIVE_HTML and "rc-fetch" in report.LIVE_HTML
    # home-server #222: the RC part of the Jobs board, with its switches and fuse, under Fetch/Health
    assert "rc-jobs" in report.LIVE_HTML



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
    # #715 step 2 (avo, 11 Nov 2026: every control on the page itself) brought the queue back, under the
    # Fetch groups of the same panel; the Health tab's own old queue card stays gone.
    assert 'id="health-queue"' not in health
    assert health.index('class="fgrps"') < health.index('id="sec-queue"') < health.index('id="needs"')
    # avo, 10 Nov 2026: the switches moved with every control to Fetch/Health, once
    assert health.count('class="topbar"') == 1 and "sw-msg-h" not in health
    assert 'id="health-live-link"' not in health and 'id="live-frame"' in health
    data = _panel(html, "panel-data")
    assert 'class="topbar"' not in data and 'href="https://gateway.example/"' in health
