"""Earnings Radar fixtures on the options store and ``market_series`` (G9): what ``uw/option_screener`` and
``uw/option_list`` would have served, written into the test's own ``MARKET_DIR`` (``module_oipd.tests.options_store``).

* ``screener_row``: one ``option_screener_daily`` row, a name that passes the gates unless told otherwise;
* ``list_name``: a list name's session: its ``option_daily`` row (GEX levels, volume), spot exposures by strike, the
  term structure and its prints, set ``list``;
* ``earnings_name``: the earnings set of one evening for one report: ``er_options`` and the same three tables, set
  ``earnings``;
* ``write_series``: ``market_series`` rows in ``market.duckdb``.
"""
from __future__ import annotations

from datetime import date, timedelta

import duckdb

from RevenueChaser.core import config
from RevenueChaser.module_oipd.tests.options_store import daily_row, write_options

PROV = {"fetched_at": 1.0, "run_id": "fixture", "observed": True}
GEX = {"call_wall": 110.0, "gamma_flip": 95.0, "gamma_magnet": 100.0, "put_wall": 90.0}


def screener_row(ticker, session, report_date, *, passes=True, **kw) -> dict:
    row = {"session_date": session, "ticker": ticker, "report_date": report_date, "report_session": "afterhours",
           "screened": True, "passes_gates": passes, "option_volume": 5000, "dollar_option_volume": 2_000_000.0,
           "call_volume": 3000.0, "put_volume": 2000.0, "total_open_interest": 40_000.0, "call_premium": 1_200_000.0,
           "put_premium": 800_000.0, "close": 100.0, "iv30d": 0.6, "iv_rank": 55.0, "marketcap": 5e10,
           "source": "uw", "source_as_of": session, **PROV}
    if not passes:
        row.update(call_volume=100.0, put_volume=50.0, option_volume=150, total_open_interest=150.0)
    row.update(kw)
    return row


def strikes(ticker, session, set_name, spot=100.0) -> list[dict]:
    """Spot exposures by strike around ``spot``: positive gamma above, negative below (a flip near spot)."""
    out = []
    for i, k in enumerate(range(80, 125, 5)):
        g = (1.0 if k >= spot else -1.0) * 1e6 * (1 + i % 3)
        out.append({"session_date": session, "ticker": ticker, "set_name": set_name, "strike": float(k),
                    "price": spot, "call_gamma_oi": g, "put_gamma_oi": g / 2, "call_gamma_vol": g,
                    "put_gamma_vol": g / 2, "source_as_of": session, "source": "uw", **PROV})
    return out


def term(ticker, session, set_name, ivs=(0.80, 0.60, 0.50, 0.45)) -> list[dict]:
    """A falling term structure (the event premium in the front expiry)."""
    return [{"session_date": session, "ticker": ticker, "set_name": set_name, "expiry": session + timedelta(days=d),
             "days_to_expiry": d, "volatility": iv, "source_as_of": session, "source": "uw", **PROV}
            for d, iv in zip((3, 10, 31, 60), ivs)]


def prints(ticker, session, set_name, *, retail_calls=0, inst_calls=2) -> list[dict]:
    """``inst_calls`` ask-side call prints of 20 lots (institutional) and ``retail_calls`` of 2 lots."""
    out = []
    for n, (size, count) in enumerate(((20, inst_calls), (2, retail_calls))):
        for i in range(count):
            out.append({"session_date": session, "ticker": ticker, "set_name": set_name, "print_id": f"p{n}{i}",
                        "executed_at": f"{session}T19:{10 + i:02d}:00Z", "option_symbol": f"{ticker}C{n}{i}",
                        "expiry": session + timedelta(days=10), "strike": 105.0, "option_type": "call",
                        "premium": 150_000.0 if size >= 10 else 600.0, "size": size, "delta": 0.4,
                        "price": 3.0, "nbbo_bid": 2.9, "nbbo_ask": 3.0, "tags": ["ask_side", "bullish"],
                        "canceled": False, "side": "ask", "package": False, "source_as_of": session,
                        "source": "uw", **PROV})
    return out


def list_name(ticker, session, **kw) -> None:
    write_options(option_daily=[daily_row(ticker, session, 100.0, call_volume=3000, put_volume=2000,
                                          call_open_interest=25_000, put_open_interest=15_000, **GEX)],
                  option_strike=strikes(ticker, session, "list"), option_expiry=term(ticker, session, "list"),
                  option_print=prints(ticker, session, "list", **kw))


def earnings_name(ticker, session, report_date, **kw) -> None:
    write_options(er_options=[{"session_date": session, "ticker": ticker, "report_date": report_date,
                               "report_session": "afterhours", "option_volume": 5000, "total_open_interest": 40_000,
                               "dollar_option_volume": 2_000_000.0, **GEX, "source": "uw",
                               "source_as_of": session, **PROV}],
                  option_strike=strikes(ticker, session, "earnings"), option_expiry=term(ticker, session, "earnings"),
                  option_print=prints(ticker, session, "earnings", **kw))


def write_series(rows, market=None) -> None:
    """``(series_id, obs_date, value[, source])`` rows into ``market_series`` of ``market.duckdb``."""
    from RevenueChaser.rcdata.build import market_series as ms
    market = market or config.MARKET_DIR
    market.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(market / "market.duckdb"))
    try:
        con.execute(f"CREATE TABLE IF NOT EXISTS market_series ({', '.join(f'{c} {t}' for c, t in ms.SCHEMA)})")
        cols = [c for c, _ in ms.SCHEMA]
        for r in rows:
            sid, day, value, source = (*r, "fred")[:4] if len(r) == 3 else r
            row = {"series_id": sid, "obs_date": day, "source": source, "value": value, "frequency": "daily",
                   "session_date": day, "source_as_of": day, "fetched_at": 1.0, "run_id": "fixture"}
            con.execute(f"INSERT INTO market_series VALUES ({', '.join('?' * len(cols))})", [row.get(c) for c in cols])
    finally:
        con.close()


def weekdays(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5]
