"""``sec/13dg``: Schedule 13D and 13G, old (``SC 13D``) and new (``SCHEDULE 13D``) form names, one row per reporting
person from the XML (since 2024-12-18), the issuer and the filer from the SGML header for a text filing with no
XML (no amounts, never invented), and the issuer named by its CUSIP in the spine, else its CIK's one ticker."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import read, serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.ownership import sec, sched13dg

from . import ownership_fixture as ox

DAY = "2026-10-27"


@pytest.fixture
def feed(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec, "_shared_pace", lambda: True)
    e = ox.Edgar()
    g = ox.sched_filing("0000102909-26-000001", "SCHEDULE 13G", DAY, (14693, "BROWN FORMAN CORP"),
                        (102909, "VANGUARD GROUP INC"),
                        ox.sched_xml("13G", 14693, "BROWN FORMAN CORP", "115637209",
                                     [("VANGUARD GROUP INC", 25_000_000, 8.4, 0)]))
    d = ox.sched_filing("0001111111-26-000002", "SCHEDULE 13D/A", DAY, (1326380, "GAMESTOP CORP"),
                        (1111111, "ACTIVIST LP"),
                        ox.sched_xml("13D", 1326380, "GAMESTOP CORP", "36467W109",
                                     [("ACTIVIST LP", 30_000_000, 6.7, 30_000_000),
                                      ("ACTIVIST GP LLC", 30_000_000, 6.7, 0)]))
    old = ox.sched_filing("0000315066-26-000003", "SC 13G/A", DAY, (320193, "APPLE INC"), (315066, "FMR LLC"))
    e.add_day(DAY, [
        (ox.idx_row(14693, "BROWN FORMAN CORP", "SCHEDULE 13G", DAY, "0000102909-26-000001"), g),
        (ox.idx_row(102909, "VANGUARD GROUP INC", "SCHEDULE 13G", DAY, "0000102909-26-000001"), None),
        (ox.idx_row(1326380, "GAMESTOP CORP", "SCHEDULE 13D/A", DAY, "0001111111-26-000002"), d),
        (ox.idx_row(320193, "APPLE INC", "SC 13G/A", DAY, "0000315066-26-000003"), old),
        (ox.idx_row(320193, "APPLE INC", "8-K", DAY, "0000320193-26-000011"), None),
    ])
    e.install(http_mock)
    ox.make_spine()
    return e


def _run(day: str = "2026-10-28") -> dict:
    return base.run_request({"dataset": sched13dg.NAME, "params": {"as_of": day}}, sleep=lambda s: None)


def _served(sql: str, *args) -> list[tuple]:
    with serving.open_read("ownership") as con:
        return con.execute(sql, list(args)).fetchall()


def test_old_and_new_form_names_are_fetched_once_each(feed, http_mock):
    row = _run()
    assert row["result"] == "changed"
    asked = sorted(str(c.url).rsplit("/", 1)[1] for c in http_mock.calls("/Archives/edgar/data/"))
    assert asked == ["0000102909-26-000001.txt", "0000315066-26-000003.txt", "0001111111-26-000002.txt"]


def test_one_row_per_reporting_person_with_the_issuer_named_by_its_cusip(feed):
    _run()
    assert _served("SELECT form, issuer_cik, cusip, ticker, ticker_source, filer_cik, person_name, amount, percent, "
                   "sole_voting, event_date, status FROM beneficial_ownership ORDER BY accession, person_line") == [
        ("SCHEDULE 13G", "14693", "115637209", "BF-B", "spine:cusip", "102909", "VANGUARD GROUP INC", 25e6, 8.4,
         0.0, date(2026, 10, 20), "ok"),
        ("SC 13G/A", "320193", None, "AAPL", "spine:cik", "315066", None, None, None, None, None, "header_only"),
        ("SCHEDULE 13D/A", "1326380", "36467W109", "GME", "spine:cusip", "1111111", "ACTIVIST LP", 30e6, 6.7,
         30e6, date(2026, 10, 20), "ok"),
        ("SCHEDULE 13D/A", "1326380", "36467W109", "GME", "spine:cusip", "1111111", "ACTIVIST GP LLC", 30e6, 6.7,
         0.0, date(2026, 10, 20), "ok")]


def test_an_xml_document_with_no_person_found_is_unparsed_not_dropped(feed):
    feed.filings["0000102909-26-000001"] = ox.sched_filing(
        "0000102909-26-000001", "SCHEDULE 13G", DAY, (14693, "BROWN FORMAN CORP"), (102909, "VANGUARD GROUP INC"),
        '<?xml version="1.0"?><edgarSubmission><formData><other>x</other></formData></edgarSubmission>')
    _run()
    assert _served("SELECT status, issuer_cik, filer_cik FROM beneficial_ownership "
                   "WHERE accession = '0000102909-26-000001'") == [("unparsed", "14693", "102909")]


def test_the_read_api_keeps_a_header_only_filing_and_leaves_out_an_unparsed_one(feed):
    _run()
    rows = read.beneficial_ownership(["GME", "AAPL"])
    assert [(r["ticker"], r["person_name"], r["parse_status"]) for r in rows] == [
        ("AAPL", None, "header_only"), ("GME", "ACTIVIST LP", "ok"), ("GME", "ACTIVIST GP LLC", "ok")]
    assert {r["source"] for r in rows} == {sched13dg.NAME} and {r["status"] for r in rows} == {"changed"}
