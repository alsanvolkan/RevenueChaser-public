"""``sec/13f`` and the EDGAR walk it shares: the daily index read once and kept, each 13F-HR and 13F-HR/A fetched
once (one request, the full submission), typed into filings and holdings keyed on report period and filed date, a
time budget that stops a deadline day's run and lets the next hour go on, a day EDGAR keeps closed marked absent
once settled while a refusal fails the run, and a backfill that moves the walk's first day."""
from __future__ import annotations

import collections
import json
from datetime import date

import duckdb
import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base, due
from RevenueChaser.rcdata.build.ownership import edgar, sec, sec13f

from . import ownership_fixture as ox

MSFT = ox.f13_holding("MICROSOFT CORP", "594918104", 500_000, 1_000)
AAPL_PUT = ox.f13_holding("APPLE INC", "037833100", 90_000, 400, put_call="Put")
BOND = ox.f13_holding("SOME CORP", "999999AA1", 10_000, 10_000, sh_type="PRN")
ETN = ox.f13_holding("EATON CORP PLC", "G29183103", 250_000, 700, figi="BBG000BJ3PD2")


@pytest.fixture
def feed(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec, "_shared_pace", lambda: True)
    monkeypatch.setenv("RC_HTTP_CONTACT", "ops@example.org")
    e = ox.Edgar()
    e.add_day("2026-10-27", [
        (ox.idx_row(1000001, "ALPHA CAPITAL", "13F-HR", "2026-10-27", "0001000001-26-000001"),
         ox.f13_filing("0001000001-26-000001", 1000001, "2026-10-27", [MSFT, AAPL_PUT, BOND, ETN])),
        (ox.idx_row(1000002, "BETA ADVISORS", "13F-HR/A", "2026-10-27", "0001000002-26-000007"),
         ox.f13_filing("0001000002-26-000007", 1000002, "2026-10-27", [MSFT], form="13F-HR/A",
                       amendment_type="RESTATEMENT", amendment_no=1)),
        (ox.idx_row(1000003, "GAMMA NOTICE LLC", "13F-NT", "2026-10-27", "0001000003-26-000002"), None),
        (ox.idx_row(1000004, "DELTA FUND", "13F-HR", "2026-10-27", "0001000004-26-000003"),
         ox.f13_filing("0001000004-26-000003", 1000004, "2026-10-27", [], report_type="13F NOTICE")),
        (ox.idx_row(1000005, "EPSILON BROKEN", "13F-HR", "2026-10-27", "0001000005-26-000004"),
         ox.submission("0001000005-26-000004", "13F-HR", "2026-10-27", [("FILER", 1000005, "EPSILON BROKEN")],
                       [("13F-HR", "primary_doc.txt", None)])),
        (ox.idx_row(789019, "MICROSOFT CORP", "4", "2026-10-27", "0001062993-26-000100"), None),
    ])
    e.add_day("2026-10-26", [(ox.idx_row(789019, "MICROSOFT CORP", "8-K", "2026-10-26", "0000789019-26-000050"),
                              None)])
    e.install(http_mock)
    return e


def _run(day: str = "2026-10-28", clock=None, sleep=lambda s: None, **params) -> dict:
    kw = {"clock": clock} if clock else {}
    return base.run_request({"dataset": sec13f.NAME, "params": {"as_of": day, **params}}, sleep=sleep, **kw)


def _parquet(sub: str, sql: str) -> list[tuple]:
    files = sorted((base.parquet_root() / "sec" / "13f" / sub).glob("day=*/part-0.parquet"))
    src = "read_parquet([" + ", ".join(f"'{f}'" for f in files) + "], union_by_name = true)"
    return duckdb.connect().execute(sql.replace("{src}", src)).fetchall()


def test_the_first_run_walks_its_lookback_and_fetches_each_13f_once(feed, http_mock):
    row = _run()
    assert row["result"] == "changed"
    days = [str(c.url).rsplit("master.", 1)[1][:8] for c in http_mock.calls("daily-index")]
    assert days == ["20261027", "20261026"]                     # newest first; 25 Oct is a Sunday
    asked = sorted(str(c.url).rsplit("/", 1)[1] for c in http_mock.calls("/Archives/edgar/data/"))
    assert asked == ["0001000001-26-000001.txt", "0001000002-26-000007.txt", "0001000004-26-000003.txt",
                     "0001000005-26-000004.txt"]                 # 13F-HR and /A only, once each
    assert row["calls"] == 6
    assert edgar.index_path(date(2026, 10, 27)).exists()


