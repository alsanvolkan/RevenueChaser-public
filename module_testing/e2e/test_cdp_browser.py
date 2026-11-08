"""The DevTools harness keeps Chrome off the network (AGENTS.md: no test touches the network).

The root conftest blocks sockets in the test process, but Chrome is a process of its own. Like the board HTML
guard, the harness maps every host to NOTFOUND except loopback (the rule matches an IP literal too, so
127.0.0.1, where the e2e dashboard runs, is excluded by name).
"""
from __future__ import annotations

import os

import pytest

from .harness.cdp_browser import Page, find_chrome
from .harness.devices import DEVICES


@pytest.fixture(scope="module")
def page():
    chrome = find_chrome()
    if chrome is None:
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail("no Chrome/Chromium for the harness check")
        pytest.skip("no Chrome/Chromium")
    with Page(chrome) as p:
        yield p


def test_a_page_cannot_reach_an_outside_host(page):
    result = page.send("Page.navigate", url="http://example.com/")
    if result.get("errorText") == "net::ERR_BLOCKED_BY_CLIENT":
        # Some sandboxes refuse every outside host before name resolution; the rule cannot be seen from there.
        pytest.skip("this environment blocks outside hosts before resolution; the check runs on an open network")
    assert result.get("errorText") == "net::ERR_NAME_NOT_RESOLVED", f"the resolver rule did not apply: {result}"


def test_the_loopback_dashboard_is_still_reachable(page, dashboard):
    result = page.send("Page.navigate", url=f"{dashboard.url}/api/jobs")
    assert "errorText" not in result or not result["errorText"], result


# What the page itself reports about the device it runs on.
DEVICE_SEEN = ("({w: document.documentElement.clientWidth, h: innerHeight, dpr: devicePixelRatio,"
               " ua: navigator.userAgent, touch: navigator.maxTouchPoints,"
               " coarse: matchMedia('(pointer: coarse)').matches})")
# A mobile viewport honours <meta name="viewport">; without one Chrome lays a page out 980 px wide.
PHONE_PAGE = "data:text/html,<meta name=viewport content='width=device-width,initial-scale=1'><p>phone"


@pytest.mark.parametrize("device", DEVICES, ids=lambda d: d.name)
def test_a_measured_phone_is_emulated_as_that_phone(page, device):
    """In landscape the realme is 919 px wide: still a phone, with its touch screen and its Android user agent.
    ``emulate`` alone derives ``mobile`` from ``width < 900`` and would make it a desktop."""
    page.emulate_device(device, scheme="dark")
    page.open(PHONE_PAGE)
    seen = page.eval(DEVICE_SEEN)
    assert (seen["w"], seen["h"]) == (device.width, device.height)
    assert seen["dpr"] == device.scale
    assert seen["ua"] == device.user_agent
    assert seen["touch"] >= 1 and seen["coarse"], f"{device.name} lost its touch screen: {seen}"


def test_a_wide_viewport_can_be_a_mobile_on_request(page):
    page.emulate(919, 360, mobile=True)
    page.open(PHONE_PAGE)
    assert page.eval("navigator.maxTouchPoints") >= 1
