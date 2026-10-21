"""The index lists CLI (#302): the RC-Index-Lists task's command, its exit codes and the sp600 first-fill flag."""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import index_registry as ir
from RevenueChaser.scripts import index_lists


def _result(statuses: dict, locked: bool = False) -> dict:
    return {"run_id": "r1", "locked": locked, "slugs": {
        s: {"status": st, "members": 500, "floor": 400, "appended": 0} for s, st in statuses.items()}}


@pytest.fixture
def calls(monkeypatch):
    seen: list[dict] = []
    box = {"res": _result({"sp500": "written", "sp600": "awaiting_first_fill"})}
    monkeypatch.setattr(ir, "refresh_lists",
                        lambda run_id, first_fill=(): seen.append({"run_id": run_id, "first_fill": list(first_fill)})
                        or box["res"])
    return seen, box


def test_weekly_runs_every_list_without_the_first_fill(calls, capsys):
    seen, _ = calls
    assert index_lists.main(["--weekly"]) == 0
    assert len(seen) == 1 and seen[0]["first_fill"] == [] and seen[0]["run_id"]
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["slugs"]["sp500"]["status"] == "written"


def test_the_first_fill_flag_names_sp600(calls):
    seen, _ = calls
    assert index_lists.main(["--weekly", "--first-fill", "sp600"]) == 0
    assert seen[0]["first_fill"] == ["sp600"]


def test_a_first_fill_of_another_slug_is_refused(calls):
    seen, _ = calls
    assert index_lists.main(["--weekly", "--first-fill", "sp500"]) == 2
    assert seen == []


def test_no_mode_is_refused(calls):
    seen, _ = calls
    assert index_lists.main([]) == 2 and seen == []


@pytest.mark.parametrize("status", ["below_floor", "error"])
def test_a_list_under_its_floor_or_failing_exits_1(calls, status):
    _, box = calls
    box["res"] = _result({"sp500": status})
    assert index_lists.main(["--weekly"]) == 1


def test_a_running_copy_exits_0(calls):
    _, box = calls
    box["res"] = _result({}, locked=True)
    assert index_lists.main(["--weekly"]) == 0
