"""#291: the scheduled SPX Gamma Fetch (RC-SPX-Gamma-Fetch, Monday to Friday 06:30).

``--nightly`` fetches the latest complete New York session, so every session gets a raw day with its all-expiry
ladder (the band walls). It runs only once the store holds that session's SPX close: a day fetched without it would
freeze a stale spot, because a complete raw day is never fetched again.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from RevenueChaser.module_spx_gamma import __main__ as cli


class _UW:
    key = "k"

    def health(self):
        return {"calls": 71, "failed": 0}


@pytest.fixture
def seams(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(cli, "latest_complete_session", lambda: date(2026, 10, 30))
    monkeypatch.setattr(cli, "UWClient", _UW)

    def fake_run(asof, uw=None, **kw):
        seen["run"] = (asof, uw)
        return {"paths": {"report_html": "r.html"}, "latest": {"asof_date": seen.get("effective", asof)}}

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(cli, "in_us_session_window", lambda: False)
    return seen


def test_nightly_fetches_the_latest_complete_session(seams, monkeypatch, capsys):
    monkeypatch.setattr(cli, "stored_spx_candles", lambda day, ticker="SPX": [{"date": day, "close": 7580.07}])
    assert cli.main(["--nightly"]) == 0
    asof, uw = seams["run"]
    assert asof == "2026-10-30" and isinstance(uw, _UW)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line == {"asof": "2026-10-30", "effective": "2026-10-30", "status": "done",
                    "uw": {"calls": 71, "failed": 0}}


def test_nightly_reports_the_effective_day_when_it_differs(seams, monkeypatch, capsys):
    """The mandatory-endpoint fallback can store the previous session: the log says so instead of a bare "done"."""
    monkeypatch.setattr(cli, "stored_spx_candles", lambda day, ticker="SPX": [{"date": day, "close": 7580.07}])
    seams["effective"] = "2026-10-29"
    assert cli.main(["--nightly"]) == 0
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["effective"] == "2026-10-29"


def test_nightly_never_fetches_inside_the_us_session(seams, monkeypatch, capsys):
    """A run caught up during the session (the box was off at 06:30) would store intraday answers as the previous
    session's complete raw day, which is never fetched again."""
    monkeypatch.setattr(cli, "stored_spx_candles", lambda day, ticker="SPX": [{"date": day, "close": 7580.07}])
    monkeypatch.setattr(cli, "in_us_session_window", lambda: True)
    assert cli.main(["--nightly"]) == cli.EXIT_NOT_READY
    assert "run" not in seams
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["status"] == "in_session"


def test_nightly_waits_for_the_stored_close_and_calls_nothing(seams, monkeypatch, capsys):
    monkeypatch.setattr(cli, "stored_spx_candles", lambda day, ticker="SPX": [{"date": "2026-10-29", "close": 1.0}])
    assert cli.main(["--nightly"]) == cli.EXIT_NOT_READY
    assert "run" not in seams
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["status"] == "not_ready" and line["asof"] == "2026-10-30"


def test_asof_and_nightly_are_exclusive():
    with pytest.raises(SystemExit):
        cli.main(["--nightly", "--asof", "2026-10-30"])


def test_nightly_is_spx_only():
    """The close guard reads the SPX series whatever the ticker (``snapshot.stored_spx_candles``)."""
    with pytest.raises(SystemExit):
        cli.main(["--nightly", "--ticker", "NDX"])
