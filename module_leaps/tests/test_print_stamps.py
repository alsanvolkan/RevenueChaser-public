"""#295 (D09): a stored option print carries source, fetched_at, session_date and run_at.

``/option-trades`` serves only the latest trading day (docs/openapi.yaml:18822-18823), so the
stamps say which session and which run a print came from. A known id is never rewritten.
Offline; the clock is the ``core.calendar._now`` seam.
"""
from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar, rawstore
from RevenueChaser.module_leaps import engine as la


def _print(pid, premium=200000.0):
    return {"id": pid, "ticker": "ZZZZ", "strike": 200.0, "expiry": "2027-06-18",
            "premium": premium, "size": 20, "option_type": "call", "delta": 0.8,
            "next_earnings_date": None, "executed_at": "2026-10-08T14:00:00Z",
            "ask_vol": 10, "bid_vol": 2}


def _rows():
    return {r["id"]: r for r in rawstore.query(
        "SELECT id, premium, source, fetched_at, session_date, run_at, discovered_at "
        "FROM leaps_flow_prints")}


# Thu 2026-10-08 17:00 ET (after the close): session 2026-10-08.
AFTER_CLOSE = datetime(2026, 10, 8, 21, 0, tzinfo=timezone.utc)
# Fri 2026-10-09 02:00 ET (before the open): the previous session, 2026-10-08.
BEFORE_OPEN = datetime(2026, 10, 9, 6, 0, tzinfo=timezone.utc)


class TestStoreStamps:
    def test_a_new_print_carries_the_four_stamps(self, rawstore_tmp_db):
        run_at = AFTER_CLOSE.timestamp()
        assert la.store_prints([_print("p1")], today=date(2026, 10, 8),
                               run_at=run_at, fetched_at=run_at + 3.0) == 1
        row = _rows()["p1"]
        assert row["source"] == "uw"
        assert row["run_at"] == run_at
        assert row["fetched_at"] == run_at + 3.0
        assert row["session_date"] == date(2026, 10, 8)

    def test_session_date_follows_rule_b_on_run_at(self, rawstore_tmp_db):
        la.store_prints([_print("p1")], run_at=BEFORE_OPEN.timestamp())
        assert _rows()["p1"]["session_date"] == date(2026, 10, 8)

    def test_session_date_is_the_runs_not_the_pages(self, rawstore_tmp_db):
        # The scan starts at 09:29:55 ET (session 2026-10-08); a page arrives after the open.
        run_at = datetime(2026, 10, 9, 13, 29, 55, tzinfo=timezone.utc).timestamp()
        la.store_prints([_print("p1")], run_at=run_at, fetched_at=run_at + 10)
        assert _rows()["p1"]["session_date"] == date(2026, 10, 8)

    def test_session_comes_from_run_at_not_the_print_or_the_clock(self, rawstore_tmp_db):
        # Fri 2025-12-26 08:00 ET (pre-open, the day after Christmas): session Wed 2025-12-24.
        # The print executed 2026-10-08 and the machine clock is today: neither may decide it.
        la.store_prints([_print("p1")],
                        run_at=datetime(2025, 12, 26, 13, 0, tzinfo=timezone.utc).timestamp())
        assert _rows()["p1"]["session_date"] == date(2025, 12, 24)

    def test_a_bulk_page_keeps_the_stamps(self, rawstore_tmp_db):
        # A production page is 250 rows, which takes rawstore's bulk insert path.
        run_at = AFTER_CLOSE.timestamp() + 0.123456
        assert la.store_prints([_print(f"b{i}") for i in range(250)], run_at=run_at,
                               fetched_at=run_at + 1.5) == 250
        assert rawstore.query(
            "SELECT DISTINCT source, fetched_at, session_date, run_at, "
            "typeof(session_date) AS t FROM leaps_flow_prints") == [
            {"source": "uw", "fetched_at": run_at + 1.5, "session_date": date(2026, 10, 8),
             "run_at": run_at, "t": "DATE"}]

    def test_a_bulk_page_past_the_holiday_table_keeps_every_print(self, rawstore_tmp_db):
        run_at = datetime(2030, 1, 3, 21, 0, tzinfo=timezone.utc).timestamp()
        assert la.store_prints([_print(f"c{i}") for i in range(100)], run_at=run_at) == 100
        assert rawstore.query("SELECT count(*) AS n, count(session_date) AS s "
                              "FROM leaps_flow_prints") == [{"n": 100, "s": 0}]

    def test_without_stamps_the_clock_seam_gives_them(self, rawstore_tmp_db, monkeypatch):
        monkeypatch.setattr(calendar, "_now", lambda: AFTER_CLOSE)
        la.store_prints([_print("p1")], today=date(2026, 10, 8))
        row = _rows()["p1"]
        assert row["run_at"] == AFTER_CLOSE.timestamp()
        assert row["fetched_at"] == AFTER_CLOSE.timestamp()
        assert row["session_date"] == date(2026, 10, 8)
        assert row["source"] == "uw"

    def test_a_known_id_is_not_rewritten(self, rawstore_tmp_db):
        first = AFTER_CLOSE.timestamp()
        la.store_prints([_print("p1", premium=100000.0)], today=date(2026, 10, 8),
                        run_at=first, fetched_at=first)
        later = BEFORE_OPEN.timestamp() + 86400 * 4    # Tue 2026-10-13 02:00 ET: session Mon 2026-10-12
        assert la.store_prints([_print("p1", premium=999999.0), _print("p2")],
                               today=date(2026, 10, 13), run_at=later, fetched_at=later) == 1
        rows = _rows()
        assert rows["p1"]["premium"] == 100000.0
        assert rows["p1"]["run_at"] == first
        assert rows["p1"]["fetched_at"] == first
        assert rows["p1"]["session_date"] == date(2026, 10, 8)
        assert rows["p1"]["discovered_at"] == "2026-10-08"
        assert rows["p2"]["run_at"] == later
        assert rawstore.count("leaps_flow_prints") == 2

    def test_a_run_outside_the_holiday_table_still_stores_the_print(self, rawstore_tmp_db, caplog):
        # session_for never guesses past 2028; the print is kept (the day would be lost
        # otherwise) with no session_date, and the gap is logged.
        run_at = datetime(2030, 1, 3, 21, 0, tzinfo=timezone.utc).timestamp()
        with caplog.at_level("WARNING"):
            assert la.store_prints([_print("p1")], run_at=run_at) == 1
        row = _rows()["p1"]
        assert row["session_date"] is None
        assert row["run_at"] == run_at
        assert any("session" in r.message.lower() for r in caplog.records)


