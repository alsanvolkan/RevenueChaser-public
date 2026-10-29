"""M-OIPD-2 (#382): ``as_of`` is the stored chain's session, not ``date.today()``.

Since G9 the chain is the options store's ``option_eod`` of the name's newest session: a Saturday Fetch of Friday's
chain gives ``as_of`` Friday and counts every DTE from Friday; with no stored chain the board is dated the latest
complete session. Offline: fixture serving files and a frozen calendar clock.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from RevenueChaser.core import calendar
from RevenueChaser.module_oipd import engine, report
from RevenueChaser.module_oipd.tests.options_store import daily_row, eod_rows, write_options
from RevenueChaser.module_oipd.tests.test_engine import _chain_for, _expiries
from RevenueChaser.rcdata import ops

_FRIDAY = "2026-09-25"
_SATURDAY = "2026-09-26"
# Saturday 14:00 UTC = 10:00 ET; the latest complete session is Friday 2026-09-25.
_SATURDAY_NOON_UTC = datetime(2026, 9, 26, 14, 0, tzinfo=timezone.utc)


def _store_friday():
    ops.watch_add("option", "TST", by="test")
    friday = date.fromisoformat(_FRIDAY)
    write_options(option_eod=eod_rows("TST", friday, [c for e in _expiries() for c in _chain_for(e)]),
                  option_daily=[daily_row("TST", friday, 100.0)])


def test_as_of_is_the_stored_session_and_dte_is_counted_from_it(monkeypatch):
    monkeypatch.setattr(engine, "_today", lambda: _SATURDAY)
    _store_friday()
    payload = engine.run(None, "TST")
    assert payload["asof"] == _FRIDAY
    first = payload["expiries"][0]
    # Expiries are 30 days after 2026-09-23, and 2026-10-23 - 2026-09-25 = 28 days.
    assert first["date"] == "2026-10-23" and first["dte"] == 28


def test_the_eod_label_shows_the_stored_session(monkeypatch):
    monkeypatch.setattr(engine, "_today", lambda: _SATURDAY)
    _store_friday()
    html = report.render_oipd_dashboard(engine.run(None, "TST"))
    assert "Friday, September 25, 2026 EOD" in html


def test_no_stored_chain_uses_the_latest_complete_session(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: _SATURDAY_NOON_UTC)
    assert engine._today() == _FRIDAY
    assert engine.run(None, "SPX")["asof"] == _FRIDAY
