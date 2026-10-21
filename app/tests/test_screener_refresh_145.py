"""#145 (27-1) at the runner and route level: what a Fetch ends with, what a Refresh may write.

- A Fetch that scores nothing ends with its reason (``JobMessage``), after the board is written.
- A Refresh (``no_fetch``) is a GET: it returns the board and never raises.
- Only a Fetch rewrites ``ticker_scores`` (Asymmetric Growth).
- ``run_universe_refresh`` still runs both boards when the first one scores nothing.
- The empty and loading states are escaped and render in memory (nothing transient is cached).
"""
from __future__ import annotations

import pytest

from RevenueChaser.app import dashboard, jobs, runners
from RevenueChaser.app.jobs import JobMessage
from RevenueChaser.core import rawstore
from RevenueChaser.core.scan_quality import no_data_payload
from RevenueChaser.module_fundamentals import config as fcfg
from RevenueChaser.module_fundamentals import pipeline as fp
from RevenueChaser.module_swing_potential import config as scfg
from RevenueChaser.module_swing_potential import pipeline as sp

BOARDS = [
    pytest.param(sp, "run_swing", "swing_potential_latest.html", id="swing"),
    pytest.param(fp, "run_fundamentals", "fundamentals_latest.html", id="fundamentals"),
]


@pytest.fixture
def out(tmp_path, monkeypatch):
    """Every artifact the runners write lands in a temp folder."""
    folder = tmp_path / "out"
    monkeypatch.setattr(runners, "OUTPUT_DIR", folder)
    monkeypatch.setattr(runners, "SWING_LATEST", folder / "swing_potential_latest.html")
    monkeypatch.setattr(runners, "FUNDAMENTALS_LATEST", folder / "fundamentals_latest.html")
    monkeypatch.setattr(runners, "ASYMMETRIC_GROWTH_LATEST", folder / "asymmetric_growth_latest.html")
    monkeypatch.setattr(scfg, "SWING_JSON", tmp_path / "swing.json")
    monkeypatch.setattr(fcfg, "FUNDAMENTALS_JSON", tmp_path / "fundamentals.json")
    return folder


def _empty(mod, reason):
    return no_data_payload(mod.build_payload([], run_stats={}), reason, {"no_ohlc": 3}, 3)


@pytest.fixture
def yf_calls(monkeypatch):
    calls: list[str] = []

    def _boom(name):
        def f(*a, **k):
            calls.append(name)
            raise AssertionError(f"yfinance.{name} was called")
        return f

    for name in ("screen", "download"):
        monkeypatch.setattr(f"yfinance.{name}", _boom(name))
    monkeypatch.setattr("yfinance.Ticker", _boom("Ticker"))
    return calls


# ── Fetch ends with its reason ────────────────────────────────────────────────

@pytest.mark.parametrize("mod,runner,artifact", BOARDS)
def test_a_fetch_that_scores_nothing_ends_with_its_reason(out, monkeypatch, mod, runner, artifact):
    monkeypatch.setattr(mod, "run_scan", lambda *a, **k: _empty(mod, "no_scorable_tickers"))
    with pytest.raises(JobMessage) as exc:
        getattr(runners, runner)(None)
    assert "none of the 3 shortlisted tickers could be scored" in exc.value.message
    assert "3 no cached price bars" in exc.value.message
    # the empty board is on disk before the message is raised
    assert "none of the 3 shortlisted tickers" in (out / artifact).read_text(encoding="utf-8")


@pytest.mark.parametrize("mod,runner,artifact", BOARDS)
def test_a_refresh_that_scores_nothing_returns_the_board_without_raising(out, monkeypatch, mod, runner, artifact):
    monkeypatch.setattr(mod, "run_scan", lambda *a, **k: _empty(mod, "no_universe_stored"))
    path = getattr(runners, runner)(None, no_fetch=True)
    assert path.name == artifact
    assert "No stored universe yet" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("mod,runner,artifact", BOARDS)
def test_a_fetch_that_scores_something_ends_normally(out, monkeypatch, mod, runner, artifact):
    monkeypatch.setattr(mod, "run_scan", lambda *a, **k: mod.build_payload([], run_stats={"regime": "RISK-ON"}))
    assert getattr(runners, runner)(None).name == artifact


@pytest.mark.parametrize("mod,runner,artifact", BOARDS)
def test_the_board_is_written_atomically_into_its_json(out, monkeypatch, tmp_path, mod, runner, artifact):
    monkeypatch.setattr(mod, "run_scan", lambda *a, **k: _empty(mod, "no_universe_stored"))
    getattr(runners, runner)(None, no_fetch=True)
    import json
    cfg_json = scfg.SWING_JSON if mod is sp else fcfg.FUNDAMENTALS_JSON
    assert json.loads(cfg_json.read_text(encoding="utf-8"))["no_data_reason"] == "no_universe_stored"


# ── the universe job runs both boards ─────────────────────────────────────────

def _universe_job(monkeypatch, swing, fundamentals):
    ran = []
    monkeypatch.setattr(runners, "run_asymmetric_growth", lambda uw, **k: ran.append("ag"))

    def _step(name, outcome):
        def run(uw, **k):
            ran.append(name)
            if outcome:
                raise JobMessage(outcome)
            return runners.FUNDAMENTALS_LATEST
        return run

    monkeypatch.setattr(runners, "run_swing", _step("swing", swing))
    monkeypatch.setattr(runners, "run_fundamentals", _step("fundamentals", fundamentals))
    return ran


def test_universe_job_runs_fundamentals_when_swing_scores_nothing(monkeypatch):
    ran = _universe_job(monkeypatch, "No data: swing empty", None)
    with pytest.raises(JobMessage) as exc:
        runners.run_universe_refresh(None)
    assert ran == ["ag", "swing", "fundamentals"]
    assert exc.value.message == "Swing: No data: swing empty"


