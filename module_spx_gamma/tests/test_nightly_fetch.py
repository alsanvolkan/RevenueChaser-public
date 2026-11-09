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


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code, self._payload = status_code, payload

    def json(self):
        return self._payload


@pytest.fixture
def seams(monkeypatch, tmp_path):
    """The dashboard at the HTTP boundary: a POST to ``/run/spxgamma`` queues a job, ``/api/job/<id>`` reports it."""
    seen: dict = {"posts": [], "job": {"status": "done", "message": None}, "post_status": 202}
    monkeypatch.setattr(cli, "latest_complete_session", lambda: date(2026, 10, 30))
    monkeypatch.setattr(cli, "in_us_session_window", lambda: False)
    monkeypatch.setattr(cli, "_POLL_S", 0)
    monkeypatch.setattr(cli, "DATA_ROOT", tmp_path)
    (tmp_path / "latest.json").write_text(json.dumps({"asof_date": "2026-10-30"}))

    def fake_post(url, data=None, headers=None, timeout=None):
        seen["posts"].append((url, data, headers))
        if seen["post_status"] != 202:
            return _Resp(seen["post_status"], {"ok": False, "error": "busy"})
        return _Resp(202, {"ok": True, "job_id": "j1"})

    def fake_get(url, timeout=None):
        assert url.endswith("/api/job/j1")
        return _Resp(200, {"ok": True, "job": seen["job"]})

    monkeypatch.setattr(cli.httpx, "post", fake_post)
    monkeypatch.setattr(cli.httpx, "get", fake_get)
    return seen


def test_nightly_queues_the_fetch_on_the_dashboard(seams, monkeypatch, capsys):
    """S14/F1: the dashboard is the one writer; the task only asks it, with the panel header, and waits."""
    monkeypatch.setattr(cli, "stored_spx_candles", lambda day, ticker="SPX": [{"date": day, "close": 7580.07}])
    assert cli.main(["--nightly"]) == 0
    [(url, data, headers)] = seams["posts"]
    assert url == "http://127.0.0.1:8000/run/spxgamma" and data == {"date": "2026-10-30"}
    assert headers == {"X-RC-Panel": "cli"}
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line == {"asof": "2026-10-30", "effective": "2026-10-30", "status": "done", "job_id": "j1",
                    "message": None}


def test_nightly_reports_the_effective_day_when_it_differs(seams, monkeypatch, capsys):
    """The mandatory-endpoint fallback can store the previous session: the log says so instead of a bare "done"."""
    monkeypatch.setattr(cli, "stored_spx_candles", lambda day, ticker="SPX": [{"date": day, "close": 7580.07}])
    (cli.DATA_ROOT / "latest.json").write_text(json.dumps({"asof_date": "2026-10-29"}))
    assert cli.main(["--nightly"]) == 0
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["effective"] == "2026-10-29"


def test_nightly_fails_when_the_dashboard_job_fails(seams, monkeypatch, capsys):
    monkeypatch.setattr(cli, "stored_spx_candles", lambda day, ticker="SPX": [{"date": day, "close": 7580.07}])
    seams["job"] = {"status": "error", "error": "UW 401"}
    assert cli.main(["--nightly"]) == 1
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["status"] == "error" and line["error"] == "UW 401" and line["job_id"] == "j1"


def test_nightly_fails_when_the_dashboard_refuses(seams, monkeypatch, capsys):
    """A 409 (the history pull is running) or a 400 (bad UW key): nothing is fetched, and the log says why."""
    monkeypatch.setattr(cli, "stored_spx_candles", lambda day, ticker="SPX": [{"date": day, "close": 7580.07}])
    seams["post_status"] = 409
    assert cli.main(["--nightly"]) == 1
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["status"] == "refused" and line["http"] == 409 and line["error"] == "busy"


def test_nightly_fails_when_the_dashboard_is_down(seams, monkeypatch, capsys):
    """D3: with the dashboard down that night's Fetch does not happen; the task never writes in its own process."""
    monkeypatch.setattr(cli, "stored_spx_candles", lambda day, ticker="SPX": [{"date": day, "close": 7580.07}])

    def down(*a, **k):
        raise cli.httpx.ConnectError("refused")

    monkeypatch.setattr(cli.httpx, "post", down)
    assert cli.main(["--nightly"]) == 1
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["status"] == "dashboard_down"


def test_nightly_never_runs_the_fetch_in_its_own_process():
    """F1: the CLI module no longer imports the module's writer or a UW client."""
    assert not hasattr(cli, "run") and not hasattr(cli, "UWClient")


def test_nightly_never_fetches_inside_the_us_session(seams, monkeypatch, capsys):
    """A run caught up during the session (the box was off at 06:30) would store intraday answers as the previous
    session's complete raw day, which is never fetched again."""
    monkeypatch.setattr(cli, "stored_spx_candles", lambda day, ticker="SPX": [{"date": day, "close": 7580.07}])
    monkeypatch.setattr(cli, "in_us_session_window", lambda: True)
    assert cli.main(["--nightly"]) == cli.EXIT_NOT_READY
    assert seams["posts"] == []
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["status"] == "in_session"


def test_nightly_waits_for_the_stored_close_and_calls_nothing(seams, monkeypatch, capsys):
    monkeypatch.setattr(cli, "stored_spx_candles", lambda day, ticker="SPX": [{"date": "2026-10-29", "close": 1.0}])
    assert cli.main(["--nightly"]) == cli.EXIT_NOT_READY
    assert seams["posts"] == []
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["status"] == "not_ready" and line["asof"] == "2026-10-30"


def test_asof_and_nightly_are_exclusive():
    with pytest.raises(SystemExit):
        cli.main(["--nightly", "--asof", "2026-10-30"])


def test_nightly_is_spx_only():
    """The close guard reads the SPX series whatever the ticker (``snapshot.stored_spx_candles``)."""
    with pytest.raises(SystemExit):
        cli.main(["--nightly", "--ticker", "NDX"])
