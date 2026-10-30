"""PR 12: the fundamentals layer on Settings' "New data system" section: a card per dataset with its cadence, the
four WRDS ones attended with their own CLI command, and the WRDS difference slot filled by the newest monthly
difference report of a WRDS check dataset (decided 27 Oct)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.module_settings import newdata, report
from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.tests.conftest import market_roots  # noqa: F401 - market roots per test

NOW = datetime(2026, 10, 28, 3, 0, tzinfo=timezone.utc)
FUNDAMENTALS = {"sec/companyfacts": "daily", "sec/filings": "daily", "alphavantage/earnings_calendar": "daily",
                "uw/financials": "daily", "yfinance/estimates": "weekly", "sec/fsds": "monthly",
                "wrds/fundq": "monthly", "wrds/funda": "monthly", "wrds/firm_ratio": "monthly",
                "wrds/segments": "monthly", "fundamentals/tables": "daily"}
WRDS = ["wrds/fundq", "wrds/funda", "wrds/firm_ratio", "wrds/segments"]


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: NOW)


def _card(data: dict, dataset: str) -> dict:
    return next(c for c in data["datasets"] if c["dataset"] == dataset)


def test_a_card_per_fundamentals_dataset_with_its_cadence_and_a_label():
    data = newdata.collect(NOW)
    assert {d: _card(data, d)["cadence"] for d in FUNDAMENTALS} == FUNDAMENTALS
    assert all(not _card(data, d)["label"].startswith(d) for d in FUNDAMENTALS)
    assert [d for d in FUNDAMENTALS if _card(data, d)["attended"]] == WRDS


def test_an_attended_card_shows_its_own_kinds_command():
    data = newdata.collect(NOW)
    assert _card(data, "wrds/fundq")["attended_command"] == (
        "python -m RevenueChaser.rcdata.build backfill wrds/fundq wrds/funda wrds/firm_ratio wrds/segments")
    assert _card(data, "wrds/co_hgic")["attended_command"] == (
        "python -m RevenueChaser.rcdata.build backfill wrds/sec_idhist wrds/co_hgic wrds/idxcst_his")
    assert _card(data, "sec/companyfacts")["attended_command"] is None


def test_the_wrds_difference_slot_is_the_newest_monthly_report():
    note = ("WRDS fundq vs the SEC: revenue 9 of 10, net income 10 of 10, diluted EPS 8 of 10 agree; "
            "3 revision(s) in the snapshot of 2026-10-27 against 2026-09-26")
    ops.record_check("wrds/fundq", "changed", source_as_of="2026-10-27", checked_on="2026-10-27", note=note)
    ops.record_check("wrds/fundq", "skipped", source_as_of="2026-10-28", checked_on="2026-10-28",
                     note="skipped: attended")                     # a skipped run is not a report
    data = newdata.collect(NOW)
    # ``produced`` since the review's S8: a slot that has not run shows a dash, not a sentence that reads as one.
    assert _card(data, "wrds/fundq")["wrds"] == {"text": note, "as_of": "2026-10-27", "produced": True}
    assert _card(data, "wrds/funda")["wrds"] == {"text": "no WRDS run yet", "produced": False}
    assert _card(data, "sec/companyfacts")["wrds"] == {"text": "no WRDS run yet", "produced": False}
    html = report.render_settings_dashboard({"newdata": data, "rows": []})
    card = html[html.index('data-dataset="wrds/fundq" data-status'):]
    # wrds/fundq is the last dataset row, so the slice ends at the Queue card that follows the rows.
    assert "3 revision(s) in the snapshot of 2026-10-27" in card[:card.index('id="nd-queue"')]


def test_editing_one_cards_wrds_slot_leaves_the_others():
    # The Opus review's N3: the "no WRDS run yet" slot was one module-level dict shared by every card.
    data = newdata.collect(NOW)
    _card(data, "wrds/funda")["wrds"]["text"] = "edited"
    assert _card(data, "sec/companyfacts")["wrds"] == {"text": "no WRDS run yet", "produced": False}
    assert newdata.NO_WRDS == {"text": "no WRDS run yet", "produced": False}
