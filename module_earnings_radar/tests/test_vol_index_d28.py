"""E0e, D28 (#305, the Cboe part): the daily vol indices and MOVE, against fake providers.

No request leaves the process: Cboe goes through a monkeypatched ``httpx.get`` (or an injected ``fetch``) and
``^MOVE`` through an injected ``download`` that returns a flat frame, the shape one ticker's ``yf.download`` gives.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from RevenueChaser.core import nightly_inputs as N
from RevenueChaser.core import rawstore
from RevenueChaser.module_earnings_radar import inputs
from RevenueChaser.module_earnings_radar.inputs import vol_index as d28

NY = ZoneInfo("America/New_York")
NIGHT = datetime(2026, 10, 13, 19, 30, tzinfo=NY).timestamp()        # session 2026-10-13, complete
INTRADAY = datetime(2026, 10, 14, 11, 0, tzinfo=NY).timestamp()

DATES = [date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13)]


class _Resp:
    def __init__(self, status, text):
        self.status_code, self.text = status, text


def _csv(dates=DATES, *, fmt="%m/%d/%Y", base=20.0) -> str:
    lines = ["DATE,OPEN,HIGH,LOW,CLOSE"]
    for i, d in enumerate(dates):
        lines.append(f"{d.strftime(fmt)},{base + i},{base + i + 1},{base + i - 1},{base + i + 0.5}")
    return "\n".join(lines) + "\n"


def _csv_close_only(symbol: str, dates=DATES, *, fmt="%m/%d/%Y", base=20.0) -> str:
    lines = [f"DATE,{symbol}"]
    for i, d in enumerate(dates):
        lines.append(f"{d.strftime(fmt)},{base + i + 0.5}")
    return "\n".join(lines) + "\n"


def _frame(dates=DATES, *, base=18.0):
    return pd.DataFrame({"Open": [base + i for i in range(len(dates))],
                         "High": [base + i + 1 for i in range(len(dates))],
                         "Low": [base + i - 1 for i in range(len(dates))],
                         "Close": [base + i + 0.25 for i in range(len(dates))]},
                        index=pd.to_datetime(dates))


def _run(sweeps, tmp_path, *, run_at=NIGHT, caps=None, run_id="r1"):
    caps = {"uw": 0, "fred": 0, "yfinance": 1, "cboe": 20, "cftc": 0, **(caps or {})}
    return N.run_step("earnings", sweeps, caps=caps, run_id=run_id, run_at=run_at, log=lambda s: None,
                      ledger_dir=tmp_path)


def _rows(table, order="symbol, session_date"):
    return rawstore.query(f"SELECT * FROM {table} ORDER BY {order}")


def _cboe(*, status=200, html=(), body=None):
    """A fake ``httpx.get``: ``html`` symbols answer an error page, every other symbol a good CSV."""
    asked = []
    seen_redirects = []

    def get(url, timeout=None, headers=None, follow_redirects=False):
        seen_redirects.append(follow_redirects)
        symbol = url.rsplit("/", 1)[-1].removesuffix("_History.csv")
        asked.append(symbol)
        if symbol in html:
            return _Resp(status, "<html><body>error</body></html>")
        return _Resp(status, body(symbol) if body else _csv())

    get.seen_redirects = seen_redirects
    return get, asked


# --- date parsing ---------------------------------------------------------------------------------------------------

def test_d28_follows_the_cboe_redirect(monkeypatch):
    # cdn.cboe.com answers 307 -> cdn-api.cboe.com; without follow_redirects the 2026-10-20 run stored HTTP 307 for all 7
    get, _ = _cboe()
    monkeypatch.setattr("httpx.get", get)
    bars, why = d28.fetch_csv("VIX")
    assert why is None and len(bars) == len(DATES)
    assert get.seen_redirects == [True]


def test_d28_parses_both_cboe_date_formats():
    assert d28.parse_date("10/13/2026") == date(2026, 10, 13)        # Cboe's CSV today
    assert d28.parse_date("2026-10-13") == date(2026, 10, 13)        # the defensive second shape
    assert d28.parse_date(" 2026-10-13 ") == date(2026, 10, 13)
    assert d28.parse_date("13/10/2026") is None                       # not MM/DD/YYYY
    assert d28.parse_date("13 October 2026") is None
    assert d28.parse_date("") is None and d28.parse_date(None) is None


def test_d28_parses_a_2026_iso_csv_into_the_same_rows():
    bars, why = d28.parse_csv(_csv(fmt="%Y-%m-%d"))
    assert why is None and [b["session_date"] for b in bars] == DATES
    assert bars[-1]["close"] == 23.5 and bars[-1]["open"] == 23.0


def test_d28_parses_a_close_only_csv_with_the_symbol_as_column_name():
    bars, why = d28.parse_csv(_csv_close_only("VVIX"))
    assert why is None and [b["session_date"] for b in bars] == DATES
    assert bars[-1]["close"] == 23.5
    assert bars[-1]["open"] is bars[-1]["high"] is bars[-1]["low"] is None


def test_d28_parses_a_close_only_csv_with_a_different_symbol_name():
    bars, why = d28.parse_csv(_csv_close_only("OVX"))
    assert why is None and [b["session_date"] for b in bars] == DATES
    assert bars[-1]["close"] == 23.5
    assert bars[-1]["open"] is bars[-1]["high"] is bars[-1]["low"] is None


def test_d28_rejects_close_only_csv_without_a_price_column():
    bars, why = d28.parse_csv("DATE,VOLUME\n10/13/2026,1000\n")
    assert bars is None and why == "not_a_csv"


def test_d28_rejects_a_three_column_csv_without_close():
    bars, why = d28.parse_csv("DATE,OPEN,HIGH\n10/13/2026,1,2\n")
    assert bars is None and why == "not_a_csv"


# --- the rows -------------------------------------------------------------------------------------------------------

def test_d28_stores_all_seven_cboe_symbols_and_move(rawstore_tmp_db, tmp_path, monkeypatch):
    get, asked = _cboe()
    monkeypatch.setattr("httpx.get", get)
    monkeypatch.setattr(d28, "download_move", lambda download=None: (
        [{"session_date": d, "open": 18.0 + i, "high": 19.0 + i, "low": 17.0 + i, "close": 18.25 + i}
         for i, d in enumerate(DATES)], None))
    res = _run((d28.SWEEP,), tmp_path)
    s = res["sweeps"]["D28"]

    assert asked == list(d28.SYMBOLS) == ["VIX", "VIX9D", "VIX3M", "VIX6M", "VIX1Y", "VVIX", "OVX"]
    assert s["status"] == "ok" and s["written"] == 7 * len(DATES) + len(DATES)
    assert res["calls"]["cboe"]["used"] == 7 and res["calls"]["yfinance"]["used"] == 1

    vix = [r for r in _rows("vol_index_daily") if r["symbol"] == "VIX" and r["source"] == "cboe"]
    assert [(r["session_date"], r["close"]) for r in vix] == [
        (DATES[0], 20.5), (DATES[1], 21.5), (DATES[2], 22.5), (DATES[3], 23.5)]
    last = vix[-1]
    assert (last["open"], last["high"], last["low"]) == (23.0, 24.0, 22.0)
    assert last["operator"] is None and last["run_id"] == "r1" and last["run_at"] == NIGHT
    assert isinstance(last["fetched_at"], float)

    move = [r for r in _rows("vol_index_daily") if r["symbol"] == d28.MOVE]
    assert [r["source"] for r in move] == ["yfinance"] * len(DATES) and d28.MOVE == "MOVE"
    assert (move[-1]["session_date"], move[-1]["close"]) == (DATES[3], 21.25)
    assert move[-1]["operator"] is None


def test_d28_stores_all_seven_symbols_when_two_serve_close_only(rawstore_tmp_db, tmp_path, monkeypatch):
    def body(symbol):
        if symbol in ("VVIX", "OVX"):
            return _csv_close_only(symbol)
        return _csv()

    get, asked = _cboe(body=body)
    monkeypatch.setattr("httpx.get", get)
    monkeypatch.setattr(d28, "download_move", lambda download=None: (
        [{"session_date": d, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0} for d in DATES], None))
    res = _run((d28.SWEEP,), tmp_path)
    s = res["sweeps"]["D28"]

    assert asked == list(d28.SYMBOLS)
    assert s["status"] == "ok" and s["written"] == 7 * len(DATES) + len(DATES)
    stored = {(r["symbol"], r["source"]): r for r in _rows("vol_index_daily")}
    for sym in d28.SYMBOLS:
        assert (sym, "cboe") in stored
    for sym in ("VVIX", "OVX"):
        row = stored[(sym, "cboe")]
        assert row["open"] is row["high"] is row["low"] is None and row["close"] == 23.5


def test_d28_a_second_run_adds_no_duplicate_row(rawstore_tmp_db, tmp_path, monkeypatch):
    get, asked = _cboe()
    monkeypatch.setattr("httpx.get", get)
    monkeypatch.setattr(d28, "download_move", lambda download=None: (
        [{"session_date": d, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0} for d in DATES], None))
    first = _run((d28.SWEEP,), tmp_path, run_id="r1", caps={"yfinance": 2})
    second = _run((d28.SWEEP,), tmp_path, run_id="r2", caps={"yfinance": 2})

    assert first["sweeps"]["D28"]["written"] == 8 * len(DATES)
    assert second["sweeps"]["D28"]["status"] == "ok" and second["sweeps"]["D28"]["written"] == 0
    assert len(_rows("vol_index_daily")) == 8 * len(DATES)


def test_d28_keeps_the_whole_file_on_the_first_run_and_sixty_after(rawstore_tmp_db, tmp_path, monkeypatch):
    long = []
    d = date(2026, 7, 1)
    while len(long) < 80:
        if d.weekday() < 5:
            long.append(d)
        d += timedelta(days=1)
    get, _ = _cboe(body=lambda symbol: _csv(long))
    monkeypatch.setattr("httpx.get", get)
    monkeypatch.setattr(d28, "download_move", lambda download=None: (None, None))

    _run((d28.SWEEP,), tmp_path)
    assert d28.FIRST_RUN_LIMIT is None
    assert len(rawstore.query("SELECT 1 FROM vol_index_daily WHERE symbol = 'VIX'")) == 80

    again = _run((d28.SWEEP,), tmp_path, run_id="r2")
    assert again["sweeps"]["D28"]["written"] == 0
    assert d28.LIMIT == 60
    # every run before it stored all 80, so the second night offered only its newest 60
    assert again["sweeps"]["D28"]["offered"] == 7 * 60


# --- failures -------------------------------------------------------------------------------------------------------

def test_d28_a_500_and_an_html_body_are_per_symbol_errors_that_name_the_symbol(rawstore_tmp_db, tmp_path,
                                                                              monkeypatch):
    get, asked = _cboe(status=500, html=("VIX",))

    def get_mixed(url, timeout=None, headers=None, follow_redirects=False):
        symbol = url.rsplit("/", 1)[-1].removesuffix("_History.csv")
        asked.append(symbol)
        if symbol == "VIX":
            return _Resp(500, "boom")
        if symbol == "VIX9D":
            return _Resp(200, "<html><body>rate limited</body></html>")
        return _Resp(200, _csv())

    monkeypatch.setattr("httpx.get", get_mixed)
    monkeypatch.setattr(d28, "download_move", lambda download=None: (
        [{"session_date": DATES[-1], "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}], None))
    res = _run((d28.SWEEP,), tmp_path)
    s = res["sweeps"]["D28"]

    assert asked == list(d28.SYMBOLS)                       # the failure stopped no later symbol
    assert s["status"] == "partial"
    assert "VIX: HTTP 500" in s["note"]                     # the note names the first failure
    assert s["symbol_errors"] == ["VIX: HTTP 500", "VIX9D: not_a_csv"]      # every failed symbol is named
    stored = {r["symbol"] for r in _rows("vol_index_daily")}
    assert stored == {"VIX3M", "VIX6M", "VIX1Y", "VVIX", "OVX", "MOVE"}
    assert res["sweeps"]["D28"]["written"] == 5 * len(DATES) + 1


def test_d28_stops_at_the_cboe_cap(rawstore_tmp_db, tmp_path, monkeypatch):
    get, asked = _cboe()
    monkeypatch.setattr("httpx.get", get)
    monkeypatch.setattr(d28, "download_move", lambda download=None: (
        [{"session_date": DATES[-1], "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}], None))
    res = _run((d28.SWEEP,), tmp_path, caps={"cboe": 2})
    s = res["sweeps"]["D28"]

    assert asked == ["VIX", "VIX9D"]                        # refused before it was sent
    assert res["calls"]["cboe"] == {"limit": 2, "before": 0, "used": 2, "refused": 5}
    assert s["status"] == "partial" and s["note"] == "call_cap" and s["failed_requests"] == 5


def test_d28_an_intraday_clock_runs_no_sweep_writes_nothing_and_sends_nothing(rawstore_tmp_db, tmp_path,
                                                                              monkeypatch):
    from RevenueChaser.module_earnings_radar.inputs import cftc_cot as d29
    asked, sent = [], []

    def get(url, timeout=None, headers=None):
        sent.append(url)
        raise AssertionError("an intraday run must not reach the network")

    monkeypatch.setattr("httpx.get", get)
    monkeypatch.setattr(d28, "download_move", lambda download=None: asked.append("MOVE") or (None, None))
    monkeypatch.setattr(d29, "fetch_rows", lambda: sent.append("cftc") or (None, None))
    res = _run((d28.SWEEP, d29.SWEEP), tmp_path, run_at=INTRADAY)

    assert res["skipped"] == "intraday" and N.exit_code(res) == 0
    assert res["sweeps"] == {} and asked == [] and sent == []
    assert _rows("vol_index_daily") == [] and _rows("cftc_cot", "report_date") == []


# --- registration ---------------------------------------------------------------------------------------------------

def test_d28_download_move_passes_a_list_not_a_string_to_the_downloader():
    captured = []

    def fake_download(tickers, period=None, **kw):
        captured.append(tickers)
        assert tickers == ["^MOVE"]
        return _frame()

    bars, why = d28.download_move(download=fake_download)
    assert why is None and len(bars) == len(DATES)
    assert captured == [["^MOVE"]]


def test_d28_is_registered_in_the_step_and_cboe_is_a_known_provider():
    names = [s.name for s in inputs.sweeps()]
    assert "D28" in names and "D29" in names
    assert d28.SWEEP.tables == ("vol_index_daily",)
    assert "cboe" in N.PROVIDERS and "cftc" in N.PROVIDERS


def test_the_cli_states_the_cboe_and_cftc_caps(capsys):
    from RevenueChaser.scripts import nightly_inputs as cli
    assert cli.main(["nope", "--max-cboe-calls", "3", "--max-cftc-calls", "2"]) == 2
    err = capsys.readouterr().err
    assert "unknown step" in err and "unrecognized" not in err


def test_d28_sends_no_request_without_a_budget(rawstore_tmp_db, tmp_path, monkeypatch):
    get, asked = _cboe()
    monkeypatch.setattr("httpx.get", get)
    monkeypatch.setattr(d28, "download_move", lambda download=None: (None, None))
    res = _run((d28.SWEEP,), tmp_path, caps={"cboe": 0, "yfinance": 0})
    assert asked == [] and res["sweeps"]["D28"]["note"] == "call_cap"


def test_d28_a_row_without_a_numeric_close_is_not_stored():
    rows = ["DATE,OPEN,HIGH,LOW,CLOSE", "10/15/2026,1,2,0.5,N/A", "10/16/2026,1,2,0.5,17.5", "10/17/2026,1,2,0.5,"]
    bars, why = d28.parse_csv("\n".join(rows) + "\n")
    assert why is None and [b["close"] for b in bars] == [17.5]


def test_d28_a_body_that_parses_to_zero_bars_is_a_per_symbol_error(rawstore_tmp_db, tmp_path, monkeypatch):
    get, asked = _cboe(body=lambda symbol: "DATE,OPEN,HIGH,LOW,CLOSE\n")
    monkeypatch.setattr("httpx.get", get)
    monkeypatch.setattr(d28, "download_move", lambda download=None: ([], None))
    res = _run((d28.SWEEP,), tmp_path)
    s = res["sweeps"]["D28"]

    assert s["status"] == "failed"
    assert s["symbol_errors"] == [f"{sym}: no_rows" for sym in d28.SYMBOLS] + [f"{d28.MOVE}: no_rows"]
    assert _rows("vol_index_daily") == []
