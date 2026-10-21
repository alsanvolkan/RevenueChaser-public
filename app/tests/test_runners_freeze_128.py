"""The forecast-ledger freeze hook in the three Fetch runners (#128).

The hook is the last step of ``run_swing`` / ``run_fundamentals`` / ``run_asymmetric_growth``, runs
only on a Fetch (``no_fetch=False``), and reports through ``core.ledger`` notes, never ``JobMessage``.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from RevenueChaser.app import jobs, runners
from RevenueChaser.core import calendar as cal
from RevenueChaser.core import ledger, rawstore

INTRADAY = datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc)        # 12:00 ET on a trading day

SWING_PAYLOAD = {"candidates": [{"ticker": "AAA"}], "is_fixture": False}
FUND_PAYLOAD = {"candidates": [{"ticker": "BBB"}], "is_fixture": False}
AG_RESULT = {"cards": [{"ticker": "CCC", "total_points": 9}], "asof": "2026-10-02T16:00:00+00:00"}


@pytest.fixture
def order(monkeypatch, tmp_path):
    """Stub the scans, the renderers and the file writes; record the order of every step."""
    calls: list[str] = []
    from RevenueChaser.module_asymmetric_growth import engine as ag_engine, report as ag_report
    from RevenueChaser.module_fundamentals import pipeline as fu_pipeline, report as fu_report
    from RevenueChaser.module_swing_potential import pipeline as sw_pipeline, report as sw_report
    from RevenueChaser.core import ticker_store

    def step(name, result=None):
        def f(*a, **k):
            calls.append(name)
            return result
        return f

    monkeypatch.setattr(sw_pipeline, "run_scan", step("swing.scan", SWING_PAYLOAD))
    monkeypatch.setattr(sw_pipeline, "save_payload", step("swing.save"))
    monkeypatch.setattr(sw_report, "render_swing_board", step("swing.render", ("<html/>", None)))
    monkeypatch.setattr(fu_pipeline, "run_scan", step("fund.scan", FUND_PAYLOAD))
    monkeypatch.setattr(fu_pipeline, "save_payload", step("fund.save"))
    monkeypatch.setattr(fu_report, "render_fundamental_board", step("fund.render", ("<html/>", None)))
    monkeypatch.setattr(ag_engine, "run_asymmetric_growth_hunter", step("ag.scan", AG_RESULT))
    monkeypatch.setattr(ag_report, "render_asymmetric_growth_hunter_html", step("ag.render", "<html/>"))
    monkeypatch.setattr(ticker_store, "write_from_cards", step("ag.ticker_store"))
    monkeypatch.setattr(runners, "_write_output", lambda module, *a, **k: calls.append(f"{module}.write"))
    # K10 (#383 R3): Swing and Fundamentals write their HTML and rows file through ``_write_board``.
    monkeypatch.setattr(runners, "_write_board",
                        lambda path, *a, **k: calls.append(f"{path.stem.removesuffix('_latest')}.write"))
    # AG writes its page and its payload file together (S7, K10 #383 C6a).
    from RevenueChaser.core import reporting_common
    monkeypatch.setattr(reporting_common, "write_board", lambda *a, **k: calls.append("asymmetric_growth.board"))
    ledger.take_notes()
    return calls


@pytest.fixture
def freezers(monkeypatch, order):
    """Replace the three freeze functions with spies that record their call and arguments."""
    from RevenueChaser.module_asymmetric_growth import freeze as ag_freeze
    from RevenueChaser.module_fundamentals import freeze as fu_freeze
    from RevenueChaser.module_swing_potential import freeze as sw_freeze
    seen: dict[str, dict] = {}

    def spy(name, attr):
        def f(payload, **kw):
            order.append(f"{name}.freeze")
            seen[name] = {"payload": payload, **kw}
        return f

    monkeypatch.setattr(sw_freeze, "freeze_payload", spy("swing", "freeze_payload"))
    monkeypatch.setattr(fu_freeze, "freeze_payload", spy("fund", "freeze_payload"))
    monkeypatch.setattr(ag_freeze, "freeze_result", spy("ag", "freeze_result"))
    return seen


# ── a Fetch freezes, as the last step ────────────────────────────────────────

def test_run_swing_fetch_freezes_last_with_the_payload(order, freezers):
    runners.run_swing(None)
    assert order == ["swing.scan", "swing.save", "swing.render", "swing_potential.write", "swing.freeze"]
    assert freezers["swing"]["payload"] is SWING_PAYLOAD
    assert len(freezers["swing"]["run_id"]) == 12


def test_run_fundamentals_fetch_freezes_last_with_the_payload(order, freezers):
    runners.run_fundamentals(None)
    assert order == ["fund.scan", "fund.save", "fund.render", "fundamentals.write", "fund.freeze"]
    assert freezers["fund"]["payload"] is FUND_PAYLOAD


def test_run_asymmetric_growth_fetch_freezes_last_with_the_result(order, freezers):
    runners.run_asymmetric_growth(None)
    assert order == ["ag.scan", "asymmetric_growth.write", "ag.render", "asymmetric_growth.board",
                     "ag.ticker_store", "ag.freeze"]
    assert freezers["ag"]["payload"] is AG_RESULT


# ── a Refresh never freezes ──────────────────────────────────────────────────

@pytest.mark.parametrize("runner", ["run_swing", "run_fundamentals", "run_asymmetric_growth"])
def test_a_refresh_never_freezes(order, freezers, runner):
    getattr(runners, runner)(None, no_fetch=True)
    assert freezers == {}
    assert not [c for c in order if c.endswith(".freeze")]


@pytest.mark.parametrize("runner", ["run_swing", "run_fundamentals", "run_asymmetric_growth"])
def test_a_refresh_writes_no_ledger_rows_even_with_the_real_freezers(order, runner, monkeypatch):
    """The refresh-purity proof: with the real freeze functions and a post-close clock, no_fetch
    still leaves ``forecast_ledger`` untouched (spy on the store's writer)."""
    monkeypatch.setattr(cal, "_now", lambda: datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc))
    writes = []
    monkeypatch.setattr(rawstore, "write_rows", lambda name, rows: writes.append(name) or 0)
    getattr(runners, runner)(None, no_fetch=True)
    assert "forecast_ledger" not in writes
    assert ledger.take_notes() == []


