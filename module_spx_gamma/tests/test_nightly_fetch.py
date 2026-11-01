"""#291: the scheduled SPX Gamma Fetch (RC-SPX-Gamma-Fetch, Tuesday to Saturday 06:30).

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
        return {"paths": {"report_html": "r.html"}}

    monkeypatch.setattr(cli, "run", fake_run)
    return seen


def test_nightly_fetches_the_latest_complete_session(seams, monkeypatch, capsys):
    monkeypatch.setattr(cli, "stored_spx_candles", lambda day, ticker="SPX": [{"date": day, "close": 7580.07}])
    assert cli.main(["--nightly"]) == 0
    asof, uw = seams["run"]
    assert asof == "2026-10-30" and isinstance(uw, _UW)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line == {"asof": "2026-10-30", "status": "done", "uw": {"calls": 71, "failed": 0}}


def test_nightly_waits_for_the_stored_close_and_calls_nothing(seams, monkeypatch, capsys):
    monkeypatch.setattr(cli, "stored_spx_candles", lambda day, ticker="SPX": [{"date": "2026-10-29", "close": 1.0}])
    assert cli.main(["--nightly"]) == cli.EXIT_NOT_READY
    assert "run" not in seams
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["status"] == "not_ready" and line["asof"] == "2026-10-30"


def test_asof_and_nightly_are_exclusive():
    with pytest.raises(SystemExit):
        cli.main(["--nightly", "--asof", "2026-10-30"])
