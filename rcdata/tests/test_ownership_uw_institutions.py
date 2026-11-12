"""``uw/institutions``: one quarterly read of UW's institution list, paged, typed into the map and its successions.

The answers below are shaped like the live account's of 12 Nov 2026 (every value a string, ``succession`` an object
with ``predecessors``, ``current_holder`` and ``succeeded_by``), answered at the HTTP boundary by the request's
``page``.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from urllib.parse import parse_qs

import pytest

from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.build import base, due
from RevenueChaser.rcdata.build.ownership import uw_institutions as uwi

DAY = "2026-11-12"
FETCHED_AT = 1_763_000_000.0


@pytest.fixture(autouse=True)
def uw_env(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    monkeypatch.setattr(unusual_whales, "_sleep", lambda _s: None)
    monkeypatch.setattr(uwi, "_now", lambda: FETCHED_AT)


def inst(cik: str, name: str, *, tags=(), hedge=False, total="1000.5", share="900.25", report="2026-09-30",
         filing="2026-11-07", short_name=None, predecessors=(), succeeded_by=None, current_holder=None) -> dict:
    """One institution as UW sends it: numbers as strings, the succession an object."""
    return {"cik": cik, "name": name, "short_name": short_name, "tags": list(tags), "is_hedge_fund": hedge,
            "date": report, "filing_date": filing, "total_value": total, "share_value": share,
            "people": [], "description": None,
            "succession": {"predecessors": list(predecessors), "current_holder": current_holder,
                           "succeeded_by": succeeded_by}}


def party(cik: str, name: str, when: str | None = None) -> dict:
    return {"cik": cik, "name": name, "effective_report_date": when}


def paged(http_mock, pages: list, fail: dict | None = None) -> None:
    """Answer page ``n`` with ``{"data": pages[n]}``; a page past the end is empty. ``fail`` maps a page to a
    status, and a ``dict`` page is sent as it is (an answer that carries no list of rows)."""
    def answer(request):
        q = parse_qs(request.url.query.decode())
        page = int(q.get("page", ["0"])[0])
        if fail and page in fail:
            return fail[page], b'{"error": "bad"}', {"content-type": "application/json"}
        rows = pages[page] if page < len(pages) else []
        body = rows if isinstance(rows, dict) else {"data": rows}
        return 200, json.dumps(body), {"content-type": "application/json"}
    http_mock.add("/institutions", answer)


def _run(day: str = DAY) -> dict:
    return base.run_request({"dataset": uwi.NAME, "params": {"as_of": day}}, sleep=lambda s: None)


def _served(sql: str, *args) -> list[tuple]:
    with serving.open_read("ownership") as con:
        return con.execute(sql, list(args)).fetchall()


def _pages_asked(http_mock) -> list:
    return [parse_qs(r.url.query.decode()).get("page", [None])[0] for r in http_mock.calls("/institutions")]


def _issues() -> list[tuple]:
    return [(r["kind"], r["detail"]) for r in ops.rows("build_issues")]


# ── The read and the two tables ────────────────────────────────────────────────────────────────────────────────

def test_one_read_serves_the_map_with_the_cik_padded_and_the_tags_sorted(http_mock):
    paged(http_mock, [[inst("1067983", "BERKSHIRE HATHAWAY INC", tags=["value_investor", "known", "activist"],
                            short_name="Berkshire"),
                       inst("0002012383", "BLACKROCK, INC.", total="6729538649155", share="6476388082861")]])
    row = _run()
    assert row["result"] == "changed" and row["rows"] == 2 and row["calls"] == 1
    assert _served("SELECT cik, name, short_name, tags, is_hedge_fund, report_date, filing_date, total_value, "
                   "share_value, snapshot_date, source FROM uw_institution ORDER BY cik") == [
        ("0001067983", "BERKSHIRE HATHAWAY INC", "Berkshire", ["activist", "known", "value_investor"], False,
         date(2026, 9, 30), date(2026, 11, 7), 1000.5, 900.25, date(2026, 11, 12), "uw/institutions"),
        ("0002012383", "BLACKROCK, INC.", None, [], False, date(2026, 9, 30), date(2026, 11, 7),
         6729538649155.0, 6476388082861.0, date(2026, 11, 12), "uw/institutions")]


def test_a_hedge_fund_flag_and_a_missing_date_are_kept_as_they_come(http_mock):
    paged(http_mock, [[inst("1791786", "ELLIOTT INVESTMENT MANAGEMENT L.P.", tags=["activist"], hedge=True,
                            report=None, filing=None, total=None, share="x")]])
    assert _run()["result"] == "changed"
    assert _served("SELECT is_hedge_fund, report_date, filing_date, total_value, share_value "
                   "FROM uw_institution") == [(True, None, None, None, None)]


def test_a_succession_is_read_from_both_sides_and_each_pair_is_one_row(http_mock):
    """HPS -> BlackRock lives only in BlackRock's ``predecessors``; a pair both sides name is ``both``."""
    paged(http_mock, [[inst("0002012383", "BLACKROCK, INC.",
                            predecessors=[party("0001670185", "HPS INVESTMENT PARTNERS, LLC", "2025-09-30"),
                                          party("0001364742", "BLACKROCK FINANCE, INC.", "2024-09-30")]),
                       inst("0001364742", "BLACKROCK FINANCE, INC.",
                            succeeded_by=party("0002012383", "BLACKROCK, INC.", "2024-09-30"),
                            current_holder=party("0002012383", "BLACKROCK, INC.")),
                       inst("0001670185", "HPS INVESTMENT PARTNERS, LLC")]])
    assert _run()["result"] == "changed"
    assert _served("SELECT predecessor_cik, successor_cik, effective_report_date, predecessor_name, "
                   "successor_name, origin, snapshot_date FROM uw_institution_succession "
                   "ORDER BY predecessor_cik") == [
        ("0001364742", "0002012383", date(2024, 9, 30), "BLACKROCK FINANCE, INC.", "BLACKROCK, INC.", "both",
         date(2026, 11, 12)),
        ("0001670185", "0002012383", date(2025, 9, 30), "HPS INVESTMENT PARTNERS, LLC", "BLACKROCK, INC.",
         "predecessors", date(2026, 11, 12))]
    assert _served("SELECT cik, succeeded_by_cik, succeeded_by_report_date, current_holder_cik FROM "
                   "uw_institution WHERE succeeded_by_cik IS NOT NULL") == [
        ("0001364742", "0002012383", date(2024, 9, 30), "0002012383")]