def test_filings_are_typed_with_their_period_amendment_and_status(feed):
    _run()
    assert _parquet("filings", "SELECT accession, cik, form, period, filed, is_amendment, amendment_no, "
                    "amendment_type, report_type, holdings, status FROM {src} ORDER BY accession") == [
        ("0001000001-26-000001", "1000001", "13F-HR", date(2026, 9, 30), date(2026, 10, 27), False, None, None,
         "13F HOLDINGS REPORT", 4, "ok"),
        ("0001000002-26-000007", "1000002", "13F-HR/A", date(2026, 9, 30), date(2026, 10, 27), True, 1,
         "RESTATEMENT", "13F HOLDINGS REPORT", 1, "ok"),
        ("0001000004-26-000003", "1000004", "13F-HR", date(2026, 9, 30), date(2026, 10, 27), False, None, None,
         "13F NOTICE", 0, "notice"),
        ("0001000005-26-000004", "1000005", "13F-HR", None, date(2026, 10, 27), False, None, None, None, 0,
         "unparsed")]
    with serving.open_read("ownership") as con:
        assert con.execute("SELECT count(*), count(*) FILTER (status = 'ok') FROM sec13f_filings").fetchone() == (4, 2)


def test_holdings_keep_every_row_with_its_period_and_filed_date(feed):
    _run()
    assert _parquet("holdings", "SELECT accession, period, filed, cusip, figi, value, value_usd, shares, sh_type, "
                    "put_call FROM {src} WHERE accession = '0001000001-26-000001' ORDER BY line") == [
        ("0001000001-26-000001", date(2026, 9, 30), date(2026, 10, 27), "594918104", None, 500000.0, 500000.0,
         1000.0, "SH", None),
        ("0001000001-26-000001", date(2026, 9, 30), date(2026, 10, 27), "037833100", None, 90000.0, 90000.0,
         400.0, "SH", "Put"),
        ("0001000001-26-000001", date(2026, 9, 30), date(2026, 10, 27), "999999AA1", None, 10000.0, 10000.0,
         10000.0, "PRN", None),
        ("0001000001-26-000001", date(2026, 9, 30), date(2026, 10, 27), "G29183103", "BBG000BJ3PD2", 250000.0,
         250000.0, 700.0, "SH", None)]


def test_a_filing_before_2023_reports_value_in_thousands(feed):
    feed.add_day("2022-11-14", [(ox.idx_row(1000001, "ALPHA CAPITAL", "13F-HR", "2022-11-14", "0001000001-22-000009"),
                                 ox.f13_filing("0001000001-22-000009", 1000001, "2022-11-14", [MSFT],
                                               period="09-30-2022"))])
    row = base.run_request({"dataset": sec13f.NAME, "params": {"mode": "backfill", "from": "2022-11-14",
                                                               "to": "2022-11-14"}}, sleep=lambda s: None)
    assert row["result"] == "changed"
    assert _parquet("holdings", "SELECT value, value_usd FROM {src} WHERE accession = '0001000001-22-000009'") == [
        (500000.0, 500_000_000.0)]


def test_a_day_already_walked_is_not_asked_again(feed, http_mock):
    _run()
    http_mock.requests.clear()
    row = _run()
    assert http_mock.calls("/Archives/edgar/data/") == [] and http_mock.calls("daily-index") == []
    assert row["calls"] == 0


def test_an_index_not_published_yet_is_asked_again_and_a_settled_missing_one_is_marked_absent(feed, http_mock):
    """A day with no index object answers 403 (the ``/Archives/`` bucket's ``AccessDenied``), not 404."""
    feed.missing.add("20261027")
    _run()
    assert not edgar.index_none(date(2026, 10, 27)).exists()     # yesterday: not published yet
    http_mock.requests.clear()
    _run("2026-11-02")
    days = [str(c.url).rsplit("master.", 1)[1][:8] for c in http_mock.calls("daily-index")]
    assert "20261027" in days and edgar.index_none(date(2026, 10, 27)).exists()


