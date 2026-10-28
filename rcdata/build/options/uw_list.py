"""``uw/option_list``: the chain, the option stats and the prints of SPX and the option watch list, once a session.

The names are SPX and ``watchlist_option`` at each run (``names.list_names``). Per name, for session S:

- **chain**: ``expiry-breakdown?date=S`` (the expiries, with contracts, open interest and volume each), then per
  expiry ``option-contracts?expiry=E`` (paged, 500 a page) and ``greeks?expiry=E&date=S`` (charm, vanna and the
  greeks' IV per strike). ``option_eod``: one row per contract and session; ``oi_change`` is ``open_interest -
  prev_oi`` of the same answer, so the chain needs no ``oi-change`` call of its own.
- **stats**: ``spot-exposures/strike?date=S`` (paged), ``greek-exposure/strike?date=S``,
  ``greek-exposure/expiry?date=S``, ``flow-per-expiry`` (the day's flow per expiry),
  ``volatility/term-structure?date=S``, ``gex-levels?date=S``, ``options-volume?limit=5`` (the row of S),
  ``interpolated-iv?date=S`` and ``iv-rank?date=S`` (the row of S). ``option_strike``, ``option_expiry``,
  ``option_daily`` and ``iv_daily`` (source ``uw``).
- **prints**: ``flow-recent``: the newest 50 prints executed on S (New York date), ``option_print``.

That is ``11 + 2 x expiries`` calls a name, plus a call per extra page (``__init__.py`` has the day's arithmetic).

Raw: ``raw/uw/option_list/<S>/<TICKER>.json.gz``, one file per name and session holding every part as UW sent it
(a paged part as ``{"data": <every page's rows>, "pages": [rows per page]}``), the parts UW refused (``errors``) and
the calls it took. It is written once every part arrived, so it is the name's resume marker. Parquet:
``parquet/uw/option_list/<table>/date=<S>/part-0.parquet`` per table, merged by key, and the ``names`` row of the
name last (``tables.NAMES``).

**When.** Only in the observed window (``uw.observed``): UW answers the chain, the flow per expiry, the prints and
the option volume for the current or last session only, so there is no backfill, and a run outside the window sends
no call. A run stops starting names after ``RUN_BUDGET_S``, under RC-Build's 50-minute limit; what it fetched is
built, and its check asks for the same session again (``next_check_due`` = the session) while names are left and
the window is open. Once the next session opens, the names left wait for the next session.
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base
from . import KIND, names, raw_retention, rows, tables, uw

NAME = "uw/option_list"
SOURCE = tables.LIST_SOURCE
OWNED = ("option_eod", "option_strike", "option_expiry", "option_daily", "option_print", "iv_daily")
RUN_BUDGET_S = uw.RUN_BUDGET_S


def raw_path(session: date, ticker: str) -> Path:
    return base.raw_root() / "uw" / "option_list" / session.isoformat() / f"{ticker}.json.gz"


def provenance(doc: dict, session: date) -> dict:
    return {"source": "uw", "source_as_of": session, "fetched_at": doc["fetched_at"], "run_id": doc["run_id"],
            "observed": bool(doc.get("observed"))}


def calls_so_far(ctx: base.RunContext) -> int:
    return ctx.calls.get("uw", [0])[0]


class UWOptionList:
    name = NAME
    kind = KIND
    providers = ("uw",)
    tables = OWNED
    # Not due before a first run from the CLI or a Fetch has built it (rcdata/build/due.py): the first hourly pass
    # after the deploy does not start it.
    starts_by_hand = True

    def default_as_of(self) -> date:
        return calendar.latest_complete_session()

    # ── fetch ──
    def _fetch_name(self, ctx: base.RunContext, session: date, ticker: str) -> Path:
        errors: dict[str, str] = {}
        before = calls_so_far(ctx)
        day = session.isoformat()
        breakdown = uw.get(ctx, uw.stock(ticker, "expiry-breakdown"), {"date": day}, "expiry_breakdown", errors)
        expiries = sorted({e for e in (tables.to_day(r.get("expiry")) for r in uw.rows_of(breakdown)
                                       if isinstance(r, dict)) if e is not None and e >= session})
        chain = {}
        for e in expiries:
            chain[e.isoformat()] = {
                "contracts": uw.paged(ctx, uw.stock(ticker, "option-contracts"), {"expiry": e.isoformat()},
                                      f"contracts {e}", errors, key=lambda r: str(r["option_symbol"])),
                "greeks": uw.get(ctx, uw.stock(ticker, "greeks"), {"expiry": e.isoformat(), "date": day},
                                 f"greeks {e}", errors)}
        parts = {
            "expiry_breakdown": breakdown, "chain": chain,
            "spot_strike": uw.paged(ctx, uw.stock(ticker, "spot-exposures/strike"), {"date": day}, "spot_strike",
                                    errors, key=lambda r: float(r["strike"])),
            "exposure_strike": uw.get(ctx, uw.stock(ticker, "greek-exposure/strike"), {"date": day},
                                      "exposure_strike", errors),
            "exposure_expiry": uw.get(ctx, uw.stock(ticker, "greek-exposure/expiry"), {"date": day},
                                      "exposure_expiry", errors),
            "flow_expiry": uw.get(ctx, uw.stock(ticker, "flow-per-expiry"), None, "flow_expiry", errors),
            "term_structure": uw.get(ctx, uw.stock(ticker, "volatility/term-structure"), {"date": day},
                                     "term_structure", errors),
            "gex_levels": uw.get(ctx, uw.stock(ticker, "gex-levels"), {"date": day}, "gex_levels", errors),
            "options_volume": uw.get(ctx, uw.stock(ticker, "options-volume"), {"limit": 5}, "options_volume", errors),
            "interpolated_iv": uw.get(ctx, uw.stock(ticker, "interpolated-iv"), {"date": day}, "interpolated_iv",
                                      errors),
            "iv_rank": uw.get(ctx, uw.stock(ticker, "iv-rank"), {"date": day}, "iv_rank", errors),
            "prints": uw.get(ctx, uw.stock(ticker, "flow-recent"), None, "prints", errors),
        }
        return base.write_raw_json(raw_path(session, ticker), {
            "session_date": day, "ticker": ticker, "set_name": tables.SET_LIST, "observed": True,
            "fetched_at": time.time(), "run_id": ctx.run_id, "calls": calls_so_far(ctx) - before,
            "parts": parts, "errors": errors})

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        """Every list name of ``as_of`` without a raw file, until the run's budget; plus the raw files of ``as_of``
        a killed run left untyped. Outside the observed window: no call, only those untyped files."""
        wanted = names.list_names()
        done = tables.names_done(SOURCE, as_of)
        untyped = [raw_path(as_of, n) for n in wanted if n not in done and raw_path(as_of, n).exists()]
        state = ctx.pending.setdefault(NAME, {"names": wanted, "failed": [], "stopped": None})
        if not uw.observed(as_of):
            state["stopped"] = "outside the observed window: no UW call"
            return untyped
        start, out = ctx.clock(), []
        for ticker in wanted:
            if raw_path(as_of, ticker).exists():
                continue
            if ctx.clock() - start >= RUN_BUDGET_S:
                state["stopped"] = f"the run's {RUN_BUDGET_S // 60} min budget"
                break
            try:
                out.append(self._fetch_name(ctx, as_of, ticker))
            except base.CapReached as e:
                state["stopped"] = str(e)
                break
            except uw.NameFailed as e:
                state["failed"].append(ticker)
                ctx.issue("fetch_failed", f"{as_of} {ticker}", str(e)[:300])
        return untyped + out

    # ── type ──
    def type_raw(self, ctx: base.RunContext, raw: Path) -> tuple[dict[str, list[dict]], dict]:
        """The table rows of one raw file and its ``names`` row."""
        doc = base.read_raw_json(raw)
        session, ticker = date.fromisoformat(doc["session_date"]), doc["ticker"]
        prov, parts, errors = provenance(doc, session), doc["parts"], doc.get("errors") or {}

        def issue(kind: str, key: str, detail: str) -> None:
            ctx.issue(kind, key, detail)

        eod = rows.eod_rows(session, ticker, parts.get("chain") or {}, prov, issue)
        daily = {"session_date": session, "ticker": ticker, "expiries": len(parts.get("chain") or {}),
                 "contracts": len(eod), **rows.gex_levels(parts.get("gex_levels")),
                 **rows.volume_fields(parts.get("options_volume"), session),
                 **rows.iv_rank_fields(parts.get("iv_rank"), session), **prov}
        out = {
            "option_eod": eod,
            "option_strike": rows.strike_rows(session, ticker, tables.SET_LIST, parts.get("spot_strike"),
                                              parts.get("exposure_strike"), prov, issue),
            "option_expiry": rows.expiry_rows(session, ticker, tables.SET_LIST, prov,
                                              breakdown=parts.get("expiry_breakdown"),
                                              exposure=parts.get("exposure_expiry"), flow=parts.get("flow_expiry"),
                                              term=parts.get("term_structure")),
            "option_daily": [daily],
            "option_print": rows.print_rows(session, ticker, tables.SET_LIST, parts.get("prints"), prov),
            "iv_daily": rows.iv_rows(session, ticker, parts.get("interpolated_iv"), prov),
        }
        if errors:
            issue("part_empty", f"{session} {ticker}",
                  ("UW refused, stored empty: " + "; ".join(f"{k} {v}" for k, v in sorted(errors.items())))[:300])
        return out, {"session_date": session, "ticker": ticker, "set_name": tables.SET_LIST,
                     "raw_bytes": raw.stat().st_size, "calls": doc.get("calls"),
                     "errors": json.dumps(errors, sort_keys=True) if errors else None,
                     "fetched_at": doc["fetched_at"], "run_id": doc["run_id"]}

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        """Type ``raws`` together: one write per table and session, then the ``names`` rows."""
        by_table: dict[str, list[dict]] = {t: [] for t in OWNED}
        done = []
        for raw in raws:
            typed, name_row = self.type_raw(ctx, raw)
            for table, table_rows in typed.items():
                by_table[table].extend(table_rows)
            done.append(name_row)
        parts = [p for table, table_rows in by_table.items() if table_rows
                 for p in tables.upsert(SOURCE, tables.TABLES[table], table_rows)]
        return parts + (tables.upsert(SOURCE, tables.NAMES, done) if done else [])

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        """None: UW answers the chain, the prints and the day's flow for the current session only."""
        ctx.notes.append("no backfill: UW serves the option chain and the prints for the current session only")

    # ── build and check ──
    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        return tables.build_tables(con, OWNED)

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        as_of = ctx.as_of or self.default_as_of()
        state = ctx.pending.get(NAME) or {}
        wanted = state.get("names") or names.list_names()
        done = tables.names_done(SOURCE, as_of)
        left = [n for n in wanted if n not in done]
        added = removed = 0
        for table in OWNED:
            a, r = tables.compare_by_day(con, tables.TABLES[table])
            added, removed = added + a, removed + r
        rows_eod = int(con.execute("SELECT count(*) FROM option_eod").fetchone()[0])
        note = [f"{len(wanted) - len(left)}/{len(wanted)} names for {as_of}"]
        if left:
            why = state.get("stopped") or ("failed: " + ", ".join(state["failed"]) if state.get("failed") else None)
            note.append(f"{len(left)} left" + (f" ({why})" if why else ""))
        sizes = [r["raw_bytes"] for r in done.values() if r.get("raw_bytes")]
        if sizes:                                   # the plan's F5 measure: raw size per list name
            big = max(done.values(), key=lambda r: r.get("raw_bytes") or 0)
            note.append(f"raw {sum(sizes) / len(sizes) / 1024:.0f} KB a name (largest {big['ticker']} "
                        f"{big['raw_bytes'] / 1024:.0f} KB)")
        again = bool(left) and uw.observed(as_of)
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": as_of.isoformat(), "rows": rows_eod, "added": added, "removed": removed,
                "coverage": round((len(wanted) - len(left)) / len(wanted), 4) if wanted else None,
                "next_check_due": as_of.isoformat() if again else base.next_due(as_of), "note": "; ".join(note)}

    def after_swap(self, ctx: base.RunContext) -> None:
        """The raw option retention, once the new file is live (``raw_retention``; nothing while it is unset)."""
        raw_retention.apply(ctx, ctx.as_of or self.default_as_of())


DATASET = UWOptionList()
