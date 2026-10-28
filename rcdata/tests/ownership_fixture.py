"""The ownership store's test sources, in each source's documented shape.

FINRA's answers follow what its API answered on 2026-10-28 (field names, the ``record-total`` header); the values are
chosen for the tests. ``make_spine`` writes a small live ``spine.duckdb`` (the tables the ownership build reads) for
tests that need tickers named without running the whole spine.
"""
from __future__ import annotations

import json

import duckdb

from RevenueChaser.rcdata import serving

SPINE_TICKERS = ("MSFT", "BF-A", "BF-B", "BRK-B", "AAPL", "GME")


def make_spine(tickers=SPINE_TICKERS) -> None:
    """A live ``spine.duckdb`` whose ``security`` table holds ``tickers`` (the columns the ownership build reads)."""
    path = serving.live_path("spine")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE security (ticker TEXT, cik TEXT, gvkey TEXT, iid TEXT, cusip TEXT, tier TEXT)")
        con.executemany("INSERT INTO security VALUES (?, NULL, NULL, NULL, NULL, 'A')", [[t] for t in tickers])
    finally:
        con.close()


def finra_row(symbol: str, day: str, short: int, adv: int = 1_000_000, dtc=1.5, name: str = "SOME ISSUE") -> dict:
    return {"stockSplitFlag": None, "previousShortPositionQuantity": short - 100, "averageDailyVolumeQuantity": adv,
            "issueName": name, "currentShortPositionQuantity": short, "changePreviousNumber": 100,
            "accountingYearMonthNumber": int(day[:7].replace("-", "")), "settlementDate": day,
            "marketClassCode": "NNM", "symbolCode": symbol, "daysToCoverQuantity": dtc,
            "issuerServicesGroupExchangeCode": "R", "revisionFlag": None, "changePercent": 0.01}


class Finra:
    """FINRA's two routes: the partitions list and the paged data answer, by settlement date and offset."""

    def __init__(self, dates: dict[str, list[dict]], total_override: dict[str, int] | None = None):
        self.dates, self.total_override = dates, total_override or {}

    def partitions(self, request):
        body = {"availablePartitions": [{"partitions": [d]} for d in sorted(self.dates, reverse=True)]}
        return 200, json.dumps(body).encode(), {"content-type": "application/json"}

    def data(self, request):
        body = json.loads(request.content)
        day = body["compareFilters"][0]["fieldValue"]
        rows = self.dates.get(day, [])
        page = rows[body["offset"]:body["offset"] + body["limit"]]
        total = self.total_override.get(day, len(rows))
        return 200, json.dumps(page).encode(), {"record-total": str(total), "record-max-limit": "5000"}

    def install(self, http_mock) -> None:
        http_mock.add("finra.test/partitions/group/otcMarket/name/consolidatedShortInterest", self.partitions)
        http_mock.add("finra.test/data/group/otcMarket/name/consolidatedShortInterest", self.data)
