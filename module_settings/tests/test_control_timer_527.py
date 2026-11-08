"""#527 step 11 (avo decision B, 6 Nov 2026): the builder timer in the top bar, with its on / off press.

The dashboard runs with NoNewPrivileges, so it cannot run ``sudo life-ctl`` itself; the press is the Grafana ops
panel's own timer button (``life-ctl life-rc-builder.timer on|off``, in sudoers since 5 Nov 2026), reached by a link
whose address comes from ``RC_BUILDER_TIMER_URL``. An unset or non-http value shows the state with no link."""
from __future__ import annotations

from RevenueChaser.module_settings import control_render, report
from RevenueChaser.module_settings.tests.test_report import _payload


def _bar(view):
    html = control_render._topbar(view)
    return html


def test_the_top_bar_shows_the_builder_timer_and_its_link():
    v = {"switches": {}, "gateway": {}, "timer": {"state": "off", "last": None, "next": None, "minute": 7},
         "timer_url": "https://grafana.example/d/home"}
    bar = _bar(v)
    assert '<span class="swt tmr none"><b>Builder timer: off</b>' in bar
    assert 'href="https://grafana.example/d/home"' in bar and "on / off in Grafana" in bar
    v["timer"]["state"] = "on"
    assert '<span class="swt tmr ok"><b>Builder timer: on</b>' in _bar(v)
    v["timer"]["state"] = None
    assert "<b>Builder timer: not known</b>" in _bar(v)
    del v["timer_url"]
    assert "on / off in Grafana" not in _bar(v)


def test_the_link_comes_from_the_variable_and_a_bad_value_shows_none(monkeypatch):
    from RevenueChaser.module_settings.tests.test_control_view import control_nd
    monkeypatch.setenv("RC_BUILDER_TIMER_URL", "https://grafana.example/d/home")
    html = report.render_settings_dashboard(dict(_payload(), newdata=control_nd()))
    assert 'href="https://grafana.example/d/home"' in html
    monkeypatch.setenv("RC_BUILDER_TIMER_URL", "javascript:alert(1)")
    html = report.render_settings_dashboard(dict(_payload(), newdata=control_nd()))
    assert "javascript:alert" not in html and "on / off in Grafana" not in html


def test_the_timer_box_has_a_press_that_says_what_it_does():
    v = {"switches": {}, "gateway": {}, "timer": {"state": "off", "last": None, "next": None, "minute": 7}}
    bar = control_render._topbar(v)
    assert 'data-job="timer" data-on="0"' in bar and ">Turn on</button>" in bar
    v["timer"]["state"] = "on"
    assert 'data-job="timer" data-on="1"' in control_render._topbar(v)
    v["timer"]["state"] = None
    assert 'data-job="timer"' not in control_render._topbar(v), "no press while the state is not known"
    assert control_render.ROUTES["timer"] == "/api/build/timer"
    assert "C.routes.timer" in control_render.CTL_JS
