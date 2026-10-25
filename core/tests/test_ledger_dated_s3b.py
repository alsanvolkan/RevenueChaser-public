"""core.ledger — the dated-forecast writer (S3b, #38): the first non-screener family of ``forecast_ledger``.

A dated forecast is one row per (source, forecast_date, target_type) that resolves on a later session
(``target_date``), e.g. SPX Gamma's weekly three-class scenario. Same rules as the picks where they apply:
append-only, first write wins, only the latest complete session, never inside the US session window.
Offline; the store is a temp DuckDB.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import ledger, rawstore

D = date(2026, 10, 2)            # a Friday session
T = date(2026, 10, 9)            # the next Friday
AFTER_CLOSE = datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc)      # 17:00 ET, past close + 30 min
IN_SESSION = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)       # 11:00 ET
PROBS = {"Consolidation": 0.45, "Upside": 0.25, "Downside": 0.30}


def _row(**kw):
    row = {"subject": "SPX", "probs": PROBS, "shown_price": 7815.0}
    row.update(kw)
    return row


def _write(row=None, *, forecast_date=D, target_date=T, target_type="scenario_3class", run_id="r1", **kw):
    return ledger.write_dated("spx_gamma", "SPX scenario", row or _row(), target_type=target_type,
                              forecast_date=forecast_date, target_date=target_date,
                              model_version="spx-s3b-1+abcd1234", run_id=run_id, **kw)


def _stored():
    return rawstore.query("SELECT * FROM forecast_ledger WHERE source = 'spx_gamma'")


def test_write_dated_stores_one_row_with_its_target_type_and_dated_target(rawstore_tmp_db):
    out = _write(frozen_at=1_790_000_000.0)
    assert (out.frozen, out.reason, out.forecast_date) == (1, None, D)
    [r] = _stored()
    assert (r["source"], r["subject"], r["target_type"]) == ("spx_gamma", "SPX", "scenario_3class")
    assert (r["forecast_date"], r["target_date"]) == (D, T)
    assert r["model_version"] == "spx-s3b-1+abcd1234" and r["run_id"] == "r1"
    assert r["frozen_at"] == 1_790_000_000.0
    probs = r["probs"] if isinstance(r["probs"], dict) else json.loads(r["probs"])
    assert probs == PROBS
    assert r["shown_price"] == 7815.0
    assert r["rank"] is None and r["score"] is None and r["stop"] is None and r["target"] is None


def test_the_first_dated_write_of_a_day_wins(rawstore_tmp_db):
    assert _write().frozen == 1
    again = _write(_row(probs={"Consolidation": 0.1, "Upside": 0.8, "Downside": 0.1}), run_id="r2")
    assert again.frozen == 0 and "already frozen" in again.reason
    [r] = _stored()
    assert r["run_id"] == "r1"


def test_a_second_write_with_another_target_date_the_same_day_is_refused(rawstore_tmp_db):
    """First write wins per (source, day, target_type), not per row: a rerun cannot add a second target."""
    _write()
    assert _write(target_date=date(2026, 10, 16)).frozen == 0
    assert len(_stored()) == 1


def test_dated_rows_do_not_count_as_the_days_picks(rawstore_tmp_db):
    _write()
    out = ledger.write_picks("spx_gamma", "x", [{"subject": "AAA", "rank": 1}], model_version="v",
                             forecast_date=D, run_id="p")
    assert out.frozen == 1


@pytest.mark.parametrize("bad", [ledger.PICK, "", None])
def test_write_dated_refuses_the_pick_type_or_no_type(rawstore_tmp_db, bad):
    with pytest.raises(ValueError):
        _write(target_type=bad)
    assert _stored() == []


@pytest.mark.parametrize("target", [D, date(2026, 10, 1)])
def test_write_dated_refuses_a_target_on_or_before_the_forecast_day(rawstore_tmp_db, target):
    with pytest.raises(ValueError):
        _write(target_date=target)
    assert _stored() == []


def test_write_dated_refuses_text_dates(rawstore_tmp_db):
    with pytest.raises(TypeError):
        _write(forecast_date="2026-10-02")
    with pytest.raises(TypeError):
        _write(target_date="2026-10-09")


def test_write_dated_needs_a_subject(rawstore_tmp_db):
    out = _write(_row(subject=" "))
    assert out.frozen == 0 and "no usable" in out.reason
    assert _stored() == []


# ── freeze_dated: the session rule ────────────────────────────────────────────

def _freeze(forecast_date=D, now=AFTER_CLOSE, problem=None):
    return ledger.freeze_dated("spx_gamma", "SPX scenario", _row(), target_type="scenario_3class",
                               forecast_date=forecast_date, target_date=T, model_version="m",
                               run_id="r", now=now, problem=problem)


def test_freeze_dated_writes_after_the_close_of_the_forecast_session(rawstore_tmp_db):
    ledger.take_notes()
    out = _freeze()
    assert out.frozen == 1
    assert ledger.take_notes() == [out.message]
    assert "Froze 1 SPX scenario rows for 2026-10-02" in out.message


def test_freeze_dated_never_runs_inside_the_session_window(rawstore_tmp_db):
    out = _freeze(now=IN_SESSION)
    assert out.frozen == 0 and "session window" in out.reason
    assert _stored() == []


def test_freeze_dated_refuses_a_day_that_is_not_the_latest_complete_session(rawstore_tmp_db):
    out = _freeze(forecast_date=date(2026, 10, 1))
    assert out.frozen == 0 and "not the latest complete session" in out.reason
    assert _stored() == []


def test_freeze_dated_refuses_on_a_payload_problem(rawstore_tmp_db):
    out = _freeze(problem="the scenario set has no bands")
    assert out.frozen == 0 and out.reason == "the scenario set has no bands"
    assert _stored() == []
