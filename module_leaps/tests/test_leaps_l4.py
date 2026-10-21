"""L4 (#377): a page-1 print-scan failure is stated, not read as an empty tape.

``discover_flow_prints`` reads the tape page by page. When the first page raises, no print was ever
fetched: without a warning the board's empty state reads like a genuinely quiet market.
"""
from datetime import date

from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.module_leaps.engine import discover_flow_prints

TODAY = date(2026, 10, 2)


class _FailingFirstPage:
    def option_trades(self, **kw):
        if kw.get("page") == 1:
            raise RuntimeError("gateway timeout")
        return []


class _EmptyTape:
    def option_trades(self, **kw):
        return []


def test_a_failing_first_page_warns(rawstore_tmp_db):
    notes = []
    discover_flow_prints(_FailingFirstPage(), today=TODAY, warnings=notes)
    assert len(notes) == 1
    assert "first page" in notes[0].lower() and "gateway timeout" in notes[0]
    assert "not an empty tape" in notes[0].lower()


def test_a_genuinely_empty_first_page_does_not_warn(rawstore_tmp_db):
    notes = []
    discover_flow_prints(_EmptyTape(), today=TODAY, warnings=notes)
    assert notes == []


class _FailedResultFirstPage:
    """The real client's shape: a failed read is an empty page with ``status == "failed"``, no exception."""

    def option_trades_result(self, **kw):
        return ProviderResult("failed", [], {"status": 502, "error": "bad gateway"}, "/option-trades")

    def option_trades(self, **kw):
        return []


class _OkEmptyResult:
    def option_trades_result(self, **kw):
        return ProviderResult("ok", [], None, "/option-trades")


def test_a_failed_first_page_result_warns(rawstore_tmp_db):
    notes = []
    discover_flow_prints(_FailedResultFirstPage(), today=TODAY, warnings=notes)
    assert len(notes) == 1 and "bad gateway" in notes[0]


def test_an_ok_empty_first_page_result_does_not_warn(rawstore_tmp_db):
    notes = []
    discover_flow_prints(_OkEmptyResult(), today=TODAY, warnings=notes)
    assert notes == []
