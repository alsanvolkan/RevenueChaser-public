"""#162: the "IBKR Gateway login" card of Settings / Data health.

The card reads the login check's own record and **nothing else**: no socket from a Refresh, so the
page keeps its budget and stays free of side effects.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from RevenueChaser.core import ibkr_login
from RevenueChaser.module_settings import inventory, report

# Deliberately NOT today: a card that ignored the clock it is given and read the wall clock instead
# would still pass against a "now" that happens to be today (CLAUDE.md: dates relative to today rot).
# This one stays in the past, so the gap only grows.
NOW = datetime(2026, 9, 15, 9, 20, tzinfo=timezone.utc)


def _seed():
    from RevenueChaser.core import rawstore
    rawstore.write_rows("equity_universe", [{"ticker": "AAPL", "market_cap": 3.0e9, "name": "Apple"}])


@pytest.fixture
def on(monkeypatch, tmp_path):
    """The check switched on, with its record under a temporary cache root."""
    monkeypatch.setenv("RC_IBKR_LOGIN_CHECK", "1")
    monkeypatch.delenv("RC_IBKR_HOST", raising=False)
    monkeypatch.delenv("RC_IBKR_PORT", raising=False)
    path = tmp_path / "ibkr_login" / "state.json"
    monkeypatch.setattr(ibkr_login, "state_path", lambda: path)
    return path


def _record(path, state, at, version=None):
    ibkr_login.record(ibkr_login.Reading(state=state, detail="d", server_version=version),
                      path=path, now=at)


# ── the payload ─────────────────────────────────────────────────────────────────────────────────

def test_collect_carries_the_login_card(rawstore_tmp_db, on):
    _seed()
    _record(on, ibkr_login.UP, NOW - timedelta(days=3), version=178)
    card = inventory.collect(now=NOW)["ibkr"]
    assert card["state"] == ibkr_login.UP
    assert card["for_text"] == "3 days"
    assert card["server_version"] == 178


def test_collect_opens_no_socket_for_the_card(rawstore_tmp_db, on, monkeypatch):
    """A probe on every Refresh would be a side effect and, on a dead gateway, a timeout."""
    monkeypatch.setattr(ibkr_login, "probe",
                        lambda *a, **kw: pytest.fail("the Data Health read must not probe the gateway"))
    _seed()
    inventory.collect(now=NOW)


def test_collect_says_not_set_up_while_the_switch_is_off(rawstore_tmp_db, monkeypatch):
    monkeypatch.delenv("RC_IBKR_LOGIN_CHECK", raising=False)
    _seed()
    card = inventory.collect(now=NOW)["ibkr"]
    assert card["state"] == ibkr_login.NOT_SET


def test_a_failing_card_read_is_recorded_and_the_rest_of_the_payload_survives(rawstore_tmp_db, monkeypatch):
    def boom(**kw):
        raise RuntimeError("login read exploded")

    monkeypatch.setattr(ibkr_login, "collect", boom)
    _seed()
    payload = inventory.collect(now=NOW)
    assert payload["ibkr"] is None
    assert {"table": "ibkr", "where": "_ibkr_login", "error": "login read exploded"} in payload["errors"]
    assert {r["key"] for r in payload["rows"]}, "the rest of the inventory is still built"
    html = report.render_settings_dashboard(payload)
    assert "login read exploded" in html and 'id="ibkr-login"' not in html


# ── the card's words ────────────────────────────────────────────────────────────────────────────

def _html(path, state, at, now=NOW, version=None):
    _record(path, state, at, version=version)
    return report._ibkr_card(ibkr_login.collect(now=now, path=path))


def test_the_card_says_logged_in_and_for_how_long(on):
    html = _html(on, ibkr_login.UP, NOW - timedelta(hours=5), version=178)
    assert 'id="ibkr-login"' in html
    assert "Logged in" in html
    # Its own line, not the "last checked" sentence, which carries a duration of its own.
    assert 'id="ibkr-for">This state has held for: 5 hours' in html
    assert 'id="ibkr-version">API server version: 178' in html


def test_the_card_says_login_needed_and_when_it_dropped_and_the_last_time_it_was_up(on):
    _record(on, ibkr_login.UP, NOW - timedelta(days=8))
    html = _html(on, ibkr_login.DOWN, NOW - timedelta(days=1))
    assert "Login needed" in html
    assert "1 day" in html, "how long it has been down"
    assert "Last logged in" in html


def test_the_card_lists_the_recent_changes_oldest_first(on):
    _record(on, ibkr_login.UP, NOW - timedelta(days=9))
    _record(on, ibkr_login.DOWN, NOW - timedelta(days=2))
    _record(on, ibkr_login.UP, NOW - timedelta(days=1))
    html = report._ibkr_card(ibkr_login.collect(now=NOW, path=on))
    assert html.index("Login needed</td>") < html.index("Logged in</td>"), \
        "the changes read oldest to newest"


def test_the_card_calls_out_a_reading_nothing_has_refreshed(on):
    html = _html(on, ibkr_login.UP, NOW - timedelta(minutes=ibkr_login.STALE_MINUTES + 10))
    assert "no check has run" in html.lower()


def test_the_card_says_not_set_up_with_the_variable_to_set(monkeypatch, tmp_path):
    monkeypatch.delenv("RC_IBKR_LOGIN_CHECK", raising=False)
    html = report._ibkr_card(ibkr_login.collect(now=NOW, path=tmp_path / "none.json"))
    assert "RC_IBKR_LOGIN_CHECK" in html
    assert "Logged in" not in html and "Login needed" not in html


def test_the_card_says_what_the_probe_never_asks_for(on):
    """The reader's one guard that this card did not grow into account data."""
    html = _html(on, ibkr_login.UP, NOW - timedelta(hours=1))
    for phrase in ("account", "position", "order"):
        assert phrase in html.lower(), f"the card no longer says it never asks for a {phrase}"