def test_a_list_with_no_succession_at_all_serves_an_empty_succession_table(http_mock):
    paged(http_mock, [[inst("1067983", "BERKSHIRE HATHAWAY INC")]])
    assert _run()["result"] == "changed"
    assert _served("SELECT count(*) FROM uw_institution_succession") == [(0,)]


def test_an_institution_that_succeeds_itself_is_no_succession(http_mock):
    paged(http_mock, [[inst("1067983", "A", succeeded_by=party("0001067983", "A", "2026-06-30"))]])
    assert _run()["result"] == "changed"
    assert _served("SELECT count(*) FROM uw_institution_succession") == [(0,)]


# ── Paging ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_pages_are_asked_until_one_is_short(http_mock, monkeypatch):
    monkeypatch.setattr(uwi, "PAGE_LIMIT", 2)
    paged(http_mock, [[inst("1", "A"), inst("2", "B")], [inst("3", "C"), inst("4", "D")], [inst("5", "E")]])
    row = _run()
    assert row["rows"] == 5 and row["calls"] == 3
    assert _pages_asked(http_mock) == [None, "1", "2"]
    assert all(q.get("limit") == ["2"] for q in (parse_qs(r.url.query.decode())
                                                 for r in http_mock.calls("/institutions")))


def test_a_total_that_fills_its_last_page_is_ended_by_the_empty_one(http_mock, monkeypatch):
    monkeypatch.setattr(uwi, "PAGE_LIMIT", 2)
    paged(http_mock, [[inst("1", "A"), inst("2", "B")], [inst("3", "C"), inst("4", "D")]])
    row = _run()
    assert row["rows"] == 4 and row["calls"] == 3                 # two full pages, then the empty one
    assert _pages_asked(http_mock) == [None, "1", "2"]


def test_an_answer_that_never_ends_stops_at_the_page_ceiling(http_mock, monkeypatch):
    monkeypatch.setattr(uwi, "PAGE_LIMIT", 1)
    monkeypatch.setattr(uwi, "MAX_PAGES", 3)
    paged(http_mock, [[inst(str(n), "A")] for n in range(1, 10)])
    row = _run()
    assert row["result"] == "failed" and "page ceiling" in row["note"]
    assert len(http_mock.calls("/institutions")) == 3
    assert not uwi.raw_path(date.fromisoformat(DAY)).exists()


def test_the_same_day_is_not_read_again(http_mock):
    paged(http_mock, [[inst("1067983", "BERKSHIRE HATHAWAY INC")]])
    _run()
    row = _run()
    assert row["calls"] == 0 and len(http_mock.calls("/institutions")) == 1


def test_a_second_snapshot_day_is_kept_beside_the_first(http_mock):
    paged(http_mock, [[inst("1067983", "BERKSHIRE HATHAWAY INC", tags=["activist"])]])
    _run()
    paged(http_mock, [[inst("1067983", "BERKSHIRE HATHAWAY INC", tags=["activist", "value_investor"])]])
    assert _run("2026-11-13")["result"] == "changed"
    assert _served("SELECT snapshot_date, tags FROM uw_institution ORDER BY snapshot_date") == [
        (date(2026, 11, 12), ["activist"]), (date(2026, 11, 13), ["activist", "value_investor"])]


# ── A list that moved while paging ─────────────────────────────────────────────────────────────────────────────

