"""#26 PL4-b: the five long writers stop at a safe boundary, and only a measured job has a deadline.

A deadline of 0 s is already past at the first checkpoint, so the job ends ``error`` naming that phase and
writes nothing of the unit it was about to start. A cancel observed at that checkpoint ends ``cancelled``.
A run that finishes inside its deadline writes what it always wrote.
"""
from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from RevenueChaser.app import jobs, runners
from RevenueChaser.app import dashboard as _dashboard  # noqa: F401 - registrations
from RevenueChaser.core import job_control, rawstore
from RevenueChaser.core.providers.result import ProviderResult

_NO_DEADLINE = "no deadline: fewer than 5 recorded runs (23 Oct); set from job_runs when measured"
_BARS_NOW = datetime(2026, 10, 8, 20, 35, tzinfo=timezone.utc)


@pytest.fixture
def sync_jobs(monkeypatch):
    """Jobs run only when a test calls ``jobs.execute``."""
    monkeypatch.setattr(jobs, "_ensure_worker", lambda: None)
    monkeypatch.setattr(jobs, "_ensure_long_worker", lambda: None)
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_SEQ", {})
    monkeypatch.setattr(jobs, "_TOKENS", {})


def _run(module, deadline_s=None, params=None):
    jid = jobs.submit(module, params or {}, deadline_s=deadline_s)
    jobs.execute(jid)
    return jobs.get(jid)


def _cancel_on_bind(monkeypatch):
    real = job_control.bind

    def bind(token):
        if token is not None:
            token.cancel()
        return real(token)

    monkeypatch.setattr(job_control, "bind", bind)


# ── registrations ─────────────────────────────────────────────────────────────

def test_the_five_long_writers_are_cancellable_and_only_daily_bars_has_a_deadline():
    assert jobs._CANCELLABLE["module_sec_13f"] is True
    assert jobs._CANCELLABLE["edgar_sync"] is True
    assert jobs._CANCELLABLE["identity"] is True
    assert jobs._CANCELLABLE["daily_bars"] is True
    assert jobs._CANCELLABLE["module_discord"] is True
    assert jobs._DEADLINE["daily_bars"] == 9600
    for module in ("module_sec_13f", "edgar_sync", "identity", "module_discord"):
        assert jobs._DEADLINE[module] is None, module
    text = Path(_dashboard.__file__).read_text(encoding="utf-8")
    assert text.count(_NO_DEADLINE) == 4


# ── 13F: between zips, before any holdings write ──────────────────────────────

def _form13f_zip(path: Path) -> None:
    sub = io.StringIO()
    w = csv.DictWriter(sub, fieldnames=[
        "ACCESSION_NUMBER", "CIK", "FILING_DATE", "SUBMISSIONTYPE", "PERIODOFREPORT"], delimiter="\t")
    w.writeheader()
    w.writerow({"ACCESSION_NUMBER": "0001-26-000001", "CIK": "1000", "FILING_DATE": "01-MAY-2026",
                "SUBMISSIONTYPE": "13F-HR", "PERIODOFREPORT": "31-MAR-2026"})
    info = io.StringIO()
    iw = csv.DictWriter(info, fieldnames=[
        "ACCESSION_NUMBER", "INFOTABLE_SK", "NAMEOFISSUER", "CUSIP", "VALUE", "SSHPRNAMT",
        "SSHPRNAMTTYPE", "PUTCALL", "TITLEOFCLASS", "INVESTMENTDISCRETION", "OTHERMANAGER",
        "VOTING_AUTH_SOLE", "VOTING_AUTH_SHARED", "VOTING_AUTH_NONE"], delimiter="\t")
    iw.writeheader()
    iw.writerow({"ACCESSION_NUMBER": "0001-26-000001", "INFOTABLE_SK": "1", "NAMEOFISSUER": "ACME",
                 "CUSIP": "111111111", "VALUE": "1000", "SSHPRNAMT": "10", "SSHPRNAMTTYPE": "SH",
                 "PUTCALL": "", "TITLEOFCLASS": "COM", "INVESTMENTDISCRETION": "SOLE", "OTHERMANAGER": "",
                 "VOTING_AUTH_SOLE": "10", "VOTING_AUTH_SHARED": "0", "VOTING_AUTH_NONE": "0"})
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("SUBMISSION.tsv", sub.getvalue())
        z.writestr("INFOTABLE.tsv", info.getvalue())