def test_universe_job_reports_every_empty_board(monkeypatch):
    ran = _universe_job(monkeypatch, "No data: a", "No data: b")
    with pytest.raises(JobMessage) as exc:
        runners.run_universe_refresh(None)
    assert ran == ["ag", "swing", "fundamentals"]
    assert exc.value.message == "Swing: No data: a Fundamentals: No data: b"


def test_universe_job_ends_normally_when_both_boards_scored(monkeypatch):
    _universe_job(monkeypatch, None, None)
    assert runners.run_universe_refresh(None) == runners.FUNDAMENTALS_LATEST


# ── Asymmetric Growth: only a Fetch writes ticker_scores ──────────────────────

@pytest.fixture
def ag_runner(out, monkeypatch):
    from RevenueChaser.core import ticker_store
    from RevenueChaser.module_asymmetric_growth import engine
    written: list[int] = []
    monkeypatch.setattr(ticker_store, "write_from_cards", lambda cards, asof=None: written.append(len(cards)))
    monkeypatch.setattr(engine, "run_asymmetric_growth_hunter", lambda uw, **k: {
        "asof": "2026-10-03T00:00:00", "universe_total": 1, "summary": {}, "thresholds": {},
        "cards": [{"ticker": "AAA"}]})
    monkeypatch.setattr("RevenueChaser.module_asymmetric_growth.report.render_asymmetric_growth_hunter_html",
                        lambda result: "<html>ag</html>")
    return written


def test_ag_refresh_leaves_ticker_scores_alone(ag_runner):
    runners.run_asymmetric_growth(None, no_fetch=True)
    assert ag_runner == []


def test_ag_fetch_writes_ticker_scores(ag_runner):
    runners.run_asymmetric_growth(None)
    assert ag_runner == [1]


# ── hostile text: the new empty states are escaped ────────────────────────────

HOSTILE = "</script><img src=x onerror=alert(1)>"


@pytest.mark.parametrize("mod,runner,artifact", BOARDS)
def test_a_hostile_no_data_message_is_escaped_on_the_screener_boards(mod, runner, artifact):
    from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard
    from RevenueChaser.module_swing_potential.report import render_swing_dashboard
    payload = mod.build_payload([], run_stats={})
    payload.update(no_data=True, no_data_reason="no_universe_stored", no_data_message=HOSTILE)
    html = (render_swing_dashboard if mod is sp else render_fundamental_dashboard)(payload)
    assert "<img src=x" not in html
    assert "&lt;img src=x" in html


def test_a_hostile_no_data_message_is_escaped_on_the_ag_board():
    from RevenueChaser.module_asymmetric_growth.engine import THRESHOLDS
    from RevenueChaser.module_asymmetric_growth.report import render_asymmetric_growth_hunter_html
    html = render_asymmetric_growth_hunter_html({
        "asof": "2026-10-03T00:00:00", "universe_total": 0, "summary": {}, "cards": [], "thresholds": THRESHOLDS,
        "no_data_reason": "no_universe_stored", "no_data_message": HOSTILE})
    assert HOSTILE not in html and "<img src=x" not in html
    assert "Click Refresh to retry" not in html


# ── the route: "Loading universe…" while a universe job runs, never a new job ─

@pytest.fixture
def client(rawstore_tmp_db, out, yf_calls):
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


@pytest.mark.parametrize("mod", ["module_swing_potential", "module_fundamentals", "module_asymmetric_growth"])
def test_refresh_with_an_empty_store_points_at_fetch_universe(client, out, yf_calls, monkeypatch, mod):
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda *a, **k: submitted.append(a) or "x")
    body = client.get(f"/view/{mod}?refresh=1").get_data(as_text=True)
    assert "No stored universe yet" in body and "Fetch Universe" in body
    assert "Loading universe" not in body
    assert yf_calls == [] and submitted == []


@pytest.mark.parametrize("mod", ["module_swing_potential", "module_fundamentals", "module_asymmetric_growth"])
def test_a_running_universe_job_shows_loading_and_caches_nothing(client, out, monkeypatch, mod):
    monkeypatch.setattr(jobs, "has_active", lambda modules: "universe" in modules)
    body = client.get(f"/view/{mod}?refresh=1").get_data(as_text=True)
    assert "Loading universe…" in body
    assert not out.exists() or list(out.iterdir()) == []


def test_loading_is_not_shown_once_the_universe_is_stored(client, out, monkeypatch):
    rawstore.replace_all("equity_universe", [{"ticker": "AAA", "market_cap": 1e9, "price": 10.0, "name": "A"}])
    monkeypatch.setattr(jobs, "has_active", lambda modules: True)
    monkeypatch.setattr(sp, "prefilter", lambda r: sp.screen_common.Prefiltered(list(r)))
    body = client.get("/view/module_swing_potential?refresh=1").get_data(as_text=True)
    assert "Loading universe" not in body


def test_loading_covers_only_the_three_universe_boards(client, monkeypatch):
    monkeypatch.setattr(jobs, "has_active", lambda modules: True)
    assert dashboard._universe_loading("module_leaps") is False
    assert dashboard._universe_loading("module_swing_potential") is True


@pytest.mark.parametrize("mod", [sp, fp])
def test_the_retry_hint_is_only_for_failed_scans(mod):
    """The "price source is reachable" hint fits a failed scan, not an unloaded store."""
    from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard
    from RevenueChaser.module_swing_potential.report import render_swing_dashboard
    render = render_swing_dashboard if mod is sp else render_fundamental_dashboard
    assert "price source is reachable" not in render(_empty(mod, "no_universe_stored"))
    assert "price source is reachable" in render(_empty(mod, "no_scorable_tickers"))