def test_a_day_edgar_keeps_closed_is_marked_absent_and_the_walk_goes_on(feed, http_mock):
    """12 Oct 2026 is Columbus Day: a federal holiday the NYSE trades through, so the walk asks it and EDGAR has no
    index for it. Measured against sec.gov on 2026-10-30: 403 for 20261012, 200 for 20261009 and 20261013."""
    acc = "0001000001-26-000011"
    feed.add_day("2026-10-13", [(ox.idx_row(1000001, "ALPHA CAPITAL", "13F-HR", "2026-10-13", acc),
                                 ox.f13_filing(acc, 1000001, "2026-10-13", [MSFT], period="09-30-2026"))])
    row = base.run_request({"dataset": sec13f.NAME, "params": {"mode": "backfill", "from": "2026-10-12",
                                                               "to": "2026-10-13"}}, sleep=lambda s: None)
    assert row["result"] == "changed"
    assert edgar.index_none(date(2026, 10, 12)).exists()
    assert len(http_mock.calls(acc)) == 1                        # 13 Oct still fetched


def test_a_refused_403_fails_the_run_and_marks_no_day_absent(feed):
    """The SEC's own 403 (an undeclared User-Agent, or the fair-access rate) is not a missing day: the run fails and
    nothing is written, so the next run asks the same day again."""
    feed.refuse = True
    row = _run()
    assert row["result"] == "failed" and "HTTP 403" in row["note"]
    assert not edgar.index_none(date(2026, 10, 27)).exists()


def test_a_stretch_of_missing_weekdays_edgar_does_not_keep_closed_stops_the_run(feed):
    """A bucket-wide refusal would answer every day with the same 403 as a closed day. One missing weekday that is
    no federal holiday is marked absent as before; a stretch of them stops the run instead of skipping real days."""
    feed.missing |= {f"202609{d:02d}" for d in range(1, 31)}
    row = base.run_request({"dataset": sec13f.NAME, "params": {"mode": "backfill", "from": "2026-09-01",
                                                               "to": "2026-09-30"}}, sleep=lambda s: None)
    assert row["result"] == "failed" and "no daily index" in row["note"]
    # Not one of them is written off, the first two included: the next run asks all three again.
    for day in (date(2026, 9, 30), date(2026, 9, 29), date(2026, 9, 28)):
        assert not edgar.index_none(day).exists(), day


def test_a_filing_the_archive_does_not_hold_is_marked_absent(feed, http_mock):
    """The index lists an accession the archive has no object for: the same 403, and the filing is not asked again."""
    acc = "0001000007-26-000001"
    feed.add_day("2026-10-26", [(ox.idx_row(1000007, "ZETA FUND", "13F-HR", "2026-10-26", acc), None)])
    _run()
    assert sec13f.DATASET.filing_none(date(2026, 10, 26), acc).exists()
    assert len(http_mock.calls(acc)) == 1


def test_an_isolated_missing_day_earlier_in_the_walk_does_not_hide_a_later_stretch(feed):
    """The walk goes newest first, so the stretch is the days it found last, not the earliest dates it holds."""
    feed.missing |= {"20260930", "20260925", "20260924", "20260923"}
    row = base.run_request({"dataset": sec13f.NAME, "params": {"mode": "backfill", "from": "2026-09-23",
                                                               "to": "2026-09-30"}}, sleep=lambda s: None)
    assert row["result"] == "failed" and "2026-09-23, 2026-09-24, 2026-09-25" in row["note"]


