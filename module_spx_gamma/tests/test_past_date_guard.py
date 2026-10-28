"""SPX-1 / #179 D5: a stored past day is served from disk and never rewritten; SPX-3: only the
newest day moves ``latest.json``; an explicit ``force`` still refetches."""
from __future__ import annotations

import functools
import hashlib
import json
from datetime import date

import pytest

from RevenueChaser.module_spx_gamma import run as spx_run, snapshot as snap

from .conftest import FakeUW
from .test_s2_fetch import CFTC, VIX, _handler

OLD, NEW = "2026-08-13", "2026-08-14"


def _candles(day):
    return lambda *a, **k: [{"date": day, "open": 1.0, "high": 2.0, "low": 0.5, "close": 7785.76, "volume": 1.0}]


def _store(root, day):
    """One complete stored raw day (with its manifest), fetched from a fake client."""
    snap.fetch_snapshot(FakeUW(_handler, prefix=""), day, data_root=root / "raw",
                        candle_fetcher=_candles(day))
    assert (root / "raw" / day / "manifest.json").exists()


def _state(folder):
    """SHA-256 and mtime of every file in a folder: byte-identical and untouched."""
    return {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(folder.rglob("*")) if p.is_file()}


class _Calls:
    """Counts the fetch and backfill calls ``run()`` makes; the fetch uses offline fetchers."""

    def __init__(self, monkeypatch, today):
        self.fetches: list[str] = []
        self.backfills = 0
        monkeypatch.setattr(spx_run, "today_ny", lambda: date.fromisoformat(today))

        def _fetch(uw, asof, **kw):
            self.fetches.append(asof)
            return functools.partial(snap.fetch_snapshot,
                                     candle_fetcher=_candles(asof))(uw, asof, **kw)

        def _backfill(*a, **k):
            self.backfills += 1

        monkeypatch.setattr(spx_run, "fetch_snapshot", _fetch)
        monkeypatch.setattr(spx_run, "_backfill_history", _backfill)


def _uw():
    uw = FakeUW(_handler, prefix="")
    uw.key = "k"
    return uw


@pytest.fixture
def two_days(tmp_path):
    _store(tmp_path, OLD)
    _store(tmp_path, NEW)
    spx_run.refresh(data_root=tmp_path)          # latest.json on the newest day, as after a Fetch
    return tmp_path


def test_a_stored_past_day_makes_no_uw_call_and_stays_byte_identical(two_days, monkeypatch):
    calls = _Calls(monkeypatch, today="2026-08-17")
    before = _state(two_days / "raw" / OLD)
    uw = _uw()
    out = spx_run.run(OLD, data_root=two_days, uw=uw)
    assert uw.paths == []
    assert calls.fetches == [] and calls.backfills == 0
    assert _state(two_days / "raw" / OLD) == before
    assert out["latest"]["asof_date"] == OLD
    assert out["paths"]["report_html"].exists()


def test_a_stored_past_day_leaves_latest_json_on_the_newest_day(two_days, monkeypatch):
    _Calls(monkeypatch, today="2026-08-17")
    latest = (two_days / "latest.json").read_bytes()
    spx_run.run(OLD, data_root=two_days, uw=_uw())
    assert (two_days / "latest.json").read_bytes() == latest


def test_force_refetches_a_stored_past_day(two_days, monkeypatch):
    calls = _Calls(monkeypatch, today="2026-08-17")
    uw = _uw()
    spx_run.run(OLD, data_root=two_days, uw=uw, force=True)
    assert calls.fetches == [OLD]
    assert uw.paths


def test_a_forced_refetch_of_an_older_day_leaves_latest_json_alone(two_days, monkeypatch):
    _Calls(monkeypatch, today="2026-08-17")
    latest = (two_days / "latest.json").read_bytes()
    spx_run.run(OLD, data_root=two_days, uw=_uw(), force=True)
    assert (two_days / "latest.json").read_bytes() == latest


def test_today_is_still_fetched_and_moves_latest_json(two_days, monkeypatch):
    calls = _Calls(monkeypatch, today=NEW)
    (two_days / "latest.json").unlink()          # the fixture's refresh already wrote NEW there
    uw = _uw()
    out = spx_run.run(NEW, data_root=two_days, uw=uw)
    assert calls.fetches == [NEW] and calls.backfills == 1
    assert uw.paths
    assert out["latest"]["asof_date"] == NEW
    assert json.loads((two_days / "latest.json").read_text(encoding="utf-8"))["asof_date"] == NEW


def test_a_past_day_without_a_manifest_is_still_fetched(tmp_path, monkeypatch):
    _store(tmp_path, NEW)
    calls = _Calls(monkeypatch, today="2026-08-17")
    uw = _uw()
    spx_run.run(OLD, data_root=tmp_path, uw=uw)
    assert calls.fetches == [OLD]
    assert uw.paths


