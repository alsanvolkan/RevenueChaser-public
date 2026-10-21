"""The daily bars CLI (#156): exit codes, the refuse-without-confirm guard and JSON output."""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import prices
from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.scripts import daily_bars


def test_fill_without_confirm_refuses_and_writes_nothing(rawstore_tmp_db, monkeypatch):
    called = []
    monkeypatch.setattr(prices, "fill_history", lambda *a, **k: called.append(1) or {})
    assert daily_bars.main(["--fill"]) == 2
    assert called == []
    assert daily_bars.main(["--fill", "--tickers", "AAPL"]) == 2


def test_nightly_verify_failure_exits_1_and_writes_nothing(rawstore_tmp_db, monkeypatch):
    class Bad:
        auth_error = None

        def verify(self):
            return (False, "missing UNUSUAL_WHALES_API_KEY")

        def close(self):
            pass

    monkeypatch.setattr(unusual_whales, "UWClient", Bad)
    called = []
    monkeypatch.setattr(prices, "ensure_daily_bars", lambda *a, **k: called.append(1) or {})
    assert daily_bars.main(["--nightly"]) == 1
    assert called == []


def test_fill_confirm_prints_one_json_line(rawstore_tmp_db, monkeypatch, capsys):
    res = prices._blank_result("rid")
    res["latest_session"] = "2026-10-08"
    monkeypatch.setattr(prices, "fill_history", lambda *a, **k: res)
    rc = daily_bars.main(["--fill", "--confirm"])
    assert rc == 0
    out = capsys.readouterr()
    parsed = json.loads(out.out.strip().splitlines()[-1])
    assert parsed["run_id"] == "rid" and parsed["latest_session"] == "2026-10-08"
    assert "Daily bars" in out.err


def test_nightly_success_exit_0_and_report_written(rawstore_tmp_db, monkeypatch, tmp_path):
    class Good:
        auth_error = None

        def verify(self):
            return (True, None)

        def close(self):
            pass

    monkeypatch.setattr(unusual_whales, "UWClient", Good)
    res = prices._blank_result("rid")
    res["latest_session"] = "2026-10-08"
    seen = {}

    def fake_ensure(tickers, **kw):
        seen.update(kw)
        return res

    monkeypatch.setattr(prices, "ensure_daily_bars", fake_ensure)
    report = tmp_path / "res.json"
    rc = daily_bars.main(["--nightly", "--max-uw-calls", "5", "--report", str(report)])
    assert rc == 0
    assert seen["max_uw_calls"] == 5
    assert json.loads(report.read_text(encoding="utf-8"))["run_id"] == "rid"


def test_a_locked_run_exits_0(rawstore_tmp_db, monkeypatch):
    class Good:
        auth_error = None

        def verify(self):
            return (True, None)

        def close(self):
            pass

    monkeypatch.setattr(unusual_whales, "UWClient", Good)
    res = prices._blank_result("rid")
    res["locked"] = True
    monkeypatch.setattr(prices, "ensure_daily_bars", lambda *a, **k: res)
    assert daily_bars.main(["--nightly"]) == 0


def test_an_error_result_exits_1(rawstore_tmp_db, monkeypatch):
    class Good:
        auth_error = None

        def verify(self):
            return (True, None)

        def close(self):
            pass

    monkeypatch.setattr(unusual_whales, "UWClient", Good)
    res = prices._blank_result("rid")
    res["errors"] = ["failed_both"]
    monkeypatch.setattr(prices, "ensure_daily_bars", lambda *a, **k: res)
    assert daily_bars.main(["--nightly"]) == 1


def test_an_unreadable_tickers_file_exits_2_and_never_fills_the_whole_scope(rawstore_tmp_db, monkeypatch,
                                                                           tmp_path):
    called = []
    monkeypatch.setattr(prices, "fill_history", lambda *a, **k: called.append(a) or prices._blank_result("r"))
    rc = daily_bars.main(["--fill", "--confirm", "--tickers-file", str(tmp_path / "missing.txt")])
    assert rc == 2
    assert called == []


