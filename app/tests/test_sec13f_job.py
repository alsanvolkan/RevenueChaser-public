"""The 13F Fetch after G9: names -> shares out -> render -> history (#206, G9).

The numbers are the builder's (``ownership.duckdb``), so the Fetch asks the SEC for nothing. What is left here:

* the Unusual Whales meta step is best-effort — a missing key or an outage keeps the stored names/sectors and says
  so on the board;
* a meta that already covers the served totals: the UW screener is not re-run (F-1);
* the client the runner opens itself is always closed;
* the CUSIPs the newest stored quarter could not name are a board note (DF89), now counted by the builder.
"""
import gzip
import json

import pytest

from RevenueChaser.app import jobs, runners
from RevenueChaser.module_sec_13f import engine as sec_13f
from RevenueChaser.rcdata.tests.ownership_fixture import serve_13f

GOOD = {"name": "Apple Inc", "sector": "Technology", "type": "Stock"}
Q = "2026-03-31"
AAPL = {"AAPL": {Q: {"shares": 300.0, "value": 3000.0, "holders": 2, "top10_pct": 100.0, "hhi": 5000.0,
                     "new_holders": None, "exited_holders": None, "buy_flow": None, "sell_flow": None,
                     "breadth": None, "breadth_chg": None}}}


@pytest.fixture(autouse=True)
def _intraday(monkeypatch):
    """Pin the run clock inside a session: the UW shares step (#300) asks nothing, so these tests count only the
    meta step's client whatever the real time is (``test_sec13f_shares_300.py`` covers the shares step)."""
    from datetime import datetime, timezone
    from RevenueChaser.core import ownership
    monkeypatch.setattr(ownership, "_clock",
                        lambda: datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc).timestamp())


def _serve(totals=None, *, cusips=None, **kw):
    """The builder's served tables, as a finished build leaves them."""
    totals = AAPL if totals is None else totals
    serve_13f(totals, cusips=cusips if cusips is not None else {"AAPL": ["111111111"]}, **kw)
    runners.SEC_13F_LATEST.unlink(missing_ok=True)


@pytest.fixture
def store(rawstore_tmp_db):
    """The dashboard's own store, empty, beside a served ownership file holding one name."""
    _serve()
    return rawstore_tmp_db


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

    def test_a_name_the_meta_does_not_cover_runs_the_meta_step(self, store, fake_uw):
        """With the ingest gone, a new quarter shows up as a ticker the stored meta has no row for."""
        sec_13f.save_sec13f_meta({"AAPL": GOOD})
        _serve({**AAPL, "NVDA": AAPL["AAPL"]})
        runners.build_sec13f(None)
        assert fake_uw.built == 1


class TestUnmatchedCusipNote:
    """DF89: the CUSIPs the newest stored quarter holds no ticker for are a board note. The builder counts them
    (``sec13f_periods``), where the retired derive counted them itself."""

    def test_fetch_puts_the_count_on_the_board_and_in_the_job_message(self, store, fake_uw):
        sec_13f.save_sec13f_meta({"AAPL": GOOD})
        _serve(periods={Q: {"unmapped_cusips": 7, "coverage": 0.82}})
        with pytest.raises(jobs.JobMessage, match="7 CUSIP"):
            runners.build_sec13f(None)
        assert "7 CUSIP" in _board()

    def test_the_note_says_the_share_of_value_left_off_the_board(self, store, fake_uw):
        sec_13f.save_sec13f_meta({"AAPL": GOOD})
        _serve(periods={Q: {"unmapped_cusips": 7, "coverage": 0.82}})
        with pytest.raises(jobs.JobMessage, match="18.0%"):
            runners.build_sec13f(None)

    def test_no_unmatched_means_no_note(self, store, fake_uw):
        sec_13f.save_sec13f_meta({"AAPL": GOOD})
        _serve(periods={Q: {"unmapped_cusips": 0, "coverage": 1.0}})
        runners.build_sec13f(None)  # does not raise
        assert "CUSIP" not in _board()