def test_a_stretch_of_filings_the_archive_does_not_answer_for_stops_the_run(feed):
    """The bucket's own ``AccessDenied``, the other shape an absent filing answers with: three in a row and nothing
    in the archive hands an object over, so the run stops rather than marking them absent for good. Until 2026-11-06
    the count alone decided this, and a filer whose filings EDGAR had removed ended a backfill at its day for good;
    now the archive is asked for a filing it is known to hold, and this is the case where it cannot."""
    feed.add_day("2026-10-26", [(ox.idx_row(1000010 + i, f"ETA {i}", "13F-HR", "2026-10-26",
                                            f"0001000010-26-00000{i}"), None) for i in range(4)])
    # 27 October is walked first and keeps its index, but every filing the archive is asked for is gone, so there
    # is nothing it demonstrably holds and the 26th's cluster cannot be told from a path that answers nothing.
    feed.not_found |= set(feed.filings)
    row = _run()
    assert row["result"] == "failed" and "did not hand over one it holds either" in row["note"]
    for i in range(3):
        assert not sec13f.DATASET.filing_none(date(2026, 10, 26), f"0001000010-26-00000{i}").exists()


def test_filings_edgar_lists_and_does_not_hold_are_marked_absent_and_the_walk_goes_on(feed, http_mock):
    """A whole filer's filings removed from the archive must not end the walk, because the walk is newest first and
    the day is never written off: every later run would die at the same day and the history behind it is lost.
    Measured 2026-11-06 on the live site: all six SCHEDULE 13G rows of CIK 2147005 in the index of 2026-08-17 answer
    404, the accession folder answers 404, and that CIK's submissions.json holds no filing at all. The archive still
    hands over a filing of the same day in the same path, which is what tells this from a path answering nothing."""
    accs = [f"0001000020-26-00000{i}" for i in range(4)]
    feed.add_day("2026-10-26", [(ox.idx_row(1000020 + i, f"THETA {i}", "13F-HR", "2026-10-26", a), None)
                                for i, a in enumerate(accs)])
    feed.not_found |= set(accs)
    older = "0001000021-26-000001"
    feed.add_day("2026-10-23", [(ox.idx_row(1000021, "IOTA FUND", "13F-HR", "2026-10-23", older),
                                 ox.f13_filing(older, 1000021, "2026-10-23",
                                               [ox.f13_holding("NVR INC", "62944T105", 1_000, 1)], "06-30-2026"))])
    row = base.run_request({"dataset": sec13f.NAME, "params": {"mode": "backfill", "from": "2026-10-23",
                                                               "to": "2026-10-28"}}, sleep=lambda s: None)
    assert row["result"] == "changed", row["note"]
    # The day behind the cluster is reached and typed: that is the failure this guards.
    assert sec13f.DATASET.filing_path(date(2026, 10, 23), older).exists()
    assert sec13f.DATASET.is_done(date(2026, 10, 26)) and sec13f.DATASET.is_done(date(2026, 10, 23))
    for a in accs:
        marker = sec13f.DATASET.filing_none(date(2026, 10, 26), a)
        assert marker.exists(), a
        assert json.loads(marker.read_text())["status"] == 404, a          # a later repair sweep looks for these
        assert len(http_mock.calls(a)) == 1, a                             # asked once, never again
    assert "1 run(s) of absent filings, and the archive handed over one it holds" in row["note"]
    # The probe went to the data path, not to an index: exactly one filing URL was asked twice.
    asked = collections.Counter(str(c.url) for c in http_mock.calls("/Archives/edgar/data/"))
    assert sorted(asked.values()) == [1] * (len(asked) - 1) + [2], asked


def test_a_path_that_hands_over_nothing_stops_the_run_even_when_it_answers_404(feed):
    """The flip side, and why the count alone cannot decide: if the data path answers 404 for everything, the walk
    must not write the days off as absent. It asks for a filing it is known to hold, has none to show, and stops."""
    day, accs = "2026-10-27", [f"0001000030-26-00000{i}" for i in range(4)]
    feed.add_day(day, [(ox.idx_row(1000030 + i, f"KAPPA {i}", "13F-HR", day, a), None)
                       for i, a in enumerate(accs)])
    feed.not_found |= set(accs)
    row = base.run_request({"dataset": sec13f.NAME, "params": {"mode": "backfill", "from": day,
                                                               "to": "2026-10-28"}}, sleep=lambda s: None)
    assert row["result"] == "failed" and "did not hand over one it holds either" in row["note"]
    assert "no filing it is known to hold" in row["note"]
    for a in accs:
        assert not sec13f.DATASET.filing_none(date(2026, 10, 27), a).exists(), a
    assert not sec13f.DATASET.is_done(date(2026, 10, 27))      # and the next run asks the day again


