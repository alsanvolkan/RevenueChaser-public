"""The IBKR gateway login shortcut on the Settings / Data health page.

avo asked for a shortcut that opens the gateway's login screen. The address is not in the code: it
comes from ``RC_IBKR_LOGIN_URL``, so an unset variable shows nothing and no host is pinned here.
Only an absolute http/https address is accepted, so a mistyped or hostile value cannot become a
live link.
"""
from __future__ import annotations

import pytest

from RevenueChaser.module_settings import report
from RevenueChaser.module_settings.tests.test_report import _payload

SECTION = 'id="ibkr-login"'
URL = "https://gateway.example.ts.net:6080/vnc.html?autoconnect=1&resize=scale"


def _card(url) -> str:
    html = report.render_settings_dashboard(dict(_payload(), ibkr_login_url=url))
    return html[html.index(SECTION):].split("</section>")[0] if SECTION in html else ""


@pytest.mark.parametrize("value", [None, "", "   "])
def test_nothing_is_shown_when_the_variable_is_not_set(value):
    assert _card(value) == ""


def test_a_missing_key_shows_nothing():
    assert SECTION not in report.render_settings_dashboard(_payload())


def test_an_https_address_becomes_the_link():
    card = _card(URL)
    assert 'id="ibkr-login-link"' in card
    # the query's & is escaped in the attribute, as HTML requires
    assert 'href="https://gateway.example.ts.net:6080/vnc.html?autoconnect=1&amp;resize=scale"' in card
    assert "IBKR" in card


def test_an_http_address_is_accepted():
    assert 'href="http://gateway.example:6080/"' in _card("http://gateway.example:6080/")


def test_the_link_opens_in_a_new_tab_without_leaking_the_referrer():
    card = _card(URL)
    assert 'target="_blank"' in card
    assert "noopener" in card and "noreferrer" in card


def test_the_card_names_the_variable_and_says_the_page_calls_nothing():
    card = _card(URL)
    assert "RC_IBKR_LOGIN_URL" in card
    assert "no call" in card


@pytest.mark.parametrize("value", [
    "javascript:alert(1)",
    "data:text/html,<script>__x(1)</script>",
    "file:///etc/passwd",
    "/vnc.html?autoconnect=1",
    "gateway.example:6080",
    "https://",
    "ftp://gateway.example/",
])
def test_an_address_that_is_not_an_absolute_http_url_shows_nothing(value):
    assert _card(value) == ""


def test_a_hostile_value_cannot_break_out_of_the_attribute():
    html = report.render_settings_dashboard(
        dict(_payload(), ibkr_login_url='https://a.example/"><img src=x onerror=__x(1)>'))
    assert "<img" not in html
    assert "&quot;&gt;&lt;img" in html


def test_the_payload_carries_the_variables_value(monkeypatch):
    """``inventory.build_payload`` reads the variable, so the page itself pins no address."""
    from RevenueChaser.module_settings import inventory
    monkeypatch.setenv("RC_IBKR_LOGIN_URL", URL)
    assert inventory._ibkr_login_url() == URL
    monkeypatch.delenv("RC_IBKR_LOGIN_URL")
    assert inventory._ibkr_login_url() == ""
