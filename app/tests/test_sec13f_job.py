"""The store-only 13F rebuild of the old totals (#206).

* ``sec13f_rebuild`` re-derives the old ``raw.duckdb`` totals with zero network and says how many CUSIPs it left out.

The old Fetch these tests also covered (download -> ingest -> derive, the UW names and shares) is gone since G9: the
board's Fetch queues ``ownership/serve`` and renders from the ownership store
(``module_sec_13f/tests/test_sec13f_store_g9.py``). The rebuild and map jobs go with the old 13F module in G10.
"""
import csv
import gzip
import io
import json
import zipfile

import pytest

from RevenueChaser.app import jobs, runners
from RevenueChaser.core import identity_cusip
from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.module_sec_13f import engine as sec_13f

GOOD = {"name": "Apple Inc", "sector": "Technology", "type": "Stock"}


def _zip(path, rows):
    """``rows`` = [(accession, cik, cusip, issuer, shares)] -> a 2026-03-31 data-set zip."""
    with zipfile.ZipFile(path, "w") as z:
        for member, fields, data in (
            ("SUBMISSION.tsv",
             ["ACCESSION_NUMBER", "CIK", "FILING_DATE", "SUBMISSIONTYPE", "PERIODOFREPORT"],
             [{"ACCESSION_NUMBER": a, "CIK": c, "FILING_DATE": "01-MAY-2026",
               "SUBMISSIONTYPE": "13F-HR", "PERIODOFREPORT": "31-MAR-2026"} for a, c, *_ in rows]),
            ("INFOTABLE.tsv",
             ["ACCESSION_NUMBER", "NAMEOFISSUER", "CUSIP", "VALUE", "SSHPRNAMT",
              "SSHPRNAMTTYPE", "PUTCALL"],
             [{"ACCESSION_NUMBER": a, "NAMEOFISSUER": n, "CUSIP": cu, "VALUE": "1000",
               "SSHPRNAMT": str(sh), "SSHPRNAMTTYPE": "SH", "PUTCALL": ""}
              for a, c, cu, n, sh in rows])):
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=fields, delimiter="\t")
            w.writeheader()
            w.writerows(data)
            z.writestr(member, buf.getvalue())
    return path


@pytest.fixture(autouse=True)
def _intraday(monkeypatch):
    """Pin the run clock inside a session: the UW shares step (#300) asks nothing, so these tests count only the
    meta step's client whatever the real time is (``test_sec13f_shares_300.py`` covers the shares step)."""
    from datetime import datetime, timezone
    from RevenueChaser.core import ownership
    monkeypatch.setattr(ownership, "_clock",
                        lambda: datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc).timestamp())


def _store(tmp_path, monkeypatch, *, mapped):
    zdir = tmp_path / "sec_13f"
    zdir.mkdir()
    monkeypatch.setattr(sec_13f, "SEC13F_DIR", zdir)
    _zip(zdir / "2026q1_form13f.zip", [("a1", "1000", "111111111", "APPLE INC", 100),
                                       ("a2", "2000", "111111111", "APPLE INC", 200)])
    sec_13f.ingest_zips(sorted(zdir.glob("*_form13f.zip")))
    sec_13f.save_cusip_ticker({"111111111": "AAPL"})
    if mapped:   # OpenFIGI has answered about the one CUSIP: a Fetch asks it nothing (#207)
        identity_cusip.record({"111111111": {"status": "present", "ticker": "AAPL", "fetched_at": 0.0}},
                              run_id="seed", run_at=0.0)
    monkeypatch.setattr(sec_13f, "download_missing_zips", lambda *a, **k: [])
    runners.SEC_13F_LATEST.unlink(missing_ok=True)
    return zdir


@pytest.fixture
def store(rawstore_tmp_db, tmp_path, monkeypatch):
    """Isolated store + zip dir, ingested once, with the CUSIP map stored and seeded (the name match is retired,
    #207)."""
    return _store(tmp_path, monkeypatch, mapped=True)


@pytest.fixture
def unmapped_store(rawstore_tmp_db, tmp_path, monkeypatch):
    """``store`` before the first OpenFIGI map: the legacy ``cusip_ticker`` only (#207)."""
    return _store(tmp_path, monkeypatch, mapped=False)


class _FakeUW:
    """A UW client double: counts constructions and what the screener was asked."""
    built = 0
    closed = 0
    ok = (True, None)
    health_ = None

    def __init__(self, *a, **k):
        type(self).built += 1

    def verify(self):
        return type(self).ok

    def screener_batch(self, tickers):
        return {}

    def health(self):
        return type(self).health_

    def close(self):
        type(self).closed += 1


@pytest.fixture
def fake_uw(monkeypatch):
    _FakeUW.built, _FakeUW.closed, _FakeUW.ok, _FakeUW.health_ = 0, 0, (True, None), None
    monkeypatch.setattr(runners, "UWClient", _FakeUW)
    return _FakeUW


def _board() -> str:
    return runners.SEC_13F_LATEST.read_text(encoding="utf-8")


def _payload() -> dict:
    """The board's rows from the payload route (#429): the page is chrome and scripts now."""
    from RevenueChaser.core.reporting_common import payload_path
    return json.loads(gzip.decompress(payload_path(runners.SEC_13F_LATEST).read_bytes()).decode("utf-8"))


def _tickers() -> set:
    return {row[0] for row in _payload()["rows"]}


class TestIngestFailureKeepsTheTotals:
    """B1: a failed ingest must never replace good totals with partial ones."""

    def test_rebuild_surfaces_a_refusal(self, store, monkeypatch):
        def refuse(*a, **k):
            raise sec_13f.DeriveRefused("orphan holdings")

        monkeypatch.setattr(sec_13f, "recompute_totals", refuse)
        with pytest.raises(sec_13f.DeriveRefused):
            runners.run_sec13f_rebuild(None)


class TestRebuild:
    def test_rederives_the_totals_with_zero_network(self, store, fake_uw, monkeypatch):
        sec_13f.save_totals({"STALE": {"2026-03-31": {"shares": 1.0}}})
        for name in ("get_text", "get_bytes"):
            monkeypatch.setattr(sec_edgar, name, lambda *a, **k: pytest.fail("rebuild hit the SEC"))
        with pytest.raises(jobs.JobMessage, match="Rebuilt 13F totals"):
            runners.run_sec13f_rebuild(None)
        assert set(sec_13f.load_totals()) == {"AAPL"}
        assert fake_uw.built == 0

    def test_nothing_ingested_keeps_the_stored_totals(self, rawstore_tmp_db):
        sec_13f.save_totals({"NVDA": {"2026-03-31": {"shares": 5.0, "holders": 1,
                                                       "value": 1.0}}})
        with pytest.raises(jobs.JobMessage, match="nothing to rebuild"):
            runners.run_sec13f_rebuild(None)
        assert "NVDA" in sec_13f.load_totals()


class TestUnmatchedCusipNote:
    """DF89: a CUSIP with no ticker is left out of the totals; the board and the job say so."""

    def _add_unmatched(self, zdir):
        _zip(zdir / "2026q1b_form13f.zip", [("b1", "3000", "999999999", "NO TICKER CORP", 50)])
        sec_13f.ingest_zips(sorted(zdir.glob("*_form13f.zip")))

    def test_rebuild_message_carries_the_count(self, store, fake_uw):
        self._add_unmatched(store)
        with pytest.raises(jobs.JobMessage, match="1 CUSIP"):
            runners.run_sec13f_rebuild(None)
        assert "NO TICKER CORP" in _board()
