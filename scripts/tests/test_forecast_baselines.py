"""The forecast baselines CLI (#25 HR-1): the RC-Forecast-Baselines task's command, its modes and exit codes."""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import baselines, rawstore
from RevenueChaser.scripts import forecast_baselines


def _result(**kw) -> dict:
    res = {"run_id": "r1", "freeze_dates": 2, "dates": 1, "rows": 11, "written": 11, "status": {"open": 11},
           "seconds": 0.1}
    res.update(kw)
    return res


@pytest.fixture
def calls(monkeypatch):
    seen: list[dict] = []
    box = {"res": _result()}

    def fake_run(*, full=False, write=True, run_id=None, now=None):
        seen.append({"full": full, "write": write, "run_id": run_id})
        if isinstance(box["res"], Exception):
            raise box["res"]
        return box["res"]

    monkeypatch.setattr(baselines, "run", fake_run)
    return seen, box


def test_nightly_recomputes_the_rows_not_yet_closed_and_writes(calls, capsys):
    seen, _ = calls
    assert forecast_baselines.main(["--nightly"]) == 0
    assert seen == [{"full": False, "write": True, "run_id": seen[0]["run_id"]}] and seen[0]["run_id"]
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["rows"] == 11


def test_full_recomputes_every_date(calls):
    seen, _ = calls
    assert forecast_baselines.main(["--full"]) == 0
    assert seen[0]["full"] is True and seen[0]["write"] is True


def test_dry_run_writes_nothing(calls):
    seen, _ = calls
    assert forecast_baselines.main(["--full", "--dry-run"]) == 0
    assert seen[0]["write"] is False


def test_a_mode_is_required(calls):
    seen, _ = calls
    assert forecast_baselines.main([]) == 2
    assert forecast_baselines.main(["--nightly", "--full"]) == 2
    assert seen == []


def test_under_a_scheduled_task_the_run_id_is_the_tasks_job_runs_id(calls, monkeypatch):
    seen, _ = calls
    monkeypatch.setenv(rawstore.RUN_ID_ENV, "task42")
    assert forecast_baselines.main(["--nightly"]) == 0
    assert seen[0]["run_id"] == "task42"


def test_an_error_exits_1_with_the_reason(calls, capsys):
    _, box = calls
    box["res"] = RuntimeError("store locked")
    assert forecast_baselines.main(["--nightly"]) == 1
    assert "store locked" in capsys.readouterr().err


def test_report_writes_the_result_json(calls, tmp_path):
    out = tmp_path / "r.json"
    assert forecast_baselines.main(["--nightly", "--report", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["written"] == 11


def test_the_real_run_end_to_end_on_an_empty_store(rawstore_tmp_db, capsys):
    """No mock: an empty ledger is a clean exit 0 with no freeze date and zero rows."""
    assert forecast_baselines.main(["--nightly"]) == 0
    res = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert (res["freeze_dates"], res["rows"]) == (0, 0)