def test_a_cik_on_two_pages_is_kept_once_and_said_so(http_mock, monkeypatch):
    """UW orders by value, so a filing that lands mid-paging can repeat a row: the first one stands."""
    monkeypatch.setattr(uwi, "PAGE_LIMIT", 100)
    first = [inst(str(n), f"N{n}") for n in range(1, 101)]
    second = [inst("100", "N100", tags=["activist"]), *[inst(str(n), f"N{n}") for n in range(101, 150)]]
    paged(http_mock, [first, second])
    row = _run()
    assert row["result"] == "changed" and row["rows"] == 149
    assert _served("SELECT tags FROM uw_institution WHERE cik = '0000000100'") == [([],)]
    assert [d for k, d in _issues() if k == "moved"] == [
        "1 of 150 rows repeated across pages while paging; the first row of a CIK stands"]


def test_a_list_that_moved_past_the_share_fails_the_run(http_mock, monkeypatch):
    monkeypatch.setattr(uwi, "PAGE_LIMIT", 100)
    first = [inst(str(n), f"N{n}") for n in range(1, 101)]
    second = [*[inst(str(n), f"N{n}") for n in range(90, 101)], inst("200", "N200")]
    paged(http_mock, [first, second])
    row = _run()
    assert row["result"] == "failed" and "moved while paging" in row["note"]
    # Refused before the raw file is kept: a moved snapshot on disk would be typed again by every later run,
    # because a held raw file is reused at no call, and the dataset would fail for good.
    assert not uwi.raw_path(date.fromisoformat(DAY)).exists()
    assert list(uwi.parquet_dir().glob("date=*/part-0.parquet")) == []


# ── An answer that is not the list ─────────────────────────────────────────────────────────────────────────────

def test_a_list_whose_rows_carry_no_cik_is_refused_and_kept_no_file(http_mock):
    paged(http_mock, [[{"name": "NO CIK", "succession": {}}, {"cik": "", "name": "EMPTY"}]])
    row = _run()
    assert row["result"] == "failed" and "no row carries a CIK" in row["note"]
    assert not uwi.raw_path(date.fromisoformat(DAY)).exists()


def test_an_empty_list_fails_the_run_and_keeps_no_file(http_mock):
    paged(http_mock, [])
    row = _run()
    assert row["result"] == "failed" and "no rows" in row["note"]
    assert not uwi.raw_path(date.fromisoformat(DAY)).exists()


def test_an_answer_with_no_list_of_rows_fails_the_run(http_mock, monkeypatch):
    monkeypatch.setattr(uwi, "PAGE_LIMIT", 1)
    paged(http_mock, [[inst("1", "A")], {"ok": True}])
    row = _run()
    assert row["result"] == "failed" and "no list of rows" in row["note"]
    assert not uwi.raw_path(date.fromisoformat(DAY)).exists()


def test_a_refused_call_fails_the_run_and_keeps_no_file(http_mock, monkeypatch):
    monkeypatch.setattr(uwi, "PAGE_LIMIT", 1)
    paged(http_mock, [[inst("1", "A")], [inst("2", "B")]], fail={1: 403})
    row = _run()
    assert row["result"] == "failed" and "403" in row["note"]
    assert not uwi.raw_path(date.fromisoformat(DAY)).exists()


def test_a_row_without_a_cik_is_counted_and_left_out(http_mock):
    paged(http_mock, [[inst("1067983", "BERKSHIRE HATHAWAY INC"), {"name": "NO CIK", "succession": {}}, "junk"]])
    row = _run()
    assert row["result"] == "changed" and row["rows"] == 1
    assert [d for k, d in _issues() if k == "unkeyed"] == ["2 rows without a CIK"]


# ── Plumbing ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_dataset_reads_quarterly_and_starts_by_hand():
    assert uwi.DATASET.cadence == "quarterly" and uwi.DATASET.starts_by_hand
    assert uwi.DATASET.kind == "ownership" and uwi.DATASET.providers == ("uw",)
    assert uwi.DATASET.tables == ("uw_institution", "uw_institution_succession")


def test_a_quarterly_dataset_is_due_again_after_its_cycle():
    assert "quarterly" in due.CADENCES and due.CYCLE_DAYS["quarterly"] == 91


def test_the_quarterly_cycle_is_what_the_due_check_counts(http_mock, monkeypatch):
    """Not due 90 days after the run that built it, due on the 91st: the cadence the dataset declares."""
    from RevenueChaser.core import calendar
    paged(http_mock, [[inst("1067983", "BERKSHIRE HATHAWAY INC")]])
    _run()
    checked = date.fromisoformat(str(ops.latest_check(uwi.NAME)["checked_on"])[:10])
    monkeypatch.setattr(calendar, "today_ny", lambda: checked + timedelta(days=90))
    assert due.is_due(uwi.DATASET, "full")[0] is False
    monkeypatch.setattr(calendar, "today_ny", lambda: checked + timedelta(days=91))
    assert due.is_due(uwi.DATASET, "full")[0] is True