class _UW:
    """Two full pages then a short one; the clock moves 10 s per call."""

    def __init__(self, clock):
        self.clock = clock

    def option_trades(self, min_dte, min_premium, limit, page):
        self.clock[0] = self.clock[0].replace(second=self.clock[0].second + 10)
        n = limit if page < 3 else 1
        return [{"id": f"p{page}-{i}", "underlying_symbol": "ABC", "strike": "10",
                 "expiry": "2027-06-18", "premium": "60000", "size": 1, "option_type": "call",
                 "delta": "0.6", "next_earnings_date": None,
                 "executed_at": "2026-10-08T14:00:00Z", "ask_vol": 1, "bid_vol": 0, "tags": []}
                for i in range(n)]


class TestDiscoverStamps:
    def test_one_run_at_per_scan_and_fetched_at_per_page(self, rawstore_tmp_db, monkeypatch):
        clock = [AFTER_CLOSE]
        monkeypatch.setattr(calendar, "_now", lambda: clock[0])
        _rows_out, new = la.discover_flow_prints(_UW(clock), limit=2, max_pages=5,
                                                 today=date(2026, 10, 8))
        assert new == 5
        rows = _rows()
        start = AFTER_CLOSE.timestamp()
        assert {r["run_at"] for r in rows.values()} == {start}
        assert rows["p1-0"]["fetched_at"] == start + 10
        assert rows["p2-1"]["fetched_at"] == start + 20
        assert rows["p3-0"]["fetched_at"] == start + 30
        assert {r["session_date"] for r in rows.values()} == {date(2026, 10, 8)}
        assert {r["source"] for r in rows.values()} == {"uw"}

    def test_an_injected_now_fixes_both_stamps(self, rawstore_tmp_db, monkeypatch):
        clock = [datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)]
        monkeypatch.setattr(calendar, "_now", lambda: clock[0])
        la.discover_flow_prints(_UW(clock), limit=2, max_pages=5, today=date(2026, 10, 8),
                                now=AFTER_CLOSE)
        rows = _rows()
        assert {r["run_at"] for r in rows.values()} == {AFTER_CLOSE.timestamp()}
        assert {r["fetched_at"] for r in rows.values()} == {AFTER_CLOSE.timestamp()}


def test_refresh_writes_no_print(rawstore_tmp_db, monkeypatch):
    """The Refresh path (scan_leaps_cached) reads the store only: no row, no stamp."""
    from RevenueChaser.module_leaps.scan import scan_leaps_cached
    la.store_prints([_print("p1")], run_at=AFTER_CLOSE.timestamp())
    before = _rows()
    monkeypatch.setattr(la, "store_prints", lambda *a, **k: pytest.fail("Refresh wrote prints"))
    scan_leaps_cached(today=date(2026, 10, 9))
    assert _rows() == before
