"""The 13F Fetch as three stages (download -> ingest -> derive) and the store-only rebuild (#206).

* an SEC index failure is reported but no longer blocks the derive;
* the Unusual Whales meta step is best-effort: a missing key or an outage keeps the
  stored names/sectors and says so on the board;
* nothing new ingested + the meta already covering the totals: the UW screener is not re-run (F-1);
* ``sec13f_rebuild`` re-derives the totals with zero network.
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


class TestSecIndexDown:
    def test_the_derive_still_runs_and_the_error_is_reported(self, store, fake_uw, monkeypatch):
        sec_13f.save_sec13f_meta({"AAPL": GOOD})  # meta complete: no UW step
        ingested = []
        monkeypatch.setattr(sec_13f, "ingest_zips", lambda z: ingested.append(z) or 0)

        def down(*a, **k):
            raise RuntimeError("SEC index down")

        monkeypatch.setattr(sec_13f, "download_missing_zips", down)
        with pytest.raises(RuntimeError, match="SEC index down"):
            runners.build_sec13f(None)

        assert ingested == [], "an unreachable index must not trigger an ingest"
        assert sec_13f.load_totals()["AAPL"]["2026-03-31"]["shares"] == 300.0
        assert "AAPL" in _tickers()  # the board was re-rendered before the error was raised


class TestIngestFailureKeepsTheTotals:
    """B1: a failed ingest must never replace good totals with partial ones."""

    def test_a_crashed_reingest_keeps_the_totals_and_raises(self, store, fake_uw, monkeypatch):
        from RevenueChaser.core import rawstore
        sec_13f.save_sec13f_meta({"AAPL": GOOD})
        sec_13f.recompute_totals()  # good totals exist before the crash
        rawstore.execute("DELETE FROM sec13f_ingested_zips")  # a forced re-ingest
        monkeypatch.setattr(sec_13f, "_HOLDINGS_CHUNK", 1)
        real, calls = rawstore.write_rows, []

        def die_second(name, rows):
            if name == "sec13f_holdings":
                calls.append(1)
                if len(calls) == 2:
                    raise RuntimeError("chunk stream died")
            return real(name, rows)

        monkeypatch.setattr(rawstore, "write_rows", die_second)
        with pytest.raises(RuntimeError, match="chunk stream died"):
            runners.build_sec13f(None)
        assert sec_13f.load_totals()["AAPL"]["2026-03-31"]["shares"] == 300.0
        assert "AAPL" in _tickers()  # still rendered

    def test_an_ingest_error_skips_the_derive(self, store, fake_uw, monkeypatch):
        sec_13f.save_sec13f_meta({"AAPL": GOOD})
        monkeypatch.setattr(sec_13f, "ingest_zips",
                            lambda z: (_ for _ in ()).throw(RuntimeError("bad zip")))
        monkeypatch.setattr(sec_13f, "recompute_totals",
                            lambda *a, **k: pytest.fail("derive ran after a failed ingest"))
        with pytest.raises(RuntimeError, match="bad zip"):
            runners.build_sec13f(None)

    def test_a_refused_derive_is_raised_after_the_render(self, store, fake_uw, monkeypatch):
        sec_13f.save_sec13f_meta({"AAPL": GOOD})
        sec_13f.recompute_totals()

        def refuse(*a, **k):
            raise sec_13f.DeriveRefused("orphan holdings")

        monkeypatch.setattr(sec_13f, "recompute_totals", refuse)
        with pytest.raises(sec_13f.DeriveRefused):
            runners.build_sec13f(None)
        assert "AAPL" in _tickers()

    def test_rebuild_surfaces_a_refusal(self, store, monkeypatch):
        def refuse(*a, **k):
            raise sec_13f.DeriveRefused("orphan holdings")

        monkeypatch.setattr(sec_13f, "recompute_totals", refuse)
        with pytest.raises(sec_13f.DeriveRefused):
            runners.run_sec13f_rebuild(None)


class TestUwIsBestEffort:
    def test_no_key_keeps_the_stored_meta_and_says_so(self, store, fake_uw):
        sec_13f.save_sec13f_meta({"OLD": GOOD})  # AAPL missing -> the meta step is attempted
        fake_uw.ok = (False, "UNUSUAL_WHALES_API_KEY is not set")
        with pytest.raises(jobs.JobMessage, match="not refreshed"):
            runners.build_sec13f(None)
        assert sec_13f.load_sec13f_meta()["OLD"] == GOOD
        assert sec_13f.load_totals()["AAPL"]  # the SEC data went through regardless
        assert "Names and sectors were not refreshed" in _board()
        assert "UNUSUAL_WHALES_API_KEY" in _board()

    def test_an_outage_keeps_stored_names_and_shows_the_health_banner(self, store, fake_uw):
        sec_13f.save_sec13f_meta({"OLD": GOOD})  # AAPL has no meta yet: the step runs
        fake_uw.health_ = {"calls": 1, "failed": 1, "auth_error": None,
                           "failures": [{"endpoint": "/api/screener/stocks", "status": 500}]}
        with pytest.raises(jobs.JobMessage):
            runners.build_sec13f(None)
        assert fake_uw.built == 1
        assert sec_13f.load_sec13f_meta()["OLD"] == GOOD
        assert "Data incomplete" in _board()

    def test_a_raising_meta_step_does_not_fail_the_job(self, store, fake_uw, monkeypatch):
        def boom(uw):
            raise RuntimeError("screener exploded")

        monkeypatch.setattr(runners, "build_sec13f_meta", boom)
        with pytest.raises(jobs.JobMessage, match="not refreshed"):
            runners.build_sec13f(None)
        assert sec_13f.load_totals()["AAPL"]


class TestOwnUwClientIsClosed:
    """N1: the client the runner opens itself is always closed."""

    def test_closed_after_a_successful_meta_step(self, store, fake_uw):
        runners.build_sec13f(None)
        assert (fake_uw.built, fake_uw.closed) == (1, 1)

    def test_closed_when_the_meta_step_raises(self, store, fake_uw, monkeypatch):
        monkeypatch.setattr(runners, "build_sec13f_meta",
                            lambda uw: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(jobs.JobMessage):
            runners.build_sec13f(None)
        assert (fake_uw.built, fake_uw.closed) == (1, 1)

    def test_closed_when_verify_itself_raises(self, store, fake_uw, monkeypatch):
        monkeypatch.setattr(fake_uw, "verify", lambda self: (_ for _ in ()).throw(OSError("net")))
        with pytest.raises(OSError):
            runners.build_sec13f(None)
        assert (fake_uw.built, fake_uw.closed) == (1, 1)

    def test_a_client_passed_in_is_not_closed(self, store, fake_uw):
        runners.build_sec13f(fake_uw())
        assert fake_uw.closed == 0


class TestSkipWhenNothingChanged:
    """F-1: no new zip and a meta that already covers the totals -> no screener re-run."""

    def test_no_new_zip_means_no_uw_client(self, store, fake_uw):
        runners.build_sec13f(None)  # first pass derives the totals and fetches the meta
        sec_13f.save_sec13f_meta({"AAPL": GOOD})
        fake_uw.built = 0
        runners.build_sec13f(None)
        assert fake_uw.built == 0
        assert sec_13f.load_totals()["AAPL"]  # totals were still recomputed

    def test_placeholder_meta_does_not_count_as_covered(self, store, fake_uw):
        """B2: a failed UW pass leaves ''/Unclassified rows; they must not suppress the retry."""
        sec_13f.save_sec13f_meta({"AAPL": {"name": "", "sector": "Unclassified", "type": "Other"}})
        runners.build_sec13f(None)
        assert fake_uw.built == 1

    def test_a_new_zip_runs_the_meta_step(self, store, fake_uw, monkeypatch):
        sec_13f.save_sec13f_meta({"AAPL": GOOD})
        _zip(store / "2026q2_form13f.zip", [("b1", "3000", "111111111", "APPLE INC", 50)])
        runners.build_sec13f(None)
        assert fake_uw.built == 1


class TestRebuild:
    def test_rederives_the_totals_with_zero_network(self, store, fake_uw, monkeypatch):
        sec_13f.save_totals({"STALE": {"2026-03-31": {"shares": 1.0}}})
        for name in ("get_text", "get_bytes"):
            monkeypatch.setattr(sec_edgar, name, lambda *a, **k: pytest.fail("rebuild hit the SEC"))
        with pytest.raises(jobs.JobMessage, match="Rebuilt 13F totals"):
            runners.run_sec13f_rebuild(None)
        assert set(sec_13f.load_totals()) == {"AAPL"}
        assert fake_uw.built == 0
        assert "AAPL" in _tickers()

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

    def test_fetch_puts_the_count_on_the_board_and_in_the_job_message(self, store, fake_uw):
        sec_13f.save_sec13f_meta({"AAPL": GOOD})
        self._add_unmatched(store)
        with pytest.raises(jobs.JobMessage, match="1 CUSIP"):
            runners.build_sec13f(None)
        assert "NO TICKER CORP" in _board()

    def test_rebuild_message_carries_the_count(self, store, fake_uw):
        self._add_unmatched(store)
        with pytest.raises(jobs.JobMessage, match="1 CUSIP"):
            runners.run_sec13f_rebuild(None)
        assert "NO TICKER CORP" in _board()

    def test_no_unmatched_means_no_note(self, store, fake_uw):
        sec_13f.save_sec13f_meta({"AAPL": GOOD})
        runners.build_sec13f(None)  # does not raise
        assert "no ticker" not in _board()