def test_no_card_at_all_when_the_payload_has_no_login_key(on):
    assert report._ibkr_card(None) == ""


def test_the_card_escapes_what_it_is_given(on):
    """``checked_detail`` carries the probe's own words, which include the host from the environment."""
    card = {"state": ibkr_login.DOWN, "target": "<img src=x>", "text": "Login needed (<img src=x>)",
            "checked_text": "Last checked 1 minute ago.", "checked_detail": "<script>alert(1)</script>",
            "for_text": "1 minute", "since": NOW.isoformat(), "last_up": None, "last_down": None,
            "checked_at": NOW.isoformat(), "stale": False, "server_version": None, "changes": []}
    html = report._ibkr_card(card)
    assert "<script>" not in html and "<img src=x>" not in html
    assert "&lt;script&gt;" in html


def test_the_rendered_page_carries_the_card(rawstore_tmp_db, on):
    _seed()
    _record(on, ibkr_login.UP, NOW - timedelta(hours=2), version=178)
    html = report.render_settings_dashboard(inventory.collect(now=NOW))
    assert 'id="ibkr-login"' in html and "Logged in" in html


def test_the_page_carries_no_card_when_the_switch_is_off_but_says_so(rawstore_tmp_db, monkeypatch):
    monkeypatch.delenv("RC_IBKR_LOGIN_CHECK", raising=False)
    _seed()
    html = report.render_settings_dashboard(inventory.collect(now=NOW))
    assert 'id="ibkr-login"' in html, "the card itself stays, so the switch is discoverable"
    assert "RC_IBKR_LOGIN_CHECK" in html


def test_a_drop_is_not_painted_like_a_healthy_state(on):
    """The colour is the first thing read on this page, so it must not say the opposite of the words."""
    down = _html(on, ibkr_login.DOWN, NOW - timedelta(hours=1))
    down_state = down[down.index('id="ibkr-state"'):].split("</div>")[0]
    assert "st-bad" in down_state and "st-ok" not in down_state

    up = _html(on, ibkr_login.UP, NOW - timedelta(minutes=30))
    up_state = up[up.index('id="ibkr-state"'):].split("</div>")[0]
    assert "st-ok" in up_state and "st-bad" not in up_state


def test_a_silent_port_is_painted_as_a_warning_not_as_an_error_or_a_login(on):
    html = _html(on, ibkr_login.SILENT, NOW - timedelta(hours=1))
    state = html[html.index('id="ibkr-state"'):].split("</div>")[0]
    assert "st-warn" in state and "st-ok" not in state
