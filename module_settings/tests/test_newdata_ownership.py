"""PR 16: the ownership store on Settings' "New data system" section: a card per ownership dataset with a label of
its own, and each card's rows and newest date read from its served table in ``ownership.duckdb``."""
from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import pytest

from RevenueChaser.core import calendar
from RevenueChaser.module_settings import newdata
from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.tests.conftest import market_roots  # noqa: F401 - market roots per test

NOW = datetime(2026, 10, 28, 3, 0, tzinfo=timezone.utc)
OWNERSHIP = ("finra/short_interest", "regsho/threshold", "ibkr/shortable", "sec/ftd", "sec/13f", "sec/form4",
             "sec/13dg", "ownership/serve")


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: NOW)


def _card(data: dict, dataset: str) -> dict:
    return next(c for c in data["datasets"] if c["dataset"] == dataset)


def test_a_card_per_ownership_dataset_with_a_label_of_its_own():
    data = newdata.collect(NOW)
    assert set(OWNERSHIP) <= {c["dataset"] for c in data["datasets"]}
    assert all(newdata.LABELS.get(d) and not _card(data, d)["label"].startswith(d) for d in OWNERSHIP)


def test_each_card_reads_its_rows_and_newest_date_from_ownership_duckdb():
    path = serving.live_path("ownership")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE finra_short_interest AS SELECT * FROM (VALUES (DATE '2026-09-30'), "
                    "(DATE '2026-10-15')) t(settlement_date)")
        con.execute("CREATE TABLE sec13f_ticker_period AS SELECT DATE '2026-06-30' AS period")
        con.execute("CREATE TABLE sec13f_periods AS SELECT DATE '2026-09-30' AS period")
    finally:
        con.close()
    data = newdata.collect(NOW)
    assert (_card(data, "finra/short_interest")["table_rows"], _card(data, "finra/short_interest")["newest"]) == (
        2, "2026-10-15")
    assert (_card(data, "ownership/serve")["table_rows"], _card(data, "ownership/serve")["newest"]) == (
        2, "2026-09-30")
