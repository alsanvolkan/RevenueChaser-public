"""Review F2 on the fundamentals layer (G6, build plan PR 13): the 8-K flag reads ``filing_item`` and its dated check.

On the layer the board reads the filing list ``sec/filings`` serves (``filing_item``, one row per filing and 8-K
item) and maps a ticker to its CIK through ``fundamental_company``, never through the old ``security_master`` /
``sec_submissions`` rows. A CIK's "checked" day is the day its list was last read
(``fundamental_company.submissions_as_of``), or the day a later good ``sec/filings`` run read the daily index (which
re-reads the list of every company that filed): only after that day does "no 8-K" mean something.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.core import fundamentals_layer
from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.module_fundamentals import events
from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build.fundamentals import companyfacts, filings
from RevenueChaser.rcdata.tests import fund_fixture as fx
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401  (fixtures)

EVENT = "0001193125-26-200001"
ROWS = [(EVENT, "8-K", "2026-06-01", "2026-05-29", "msft-8k2.htm", "2.01,9.01"), *fx.MSFT_FILINGS]


@pytest.fixture
def layer(monkeypatch, http_mock):  # noqa: F811
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec_edgar, "SEC_DATA_BASE", "https://data.sec.test")
    monkeypatch.delenv(fundamentals_layer.SOURCE_ENV, raising=False)
    # A run's own check is dated by the clock: pinned, so the stale line does not move with the day CI runs.
    monkeypatch.setattr(ops, "_today", lambda: "2026-10-28")
    fx.spine()
    fx.sec_routes(http_mock, {"789019": fx.msft_facts()})
    fx.filing_routes(http_mock, {"789019": fx.submissions(789019, ROWS, "MICROSOFT CORP")},
                     {"msft-10k.htm": fx.ten_k(), "msft-def14a.htm": fx.proxy()})
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-26")
    fx.run(filings.NAME, mode="backfill", to="2026-10-28")
    assert fundamentals_layer.reads_layer()
    return http_mock


def _checked_on(day: str) -> None:
    """One more good ``sec/filings`` check, dated ``day``."""
    ops.record_check(filings.NAME, "unchanged", checked_on=day)


def test_the_flag_reads_the_layers_filing_items(layer):
    _checked_on("2026-10-28")
    stats: dict = {}
    found = events.corporate_events(["MSFT", "ORCL", "HEI", "NOPE"], today=date(2026, 10, 29), stats=stats)
    assert found == {"MSFT": {"items": ["2.01"], "last_filing_date": "2026-06-01"}}
    # ORCL and HEI: a CIK whose list was never read (404); NOPE: no CIK at all. MSFT was read and is current.
    assert stats == {"no_cik": 1, "no_submissions": 2, "stale_submissions": 0}


def test_a_list_not_read_lately_is_stale(layer):
    _checked_on("2026-10-28")
    stats: dict = {}
    events.corporate_events(["MSFT"], today=date(2026, 11, 20), stats=stats)
    assert stats["stale_submissions"] == 1


def test_a_later_good_run_keeps_a_quiet_filers_list_current(layer):
    """MSFT's list was read on 28 Oct; a good run on 18 Nov read the index and found nothing new for it."""
    _checked_on("2026-11-18")
    stats: dict = {}
    events.corporate_events(["MSFT"], today=date(2026, 11, 20), stats=stats)
    assert stats["stale_submissions"] == 0


def test_a_failed_run_vouches_for_nothing(layer):
    _checked_on("2026-10-28")
    ops.record_check(filings.NAME, "failed", checked_on="2026-11-19")
    stats: dict = {}
    events.corporate_events(["MSFT"], today=date(2026, 11, 20), stats=stats)
    assert stats["stale_submissions"] == 1


def test_the_layer_never_reads_the_old_rows(layer, monkeypatch):
    from RevenueChaser.core import rawstore

    def refuse(*a, **k):
        raise AssertionError("the old store was read")
    monkeypatch.setattr(rawstore, "query", refuse)
    assert events.corporate_events(["MSFT"], today=date(2026, 10, 29))["MSFT"]["items"] == ["2.01"]
