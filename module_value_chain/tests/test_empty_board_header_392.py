"""#392 and #391 (Value Chain half): an empty board says nothing was synced and names a control that exists.

With an empty store the header printed ``_now()`` as the data date, so a skim read "synced today" while the body
said there was no data; and the empty state told the user to press "Fetch (EDGAR)", a button this board does not
render -- the control that fills the corpus is Settings -> "Fetch 10-K sync".

The Relatedness and Co-movement halves of both issues belong to those boards.
"""
from __future__ import annotations

from datetime import datetime, timezone

from RevenueChaser.module_value_chain import report


def test_an_empty_board_does_not_claim_a_sync_today():
    html = report.render_value_chain_dashboard({"filings": [], "edges": [], "universe_total": 0, "pending": 0})
    assert "Last sync: no sync yet" in html
    assert datetime.now(timezone.utc).strftime("%B %d, %Y") not in html


def test_a_stored_sync_is_still_printed():
    html = report.render_value_chain_dashboard({"as_of": "2026-09-17T00:00:00+00:00",
                                                "filings": [], "edges": [], "universe_total": 0, "pending": 0})
    assert "Last sync: Thursday, September 17, 2026 EOD" in html


def test_the_empty_state_points_at_the_control_that_exists():
    html = report.render_value_chain_dashboard({"filings": [], "edges": [], "universe_total": 0, "pending": 0})
    assert "Fetch 10-K sync" in html
    assert "Run <b>Fetch (EDGAR)</b>" not in html
