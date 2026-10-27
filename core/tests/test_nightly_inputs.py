"""Nightly inputs steps (C25a/K11, #383): independent sweeps, one session per run, per-night call caps.

Nothing here makes a request: the UW client gets a fake transport, and the sweeps are plain functions.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from RevenueChaser.core import nightly_inputs as N
from RevenueChaser.core import rawstore

NY = ZoneInfo("America/New_York")
# 19:30 ET on Tuesday 2026-10-13 = 01:30 Amsterdam on Wednesday: the complete session of the 13th.
NIGHT = datetime(2026, 10, 13, 19, 30, tzinfo=NY).timestamp()
NEXT_NIGHT = datetime(2026, 10, 14, 19, 30, tzinfo=NY).timestamp()
INTRADAY = datetime(2026, 10, 13, 14, 0, tzinfo=NY).timestamp()
CAPS = {"uw": 0, "fred": 0, "yfinance": 0}


@pytest.fixture
def lines():
    return []


def _run(sweeps, lines, *, caps=None, run_at=NIGHT, tmp_path=None, **kw):
    return N.run_step("test", sweeps, caps=caps or CAPS, run_id="r1", run_at=run_at, log=lines.append,
                      ledger_dir=tmp_path, **kw)


def _ok(name, seen):
    def run(ctx):
        seen.append((name, ctx.session_date, ctx.run_id))
        return {"status": "ok", "written": 1}
    return N.Sweep(name, ("short_interest",), run)


# --- sweep isolation ----------------------------------------------------------------------------------------------

def test_one_raising_sweep_leaves_the_others_running(lines, tmp_path):
    seen = []

    def boom(ctx):
        raise RuntimeError("provider shape changed")

    res = _run((_ok("A", seen), N.Sweep("B", ("short_interest",), boom), _ok("C", seen)), lines, tmp_path=tmp_path)
    assert [s[0] for s in seen] == ["A", "C"]
    assert res["sweeps"]["A"]["status"] == "ok" and res["sweeps"]["C"]["status"] == "ok"
    assert res["sweeps"]["B"]["status"] == "failed"
    assert "provider shape changed" in res["sweeps"]["B"]["error"]
    assert N.exit_code(res) == 1
    assert any("B failed" in ln for ln in lines)


def test_every_sweep_ok_exits_0_and_a_capped_sweep_is_not_a_failure(lines, tmp_path):
    res = _run((N.Sweep("A", ("short_interest",), lambda ctx: {"status": "partial", "note": "call_cap"}),
                N.Sweep("B", ("short_interest",), lambda ctx: {"status": "skipped"})), lines, tmp_path=tmp_path)
    assert N.exit_code(res) == 0


def test_a_sweep_that_returns_no_result_is_failed(lines, tmp_path):
    res = _run((N.Sweep("A", ("short_interest",), lambda ctx: None),), lines, tmp_path=tmp_path)
    assert res["sweeps"]["A"]["status"] == "failed" and N.exit_code(res) == 1


# --- one session per run (rule B) -----------------------------------------------------------------------------------

def test_sweeps_get_the_run_session_and_run_id(lines, tmp_path):
    seen = []
    res = _run((_ok("A", seen),), lines, tmp_path=tmp_path)
    assert seen == [("A", date(2026, 10, 13), "r1")]
    assert res["session_date"] == "2026-10-13" and res["run_id"] == "r1"


def test_an_intraday_run_runs_no_sweep_spends_nothing_and_exits_0(lines, tmp_path):
    seen = []
    res = _run((_ok("A", seen),), lines, run_at=INTRADAY, tmp_path=tmp_path)
    assert seen == [] and res["skipped"] == "intraday" and N.exit_code(res) == 0
    assert not list(tmp_path.glob("*.json"))


def test_a_run_outside_the_holiday_table_exits_1_and_runs_nothing(lines, tmp_path):
    seen = []
    res = _run((_ok("A", seen),), lines, run_at=datetime(2031, 3, 4, 20, 0, tzinfo=NY).timestamp(),
               tmp_path=tmp_path)
    assert seen == [] and res["error"] and N.exit_code(res) == 1


def test_the_run_id_is_the_task_job_runs_id_under_the_scheduler(monkeypatch, lines, tmp_path):
    monkeypatch.setenv(rawstore.RUN_ID_ENV, "task42")
    res = N.run_step("test", (), caps=CAPS, run_at=NIGHT, log=lines.append, ledger_dir=tmp_path)
    assert res["run_id"] == "task42"


# --- call caps -------------------------------------------------------------------------------------------------------

def test_a_budget_refuses_past_its_limit_and_says_so_once():
    told = []
    b = N.CallBudget("uw", 2, log=told.append)
    assert [b.take() for _ in range(4)] == [True, True, False, False]
    assert b.as_dict() == {"limit": 2, "before": 0, "used": 2, "refused": 2}
    assert len(told) == 1 and "uw" in told[0] and "2" in told[0]


def test_a_zero_budget_makes_no_call():
    b = N.CallBudget("fred", 0)
    assert not b.take() and b.left == 0


def test_a_negative_cap_is_refused():
    with pytest.raises(ValueError):
        N.CallBudget("uw", -1)


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.text, self.headers = status, body, "", {}

    def json(self):
        return self._body if self._body is not None else {"data": []}


class _Transport:
    def __init__(self, status=200):
        self.urls, self.status = [], status

    def get(self, url, params=None):
        self.urls.append(url)
        return _Resp(self.status)

    def close(self):
        pass


def _client(limit, *, key="k", status=200, log=None):
    c = N.CappedUWClient(N.CallBudget("uw", limit, log=log), key=key)
    c._client = _Transport(status)
    return c


def test_the_capped_uw_client_sends_no_request_past_the_cap():
    told = []
    c = _client(2, log=told.append)
    out = [c.get_result(f"/x/{i}") for i in range(3)]
    assert [r.status for r in out] == ["ok", "ok", "failed"]
    assert out[2].error["error"] == "call_cap" and out[2].error["attempts"] == 0
    assert len(c._client.urls) == 2 and c.health()["calls"] == 2 and len(told) == 1


def test_the_cap_counts_every_chunk_of_a_batched_endpoint():
    c = _client(2)
    res = c._batched("/short_screener", [f"T{i}" for i in range(250)], "tickers", {}, ("symbol", "ticker"))
    assert res.status == "partial" and len(c._client.urls) == 2


def test_the_capped_uw_client_without_a_key_sends_nothing(monkeypatch):
    from RevenueChaser.core import credentials
    monkeypatch.setattr(credentials, "key_for", lambda provider: None)
    c = _client(5, key=None)
    r = c.get_result("/x")
    assert r.status == "failed" and r.error["error"] == "missing_key" and c._client.urls == []
    assert c.budget.used == 0


def test_the_capped_uw_client_stops_after_the_key_is_rejected():
    c = _client(5, status=401)
    assert c.get_result("/a").status == "failed"
    r = c.get_result("/b")
    assert r.error["error"] == "auth_rejected" and len(c._client.urls) == 1 and c.budget.used == 1


def test_the_step_shares_one_uw_client_and_budget_across_its_sweeps(lines, tmp_path):
    transport, built = _Transport(), []

    def factory(budget):
        c = N.CappedUWClient(budget, key="k")
        c._client = transport
        built.append(c)
        return c

    def sweep(ctx):
        uw = ctx.uw()
        got = [uw.get_result("/x").status for _ in range(2)]
        return {"status": "ok" if "failed" not in got else "partial", "got": got}

    res = _run((N.Sweep("A", ("short_interest",), sweep), N.Sweep("B", ("short_interest",), sweep)), lines,
               caps={"uw": 3, "fred": 0, "yfinance": 0}, tmp_path=tmp_path, uw_factory=factory)
    assert res["sweeps"]["A"]["got"] == ["ok", "ok"] and res["sweeps"]["B"]["got"] == ["ok", "failed"]
    assert res["calls"]["uw"] == {"limit": 3, "before": 0, "used": 3, "refused": 1}
    assert len(built) == 1 and len(transport.urls) == 3


def _spend(n):
    def run(ctx):
        b = ctx.budget("uw")
        return {"status": "ok", "taken": sum(1 for _ in range(n) if b.take())}
    return N.Sweep("A", ("short_interest",), run)


def test_the_cap_is_per_night_across_two_runs_of_the_same_session(lines, tmp_path):
    caps = {"uw": 3, "fred": 0, "yfinance": 0}
    first = _run((_spend(2),), lines, caps=caps, tmp_path=tmp_path)
    second = _run((_spend(2),), lines, caps=caps, tmp_path=tmp_path)
    assert first["sweeps"]["A"]["taken"] == 2 and second["sweeps"]["A"]["taken"] == 1
    assert second["calls"]["uw"]["before"] == 2
    third = _run((_spend(2),), lines, caps=caps, run_at=NEXT_NIGHT, tmp_path=tmp_path)
    assert third["sweeps"]["A"]["taken"] == 2


def test_an_unreadable_call_ledger_starts_the_night_at_zero(lines, tmp_path):
    (tmp_path / "test_calls.json").write_text("{not json", encoding="utf-8")
    res = _run((_spend(2),), lines, caps={"uw": 3, "fred": 0, "yfinance": 0}, tmp_path=tmp_path)
    assert res["sweeps"]["A"]["taken"] == 2
    assert json.loads((tmp_path / "test_calls.json").read_text(encoding="utf-8"))["spent"]["uw"] == 2


def _ledger_files(tmp_path):
    return sorted(f.name for f in tmp_path.iterdir())


def _interrupt_the_write(monkeypatch):
    """Every way a ledger write can land: a plain write_text dies half way, and so does the final rename."""
    real_write_text = Path.write_text

    def half_write(self, data, *a, **kw):
        real_write_text(self, data[: len(data) // 2], *a, **kw)
        raise OSError("disk full")

    def no_replace(src, dst):
        raise OSError("interrupted before the rename")

    monkeypatch.setattr(Path, "write_text", half_write)
    monkeypatch.setattr(os, "replace", no_replace)


def test_an_interrupted_ledger_write_leaves_the_previous_ledger_intact(lines, tmp_path, monkeypatch):
    caps = {"uw": 10, "fred": 0, "yfinance": 0}
    _run((_spend(2),), lines, caps=caps, tmp_path=tmp_path)
    ledger_file = tmp_path / "test_calls.json"
    before = ledger_file.read_text(encoding="utf-8")
    _interrupt_the_write(monkeypatch)
    _run((_spend(2),), lines, caps=caps, tmp_path=tmp_path)
    monkeypatch.undo()
    assert ledger_file.read_text(encoding="utf-8") == before
    assert json.loads(before)["spent"]["uw"] == 2
    assert any("call ledger not saved" in ln for ln in lines)
    assert _ledger_files(tmp_path) == ["test_calls.json"]


def test_a_saved_ledger_leaves_no_temp_file_behind(lines, tmp_path):
    _run((_spend(1),), lines, caps={"uw": 3, "fred": 0, "yfinance": 0}, tmp_path=tmp_path)
    assert _ledger_files(tmp_path) == ["test_calls.json"]


def test_a_cap_for_an_unknown_provider_is_refused(lines, tmp_path):
    with pytest.raises(ValueError):
        _run((), lines, caps={"uw": 1, "fred": 0, "yfinance": 0, "sec": 2}, tmp_path=tmp_path)


# --- first write wins -----------------------------------------------------------------------------------------------

def _si(value):
    return {"ticker": "AAPL", "session_date": date(2026, 10, 13), "short_interest": value, "source": "uw_short_screener",
            "fetched_at": 1.0, "run_id": "r1", "run_at": 1.0}


def test_record_first_keeps_the_first_write_of_a_key(rawstore_tmp_db):
    assert N.record_first("short_interest", [_si(5.0)]) == {"offered": 1, "written": 1}
    assert N.record_first("short_interest", [_si(9.0)]) == {"offered": 1, "written": 0}
    rows = rawstore.query("SELECT short_interest FROM short_interest")
    assert rows == [{"short_interest": 5.0}]


def test_record_first_refuses_a_table_that_is_not_append_only(rawstore_tmp_db):
    with pytest.raises(ValueError):
        N.record_first("equity_universe", [{"ticker": "AAPL"}])


def test_the_summary_line_names_each_sweep_and_the_calls(lines, tmp_path):
    res = _run((N.Sweep("A", ("short_interest",), lambda ctx: {"status": "ok", "written": 3}),), lines,
               caps={"uw": 2, "fred": 0, "yfinance": 0}, tmp_path=tmp_path)
    text = N.summary_line(res)
    assert "test" in text and "2026-10-13" in text and "A ok" in text and "uw 0/2" in text
