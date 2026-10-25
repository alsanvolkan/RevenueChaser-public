"""The DevTools harness keeps Chrome off the network (AGENTS.md: no test touches the network).

The root conftest blocks sockets in the test process, but Chrome is a process of its own. Like the board HTML
guard, the harness maps every host to NOTFOUND except loopback (the rule matches an IP literal too, so
127.0.0.1, where the e2e dashboard runs, is excluded by name).
"""
from __future__ import annotations

import os

import pytest

from .harness.cdp_browser import Page, find_chrome


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
    # On an open network (CI) the resolver rule answers ERR_NAME_NOT_RESOLVED; a sandbox that blocks egress
    # may refuse first with an error of its own. Either way the page must not load.
    result = page.send("Page.navigate", url="http://example.com/")
    assert result.get("errorText"), f"Chrome reached an outside host: {result}"


def test_the_loopback_dashboard_is_still_reachable(page, dashboard):
    result = page.send("Page.navigate", url=f"{dashboard.url}/api/jobs")
    assert "errorText" not in result or not result["errorText"], result