def test_the_probe_asks_a_filing_an_earlier_run_kept_and_stops_when_that_one_is_gone_too(feed, http_mock):
    """The daily shape: a run before this one fetched filings and left a manifest, so the probe has a URL on disk
    even when nothing has answered yet in this run. If that one is gone too, nothing is written off."""
    _run()                                                      # leaves the manifests of 26 and 27 October
    kept = sorted(json.loads(l)["accession"]
                  for l in (sec13f.DATASET.day_dir(date(2026, 10, 27)) / edgar.MANIFEST).read_text().splitlines()
                  if l.strip())
    day, accs = "2026-10-28", [f"0001000040-26-00000{i}" for i in range(4)]
    feed.add_day(day, [(ox.idx_row(1000040 + i, f"LAMBDA {i}", "13F-HR", day, a), None)
                       for i, a in enumerate(accs)])
    feed.not_found |= {*accs, *kept}                            # the kept filing has been removed as well
    http_mock.requests.clear()
    row = base.run_request({"dataset": sec13f.NAME, "params": {"as_of": "2026-10-29"}}, sleep=lambda s: None)
    assert row["result"] == "failed" and "did not hand over one it holds either" in row["note"]
    assert any(k in row["note"] for k in kept), row["note"]      # it asked the one from disk, and named it
    assert [str(c.url).rsplit("/", 1)[1][:-4] for c in http_mock.calls("/Archives/edgar/data/")
            if str(c.url).rsplit("/", 1)[1][:-4] in kept], "the probe never asked the filing on disk"
    for a in accs:
        assert not sec13f.DATASET.filing_none(date(2026, 10, 28), a).exists(), a


def test_an_index_answer_does_not_write_a_filing_off(feed):
    """The two paths fail apart, so the evidence is kept apart. A day with fewer absent filings than ``MISSING_RUN``
    never reaches the check, and if the next day's *index* answer could write its filing markers, a data path that
    answers nothing for anything would be written off a day or two at a time -- which is the disease the check
    exists for. A filing marker waits for the data path itself, at the end of the walk too."""
    feed.add_day("2026-10-27", [(ox.idx_row(1000050, "MU FUND", "13F-HR", "2026-10-27", "0001000050-26-000001"),
                                 None)])
    feed.add_day("2026-10-26", [(ox.idx_row(1000051, "NU FUND", "13F-HR", "2026-10-26", "0001000051-26-000001"),
                                 None)])
    feed.not_found |= {"0001000050-26-000001", "0001000051-26-000001"}
    feed.filings.clear()                       # nothing in the archive hands an object over, on either day
    ctx = base.RunContext(run_id="r9", dataset=sec13f.NAME)
    complete, _partial, _more = sec13f.DATASET.walk(ctx, date(2026, 10, 26), date(2026, 10, 27),
                                                    date(2026, 10, 28), None)
    # Both indexes answered 200, so the walk itself is fine; neither filing may be written off by that.
    assert not sec13f.DATASET.filing_none(date(2026, 10, 27), "0001000050-26-000001").exists()
    assert not sec13f.DATASET.filing_none(date(2026, 10, 26), "0001000051-26-000001").exists()
    assert complete == [], complete
    assert any("absent filing markers held back" in n for n in ctx.notes), ctx.notes


def test_the_bucket_shaped_absence_is_written_off_too_once_the_archive_answers(feed, http_mock):
    """The other shape, with the check passing: ``AccessDenied`` is what the bucket answers for an object it does
    not hold, and it is also what a path refusing everything answers -- which is why the count never decided it.
    The archive hands over one it holds, so these are absences and the walk goes on."""
    accs = [f"0001000100-26-00000{i}" for i in range(4)]
    feed.add_day("2026-10-26", [(ox.idx_row(1000100, "TAU FUND", "13F-HR", "2026-10-26", a), None) for a in accs])
    row = _run()                                   # 27 October's filings still answer, so the probe passes
    assert row["result"] != "failed", row["note"]
    assert "handed over one it holds" in row["note"], row["note"]
    for a in accs:
        marker = sec13f.DATASET.filing_none(date(2026, 10, 26), a)
        assert marker.exists(), a
        assert marker.exists() and json.loads(marker.read_text())["status"] == 403, a