def _quiet_13f(monkeypatch, tmp_path):
    from RevenueChaser.module_sec_13f import engine as sec_13f
    zdir = tmp_path / "zips"
    _form13f_zip(zdir / "2026q1_form13f.zip")
    monkeypatch.setattr(sec_13f, "SEC13F_DIR", zdir)
    monkeypatch.setattr(sec_13f, "available_zips", lambda: {})
    monkeypatch.setattr(runners, "_13f_map_step", lambda *a, **k: (None, None, {}))
    monkeypatch.setattr(runners, "_refresh_13f_meta", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(runners, "_store_13f_shares", lambda *a, **k: None)
    monkeypatch.setattr(runners, "_render_13f", lambda *a, **k: zdir / "board.html")
    monkeypatch.setattr(runners, "_record_13f", lambda *a, **k: None)


def _13f_rows():
    return (rawstore.count("sec13f_holdings") + rawstore.count("sec13f_submissions")
            + rawstore.count("sec13f_ingested_zips"))


def test_a_13f_deadline_at_the_first_zip_writes_nothing(sync_jobs, rawstore_tmp_db, monkeypatch, tmp_path):
    _quiet_13f(monkeypatch, tmp_path)
    job = _run("module_sec_13f", deadline_s=0)
    assert job["status"] == "error", job
    assert "deadline" in job["error"] and "13F zip 2026q1_form13f.zip" in job["error"]
    assert _13f_rows() == 0


def test_a_13f_cancel_at_the_first_zip_writes_nothing(sync_jobs, rawstore_tmp_db, monkeypatch, tmp_path):
    _quiet_13f(monkeypatch, tmp_path)
    _cancel_on_bind(monkeypatch)
    job = _run("module_sec_13f", deadline_s=3600)
    assert job["status"] == "cancelled", job
    assert "13F zip 2026q1_form13f.zip" in job["message"]
    assert _13f_rows() == 0


def test_a_13f_run_inside_its_deadline_ingests_the_zip(sync_jobs, rawstore_tmp_db, monkeypatch, tmp_path):
    _quiet_13f(monkeypatch, tmp_path)
    job = _run("module_sec_13f", deadline_s=3600)
    assert "deadline" not in (job["error"] or "")
    assert rawstore.count("sec13f_ingested_zips") == 1
    assert rawstore.count("sec13f_holdings") == 1


# ── EDGAR: between filers, before any submissions request ─────────────────────

def _seed_edgar(monkeypatch, tickers=("NVDA",)):
    from RevenueChaser.module_value_chain import sync
    rawstore.replace_all("equity_universe",
                         [{"ticker": t, "market_cap": 1e9, "name": t} for t in tickers])
    rawstore.write_rows("sec_exchange_file", [
        {"ticker": t, "cik": str(1045810 + i), "name": t, "status": "present", "run_at": 1.0}
        for i, t in enumerate(tickers)])
    seen = []

    def _get_json(url, client=None):
        seen.append(url)
        return {"filings": {"recent": {
            "form": ["10-K"], "filingDate": ["2026-02-01"],
            "accessionNumber": ["acc-1"], "primaryDocument": ["doc.htm"]}}}

    monkeypatch.setattr(sync.sec_edgar, "get_json", _get_json)
    monkeypatch.setattr(sync.edgar_text, "fetch_document",
                        lambda ticker, cik, filing, client=None, run_at=None: (
                            "<html>Item 1. Business</html>",
                            {"ticker": ticker.upper(), "cik": str(cik), "form": filing["form"],
                             "filed": filing["filed"], "accession": filing["accession"],
                             "primary_doc": "u", "item_text": "x", "quality": "ok"}))
    monkeypatch.setattr(runners, "run_value_chain", lambda *a, **k: Path("board.html"))
    return seen


def test_an_edgar_deadline_at_the_first_filer_writes_nothing(sync_jobs, rawstore_tmp_db, monkeypatch):
    seen = _seed_edgar(monkeypatch)
    job = _run("edgar_sync", deadline_s=0)
    assert job["status"] == "error", job
    assert "deadline" in job["error"] and "EDGAR filer 1045810" in job["error"]
    assert seen == []
    assert rawstore.count("chain_filings") == 0
    assert rawstore.count("edgar_documents") == 0


def test_an_edgar_cancel_at_the_first_filer_writes_nothing(sync_jobs, rawstore_tmp_db, monkeypatch):
    seen = _seed_edgar(monkeypatch)
    _cancel_on_bind(monkeypatch)
    job = _run("edgar_sync", deadline_s=3600)
    assert job["status"] == "cancelled", job
    assert "EDGAR filer 1045810" in job["message"]
    assert seen == []
    assert rawstore.count("chain_filings") == 0


def test_an_edgar_run_inside_its_deadline_stores_the_filing(sync_jobs, rawstore_tmp_db, monkeypatch):
    seen = _seed_edgar(monkeypatch)
    job = _run("edgar_sync", deadline_s=3600)
    assert job["status"] == "done", job
    assert "deadline" not in (job["error"] or "")
    assert seen and rawstore.get("chain_filings", "NVDA")["accession"] == "acc-1"


def test_an_edgar_cancel_inside_the_pool_does_not_start_the_next_filer(sync_jobs, rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_value_chain import sync
    seen = _seed_edgar(monkeypatch, ("AAA", "BBB"))
    real = sync.sync_filings
    held = {}
    real_bind = job_control.bind

    def _bind(token):
        if token is not None:
            held["token"] = token
        return real_bind(token)

    monkeypatch.setattr(job_control, "bind", _bind)

    def _one_worker(*args, **kwargs):
        kwargs["workers"] = 1
        return real(*args, **kwargs)

    monkeypatch.setattr(sync, "sync_filings", _one_worker)

    def _get_json(url, client=None):
        seen.append(url)
        held["token"].cancel()  # cancel() is safe from the pool thread; the token is not thread-local
        return {"filings": {"recent": {
            "form": ["10-K"], "filingDate": ["2026-02-01"],
            "accessionNumber": ["acc-1"], "primaryDocument": ["doc.htm"]}}}

    monkeypatch.setattr(sync.sec_edgar, "get_json", _get_json)
    job = _run("edgar_sync", deadline_s=3600, params={"limit": 2})
    assert job["status"] == "cancelled", job
    assert len(seen) == 1
    # The filer that was fetched before the stop stays committed: its submissions answer sat in the capture buffer
    # when the stop raised, and the pool's ``finally`` flushes it instead of throwing it away.
    stored = {r["cik"] for r in rawstore.query("SELECT cik FROM sec_submissions", db="edgar")}
    assert "1045810" in stored and "1045811" not in stored


# ── identity: between sources, before the first source write ─────────────────

def _identity_fakes(monkeypatch):
    from RevenueChaser.core.identity.tests.test_identity_refresh import Fakes
    from RevenueChaser.core.identity import refresh as identity_refresh
    rawstore.write_rows("equity_universe", [{"ticker": "AAPL", "name": "Apple Inc.", "market_cap": 3.0e12}])
    fk = Fakes()
    monkeypatch.setattr(identity_refresh, "_default_clients", fk.clients)
    return fk


def test_an_identity_deadline_at_the_first_source_writes_nothing(sync_jobs, rawstore_tmp_db, monkeypatch):
    fk = _identity_fakes(monkeypatch)
    job = _run("identity", deadline_s=0)
    assert job["status"] == "error", job
    assert "deadline" in job["error"] and "identity sec_exchange" in job["error"]
    assert fk.calls == {}
    assert rawstore.count("identity_runs") == 0
    assert rawstore.count("identity_source_cache") == 0
    assert rawstore.count("security_master") == 0


def test_an_identity_cancel_at_the_first_source_writes_nothing(sync_jobs, rawstore_tmp_db, monkeypatch):
    fk = _identity_fakes(monkeypatch)
    _cancel_on_bind(monkeypatch)
    job = _run("identity", deadline_s=3600)
    assert job["status"] == "cancelled", job
    assert "identity sec_exchange" in job["message"]
    assert fk.calls == {}
    assert rawstore.count("identity_runs") == 0


def test_an_identity_stop_at_the_master_checkpoint_keeps_the_sources_and_writes_no_master(
        sync_jobs, rawstore_tmp_db, monkeypatch):
    """The ``security_master`` write is the most critical write of the PL4 change: a stop requested after the
    last source and before the master must leave the master unwritten (delete the guard and this goes red)."""
    fk = _identity_fakes(monkeypatch)
    held = {}
    real_bind, real_cp = job_control.bind, job_control.checkpoint

    def _bind(token):
        if token is not None:
            held["token"] = token
        return real_bind(token)

    def _cp(name=None):
        if name == "identity master":
            held["token"].cancel()
        return real_cp(name)

    monkeypatch.setattr(job_control, "bind", _bind)
    monkeypatch.setattr(job_control, "checkpoint", _cp)
    job = _run("identity", deadline_s=3600)
    assert job["status"] == "cancelled", job
    assert "identity master" in job["message"]
    assert "sec_exchange" in fk.calls
    assert rawstore.count("identity_source_cache") >= 1      # the earlier sources stay committed
    assert rawstore.count("security_master") == 0


def test_an_identity_run_inside_its_deadline_writes_the_master(sync_jobs, rawstore_tmp_db, monkeypatch):
    fk = _identity_fakes(monkeypatch)
    job = _run("identity", deadline_s=3600)
    assert job["status"] == "done", job
    assert "deadline" not in (job["error"] or "")
    assert "sec_exchange" in fk.calls
    assert rawstore.count("security_master") >= 1
    assert rawstore.count("identity_runs") == 1


# ── daily bars: between chunks, before the chunk's atomic write ──────────────

class _BarsUW:
    def __init__(self):
        self.requests = []
        self.auth_error = None
        self.breaker_open = False
        self._calls = 0

    def verify(self):
        return True, ""

    def health(self):
        return {"calls": self._calls, "breaker_open": False, "auth_error": None}

    def ohlc_daily_result(self, ticker, days=500):
        self._calls += 1
        self.requests.append(ticker)
        return ProviderResult("ok", [{"date": "2026-10-08", "close": 11.0, "volume": 1}])

    def close(self):
        pass


def _seed_bars():
    from RevenueChaser.core.tests.test_prices import bar_row, seed_spy
    seed_spy()
    rawstore.write_rows("price_bars_daily", [bar_row("AAA", "2026-10-07", close=10.0)])


def _quiet_bars(monkeypatch):
    from RevenueChaser.core import prices
    uw = _BarsUW()
    monkeypatch.setattr(jobs, "UWClient", lambda: uw)
    monkeypatch.setattr(prices, "_now", lambda now=None: _BARS_NOW if now is None else now)
    monkeypatch.setattr(prices, "_yf_download", lambda *a, **k: __import__("pandas").DataFrame())
    return uw


def test_a_bars_deadline_at_the_first_chunk_writes_nothing(sync_jobs, rawstore_tmp_db, monkeypatch):
    _seed_bars()
    uw = _quiet_bars(monkeypatch)
    before = rawstore.count("price_bars_daily")
    job = _run("daily_bars", deadline_s=0, params={"tickers": ["AAA"]})
    assert job["status"] == "error", job
    assert "deadline" in job["error"] and "daily bars AAA" in job["error"]
    assert uw.requests == []
    assert rawstore.count("price_bars_daily") == before


def test_a_bars_cancel_at_the_first_chunk_writes_nothing(sync_jobs, rawstore_tmp_db, monkeypatch):
    _seed_bars()
    uw = _quiet_bars(monkeypatch)
    _cancel_on_bind(monkeypatch)
    before = rawstore.count("price_bars_daily")
    job = _run("daily_bars", deadline_s=9600, params={"tickers": ["AAA"]})
    assert job["status"] == "cancelled", job
    assert "daily bars AAA" in job["message"]
    assert uw.requests == []
    assert rawstore.count("price_bars_daily") == before


def test_a_bars_stop_between_two_chunks_keeps_the_first_and_never_starts_the_second(
        sync_jobs, rawstore_tmp_db, monkeypatch):
    """Per-chunk atomicity: the first chunk's write stays, the second chunk's calls and write never start."""
    from RevenueChaser.core import prices
    from RevenueChaser.core.tests.test_prices import bar_row, rows_for
    _seed_bars()
    rawstore.write_rows("price_bars_daily", [bar_row("BBB", "2026-10-07", close=20.0)])
    uw = _quiet_bars(monkeypatch)
    monkeypatch.setattr(prices, "CHUNK", 1)
    held = {}
    real_bind = job_control.bind

    def _bind(token):
        if token is not None:
            held["token"] = token
        return real_bind(token)

    monkeypatch.setattr(job_control, "bind", _bind)
    real_request = uw.ohlc_daily_result

    def _request(ticker, days=500):
        out = real_request(ticker, days=days)
        held["token"].cancel()          # the stop arrives while the first chunk is being served
        return out

    uw.ohlc_daily_result = _request
    job = _run("daily_bars", deadline_s=9600, params={"tickers": ["AAA", "BBB"]})
    assert job["status"] == "cancelled", job
    assert uw.requests == ["AAA"]                  # the second chunk never asked
    assert rows_for("AAA", "2026-10-08")           # the first chunk's session is committed
    assert not rows_for("BBB", "2026-10-08")


def test_a_bars_run_inside_its_deadline_writes_the_session(sync_jobs, rawstore_tmp_db, monkeypatch):
    from RevenueChaser.core.tests.test_prices import rows_for
    _seed_bars()
    uw = _quiet_bars(monkeypatch)
    job = _run("daily_bars", deadline_s=9600, params={"tickers": ["AAA"]})
    assert job["status"] == "done", job
    assert "deadline" not in (job["error"] or "")
    assert uw.requests == ["AAA"]
    assert rows_for("AAA", "2026-10-08")


# ── Discord: between channels, before the channel export ─────────────────────

_CHANNEL = "900000000000000010"


def _quiet_discord(monkeypatch, tmp_path):
    from RevenueChaser.module_discord import exporter, store
    from RevenueChaser.module_discord.tests.test_engine import FakeCli
    exe = tmp_path / "dce-fake"
    exe.write_text("")
    monkeypatch.setenv("RC_DCE_CLI", str(exe))
    monkeypatch.setenv("DISCORD_TOKEN", "test-token-not-real")
    store.upsert_discovered([{"channel_id": _CHANNEL, "guild_id": "g", "name": "ideas", "category": "y"}])
    store.set_enabled([_CHANNEL], True)
    cli = FakeCli()
    monkeypatch.setattr(exporter, "run_cli", cli)
    return cli


def test_a_discord_deadline_at_the_first_channel_writes_nothing(sync_jobs, rawstore_tmp_db, monkeypatch, tmp_path):
    cli = _quiet_discord(monkeypatch, tmp_path)
    job = _run("module_discord", deadline_s=0, params={"channel_id": _CHANNEL, "force": True})
    assert job["status"] == "error", job
    assert "deadline" in job["error"] and f"Discord channel {_CHANNEL}" in job["error"]
    assert cli.calls == []
    assert rawstore.count("discord_messages") == 0
    assert rawstore.count("discord_export_runs") == 0


def test_a_discord_cancel_at_the_first_channel_writes_nothing(sync_jobs, rawstore_tmp_db, monkeypatch, tmp_path):
    cli = _quiet_discord(monkeypatch, tmp_path)
    _cancel_on_bind(monkeypatch)
    job = _run("module_discord", deadline_s=3600, params={"channel_id": _CHANNEL, "force": True})
    assert job["status"] == "cancelled", job
    assert f"Discord channel {_CHANNEL}" in job["message"]
    assert cli.calls == []
    assert rawstore.count("discord_messages") == 0


def test_a_discord_run_inside_its_deadline_stores_the_channel(sync_jobs, rawstore_tmp_db, monkeypatch, tmp_path):
    cli = _quiet_discord(monkeypatch, tmp_path)
    job = _run("module_discord", deadline_s=3600, params={"channel_id": _CHANNEL, "force": True})
    assert job["status"] == "done", job
    assert "deadline" not in (job["error"] or "")
    assert any(c[0][0] == "export" for c in cli.calls)
    assert rawstore.count("discord_messages") == 6