# ── the dashboard: routing, force plumbing, and which day the board shows ──
@pytest.fixture
def board(two_days, monkeypatch):
    """The dashboard pointed at ``two_days``; no shown day yet, as after a restart."""
    from RevenueChaser.app import dashboard, runners
    from RevenueChaser.module_spx_gamma import config

    monkeypatch.setattr(spx_run, "today_ny", lambda: date(2026, 8, 17))
    monkeypatch.setattr(spx_run, "DATA_ROOT", two_days)
    monkeypatch.setattr(config, "DATA_ROOT", two_days)
    monkeypatch.setattr(config, "REPORT_ROOT", two_days / "reports")
    monkeypatch.setattr(runners, "_SPX_VIEW", None, raising=False)
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


def _report(root, day):
    return (root / "reports" / day / "section.html").read_bytes()


def test_the_route_sends_a_stored_past_day_to_the_no_uw_job(board, two_days, monkeypatch):
    from RevenueChaser.app import dashboard

    seen = []
    monkeypatch.setattr(dashboard, "_submit", lambda module, params: seen.append((module, params)) or ("ok", 200))
    (two_days / "raw" / OLD / "manifest.json").rename(two_days / "raw" / OLD / "manifest.bak")
    board.post("/run/spxgamma", data={"date": OLD}, headers={"X-RC-Panel": "cli"})       # no complete day
    (two_days / "raw" / OLD / "manifest.bak").rename(two_days / "raw" / OLD / "manifest.json")
    board.post("/run/spxgamma", data={"date": OLD}, headers={"X-RC-Panel": "cli"})       # stored
    board.post("/run/spxgamma", data={"date": OLD, "force": "1"}, headers={"X-RC-Panel": "cli"})
    board.post("/run/spxgamma", data={"date": "2026-08-17"}, headers={"X-RC-Panel": "cli"})  # today
    assert seen == [("spxgamma", {"asof_date": OLD}),
                    ("spxgamma_stored", {"asof_date": OLD}),
                    ("spxgamma", {"asof_date": OLD, "force": True}),
                    ("spxgamma", {"asof_date": "2026-08-17"})]


def test_a_stored_past_day_click_sends_no_auth_probe(board, monkeypatch):
    from RevenueChaser.app import dashboard, jobs

    probes, submitted = [], []

    class _Probe:
        def verify(self):
            probes.append(1)
            return True, "ok"

    monkeypatch.setattr(dashboard, "_uw", lambda: _Probe())
    monkeypatch.setattr(jobs, "submit", lambda module, params: submitted.append(module) or "j1")
    assert board.post("/run/spxgamma", data={"date": OLD}, headers={"X-RC-Panel": "cli"}).status_code == 202
    assert submitted == ["spxgamma_stored"] and probes == []
    assert jobs._REQUIRES_UW["spxgamma_stored"] is False      # the worker builds no client either
    board.post("/run/spxgamma", data={"date": OLD, "force": "1"}, headers={"X-RC-Panel": "cli"})
    assert submitted[-1] == "spxgamma" and probes == [1]


def test_the_stored_job_rebuilds_from_disk_with_no_network(board, two_days, monkeypatch):
    from RevenueChaser.app import runners

    def boom(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(spx_run, "fetch_snapshot", boom)
    monkeypatch.setattr(spx_run, "UWClient", boom)
    before = _state(two_days / "raw" / OLD)
    latest = (two_days / "latest.json").read_bytes()
    runners.run_spxgamma_stored(None, OLD)
    assert _state(two_days / "raw" / OLD) == before
    assert (two_days / "latest.json").read_bytes() == latest


def test_the_runner_passes_force_to_the_module(monkeypatch):
    from RevenueChaser.app import runners

    got = []
    monkeypatch.setattr("RevenueChaser.module_spx_gamma.run.run", lambda asof, **k: got.append(k["force"]))
    monkeypatch.setattr(runners, "_SPX_VIEW", None, raising=False)
    runners.run_spxgamma(object(), OLD)
    runners.run_spxgamma(object(), OLD, force=True)
    assert got == [False, True]


def test_the_board_shows_the_past_day_just_built(board, two_days):
    from RevenueChaser.app import runners

    runners.run_spxgamma(None, OLD)       # a calendar click on a stored day
    assert _report(two_days, OLD) != _report(two_days, NEW)
    assert board.get("/view/module_spx_gamma").get_data() == _report(two_days, OLD)


def test_after_a_restart_the_board_shows_the_newest_day(board, two_days, monkeypatch):
    from RevenueChaser.app import runners

    runners.run_spxgamma(None, OLD)       # a calendar click on a stored day
    monkeypatch.setattr(runners, "_SPX_VIEW", None, raising=False)   # a new dashboard process
    assert board.get("/view/module_spx_gamma").get_data() == _report(two_days, NEW)


def test_refresh_puts_the_board_back_on_the_newest_day(board, two_days, monkeypatch):
    from RevenueChaser.app import runners

    monkeypatch.setattr("RevenueChaser.app.dashboard._from_another_site", lambda: False)
    runners.run_spxgamma(None, OLD)       # a calendar click on a stored day
    assert board.get("/view/module_spx_gamma?refresh=1").get_data() == _report(two_days, NEW)
    assert board.get("/view/module_spx_gamma").get_data() == _report(two_days, NEW)