# ── a freeze error is an error, a skipped freeze is a note ───────────────────

def test_a_freeze_exception_propagates_and_fails_the_job(order, monkeypatch):
    from RevenueChaser.module_swing_potential import freeze as sw_freeze

    def boom(payload, **kw):
        raise RuntimeError("ledger write failed")

    monkeypatch.setattr(sw_freeze, "freeze_payload", boom)
    with pytest.raises(RuntimeError, match="ledger write failed"):
        runners.run_swing(None)


def test_the_run_id_is_the_running_jobs_id(order, freezers, monkeypatch):
    monkeypatch.setattr(jobs, "_ensure_worker", lambda: None)
    monkeypatch.setattr(jobs, "_ensure_long_worker", lambda: None)
    jobs.register("t_swing_run_id", lambda uw, **p: runners.run_swing(uw), [], requires_uw=False)
    jid = jobs.submit("t_swing_run_id")
    jobs.execute(jid)
    assert jobs.get(jid)["status"] == "done"
    assert freezers["swing"]["run_id"] == jid


def test_a_universe_job_runs_all_three_families_and_its_notes_name_each(order, monkeypatch):
    """Intraday clock: every freeze is refused. A refusal must not stop the next family, so all
    three boards still run, and the job's notes carry one sentence per family."""
    monkeypatch.setattr(cal, "_now", lambda: INTRADAY)
    monkeypatch.setattr(jobs, "_ensure_worker", lambda: None)
    monkeypatch.setattr(jobs, "_ensure_long_worker", lambda: None)
    writes = []
    monkeypatch.setattr(rawstore, "write_rows", lambda name, rows: writes.append(name) or 0)
    jobs.register("t_universe_128", runners.run_universe_refresh, [], requires_uw=False, lane="long")
    jid = jobs.submit("t_universe_128")
    jobs.execute(jid)
    job = jobs.get(jid)
    assert job["status"] == "done", job
    assert {"ag.scan", "swing.scan", "fund.scan"} <= set(order)
    assert job["message"] is None
    notes = job["notes"]
    for label in ("AG not frozen", "Swing not frozen", "Fundamentals not frozen"):
        assert sum(label in n for n in notes) == 1
    assert sum("session window" in n for n in notes) == 3
    assert "forecast_ledger" not in writes


# ── a Fetch opens no popup: the freeze outcome is in ``notes``, never in ``message`` ─────────────────────
# The dashboard calls ``alert(j.message)`` for every finished job that has a message
# (``templates/dashboard.html``, ``poll``). On main these four jobs never have one: they are registered with
# ``requires_uw=False`` (``app/dashboard.py``), so there is no provider-gap message either.

_FETCH_JOBS = {          # job key -> (runner, lane), as ``app/dashboard.py`` registers them
    "swing": (runners.run_swing, "short"),
    "module_fundamentals": (runners.run_fundamentals, "short"),
    "module_asymmetric_growth": (runners.run_asymmetric_growth, "short"),
    "universe": (runners.run_universe_refresh, "long"),
}
_AFTER_CLOSE = datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc)      # 17:00 ET on a trading day


@pytest.mark.parametrize("clock", [INTRADAY, _AFTER_CLOSE], ids=["refused", "frozen"])
@pytest.mark.parametrize("key", sorted(_FETCH_JOBS))
def test_a_fetch_job_leaves_its_message_empty_so_the_dashboard_opens_no_alert(order, monkeypatch, key, clock):
    runner, lane = _FETCH_JOBS[key]
    monkeypatch.setattr(cal, "_now", lambda: clock)
    monkeypatch.setattr(jobs, "_ensure_worker", lambda: None)
    monkeypatch.setattr(jobs, "_ensure_long_worker", lambda: None)
    monkeypatch.setattr(rawstore, "write_rows", lambda name, rows: 0)
    monkeypatch.setattr(ledger, "_day_frozen", lambda *a, **k: False)
    monkeypatch.setattr(ledger, "payload_problem", lambda *a, **k: None)
    monkeypatch.setattr(ledger, "spy_anchor_problem", lambda session: None)
    monkeypatch.setattr(ledger, "ohlc_gaps", lambda session, tickers: {})
    jobs.register(f"t_{key}_popup", runner, [], requires_uw=False, lane=lane)
    jid = jobs.submit(f"t_{key}_popup")
    jobs.execute(jid)
    job = jobs.get(jid)
    assert job["status"] == "done", job
    assert job["message"] is None, "a message is what the dashboard shows with alert()"
    assert job["error"] is None
    assert job["notes"], "the freeze outcome is still recorded, in its own field"
    assert all(("Froze" in n or "not frozen" in n) for n in job["notes"])
