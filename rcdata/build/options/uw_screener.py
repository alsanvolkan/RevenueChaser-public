"""``uw/option_screener``: the screener set (every name reporting in the next 28 days) and, from it, the earnings
set of the evening.

**The calendar** (G8 owns UW's earnings calendar): ``/earnings/premarket`` and ``/earnings/afterhours`` with
``date=D`` for every trading day D after the session S and at most ``DAYS_AHEAD`` days ahead, paged at UW's
documented maximum of 100 rows (0-based pages). Raw: ``raw/uw/earnings_calendar/<S>/<D>-<session>.json.gz``. A name
on more than one day takes its first report.

**The screener**: ``/screener/stocks?ticker=<100 names>&date=S&limit=500`` over the calendar's names in ticker order,
100 a call (the plan: 1,727 of 1,834 calendar names). Raw: ``raw/uw/screener/<S>/part-NN.json.gz``, one per batch.
It waits until every calendar file of S is there, so the batches of a session never change. ``option_screener_daily``:
one row per calendar name and session, with its report date and session, its screener row (``screened`` false when
UW has none), the option volume, the dollar option volume (the day's call plus put premium as UW served it,
``premium``) and ``passes_gates``: the Earnings Radar gates of ``core/config.py`` (option volume >= 500, total open
interest >= 200, dollar option volume >= 50k; ``module_earnings_radar/pipeline.py`` ``preselect``). The dollar gate
reads the real premium, not the radar's old volume x 100 estimate, which made it the volume gate a second time (500
contracts x 100 is 50,000; Earnings Radar review N3). So it is the stricter count: in the first real sample
(2026-10-28, 310 screened names) 121 pass, against 125 under volume x 100. The 4 left out trade 500 or more
contracts with under $50k of premium, or with no premium served, which fails the gate too.

**The earnings set** (``names.earnings_set``): every screener-set name that reports on the next session, passes
the gates on S and is not in the list set (SPX and the option watch list, which have their full chain), once, on
that evening: ``spot-exposures/strike?date=S`` (paged), ``gex-levels?date=S``, ``volatility/term-structure?date=S``,
``options-volume?limit=5`` (the row of S) and ``flow-recent`` (the newest 50 prints of S): five calls a name, not the
full chain (the plan: 527 names over 9 report days). Raw: ``raw/uw/er_options/<S>/<TICKER>.json.gz``, the name's
resume marker. ``er_options``: one row per name and evening, with the report date it was fetched for, the gate
inputs, the GEX levels and the option volume; its strikes, term structure and prints go to ``option_strike``,
``option_expiry`` and ``option_print`` as set ``earnings``.

All of it takes a ``date`` or is fetched in the observed window (``uw.observed``) like the rest of the options data, so
the screener row, the calendar and the earnings set of an evening are one moment. A run stops starting calls after
``uw.RUN_BUDGET_S``; the check asks for the same session again while anything is left and the window is open. A
batch or calendar day UW fails with 429, 5xx or a transport error is asked again by the next run.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, timedelta
from pathlib import Path

import duckdb

from ....core import calendar, config
from .. import base
from . import KIND, names, rows, tables, uw

NAME = "uw/option_screener"
SOURCE = tables.SCREENER_SOURCE
OWNED = ("option_screener_daily", "er_options")
DAYS_AHEAD = 28                 # the radar's calendar window (D26)
CALENDAR_SESSIONS = ("premarket", "afterhours")
CALENDAR_LIMIT = 100            # UW's documented maximum for /earnings/{session} (docs/openapi.yaml)
SCREENER_BATCH = 100            # names per screener call (the plan; core/quotes.py UW_BATCH)
SCREENER_LIMIT = 500            # the screener's default page is about 50 rows (UWClient.screener_batch_result)
# A ticker the screener's comma list can carry; anything else is left out with an issue, so one odd name cannot
# make UW refuse the whole batch of 100.
TICKER = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")


def calendar_path(session: date, day: date, report_session: str) -> Path:
    folder = base.raw_root() / "uw" / "earnings_calendar" / session.isoformat()
    return folder / f"{day.isoformat()}-{report_session}.json.gz"


def screener_path(session: date, part: int) -> Path:
    return base.raw_root() / "uw" / "screener" / session.isoformat() / f"part-{part:02d}.json.gz"


def er_path(session: date, ticker: str) -> Path:
    return base.raw_root() / "uw" / "er_options" / session.isoformat() / f"{ticker}.json.gz"


def report_days(session: date) -> list[date]:
    return list(base.iter_sessions(session + timedelta(days=1), session + timedelta(days=DAYS_AHEAD)))


def calendar_files(session: date) -> list[Path]:
    return [calendar_path(session, d, s) for d in report_days(session) for s in CALENDAR_SESSIONS]


def calendar_names(session: date) -> dict[str, tuple[date, str]]:
    """``{ticker: (report day, report session)}`` from the calendar files of ``session`` there are; the first
    report of a name wins."""
    out: dict[str, tuple[date, str]] = {}
    for path in calendar_files(session):
        if not path.exists():
            continue
        doc = base.read_raw_json(path)
        day, rs = date.fromisoformat(doc["report_date"]), doc["report_session"]
        for item in uw.rows_of(doc.get("body")):
            ticker = str((item or {}).get("symbol") or (item or {}).get("ticker") or "").strip().upper()
            if ticker and (ticker not in out or (day, CALENDAR_SESSIONS.index(rs)) <
                           (out[ticker][0], CALENDAR_SESSIONS.index(out[ticker][1]))):
                out[ticker] = (day, rs)
    return out


def batches(names) -> list[list[str]]:
    tickers = sorted(t for t in names if TICKER.match(t))
    return [tickers[i:i + SCREENER_BATCH] for i in range(0, len(tickers), SCREENER_BATCH)]


def passes_gates(option_volume: int, total_oi: int, dollar_volume: float | None) -> bool:
    """Earnings Radar's gates (``core/config.py``). A name whose premium UW did not serve fails the dollar gate."""
    return (option_volume >= config.MIN_OPTION_VOLUME and total_oi >= config.MIN_TOTAL_OI
            and dollar_volume is not None and dollar_volume >= config.MIN_DOLLAR_OPTION_VOLUME)


