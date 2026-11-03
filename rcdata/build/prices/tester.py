"""Alpaca is the tester, not a price source (master plan of 2 Nov 2026, item 3; avo: "alpacayi tester olarak ekle ...
ilk ve ikinci kaynaktan gelen değerleri kontrol eder, bütün set için değil belli seçilmiş tickerlar icin, hata
yapılma olasılığı olanlardan").

**Which names.** The daily ``alpaca/bars`` run (a request that names nothing) asks only the names of its scope that
``at_risk`` finds, each for a reason (``REASONS``), over the ``RISK_DAYS`` calendar days up to the run's session:

- ``split``: ``ajexdi`` changed in Compustat's newest ``comp.secd`` rows, or the served store holds a split or stock
  dividend going ex in the window;
- ``trfd_empty``: a priced ``comp.secd`` day without ``trfd`` (no Compustat return, CRSP's fallback day);
- ``dividend`` / ``spin_off``: a cash dividend or a spin-off going ex in the window (``corporate_action``);
- ``price_gap``: an open ``price_gap_no_action`` issue of ``prices/serving``;
- ``exchange_move``: a ``comp.sec_idhist`` EXCHG period starting in the window (``secd.exchg`` is today's code);
- ``low_price``: the newest ``prccd`` under ``LOW_PRICE``.

A request that names tiers or tickers gets what it names, as before (#265). The run is paced and capped as every
Alpaca run is (``sources.alpaca.pacer``, ``caps.DAY_CAPS["alpaca"]``).

**What it checks.** ``prices/serving`` compares the staged Alpaca bars with the history bars of the same security and
session (Compustat's, and CRSP's where it has one): the close (``alpaca_close_differs``) and the day return of the
served bar (``adjustment_differs``). Alpaca's bars write issues only and are never served. The comparison runs in
every serving build, so a session's Alpaca bar is checked again once Compustat's update brings that session.
"""
from __future__ import annotations

from datetime import date, timedelta

import duckdb

from ... import ops, serving
from .. import base
from ..spine import wrds
from . import wrds_history

RISK_DAYS = 10
# shortcut: a fixed dollar line for "low price"; replace by the measured price where Compustat and Alpaca closes part
# most (the board of ``alpaca_close_differs``) once a month of tester issues is in.
LOW_PRICE = 5.0
REASONS = ("split", "trfd_empty", "dividend", "spin_off", "price_gap", "exchange_move", "low_price")


def at_risk(as_of: date) -> dict[str, list[str]]:
    """``{security_id: [reason, ...]}`` of the securities at risk on ``as_of`` (see the module docstring)."""
    lo = as_of - timedelta(days=RISK_DAYS)
    found: dict[str, set[str]] = {}
    con = duckdb.connect()
    try:
        files = [p for p in (wrds_history.partition_path("secd", y) for y in range(lo.year, as_of.year + 1))
                 if p.is_file()]
        rows = []
        if files:
            rows += con.execute(f"""
                WITH s AS (SELECT gvkey || ':' || iid AS sid, datadate, prccd, ajexdi, trfd, exchg
                           FROM read_parquet([{', '.join(base.sql_path(p) for p in files)}], union_by_name = true)
                           WHERE datadate BETWEEN ? AND ? AND prccd > 0)
                SELECT sid, 'trfd_empty' FROM s WHERE trfd IS NULL GROUP BY sid
                UNION ALL SELECT sid, 'split' FROM s GROUP BY sid HAVING count(DISTINCT ajexdi) > 1
                UNION ALL SELECT sid, 'low_price' FROM s GROUP BY sid HAVING arg_max(prccd, datadate) < {LOW_PRICE}
                """, [lo, as_of]).fetchall()
        idhist = wrds.parquet_files("wrds/sec_idhist", "sec_idhist")
        if idhist:
            # secd.exchg is today's code: a move is a sec_idhist EXCHG period starting in the window.
            rows += con.execute(f"""
                SELECT DISTINCT gvkey || ':' || iid, 'exchange_move'
                FROM read_parquet([{', '.join(base.sql_path(p) for p in idhist)}], union_by_name = true)
                WHERE item = 'EXCHG' AND efffrom BETWEEN ? AND ?""", [lo, as_of]).fetchall()
        live = serving.live_path("prices")
        if live.exists():
            con.execute(f"ATTACH {base.sql_path(live)} AS live_prices (READ_ONLY)")
            rows += con.execute("""
                SELECT security_id, CASE WHEN action IN ('split', 'stock_dividend') THEN 'split'
                                         WHEN action = 'cash_dividend' THEN 'dividend' ELSE action END
                FROM live_prices.corporate_action
                WHERE ex_date BETWEEN ? AND ? AND action IN ('split', 'stock_dividend', 'cash_dividend', 'spin_off')
                """, [lo, as_of]).fetchall()
    finally:
        con.close()
    from .serving import NAME as SERVING
    n, _ = ops.open_issues(SERVING, limit=1)
    for issue in ops.open_issues(SERVING, limit=max(n, 1))[1]:
        if issue["kind"] == "price_gap_no_action":
            rows.append((issue["key"].rsplit(":", 1)[0], "price_gap"))
    for sid, why in rows:
        found.setdefault(sid, set()).add(why)
    return {sid: sorted(w) for sid, w in found.items()}


def select(names: list[dict], as_of: date) -> tuple[list[dict], str]:
    """The names of ``names`` at risk on ``as_of``, and the run note that says how many and why."""
    risky = at_risk(as_of)
    kept = [n for n in names if n.get("security_id") in risky]
    counts = {w: sum(w in risky[n["security_id"]] for n in kept) for w in REASONS}
    why = ", ".join(f"{c} {w}" for w, c in counts.items() if c) or "none"
    return kept, f"Alpaca tester: {len(kept)} of {len(names)} names at risk ({why})"