def test_a_cluster_past_the_ceiling_stops_the_run_however_the_probe_answers(feed, http_mock):
    """Past ``CLUSTER_MAX`` on one day, an answer about one object cannot speak for the rest: the run stops and the
    message says what the cluster looked like. Counted over the whole day, not since the last probe -- otherwise
    the probe's reset pushes the ceiling out of reach and tens of filings are written off on the way there."""
    n = edgar.CLUSTER_MAX + 2
    accs = [f"0001000060-26-{i:06d}" for i in range(n)]
    feed.add_day("2026-10-26", [(ox.idx_row(1000060, "XI FUND", "13F-HR", "2026-10-26", a), None) for a in accs])
    feed.not_found |= set(accs)                       # the archive still hands over 27 October's filings
    row = _run()
    assert row["result"] == "failed", row["note"]
    assert f"more than {edgar.CLUSTER_MAX} on one day" in row["note"], row["note"]
    assert f"{edgar.CLUSTER_MAX}x404" in row["note"] and "1 accession prefix" in row["note"], row["note"]
    # Whatever the probe said on the way, no more than the probes' own flushes may have been written off.
    written = sum(1 for a in accs if sec13f.DATASET.filing_none(date(2026, 10, 26), a).exists())
    assert written < edgar.CLUSTER_MAX, written
    assert not sec13f.DATASET.is_done(date(2026, 10, 26))


def test_the_archive_is_asked_once_per_doubling_not_once_per_absence(feed, http_mock):
    """The probe costs a request, so a day of absences must cost O(log n) of them, not one each."""
    accs = [f"0001000070-26-00000{i}" for i in range(9)]
    feed.add_day("2026-10-26", [(ox.idx_row(1000070, "OMICRON FUND", "13F-HR", "2026-10-26", a), None)
                                for a in accs])
    feed.not_found |= set(accs)
    row = _run()
    assert row["result"] == "changed", row["note"]
    # 9 absences: probes at 3 and at 6 more, so two, not three and not nine.
    assert "2 run(s) of absent filings" in row["note"], row["note"]


def test_the_probe_falls_through_to_an_older_day_when_the_first_candidate_is_gone(feed, http_mock):
    """Why ``_known_filing`` returns several: a candidate may itself be a filing EDGAR has since removed, which
    would wedge the walk through its own guard."""
    older = "0001000081-26-000001"
    feed.add_day("2026-10-26", [(ox.idx_row(1000081, "SIGMA FUND", "13F-HR", "2026-10-26", older),
                                 ox.f13_filing(older, 1000081, "2026-10-26",
                                               [ox.f13_holding("NVR INC", "62944T105", 1_000, 1)], "06-30-2026"))])
    _run()                                                     # leaves a manifest for 26 and for 27 October
    newest = sorted(json.loads(l)["accession"]
                    for l in (sec13f.DATASET.day_dir(date(2026, 10, 27)) / edgar.MANIFEST).read_text().splitlines()
                    if l.strip())
    day, accs = "2026-10-28", [f"0001000080-26-00000{i}" for i in range(4)]
    feed.add_day(day, [(ox.idx_row(1000080, "PI FUND", "13F-HR", day, a), None) for a in accs])
    feed.not_found |= {*accs, *newest}                         # the newest day's candidate is gone too
    row = base.run_request({"dataset": sec13f.NAME, "params": {"as_of": "2026-10-29"}}, sleep=lambda s: None)
    # The newest candidate is gone, an older day's filing still answers, so the absences are written off.
    assert row["result"] != "failed", row["note"]
    assert "handed over one it holds" in row["note"], row["note"]
    for a in accs:
        assert sec13f.DATASET.filing_none(date(2026, 10, 28), a).exists(), a


