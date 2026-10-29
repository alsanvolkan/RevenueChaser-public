"""LEAPS fixtures on the options store (G9): what ``uw/option_list`` would have served for a few names.

``store_footprint`` writes one session of one name: its contracts' open interest per expiry and side
(``option_eod``), the ask-side premium per expiry (``option_expiry``), the IV rank of the last sessions
(``option_daily``) and its prints (``option_print``). ``pin`` puts names on the stock watch list (the LEAPS pins) and
on the option watch list (the names the store holds).
"""
from __future__ import annotations

from datetime import date, timedelta

from RevenueChaser.module_oipd.tests.options_store import daily_row, write_options
from RevenueChaser.rcdata import ops

PROV = {"source": "uw/option_list", "fetched_at": 1.0, "run_id": "fixture", "observed": True}


def _day(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def eod(ticker, session, expiry, kind, oi) -> dict:
    expiry = _day(expiry)
    sym = f"{ticker}{expiry:%y%m%d}{'C' if kind == 'call' else 'P'}00100000"
    return {"session_date": session, "ticker": ticker, "option_symbol": sym, "expiry": expiry, "strike": 100.0,
            "option_type": kind, "days_to_expiry": (expiry - session).days, "open_interest": oi,
            "source_as_of": session, **PROV}


def print_row(ticker, pid, session, expiry, *, premium=250_000.0, set_name="list", **kw) -> dict:
    return {"session_date": session, "ticker": ticker, "set_name": set_name, "print_id": pid,
            "executed_at": f"{session}T15:00:00Z", "option_symbol": f"{ticker}{pid}", "expiry": _day(expiry),
            "strike": 100.0, "option_type": "call", "premium": premium, "delta": 0.7, "size": 10, "ask_vol": 9,
            "bid_vol": 1, "canceled": False, "source_as_of": session, **PROV, **kw}


def store_footprint(ticker, session, oi, *, rank=(55.0,), flow=(), prints=(), market=None) -> None:
    """``oi`` maps an expiry to ``(call_oi, put_oi)``; ``flow`` holds ``(expiry, call_ask, put_ask)``; ``rank`` is
    the IV rank of the sessions ending at ``session``, one a day; ``market`` is an e2e dashboard's root."""
    contracts = [eod(ticker, session, e, kind, n) for e, (c, p) in oi.items()
                 for kind, n in (("call", c), ("put", p))]
    days = [session - timedelta(days=len(rank) - 1 - i) for i in range(len(rank))]
    write_options(market, option_eod=contracts,
                  option_expiry=[{"session_date": session, "ticker": ticker, "set_name": "list", "expiry": _day(e),
                                  "call_premium_ask_side": c, "put_premium_ask_side": p, "source_as_of": session,
                                  **PROV} for e, c, p in flow],
                  option_daily=[daily_row(ticker, d, 100.0, iv_rank_1y=r) for d, r in zip(days, rank)],
                  option_print=list(prints))


def pin(*tickers, option=None) -> None:
    """Pin ``tickers`` (the stock watch list) and put ``option`` (``tickers`` by default) on the option watch list."""
    for t in tickers:
        ops.watch_add("stock", t, by="test")
    for t in (tickers if option is None else option):
        ops.watch_add("option", t, by="test")
