"""A UW ``/spot-exposures/expiry-strike`` answer shaped like the 26 Oct 2026 test answer: 391 rows (155 at 0 DTE,
236 at 1 DTE, strikes 4,800 to 9,200), every documented numeric field as a string, as UW sends them."""
from __future__ import annotations

from datetime import date

from RevenueChaser.core import calendar
from RevenueChaser.rcdata.build import uw_spot_expiry_strike as uw

SESSION = date(2026, 10, 26)


def body(session: date = SESSION) -> dict:
    nxt = calendar.next_trading_day(session)
    strikes0 = [6000.0 + 10 * i for i in range(155)]
    strikes1 = [4800.0 + 5 * i for i in range(235)] + [9200.0]
    rows = []
    for expiry, strikes in ((session, strikes0), (nxt, strikes1)):
        for k, strike in enumerate(strikes):
            row = {"expiry": expiry.isoformat(), "strike": f"{strike:.0f}", "time": f"{session}T20:15:00Z"}
            row.update({f: f"{(k + 1) * 1000.5 * (i + 1):.2f}" for i, f in enumerate(uw.NUMERIC_FIELDS)})
            row["price"] = "6812.34"
            rows.append(row)
    return {"data": rows}