def premium(item: dict) -> float | None:
    """The day's option premium of a screener row: call plus put premium, None when either is missing. Not volume x
    100: that estimate makes the dollar gate the volume gate again (Earnings Radar review N3)."""
    calls, puts = tables.to_float(item.get("call_premium")), tables.to_float(item.get("put_premium"))
    return None if calls is None or puts is None else calls + puts


def screener_rows(session: date) -> list[dict] | None:
    """The ``option_screener_daily`` rows of ``session``; None until its calendar and every batch are there."""
    if not all(p.exists() for p in calendar_files(session)):
        return None
    cal = calendar_names(session)
    parts = batches(cal)
    if not all(screener_path(session, n).exists() for n in range(len(parts))):
        return None
    found: dict[str, tuple[dict, dict]] = {}
    for n in range(len(parts)):
        doc = base.read_raw_json(screener_path(session, n))
        for item in uw.rows_of(doc.get("body")):
            ticker = str((item or {}).get("ticker") or "").strip().upper()
            if ticker in cal and ticker not in found:
                found[ticker] = (item, doc)
    out = []
    for ticker, (day, rs) in sorted(cal.items()):
        item, doc = found.get(ticker, ({}, None))
        volume = int((tables.to_float(item.get("call_volume")) or 0) + (tables.to_float(item.get("put_volume")) or 0))
        oi = tables.to_int(item.get("total_open_interest")) or 0
        dollar = premium(item)
        out.append({"session_date": session, "ticker": ticker, "report_date": day, "report_session": rs,
                    "screened": doc is not None, "passes_gates": doc is not None and passes_gates(volume, oi, dollar),
                    "option_volume": volume if doc is not None else None,
                    "dollar_option_volume": dollar if doc is not None else None,
                    **tables.numbers(item, tables.SCREENER_DOUBLES),
                    **{f: (None if item.get(f) in (None, "") else str(item[f])) for f in tables.SCREENER_TEXTS},
                    "is_index": tables.to_bool(item.get("is_index")),
                    "next_earnings_date": tables.to_day(item.get("next_earnings_date")),
                    "next_dividend_date": tables.to_day(item.get("next_dividend_date")),
                    "source": "uw", "source_as_of": session,
                    "fetched_at": doc["fetched_at"] if doc else None, "run_id": doc["run_id"] if doc else None,
                    "observed": bool(doc.get("observed")) if doc else None})
    return out