def test_a_200_that_is_not_a_submission_is_not_a_filing(feed, http_mock):
    """The index path refuses an answer that is not an index; the data path needs the same, or a host answering 200
    with an error body for everything fills the raw store with error pages and passes the archive check with them."""
    acc = "0001000090-26-000001"
    feed.add_day("2026-10-26", [(ox.idx_row(1000090, "RHO FUND", "13F-HR", "2026-10-26", acc), None)])
    feed.filings[acc] = b"<html><title>Request Rate Threshold Exceeded</title></html>"
    row = _run()
    assert row["result"] == "failed" and "not a full submission" in row["note"], row["note"]
    assert not sec13f.DATASET.filing_path(date(2026, 10, 26), acc).exists()


def test_a_day_held_at_the_end_of_a_run_is_written_once_the_archive_answers(feed, http_mock):
    """Nothing answered after the last missing day, so the walk asks again for one index the archive is known to
    hold: a 200 says it is not refusing, and only then is the day written off."""
    feed.missing.add("20261012")
    _run()                                                       # keeps the indexes of 26 and 27 Oct
    http_mock.requests.clear()
    base.run_request({"dataset": sec13f.NAME, "params": {"mode": "backfill", "from": "2026-10-12",
                                                         "to": "2026-10-12"}}, sleep=lambda s: None)
    assert edgar.index_none(date(2026, 10, 12)).exists()
    assert sec13f.DATASET.is_done(date(2026, 10, 12))
    # The check re-asks a day whose index is already kept, not the missing one.
    asked = [str(c.url).rsplit("master.", 1)[1][:8] for c in http_mock.calls("daily-index")]
    assert asked == ["20261012", "20261027"]


def test_a_day_no_answer_can_confirm_is_left_for_the_next_run(feed):
    """The run kept no index of its own and the archive answered nothing: the missing day may be the bucket
    refusing, so it is neither written off nor finished."""
    feed.missing.add("20261012")
    base.run_request({"dataset": sec13f.NAME, "params": {"mode": "backfill", "from": "2026-10-12",
                                                         "to": "2026-10-12"}}, sleep=lambda s: None)
    assert not edgar.index_none(date(2026, 10, 12)).exists()
    assert not sec13f.DATASET.is_done(date(2026, 10, 12))


def test_the_last_filing_of_a_day_is_written_off_only_with_the_archive_answering(feed):
    """A day whose last listed filing is missing: the marker is written, and the day finished, because the day's
    own index came from the archive in this run."""
    gone = "0001000008-26-000009"
    feed.add_day("2026-10-26", [
        (ox.idx_row(1000008, "THETA FUND", "13F-HR", "2026-10-26", "0001000008-26-000001"),
         ox.f13_filing("0001000008-26-000001", 1000008, "2026-10-26", [MSFT], period="09-30-2026")),
        (ox.idx_row(1000009, "IOTA FUND", "13F-HR", "2026-10-26", gone), None)])
    _run()
    assert sec13f.DATASET.filing_none(date(2026, 10, 26), gone).exists()
    assert sec13f.DATASET.is_done(date(2026, 10, 26))


def test_a_day_whose_filing_markers_are_held_is_not_finished(feed):
    """A ``.done`` would skip the day for good, so a day is finished only once its markers are written: here the
    data path hands nothing over, so the filing is neither written off nor forgotten. It is that path, not the
    index path, that has to answer for a filing (2026-11-06: the index answered 200 throughout while the data path
    had lost a filer's filings)."""
    gone = "0001000009-26-000009"
    _run()                                                       # keeps the filings and indexes of 26 and 27 Oct
    feed.add_day("2026-10-23", [(ox.idx_row(1000009, "IOTA FUND", "13F-HR", "2026-10-23", gone), None)])
    feed.filings.clear()                                         # nothing in the archive hands an object over
    base.run_request({"dataset": sec13f.NAME, "params": {"mode": "backfill", "from": "2026-10-23",
                                                         "to": "2026-10-23"}}, sleep=lambda s: None)
    assert not sec13f.DATASET.filing_none(date(2026, 10, 23), gone).exists()
    assert not sec13f.DATASET.is_done(date(2026, 10, 23))


