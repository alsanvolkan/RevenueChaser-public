"""``ibkr/shortable``: one download of IBKR's public short-stock file a day (the public ``shortstock`` login, no
account), parsed by its header, refused when cut short or not the file, retried and counted on an FTP error, and
each symbol named by the spine."""
from __future__ import annotations

import ftplib
from datetime import date, datetime, timezone

import pytest

from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.ownership import ibkr

from . import ownership_fixture as ox

NOON_NY = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc).timestamp()      # 12:00 New York, a session day


@pytest.fixture
def ftp(monkeypatch):
    fake = ox.FakeFtp(ox.ibkr_file())
    fake.sessions = []
    monkeypatch.setattr(ibkr, "_ftp_factory", fake)
    monkeypatch.setattr(ibkr, "_now", lambda: NOON_NY)
    monkeypatch.setenv("RC_IBKR_SHORT_FTP_HOST", "ftp.ibkr.test")
    ox.make_spine()
    return fake


def _run(day: str = "2026-10-28") -> dict:
    return base.run_request({"dataset": ibkr.NAME, "params": {"as_of": day}}, sleep=lambda s: None)


def _served(sql: str, *args) -> list[tuple]:
    with serving.open_read("ownership") as con:
        return con.execute(sql, list(args)).fetchall()


def test_one_download_with_the_public_login_typed_and_named(ftp):
    row = _run()
    assert row["result"] == "changed" and row["rows"] == 4 and row["calls"] == 1
    (session,) = ftp.sessions
    assert session.host == "ftp.ibkr.test" and session.login_args == ("shortstock", "")
    assert session.commands == ["RETR usa.txt"]
    assert _served("SELECT symbol, ticker, session_date, fee_rate, rebate_rate, available, available_capped, "
                   "conid, isin, figi, snapshot_at FROM ibkr_shortable ORDER BY symbol") == [
        ("AAPL", "AAPL", date(2026, 10, 28), 0.25, 3.3383, 10_000_000, True, "265598", "US0378331005",
         "BBG000B9XRY4", "2026.10.28 09:45:02"),
        ("BRK-B", "BRK-B", date(2026, 10, 28), 0.25, 3.1, 3_500_000, False, "72063691", "US0846707026",
         "BBG000DWG505", "2026.10.28 09:45:02"),
        ("GME", "GME", date(2026, 10, 28), 16.0, -12.5, 150_000, False, "36285627", "US36467W1099",
         "BBG000BB5BF6", "2026.10.28 09:45:02"),
        ("NOAV", None, date(2026, 10, 28), None, None, 0, False, "99", "US0000000001", None,
         "2026.10.28 09:45:02")]


def test_the_same_day_is_not_downloaded_again(ftp):
    _run()
    row = _run()
    assert len(ftp.sessions) == 1 and row["calls"] == 0


@pytest.mark.parametrize("body, why", [(ox.ibkr_file(eof=False), "no #EOF"),
                                       (ox.ibkr_file(header="#SYMBOL|CURRENCY|"), "header"),
                                       (b"<html>maintenance</html>", "#BOF")])
def test_a_file_cut_short_or_not_the_file_is_refused_and_not_kept(ftp, body, why):
    ftp.body = body
    row = _run()
    assert row["result"] == "failed" and why in row["note"]
    assert not ibkr.raw_path(date(2026, 10, 28)).exists()


def test_an_ftp_error_is_retried_and_every_attempt_counted(ftp):
    ftp.fail = [ftplib.error_temp("421 too many users"), EOFError()]
    row = _run()
    assert row["result"] == "changed" and row["calls"] == 3 and len(ftp.sessions) == 3


def test_an_ftp_that_keeps_failing_fails_the_run(ftp):
    ftp.fail = [ftplib.error_temp("421")] * base.HTTP_ATTEMPTS
    row = _run()
    assert row["result"] == "failed" and "421" in row["note"]