class UWOptionScreener:
    name = NAME
    kind = KIND
    providers = ("uw",)
    tables = OWNED
    starts_by_hand = True

    def default_as_of(self) -> date:
        return calendar.latest_complete_session()

    # ── fetch ──
    def _over_budget(self, ctx: base.RunContext, state: dict) -> bool:
        if ctx.clock() - state["start"] >= uw.RUN_BUDGET_S:
            state["stopped"] = f"the run's {uw.RUN_BUDGET_S // 60} min budget"
            return True
        return False

    def _write(self, ctx: base.RunContext, path: Path, doc: dict, before: int) -> Path:
        return base.write_raw_json(path, {**doc, "observed": True, "fetched_at": time.time(), "run_id": ctx.run_id,
                                          "calls": ctx.calls.get("uw", [0])[0] - before})

    def _calendar(self, ctx: base.RunContext, session: date, state: dict) -> bool:
        """Fetch the calendar files of ``session`` not there yet; True when every one is there."""
        complete = True
        for day in report_days(session):
            for rs in CALENDAR_SESSIONS:
                path = calendar_path(session, day, rs)
                if path.exists():
                    continue
                if self._over_budget(ctx, state):
                    return False
                errors: dict[str, str] = {}
                before = ctx.calls.get("uw", [0])[0]
                try:
                    body = uw.paged(ctx, f"/earnings/{rs}", {"date": day.isoformat()}, "calendar", errors,
                                    key=lambda r: str(r["symbol"]), limit=CALENDAR_LIMIT)
                except uw.NameFailed as e:
                    complete = False
                    ctx.issue("fetch_failed", f"{session} calendar {day} {rs}", str(e)[:300])
                    continue
                if errors:
                    ctx.issue("part_empty", f"{session} calendar {day} {rs}", f"UW refused: {errors['calendar']}")
                self._write(ctx, path, {"session_date": session.isoformat(), "report_date": day.isoformat(),
                                        "report_session": rs, "body": body, "errors": errors}, before)
        return complete

    def _screener(self, ctx: base.RunContext, session: date, state: dict) -> bool:
        """Fetch the screener batches of ``session`` not there yet; True when every one is there."""
        cal = calendar_names(session)
        odd = sorted(t for t in cal if not TICKER.match(t))
        if odd:
            ctx.issue("bad_ticker", f"{session} screener", f"{len(odd)} calendar names the screener cannot take: "
                                                           + ", ".join(odd[:10]))
        complete = True
        for n, batch in enumerate(batches(cal)):
            path = screener_path(session, n)
            if path.exists():
                continue
            if self._over_budget(ctx, state):
                return False
            errors: dict[str, str] = {}
            before = ctx.calls.get("uw", [0])[0]
            try:
                body = uw.get(ctx, "/screener/stocks", {"ticker": ",".join(batch), "date": session.isoformat(),
                                                        "limit": SCREENER_LIMIT}, "screener", errors)
            except uw.NameFailed as e:
                complete = False
                ctx.issue("fetch_failed", f"{session} screener part {n}", str(e)[:300])
                continue
            if errors:
                ctx.issue("part_empty", f"{session} screener part {n}", f"UW refused: {errors['screener']}")
            self._write(ctx, path, {"session_date": session.isoformat(), "part": n, "tickers": batch, "body": body,
                                    "errors": errors}, before)
        return complete

    def _earnings(self, ctx: base.RunContext, session: date, state: dict) -> None:
        """Fetch the earnings-set names of ``session`` without a raw file."""
        day = session.isoformat()
        for r in names.earnings_set(session, screener_rows(session) or [], set(names.list_names())):
            ticker = r["ticker"]
            path = er_path(session, ticker)
            if path.exists():
                continue
            if self._over_budget(ctx, state):
                return
            errors: dict[str, str] = {}
            before = ctx.calls.get("uw", [0])[0]
            try:
                parts = {
                    "spot_strike": uw.paged(ctx, uw.stock(ticker, "spot-exposures/strike"), {"date": day},
                                            "spot_strike", errors, key=lambda x: float(x["strike"])),
                    "gex_levels": uw.get(ctx, uw.stock(ticker, "gex-levels"), {"date": day}, "gex_levels", errors),
                    "term_structure": uw.get(ctx, uw.stock(ticker, "volatility/term-structure"), {"date": day},
                                             "term_structure", errors),
                    "options_volume": uw.get(ctx, uw.stock(ticker, "options-volume"), {"limit": 5}, "options_volume",
                                             errors),
                    "prints": uw.get(ctx, uw.stock(ticker, "flow-recent"), None, "prints", errors),
                }
            except uw.NameFailed as e:
                state["failed"].append(ticker)
                ctx.issue("fetch_failed", f"{session} {ticker}", str(e)[:300])
                continue
            self._write(ctx, path, {
                "session_date": day, "ticker": ticker, "set_name": tables.SET_EARNINGS,
                "report_date": r["report_date"].isoformat(), "report_session": r["report_session"],
                "gates": {"option_volume": r["option_volume"], "total_open_interest": r["total_open_interest"],
                          "dollar_option_volume": r["dollar_option_volume"]},
                "parts": parts, "errors": errors}, before)

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        state = ctx.pending.setdefault(NAME, {"stopped": None, "start": ctx.clock(), "failed": []})
        if not uw.observed(as_of):
            state["stopped"] = "outside the observed window: no UW call"
            return self._raws(as_of)
        try:
            if self._calendar(ctx, as_of, state) and self._screener(ctx, as_of, state):
                self._earnings(ctx, as_of, state)
        except base.CapReached as e:
            state["stopped"] = str(e)
        return self._raws(as_of)

    def _raws(self, session: date) -> list[Path]:
        """The session's calendar and screener files (small, so every run types the session's screener set again),
        and its earnings-set files not typed yet."""
        screener = sorted((base.raw_root() / "uw" / "screener" / session.isoformat()).glob("part-*.json.gz"))
        done = tables.names_done(SOURCE, session)
        earnings = sorted(p for p in (base.raw_root() / "uw" / "er_options" / session.isoformat()).glob("*.json.gz")
                          if p.name.removesuffix(".json.gz") not in done)
        return [p for p in calendar_files(session) if p.exists()] + screener + earnings

    # ── type ──
    def type_earnings(self, ctx: base.RunContext, raw: Path) -> tuple[dict[str, list[dict]], dict]:
        """The table rows of one earnings-set raw file and its ``names`` row."""
        doc = base.read_raw_json(raw)
        session, ticker = date.fromisoformat(doc["session_date"]), doc["ticker"]
        prov = {"source": "uw", "source_as_of": session, "fetched_at": doc["fetched_at"], "run_id": doc["run_id"],
                "observed": bool(doc.get("observed"))}
        parts, errors, gates = doc["parts"], doc.get("errors") or {}, doc.get("gates") or {}

        def issue(kind: str, key: str, detail: str) -> None:
            ctx.issue(kind, key, detail)

        strike = rows.strike_rows(session, ticker, tables.SET_EARNINGS, parts.get("spot_strike"), None, prov, issue)
        expiry = rows.expiry_rows(session, ticker, tables.SET_EARNINGS, prov, term=parts.get("term_structure"))
        prints = rows.print_rows(session, ticker, tables.SET_EARNINGS, parts.get("prints"), prov)
        er = {"session_date": session, "ticker": ticker, "report_date": tables.to_day(doc.get("report_date")),
              "report_session": doc.get("report_session"), "option_volume": tables.to_int(gates.get("option_volume")),
              "total_open_interest": tables.to_int(gates.get("total_open_interest")),
              "dollar_option_volume": tables.to_float(gates.get("dollar_option_volume")),
              "strikes": len(strike), "expiries": len(expiry), "prints": len(prints),
              **rows.gex_levels(parts.get("gex_levels")), **rows.volume_fields(parts.get("options_volume"), session),
              **prov}
        if errors:
            issue("part_empty", f"{session} {ticker}",
                  ("UW refused, stored empty: " + "; ".join(f"{k} {v}" for k, v in sorted(errors.items())))[:300])
        out = {"er_options": [er], "option_strike": strike, "option_expiry": expiry, "option_print": prints}
        return out, {"session_date": session, "ticker": ticker, "set_name": tables.SET_EARNINGS,
                     "raw_bytes": raw.stat().st_size, "calls": doc.get("calls"),
                     "errors": json.dumps(errors, sort_keys=True) if errors else None,
                     "fetched_at": doc["fetched_at"], "run_id": doc["run_id"]}

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        """The screener set of each session the files belong to, then the earnings-set files: one write per table and
        session, and their ``names`` rows last."""
        er_root = base.raw_root() / "uw" / "er_options"
        out: list[Path] = []
        for session in sorted({date.fromisoformat(p.parent.name) for p in raws if er_root not in p.parents}):
            day_rows = screener_rows(session)
            if day_rows:
                out += tables.upsert(SOURCE, tables.TABLES["option_screener_daily"], day_rows)
        by_table: dict[str, list[dict]] = {}
        done = []
        for raw in (p for p in raws if er_root in p.parents):
            typed, name_row = self.type_earnings(ctx, raw)
            for table, table_rows in typed.items():
                by_table.setdefault(table, []).extend(table_rows)
            done.append(name_row)
        out += [p for table, table_rows in sorted(by_table.items()) if table_rows
                for p in tables.upsert(SOURCE, tables.TABLES[table], table_rows)]
        return out + (tables.upsert(SOURCE, tables.NAMES, done) if done else [])

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        ctx.notes.append("no backfill: the earnings set needs the prints and the option volume of the evening, which "
                         "UW serves for the current session only")

    # ── build and check ──
    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        return tables.build_tables(con, OWNED)

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        as_of = ctx.as_of or self.default_as_of()
        state = ctx.pending.get(NAME) or {}
        added = removed = 0
        for table in OWNED:
            a, r = tables.compare_by_day(con, tables.TABLES[table])
            added, removed = added + a, removed + r
        cal_done = all(p.exists() for p in calendar_files(as_of))
        cal = calendar_names(as_of) if cal_done else {}
        parts = batches(cal)
        asked = {t for n, b in enumerate(parts) if screener_path(as_of, n).exists() for t in b}
        day_rows = screener_rows(as_of) or []
        note = [f"{len(cal)} names report in the next {DAYS_AHEAD} days" if cal_done
                else "the calendar is not complete yet",
                f"{sum(r['screened'] for r in day_rows)} screened, "
                f"{sum(r['passes_gates'] for r in day_rows)} pass the gates"]
        left = not cal_done or len(asked) < sum(len(b) for b in parts)
        if day_rows:
            wanted = names.earnings_set(as_of, day_rows, set(names.list_names()))
            done = tables.names_done(SOURCE, as_of)
            have = sum(r["ticker"] in done for r in wanted)
            note.append(f"earnings set {have}/{len(wanted)} for {base.next_due(as_of)}")
            left = left or have < len(wanted)
            if state.get("failed"):
                note.append("failed: " + ", ".join(state["failed"][:10]))
        if left and state.get("stopped"):
            note.append(f"stopped: {state['stopped']}")
        again = left and uw.observed(as_of)
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": as_of.isoformat(),
                "rows": int(con.execute("SELECT count(*) FROM option_screener_daily").fetchone()[0]),
                "added": added, "removed": removed,
                "coverage": (round(len(asked) / len(cal), 4) if cal else (1.0 if cal_done else 0.0)),
                "next_check_due": as_of.isoformat() if again else base.next_due(as_of), "note": "; ".join(note)}


DATASET = UWOptionScreener()
