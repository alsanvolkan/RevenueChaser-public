"""``ibkr/iv_history``: the IBKR year of 30-day implied volatility, once per list name.

The names are the list set (``names.list_names``: SPX and the option watch list, read at each run). A name without
its raw file gets one request: daily ``OPTION_IMPLIED_VOLATILITY`` bars over ``1 Y``, regular hours
(``core/providers/ibkr_tws.py``, the minimal TWS client: market data only, no positions request). So a name added
to the list later gets its year on the next run, and never a second time. A run with no name missing opens no
connection.

IBKR allows 60 historical requests in 10 minutes: the run sends at most ``PER_MINUTE`` a minute (``base.Pacer``),
about 1.5 hours for 500 names, so a run stops starting requests after ``uw.RUN_BUDGET_S`` and the next one goes on.
The gateway on homesrv is logged in by a person (``core/ibkr_login.py`` shows whether it is up): a run with names
missing and no gateway is a ``failed`` check, which the hourly pass retries.

Raw: ``raw/ibkr/iv_history/<TICKER>.json.gz`` (the bars as the gateway sent them, with the contract and the request),
the name's resume marker. An answer of "no data" or "no such contract" (``NO_DATA_CODES``) is kept as a raw file
without bars and a ``no_history`` issue, so the name is not asked every day. Parquet:
``parquet/ibkr/iv_history/iv_daily/ticker=<TICKER>/part-0.parquet``. The rows go to ``iv_daily`` (built by
``uw/option_list``) as source ``ibkr``, ``days`` 30, ``observed`` false: a backfilled history, never the daily
observation. A bar dated after the latest complete session (an unfinished day) is left out.
"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import duckdb

from ....core import calendar, ibkr_login
from ....core.providers import ibkr_tws
from ... import serving
from .. import base
from . import KIND, names, tables, uw

NAME = "ibkr/iv_history"
SOURCE = tables.IBKR_SOURCE
PER_MINUTE = 6                       # 60 historical requests in 10 minutes (IBKR's pacing rule)
IV_DAYS = 30                         # IBKR's option implied volatility is a 30-day measure
DURATION, BAR_SIZE, WHAT = "1 Y", "1 day", "OPTION_IMPLIED_VOLATILITY"
# "Historical market data service: query returned no data" and "no security definition": nothing to retry for.
NO_DATA_CODES = (162, 200)


def raw_path(ticker: str) -> Path:
    return base.raw_root() / "ibkr" / "iv_history" / f"{ticker}.json.gz"


def partition(ticker: str) -> Path:
    return tables.ticker_partition_path(SOURCE, "iv_daily", ticker)


class IBKRIVHistory:
    name = NAME
    kind = KIND
    providers = ("ibkr",)
    # iv_daily is built by uw/option_list, from both sources' Parquet; this dataset only adds its folder.
    tables = ()
    starts_by_hand = True
    backfill_needs_from = False

    def default_as_of(self) -> date:
        return calendar.latest_complete_session()

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        wanted = names.list_names()
        untyped = [raw_path(t) for t in wanted if raw_path(t).exists() and not partition(t).exists()]
        missing = [t for t in wanted if not raw_path(t).exists()]
        state = ctx.pending.setdefault(NAME, {"names": wanted, "stopped": None})
        if not missing:
            return untyped
        host, port = ibkr_login.target()
        pacer = base.Pacer(PER_MINUTE, clock=ctx.clock, sleep=ctx.sleep)
        start, out = ctx.clock(), []
        try:
            with ibkr_tws.TWSClient(host, port) as client:
                for req_id, ticker in enumerate(missing, start=1):
                    if ctx.clock() - start >= uw.RUN_BUDGET_S:
                        state["stopped"] = f"the run's {uw.RUN_BUDGET_S // 60} min budget"
                        break
                    ctx.check_cap("ibkr")
                    pacer.wait()
                    ctx.count("ibkr", calls=1)
                    contract = ibkr_tws.contract_for(ticker)
                    doc = {"ticker": ticker, "as_of": as_of.isoformat(), "contract": contract.fields(),
                           "request": {"duration": DURATION, "bar_size": BAR_SIZE, "what": WHAT, "use_rth": True},
                           "fetched_at": time.time(), "run_id": ctx.run_id}
                    try:
                        bars = client.history(req_id, contract, duration=DURATION, bar_size=BAR_SIZE, what=WHAT)
                    except ibkr_tws.HistoryError as e:
                        if e.code not in NO_DATA_CODES:
                            ctx.count("ibkr", failures=1)
                            ctx.issue("fetch_failed", ticker, str(e)[:300])
                            continue
                        ctx.issue("no_history", ticker, str(e)[:300])
                        bars, doc["error"] = [], {"code": e.code, "text": e.text}
                    out.append(base.write_raw_json(raw_path(ticker), {
                        **doc, "bars": [{**b, "date": b["date"].isoformat()} for b in bars]}))
        except base.CapReached as e:
            state["stopped"] = str(e)
        except ibkr_tws.TWSError as e:
            if not out:
                raise base.SourceError(f"IBKR gateway {host}:{port}: {e}") from e
            state["stopped"] = f"the gateway went away: {e}"
        return untyped + out

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        doc = base.read_raw_json(raw)
        ticker = doc["ticker"]
        as_of = date.fromisoformat(doc["as_of"])
        last = calendar.latest_complete_session()
        rows = []
        for bar in doc.get("bars") or []:
            day = tables.to_day(bar.get("date"))
            if day is None or day > min(as_of, last):
                continue
            rows.append({"session_date": day, "ticker": ticker, "days": IV_DAYS, "volatility": tables.to_float(
                bar.get("close")), "implied_move_perc": None, "percentile": None, "source": "ibkr",
                "source_as_of": as_of, "fetched_at": doc["fetched_at"], "run_id": doc["run_id"], "observed": False})
        t = tables.TABLES["iv_daily"]
        # A name without bars still gets its (empty) partition: the marker that its raw file is typed.
        return [base.upsert_partition(rows, t.schema, list(t.key), partition(ticker))]

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        return [p for raw in raws for p in self.to_parquet(ctx, raw)]

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        """The same as the daily run: each list name's year, once."""
        ctx.as_of = end
        self.to_parquet_all(ctx, self.fetch(ctx, end))

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        as_of = ctx.as_of or self.default_as_of()
        state = ctx.pending.get(NAME) or {}
        wanted = state.get("names") or names.list_names()
        have = [t for t in wanted if partition(t).exists()]
        rows = int(con.execute("SELECT count(*) FROM iv_daily WHERE source = 'ibkr'").fetchone()[0])
        live = 0
        if serving.live_path(KIND).exists():
            with serving.open_read(KIND) as old:
                if "iv_daily" in {r[0] for r in old.execute("SELECT table_name FROM duckdb_tables()").fetchall()}:
                    live = int(old.execute("SELECT count(*) FROM iv_daily WHERE source = 'ibkr'").fetchone()[0])
        added, removed = max(rows - live, 0), max(live - rows, 0)
        note = [f"{len(have)}/{len(wanted)} list names have their IBKR year"]
        if state.get("stopped"):
            note.append(f"stopped: {state['stopped']}")
        left = len(have) < len(wanted)
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": as_of.isoformat(), "rows": rows, "added": added, "removed": removed,
                "coverage": round(len(have) / len(wanted), 4) if wanted else None,
                "next_check_due": as_of.isoformat() if left and state.get("stopped") else base.next_due(as_of),
                "note": "; ".join(note)}


DATASET = IBKRIVHistory()