def test_the_time_budget_stops_a_run_and_the_next_one_goes_on(feed, http_mock, monkeypatch):
    many = [(ox.idx_row(2000000 + i, f"FILER {i}", "13F-HR", "2026-10-27", f"0002000000-26-{i:06d}"),
             ox.f13_filing(f"0002000000-26-{i:06d}", 2000000 + i, "2026-10-27", [MSFT])) for i in range(20)]
    feed.add_day("2026-10-27", many)
    monkeypatch.setattr("RevenueChaser.rcdata.build.ownership.sec._shared_pace", lambda: False)   # the run's clock
    monkeypatch.setattr(sec13f.DATASET, "budget_s", 4.0)
    now = [0.0]

    def sleep(s):
        now[0] += s

    first = _run(clock=lambda: now[0], sleep=sleep)
    fetched = len(http_mock.calls("/Archives/edgar/data/"))
    assert first["result"] == "changed" and 5 <= fetched < 20
    assert first["next_check_due"] == "2026-10-28" and "time budget" in first["note"]
    monkeypatch.setattr(due.calendar, "today_ny", lambda: date(2026, 10, 28))
    assert due.is_due(sec13f.DATASET)[0] is True
    for _ in range(5):
        _run(clock=lambda: now[0], sleep=sleep)
    assert len(http_mock.calls("/Archives/edgar/data/")) == 20       # each one once, across the runs
    assert sec13f.DATASET.is_done(date(2026, 10, 27))
    assert _parquet("filings", "SELECT count(*) FROM {src}") == [(20,)]


def test_a_backfill_walks_its_range_and_moves_the_first_day(feed):
    feed.add_day("2026-08-14", [(ox.idx_row(1000001, "ALPHA CAPITAL", "13F-HR", "2026-08-14", "0001000001-26-000000"),
                                 ox.f13_filing("0001000001-26-000000", 1000001, "2026-08-14", [MSFT],
                                               period="06-30-2026"))])
    base.run_request({"dataset": sec13f.NAME, "params": {"mode": "backfill", "from": "2026-08-14",
                                                         "to": "2026-08-14"}}, sleep=lambda s: None)
    assert sec13f.DATASET.start_day(date(2026, 10, 28)) == date(2026, 8, 14)
    assert _parquet("filings", "SELECT period FROM {src}") == [(date(2026, 6, 30),)]


def test_a_filing_the_index_lists_twice_is_fetched_once(feed, http_mock):
    acc = "0001000001-26-000001"
    feed.add_day("2026-10-27", [
        (ox.idx_row(1000001, "ALPHA CAPITAL", "13F-HR", "2026-10-27", acc),
         ox.f13_filing(acc, 1000001, "2026-10-27", [MSFT])),
        ((1000009, "ALPHA PARENT", "13F-HR", "20261027", f"edgar/data/1000001/{acc}.txt"), None)])
    _run()
    assert len(http_mock.calls(acc)) == 1


def test_the_index_parse_refuses_an_answer_that_is_not_the_index():
    with pytest.raises(base.SourceError, match="not the EDGAR daily index"):
        edgar.parse_index(b"<html>Request Rate Threshold Exceeded</html>")
    rows = edgar.parse_index(ox.daily_index([ox.idx_row(5, "X", "13F-HR/A", "2026-10-27", "0000000005-26-000001")]))
    assert rows == [{"cik": "5", "company": "X", "form": "13F-HR/A", "filed": date(2026, 10, 27),
                     "path": "edgar/data/5/0000000005-26-000001.txt", "accession": "0000000005-26-000001"}]


def test_a_days_filings_are_read_one_at_a_time_not_held_at_once(feed, monkeypatch):
    """A deadline day holds up to 5,400 full submissions: the typing reads one, types it and lets it go."""
    _run()
    reads = []
    real = base.read_raw
    monkeypatch.setattr(base, "read_raw", lambda path: reads.append(path.name) or real(path))
    filings = sec13f.DATASET.filings_of(date(2026, 10, 27))
    first = next(iter(filings))
    assert first[0] == "0001000001-26-000001" and len(reads) == 1
    assert [acc for acc, _e, _t in filings] == ["0001000002-26-000007", "0001000004-26-000003",
                                                "0001000005-26-000004"]
