"""#299 (Value Chain half): the board's amber banner is backed by a real ProviderHealth.

The board rendered ``provider_health_html(payload.get("provider_health"))`` while nothing built a
``ProviderHealth``, so a failed SEC fetch was invisible on the board and ``result_runs.provider_health``
stayed NULL. The request sites in ``edgar_text`` now count every call, and a Fetch or a sync hands the
health to the payload and to the result history.

The Financials and SEC 13F halves of the issue belong to those boards.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.module_value_chain import edgar_text, health, history, report

_CIK = "0000320193"


@pytest.fixture(autouse=True)
def _clean_health():
    health.EDGAR.reset()
    yield
    health.EDGAR.reset()


def test_a_failed_submissions_fetch_is_counted(monkeypatch):
    def _boom(url, client=None):
        raise RuntimeError("503 Service Unavailable")
    monkeypatch.setattr(sec_edgar, "get_json", _boom)

    got = edgar_text.fetch_filing("AAPL", _CIK)
    assert got["error"]

    h = health.EDGAR.health()
    assert (h["provider"], h["calls"], h["failed"]) == ("SEC EDGAR", 1, 1)
    assert h["failures"][0]["endpoint"] == "AAPL.submissions"


def test_a_submissions_fetch_that_worked_is_a_call_too(monkeypatch):
    """Without this the banner could only ever read "1 of 1 calls failed"."""
    monkeypatch.setattr(sec_edgar, "get_json", lambda url, client=None: {"filings": {"recent": {}}})

    got = edgar_text.fetch_filing("AAPL", _CIK)
    assert got["miss"] == "no_filing"

    h = health.EDGAR.health()
    assert (h["calls"], h["failed"], h["failures"]) == (1, 0, [])


def test_the_board_banner_fires_on_a_failed_call():
    health.EDGAR.call(error="503 Service Unavailable", endpoint="AAPL.submissions")
    html = report.render_value_chain_dashboard({"filings": [], "edges": [], "universe_total": 0, "pending": 0,
                                                "provider_health": health.EDGAR.health()})
    assert "SEC EDGAR" in html


def test_a_clean_run_shows_no_banner():
    health.EDGAR.call(endpoint="AAPL.submissions")
    html = report.render_value_chain_dashboard({"filings": [], "edges": [], "universe_total": 0, "pending": 0,
                                                "provider_health": health.EDGAR.health()})
    assert "SEC EDGAR" not in html


def test_the_run_records_the_health(monkeypatch):
    seen = {}
    from RevenueChaser.core import history as core_history
    monkeypatch.setattr(core_history, "record_run", lambda *a, **kw: seen.update(kw) or {})

    health.EDGAR.call(error="503", endpoint="AAPL.submissions")
    history.record([], run_id="r1", run_at=0.0, counts={}, provider_health=health.EDGAR.health())
    assert seen["provider_health"]["failed"] == 1
