"""Step 8: the cross-check table (A17-B, Karar 32-B).

avo chose the opposite of my proposal here: the cross-check button **may** ask the live source. Three conditions
make that safe, and each is a test below: only on a press, through the layer (the button queues a row and never
calls a provider itself), and with the call count written beside it. Opening the page still fetches nothing.

A real second source is looked for in two fields only (Karar 32-B): prices and interest rates. The code has a
second-source report for prices - the monthly WRDS comparison and the revision log - and none for interest rates,
so the table says that in words rather than leaving a blank that reads like "no difference".
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

import pytest

from RevenueChaser.module_settings import newdata, report
from RevenueChaser.module_settings.tests.test_newdata_findings import _page, _section

NOW = datetime(2026, 10, 29, 18, 30, tzinfo=timezone.utc)


def _with(cross: dict | None):
    from RevenueChaser.core.tests.board_fixtures import newdata_fixture
    nd = newdata_fixture()
    nd["cross_check"] = cross
    return nd


FULL = {"fields": [
    {"field": "prices", "need": "prices.wrds_diff", "second_source": "WRDS (CRSP and Compustat)",
     "as_of": "2026-10-31", "checked": "20 sessions", "differs": 1, "worst": "0.012%",
     "dataset": "wrds/prices", "calls": 3, "remaining": 47},
    {"field": "interest rates", "need": "macro.dgs10", "second_source": None,
     "why": "no second source is stored for this field yet", "dataset": None},
]}


# ── the table ──

# ── the button: press-only, through the layer, with its cost ──

def test_the_script_queues_through_the_build_request_route_and_calls_no_provider():
    assert "'cross'" in report.ND_JS, "the click handler knows the action"
    body = report.ND_JS[report.ND_JS.index("function ndCross"):]
    body = body[:body.index("\n}")]
    assert "/api/build/request/" in body, "the press queues a run; it is not a no-op"
    assert "ndPost(" in body
    cross = report.ND_JS[report.ND_JS.index("function ndCross"):]
    for word in ("unusualwhales", "api.unusualwhales", "https://", "wrds.wharton"):
        assert word not in cross, f"the page calls no provider of its own ({word})"


# ── the collector ──

def test_the_collector_names_the_two_fields_avo_chose_and_no_others():
    fields = [f["field"] for f in newdata.cross_check(NOW, {}, {})["fields"]]
    assert fields == ["prices", "interest rates"], "Karar 32-B: these two fields and no more"


def test_the_prices_check_cannot_be_queued_because_wrds_is_attended():
    """The finding of this step: A17-B lets a press ask the live source, and the one field that has a second
    source cannot use it - WRDS wants a login and Duo, so its run is attended and the queue cannot start it. The
    card says that and shows the command, rather than offering a button that queues a run nobody picks up."""
    row = newdata.cross_check(NOW, {}, {})["fields"][0]
    assert row["dataset"] is None and row["attended"] is True
    assert "attended" in row["cannot_queue"] and "Duo" in row["cannot_queue"]
    assert row["command"] == newdata.ATTENDED_COMMANDS["wrds/prices"]


def test_the_prices_row_reads_the_newest_comparison_report(monkeypatch):
    monkeypatch.setattr(newdata, "_diff_rows", lambda: [
        {"comparison": "crsp_vs_alpaca", "as_of": date(2026, 10, 31), "sessions": 20, "securities": 5,
         "close_differs": 1, "max_close_diff": 0.00012, "return_differs": 0, "max_return_diff": None}])
    row = newdata.cross_check(NOW, {}, {"wrds": 3})["fields"][0]
    assert row["as_of"] == "2026-10-31" and row["differs"] == 1
    assert "20 sessions" in row["checked"] and row["second_source"]
    assert row["remaining"] is None                  # WRDS publishes no day quota: no cap, nothing remaining


def test_the_prices_row_with_no_report_says_it_has_not_run(monkeypatch):
    monkeypatch.setattr(newdata, "_diff_rows", lambda: [])
    row = newdata.cross_check(NOW, {}, {})["fields"][0]
    assert row["as_of"] is None and "not run" in (row.get("why") or "").lower()


def test_the_interest_rate_row_names_no_second_source_and_no_dataset_to_queue():
    row = newdata.cross_check(NOW, {}, {})["fields"][1]
    assert row["second_source"] is None and row["dataset"] is None
    assert row["why"], "the reason is written, so the blank is not read as agreement"


def test_the_collector_reads_no_provider_and_opens_no_connection_on_a_render(monkeypatch):
    calls = []
    monkeypatch.setattr(newdata, "_diff_rows", lambda: calls.append("read") or [])
    newdata.cross_check(NOW, {}, {})
    assert calls == ["read"], "one read of the stored report, and nothing else"
