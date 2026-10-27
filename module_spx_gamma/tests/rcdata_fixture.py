"""A ``market.duckdb`` for SPX Gamma's tests, built through the real builder path (PR 3, sub-step 1).

Raw files are written where the builder's fetch writes them (``uw/spot_expiry_strike`` raw JSON, Cboe CSVs, the CFTC
JSON), typed to Parquet by the datasets' own ``to_parquet``, and the serving file is built and swapped in by
``base.build_kind``. No SQL is written into the serving file by hand.

The default set: five sessions of ``spx_gamma_expiry_strike`` (Tue 20 Oct to Mon 26 Oct 2026, so Friday 23 Oct's
next expiry is the Monday at 1 DTE), each with two expiries of 155 and 239 strikes (the shape of the 27 Oct row set);
the Cboe VIX family; the ``SPX`` closes for nine weeks (Cboe's SPX file has one column, so no OHLC); and four weekly
CFTC TFF reports of the E-mini (``13874A``).
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from RevenueChaser.core import calendar
from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build import base, market_series as ms, uw_spot_expiry_strike as uw

SESSIONS = (date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 26))
FRIDAY = date(2026, 10, 23)
LAST = SESSIONS[-1]
SNAPSHOT_TIME = "T20:15:00Z"
SPOT = 6800.0
SERIES_START = date(2026, 8, 17)
COT_REPORTS = (date(2026, 9, 29), date(2026, 10, 6), date(2026, 10, 13), date(2026, 10, 20))
RUN_ID = "fixture-run"


def uw_row(session: date, expiry: date, strike: float, call_gamma_oi: float = 0.0, put_gamma_oi: float = 0.0,
           price: float = SPOT) -> dict:
    """One row of UW's expiry-strike answer, every numeric field a string as UW sends them."""
    row = {"expiry": expiry.isoformat(), "strike": f"{strike:g}", "time": f"{session.isoformat()}{SNAPSHOT_TIME}"}
    row.update({f: "0" for f in uw.NUMERIC_FIELDS})
    row.update(call_gamma_oi=f"{call_gamma_oi:.2f}", put_gamma_oi=f"{put_gamma_oi:.2f}", price=f"{price:.2f}")
    return row


def default_rows(session: date) -> list[dict]:
    """155 strikes on the session's own expiry and 239 on the next one, around ``SPOT``."""
    nxt = calendar.next_trading_day(session)
    rows = [uw_row(session, session, 6430.0 + 5 * i, 1.0e8 * (1 + i % 7), -0.5e8 * (1 + i % 5)) for i in range(155)]
    rows += [uw_row(session, nxt, 6205.0 + 5 * i, 0.8e8 * (1 + i % 3), -0.6e8 * (1 + i % 4)) for i in range(239)]
    return rows


def _sessions(start: date, end: date) -> list[date]:
    return list(base.iter_sessions(start, end))


def cboe_csv(symbol: str, end: date = LAST) -> str:
    """A Cboe history file: OPEN/HIGH/LOW/CLOSE for the VIX family, one value column for SPX and VVIX."""
    days = _sessions(SERIES_START, end)
    if symbol in ("SPX", "VVIX"):
        level = {"SPX": SPOT - 100.0, "VVIX": 90.0}[symbol]
        lines = [f"DATE,{symbol}"] + [f"{d:%m/%d/%Y},{level + i:.2f}" for i, d in enumerate(days)]
    else:
        level = {"VIX": 16.0, "VIX9D": 15.0, "VIX3M": 18.0, "VIX6M": 19.0, "VIX1Y": 20.0}[symbol]
        lines = ["DATE,OPEN,HIGH,LOW,CLOSE"] + [
            f"{d:%m/%d/%Y},{level:.2f},{level + 1:.2f},{level - 1:.2f},{level + (i % 5) / 10:.2f}"
            for i, d in enumerate(days)]
    return "\n".join(lines) + "\n"


def cot_item(report: date, k: int) -> dict:
    """One futures-only TFF row of the E-mini, as Socrata serves it (numbers as strings)."""
    return {"report_date_as_yyyy_mm_dd": f"{report.isoformat()}T00:00:00.000",
            "cftc_contract_market_code": "13874A", "futonly_or_combined": "FutOnly",
            "market_and_exchange_names": "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE",
            "open_interest_all": str(2_000_000 + 1000 * k),
            "asset_mgr_positions_long": str(1_000_000 + 10_000 * k),
            "asset_mgr_positions_short": str(300_000 + 1000 * k),
            "lev_money_positions_long": str(200_000 - 5000 * k),
            "lev_money_positions_short": str(500_000 + 2000 * k),
            "dealer_positions_long_all": str(150_000 + 3000 * k),
            "dealer_positions_short_all": str(250_000 - 1000 * k),
            "change_in_asset_mgr_long": "999999",          # the parser drops change_* (A5 computes it)
            "pct_of_oi_asset_mgr_long": "50.0"}


def _write_spx(ctx, spx: dict[date, list[dict]]) -> None:
    for session, rows in sorted(spx.items()):
        raw = base.write_raw_json(uw.raw_path(session), {
            "request": {"endpoint": uw.ENDPOINT, "params": {"date": session.isoformat()}},
            "session_date": session.isoformat(), "observed": True,
            "fetched_at": 1_761_600_000.0, "run_id": RUN_ID, "body": {"data": rows}})
        uw.DATASET.to_parquet(ctx, raw)


def _write_series(ctx, symbols, cot_reports, end: date) -> None:
    raws = []
    for sym in symbols:
        path = base.raw_root() / "cboe" / "index" / end.isoformat() / f"{sym}_History.csv"
        raws.append(ms.save_raw(ctx, path, cboe_csv(sym, end).encode(), end, "https://cboe.test", symbol=sym))
    for k, report in enumerate(cot_reports):
        path = base.raw_root() / "cftc" / "cot" / f"{report.isoformat()}.json.gz"
        doc = json.dumps({"tff": [cot_item(report, k)], "disagg": []}).encode()
        raws.append(ms.save_raw(ctx, path, doc, end, "https://cftc.test"))
    if raws:
        ms.DATASET.to_parquet_all(ctx, raws)


VIX_SYMBOLS = ("VIX", "VIX9D", "VIX3M", "VIX6M", "VIX1Y", "VVIX")


def serve(*, spx: dict[date, list[dict]] | None = None, symbols=VIX_SYMBOLS + ("SPX",),
          cot_reports=COT_REPORTS, series_end: date = LAST, check: str | None = "changed") -> None:
    """Build and swap in a ``market`` serving file. ``spx=None`` is the default five sessions; ``{}`` none."""
    spx = {s: default_rows(s) for s in SESSIONS} if spx is None else spx
    # allow_shrink: a test may serve again with fewer rows for a session it already served.
    ctx = base.RunContext(run_id=RUN_ID, dataset=uw.NAME, trigger="test", allow_shrink=True)
    _write_spx(ctx, spx)
    ctx.dataset = ms.NAME
    _write_series(ctx, symbols, cot_reports, series_end)
    base.parquet_root().mkdir(parents=True, exist_ok=True)
    base.build_kind(ctx, uw.DATASET)
    if check:
        for name in (uw.NAME, ms.NAME):
            ops.record_check(name, check, run_id=RUN_ID, source_as_of=series_end.isoformat())


def sessions_back(day: date, n: int) -> date:
    """The session ``n`` sessions before ``day`` (a helper for the lag tests)."""
    for _ in range(n):
        day = calendar.previous_trading_day(day - timedelta(days=1))
    return day