@pytest.mark.parametrize("arg", ["CL=F", "EURUSD=X", "A B", "TOOLONGSYMBOL1", "-X", ".X"])
def test_n13_a_non_equity_symbol_exits_2_and_never_fills(rawstore_tmp_db, monkeypatch, arg, capsys):
    called = []
    monkeypatch.setattr(prices, "fill_history", lambda *a, **k: called.append(a) or prices._blank_result("r"))
    rc = daily_bars.main(["--fill", "--confirm", "--tickers", f"AAPL,{arg}"])
    assert rc == 2 and called == []
    assert arg.upper() in capsys.readouterr().err


def test_n13b_equity_symbols_and_index_levels_pass(rawstore_tmp_db, monkeypatch):
    got = []
    monkeypatch.setattr(prices, "fill_history", lambda t, **k: got.append(t) or prices._blank_result("r"))
    assert daily_bars.main(["--fill", "--confirm", "--tickers", "brk.b,^GSPC,BF-B"]) == 0
    assert got == [["BRK.B", "^GSPC", "BF-B"]]


# --- the SEC-file step (#301): a best-effort first step of --nightly ------------------------------------------------

class _GoodUW:
    auth_error = None

    def verify(self):
        return (True, None)

    def close(self):
        pass


def _bars(monkeypatch, order, errors=None):
    res = prices._blank_result("rid")
    res["errors"] = list(errors or [])
    monkeypatch.setattr(unusual_whales, "UWClient", _GoodUW)
    monkeypatch.setattr(prices, "ensure_daily_bars", lambda *a, **k: order.append(("bars", k["run_id"])) or res)


def test_a_failing_sec_step_leaves_the_daily_bars_exit_at_0(rawstore_tmp_db, monkeypatch, capsys):
    from RevenueChaser.core import sec_files

    order = []
    _bars(monkeypatch, order)

    def boom(**kw):
        order.append(("sec", kw["run_id"]))
        raise RuntimeError("SEC is down")

    monkeypatch.setattr(sec_files, "nightly", boom)
    assert daily_bars.main(["--nightly", "--sec-files"]) == 0
    assert [o[0] for o in order] == ["sec", "bars"] and order[0][1] == order[1][1]     # first, one run id
    out = capsys.readouterr()
    sec = json.loads(out.out.strip().splitlines()[-1])["sec_files"]
    assert sec["status"] == "failed" and "SEC is down" in sec["error"]
    assert "SEC files:" in out.err


