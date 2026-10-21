"""The nightly inputs CLI (C25a/K11, #383): step lookup, cap flags, exit codes and the report."""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from RevenueChaser.core import nightly_inputs as N
from RevenueChaser.scripts import nightly_inputs as cli

NIGHT = datetime(2026, 10, 13, 19, 30, tzinfo=ZoneInfo("America/New_York")).timestamp()
SEEN: list = []


def fake_sweeps():
    def ok(ctx):
        SEEN.append(("ok", {p: ctx.budget(p).limit for p in N.PROVIDERS}))
        return {"status": "ok", "written": 2}

    def bad(ctx):
        raise RuntimeError("boom")

    return (N.Sweep("A", ("short_interest",), ok), N.Sweep("B", ("short_interest",), bad))


def good_sweeps():
    return (N.Sweep("A", ("short_interest",), lambda ctx: {"status": "ok"}),)


@pytest.fixture(autouse=True)
def _steps(monkeypatch, tmp_path):
    SEEN.clear()
    monkeypatch.setattr(N, "STEPS", {"fake": f"{__name__}:fake_sweeps", "good": f"{__name__}:good_sweeps"})
    monkeypatch.setattr(N.config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(N.time, "time", lambda: NIGHT)


def test_an_unknown_step_exits_2_and_runs_nothing(capsys):
    assert cli.main(["nope"]) == 2
    assert "unknown step" in capsys.readouterr().err and SEEN == []


def test_a_negative_cap_exits_2(capsys):
    assert cli.main(["fake", "--max-uw-calls", "-1"]) == 2 and SEEN == []


def test_caps_default_to_no_calls():
    cli.main(["fake"])
    assert SEEN == [("ok", {"uw": 0, "fred": 0, "yfinance": 0, "cboe": 20, "cftc": 5})]


def test_the_caps_reach_the_sweeps():
    cli.main(["fake", "--max-uw-calls", "200", "--max-fred-calls", "10", "--max-yf-calls", "7"])
    assert SEEN == [("ok", {"uw": 200, "fred": 10, "yfinance": 7, "cboe": 20, "cftc": 5})]


def test_a_failed_sweep_exits_1_prints_json_and_writes_the_report(tmp_path, capsys):
    report = tmp_path / "r.json"
    assert cli.main(["fake", "--report", str(report)]) == 1
    out = capsys.readouterr()
    parsed = json.loads(out.out.strip().splitlines()[-1])
    assert parsed["sweeps"]["A"]["status"] == "ok" and parsed["sweeps"]["B"]["status"] == "failed"
    assert json.loads(report.read_text(encoding="utf-8"))["step"] == "fake"
    assert "inputs fake" in out.err


def test_every_sweep_ok_exits_0():
    assert cli.main(["good"]) == 0
