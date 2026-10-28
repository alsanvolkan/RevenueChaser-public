"""``sec/ftd``: the SEC's fails-to-deliver halves, the next ones asked until one is not published yet (404), the first
run taking the newest published half, a backfill every half of its range, an answer that is not the zip refused, and
the rows typed and named by the spine."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.ownership import ftd, sec

from . import ownership_fixture as ox

SEP = ["20260916|037833100|AAPL|120345|APPLE INC (COM)|252.31", "20260917|36467W109|GME|9000|GAMESTOP CORP|.",
       "20260917|000000000|ZZZZ|5|NOBODY CORP|1.00"]


@pytest.fixture
def src(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec, "_shared_pace", lambda: True)
    monkeypatch.setenv("RC_HTTP_CONTACT", "ops@example.org")
    feed = ox.Ftd()
    feed.halves["202609b"] = ox.ftd_zip(SEP)
    feed.install(http_mock)
    ox.make_spine()
    return feed


def _run(day: str, **params) -> dict:
    return base.run_request({"dataset": ftd.NAME, "params": {"as_of": day, **params}}, sleep=lambda s: None)


def _served(sql: str, *args) -> list[tuple]:
    with serving.open_read("ownership") as con:
        return con.execute(sql, list(args)).fetchall()


def _asked(http_mock) -> list[str]:
    return [str(c.url).rsplit("/", 1)[1] for c in http_mock.calls("fails-deliver-data")]


def test_the_first_run_takes_the_newest_published_half(src, http_mock):
    row = _run("2026-10-28")
    assert row["result"] == "changed" and row["rows"] == 3
    assert _asked(http_mock) == ["cnsfails202610a.zip", "cnsfails202609b.zip"]
    assert "ops@example.org" in http_mock.calls("fails-deliver-data")[0].headers["user-agent"]
    assert _served("SELECT settlement_date, cusip, symbol, ticker, quantity, price, half FROM sec_ftd "
                   "ORDER BY settlement_date, symbol") == [
        (date(2026, 9, 16), "037833100", "AAPL", "AAPL", 120345, 252.31, "2026-09b"),
        (date(2026, 9, 17), "36467W109", "GME", "GME", 9000, None, "2026-09b"),
        (date(2026, 9, 17), "000000000", "ZZZZ", None, 5, 1.0, "2026-09b")]


def test_a_later_run_asks_the_next_halves_until_one_is_not_published(src, http_mock):
    _run("2026-10-28")
    src.halves["202610a"] = ox.ftd_zip(["20261001|037833100|AAPL|1|APPLE INC (COM)|250.00"], "cnsfails202610a.txt")
    src.halves["202610b"] = ox.ftd_zip(["20261016|037833100|AAPL|2|APPLE INC (COM)|251.00"], "cnsfails202610b.txt")
    http_mock.requests.clear()
    row = _run("2026-11-20")
    assert _asked(http_mock) == ["cnsfails202610a.zip", "cnsfails202610b.zip", "cnsfails202611a.zip"]
    assert row["source_as_of"] == "2026-10-16" and ftd.stored() == ["202609b", "202610a", "202610b"]


def test_a_backfill_asks_every_half_of_its_range(src, http_mock):
    src.halves["202608a"] = ox.ftd_zip(["20260803|037833100|AAPL|1|APPLE INC (COM)|200.00"], "cnsfails202608a.txt")
    row = base.run_request({"dataset": ftd.NAME, "params": {"mode": "backfill", "from": "2026-08-01",
                                                            "to": "2026-09-30"}}, sleep=lambda s: None)
    assert row["result"] == "changed"
    assert _asked(http_mock) == ["cnsfails202608a.zip", "cnsfails202608b.zip", "cnsfails202609a.zip",
                                 "cnsfails202609b.zip"]
    assert ftd.stored() == ["202608a", "202609b"]


def test_an_answer_that_is_not_the_zip_is_refused_and_not_kept(src):
    src.halves["202609b"] = b"<html>We are sorry</html>"
    row = _run("2026-10-28")
    assert row["result"] == "failed" and "not a zip" in row["note"]
    assert ftd.stored() == []


def test_the_sec_pacer_keeps_its_requests_apart():
    now, slept = [0.0], []

    def sleep(s):
        slept.append(s)
        now[0] += s

    ctx = base.RunContext(run_id="r", dataset="d", clock=lambda: now[0], sleep=sleep)
    pacer = sec.Pacer(ctx, shared=lambda: True)
    times = []
    for _ in range(10):
        pacer.wait()
        times.append(now[0])
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert min(gaps) >= 1 / sec.PER_SECOND - 1e-9
