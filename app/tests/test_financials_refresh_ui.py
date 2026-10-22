"""Financials Refresh must rebuild, then stamp state json, then load the iframe.

The first click after a ticker switch used to point the iframe at ?refresh=1
and call /api/data_asof in parallel. data_asof reads
.cache/financials/financials_latest.json, which still named the previous
ticker, so the chrome date (and anything else keyed off that file) stayed
stale until a second click.
"""
from __future__ import annotations

import re

import pytest

from RevenueChaser.app import dashboard
from RevenueChaser.core.tests.open_issues_shell import strip_open_issues_data


@pytest.fixture(scope="module")
def index_html() -> str:
    return strip_open_issues_data(dashboard.app.test_client().get("/").get_data(as_text=True))


def _fn_body(html: str, name: str) -> str:
    start = html.index(f"function {name}(")
    rest = html[start + 1:]
    end = rest.find("\nfunction ")
    body = rest[:end if end != -1 else len(rest)]
    return "\n".join(re.sub(r"//.*$", "", line) for line in body.splitlines())


class TestFinancialsRefreshOrder:
    def test_rebuild_is_fetched_before_the_iframe_src_is_set(self, index_html):
        body = _fn_body(index_html, "refreshTab")
        fin = body[body.index("module_financials"):]
        assert "fetch(" in fin
        assert fin.index("fetch(") < fin.index("setAttribute('src'")

    def test_data_as_of_waits_until_after_the_rebuild(self, index_html):
        body = _fn_body(index_html, "refreshTab")
        fin = body[body.index("module_financials"):]
        assert fin.index("fetch(") < fin.index("checkDataAsOf")
        assert ".then(" in fin

    def test_the_box_ticker_is_still_passed(self, index_html):
        body = _fn_body(index_html, "refreshTab")
        assert "quick-ticker" in body
        assert "'&ticker='" in body or '"&ticker="' in body