def test_a_bars_error_still_exits_1_with_a_good_sec_step(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.core import sec_files

    order = []
    _bars(monkeypatch, order, errors=["failed_both"])
    monkeypatch.setattr(sec_files, "nightly", lambda **kw: order.append(("sec", None)) or {"status": "ok"})
    assert daily_bars.main(["--nightly", "--sec-files"]) == 1


@pytest.mark.parametrize("argv", [["--nightly"], ["--nightly", "--sec-files", "--tickers", "AAPL"],
                                  ["--nightly", "--sec-files", "--no-fetch"]])
def test_the_sec_step_runs_only_on_a_whole_scope_nightly_with_the_flag(rawstore_tmp_db, monkeypatch, argv):
    from RevenueChaser.core import sec_files

    order = []
    _bars(monkeypatch, order)
    monkeypatch.setattr(sec_files, "nightly", lambda **kw: order.append(("sec", None)) or {"status": "ok"})
    assert daily_bars.main(argv) == 0
    assert [o[0] for o in order] == ["bars"]


def test_the_sec_step_runs_before_a_failed_uw_verify(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.core import sec_files

    class Bad(_GoodUW):
        def verify(self):
            return (False, "missing UNUSUAL_WHALES_API_KEY")

    monkeypatch.setattr(unusual_whales, "UWClient", Bad)
    asked = []
    monkeypatch.setattr(sec_files, "nightly", lambda **kw: asked.append(1) or {"status": "ok"})
    assert daily_bars.main(["--nightly", "--sec-files"]) == 1
    assert asked == [1]


def test_fill_refuses_the_sec_flag(rawstore_tmp_db, monkeypatch):
    called = []
    monkeypatch.setattr(prices, "fill_history", lambda *a, **k: called.append(1) or prices._blank_result("r"))
    assert daily_bars.main(["--fill", "--confirm", "--sec-files"]) == 2
    assert called == []


# --- the ^GSPC daily levels (D03, #293, M-SPX-0 #376): a best-effort last step of --nightly, and the one-time fill ---

def _index(monkeypatch, order, result=None, boom=None):
    from RevenueChaser.core import index_levels

    def fake(**kw):
        order.append(("index", kw.get("run_id")))
        if boom:
            raise RuntimeError(boom)
        return dict(result or {"status": "ok", "requests": 1, "rows_inserted": 1, "latest_session": "2026-10-08",
                               "newest_after": "2026-10-08"})

    monkeypatch.setattr(index_levels, "nightly", fake)


def test_the_index_step_runs_after_the_bars_with_one_run_id_and_rides_in_the_json(rawstore_tmp_db, monkeypatch,
                                                                                capsys):
    from RevenueChaser.core import sec_files

    order = []
    _bars(monkeypatch, order)
    monkeypatch.setattr(sec_files, "nightly", lambda **kw: order.append(("sec", kw["run_id"])) or {"status": "ok"})
    _index(monkeypatch, order)
    assert daily_bars.main(["--nightly", "--sec-files", "--index-levels"]) == 0
    assert [o[0] for o in order] == ["sec", "bars", "index"]
    assert len({o[1] for o in order}) == 1                                      # one run id for the night
    out = capsys.readouterr()
    assert json.loads(out.out.strip().splitlines()[-1])["index_levels"]["status"] == "ok"
    assert "Index levels" in out.err


def test_a_failing_index_step_leaves_the_daily_bars_exit_at_0(rawstore_tmp_db, monkeypatch, capsys):
    order = []
    _bars(monkeypatch, order)
    _index(monkeypatch, order, boom="Yahoo is down")
    assert daily_bars.main(["--nightly", "--index-levels"]) == 0
    idx = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["index_levels"]
    assert idx["status"] == "failed" and "Yahoo is down" in idx["error"]


def test_a_failed_index_status_leaves_the_exit_at_0(rawstore_tmp_db, monkeypatch):
    order = []
    _bars(monkeypatch, order)
    _index(monkeypatch, order, result={"status": "failed", "error": "empty download", "requests": 1})
    assert daily_bars.main(["--nightly", "--index-levels"]) == 0


def test_a_bars_error_still_exits_1_with_a_good_index_step(rawstore_tmp_db, monkeypatch):
    order = []
    _bars(monkeypatch, order, errors=["failed_both"])
    _index(monkeypatch, order)
    assert daily_bars.main(["--nightly", "--index-levels"]) == 1
    assert [o[0] for o in order] == ["bars", "index"]


@pytest.mark.parametrize("argv", [["--nightly"], ["--nightly", "--index-levels", "--tickers", "AAPL"],
                                  ["--nightly", "--index-levels", "--no-fetch"]])
def test_the_index_step_runs_only_on_a_whole_scope_nightly_with_the_flag(rawstore_tmp_db, monkeypatch, argv):
    order = []
    _bars(monkeypatch, order)
    _index(monkeypatch, order)
    assert daily_bars.main(argv) == 0
    assert [o[0] for o in order] == ["bars"]


def test_the_index_step_still_runs_after_a_failed_uw_verify(rawstore_tmp_db, monkeypatch, capsys):
    """The step needs no UW key: an expired key stops the bars (exit 1), not the SPX levels."""
    class Bad(_GoodUW):
        def verify(self):
            return (False, "missing UNUSUAL_WHALES_API_KEY")

    monkeypatch.setattr(unusual_whales, "UWClient", Bad)
    bars = []
    monkeypatch.setattr(prices, "ensure_daily_bars", lambda *a, **k: bars.append(1) or {})
    order = []
    _index(monkeypatch, order)
    assert daily_bars.main(["--nightly", "--index-levels"]) == 1
    assert bars == [] and [o[0] for o in order] == ["index"]
    assert "Index levels" in capsys.readouterr().err


def test_the_scheduled_command_runs_the_index_step_once(rawstore_tmp_db, monkeypatch):
    """The RC-Daily-Bars registry args, as the task runs them, reach the step."""
    from RevenueChaser.core import automations, sec_files

    order = []
    _bars(monkeypatch, order)
    monkeypatch.setattr(sec_files, "nightly", lambda **kw: order.append(("sec", None)) or {"status": "ok"})
    _index(monkeypatch, order)
    args = automations.entry("RC-Daily-Bars").args
    assert args[:2] == ("-m", "RevenueChaser.scripts.daily_bars")
    assert daily_bars.main(list(args[2:])) == 0
    assert [o[0] for o in order] == ["sec", "bars", "index"]


class _NoUW:
    def __init__(self, *a, **k):
        raise AssertionError("the index fill needs no UW client")


def _refuse(what):
    def boom(*a, **k):
        raise AssertionError(f"the index fill must not run the {what}")
    return boom


def _index_fill(monkeypatch, result=None):
    from RevenueChaser.core import index_levels

    calls = []
    monkeypatch.setattr(index_levels, "fill", lambda **kw: calls.append(kw) or dict(
        result or {"status": "ok", "requests": 1, "rows_inserted": 3, "latest_session": "2026-10-08"}))
    monkeypatch.setattr(unusual_whales, "UWClient", _NoUW)
    monkeypatch.setattr(prices, "fill_history", _refuse("D01 fill"))
    monkeypatch.setattr(prices, "ensure_daily_bars", _refuse("bars nightly"))
    return calls


def test_index_fill_without_confirm_refuses_and_asks_nothing(rawstore_tmp_db, monkeypatch):
    calls = _index_fill(monkeypatch)
    assert daily_bars.main(["--index-fill"]) == 2
    assert calls == []


def test_index_fill_confirm_runs_the_one_fill_and_prints_one_json_line(rawstore_tmp_db, monkeypatch, capsys):
    calls = _index_fill(monkeypatch)
    assert daily_bars.main(["--index-fill", "--confirm"]) == 0
    assert len(calls) == 1 and calls[0].get("run_id")
    out = capsys.readouterr()
    assert json.loads(out.out.strip().splitlines()[-1])["rows_inserted"] == 3
    assert "Index levels" in out.err


def test_a_failed_index_fill_exits_1(rawstore_tmp_db, monkeypatch):
    _index_fill(monkeypatch, result={"status": "failed", "error": "SPY's D01 fill is not done", "requests": 0})
    assert daily_bars.main(["--index-fill", "--confirm"]) == 1


@pytest.mark.parametrize("extra", [["--tickers", "SPY"], ["--sec-files"], ["--index-levels"], ["--no-fetch"],
                                   ["--nightly"], ["--fill"]])
def test_index_fill_refuses_any_other_mode_or_flag(rawstore_tmp_db, monkeypatch, extra):
    calls = _index_fill(monkeypatch)
    assert daily_bars.main(["--index-fill", "--confirm", *extra]) == 2
    assert calls == []


def test_fill_refuses_the_index_flag(rawstore_tmp_db, monkeypatch):
    called = []
    monkeypatch.setattr(prices, "fill_history", lambda *a, **k: called.append(1) or prices._blank_result("r"))
    assert daily_bars.main(["--fill", "--confirm", "--index-levels"]) == 2
    assert called == []
