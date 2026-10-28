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
UW has none), the option volume, the dollar option volume (volume x 100, the radar's own estimate) and
``passes_gates``: the Earnings Radar gates of ``core/config.py`` (option volume >= 500, total open interest >= 200,
dollar option volume >= 50k; ``module_earnings_radar/pipeline.py`` ``preselect``).

Both take a ``date`` but are fetched in the observed window (``uw.observed``) like the rest of the options data, so
the screener row, the calendar and the earnings set of an evening are one moment. A run stops starting calls after
``uw.RUN_BUDGET_S``; the check asks for the same session again while anything is left and the window is open. A
batch or calendar day UW fails with 429, 5xx or a transport error is asked again by the next run.
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta
from pathlib import Path

import duckdb

from ....core import calendar, config
from .. import base
from . import KIND, tables, uw

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


def passes_gates(option_volume: int, total_oi: int, dollar_volume: float) -> bool:
    return (option_volume >= config.MIN_OPTION_VOLUME and total_oi >= config.MIN_TOTAL_OI
            and dollar_volume >= config.MIN_DOLLAR_OPTION_VOLUME)


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
        dollar = float(volume * 100)                # the radar's estimate: $100 a contract
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

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        state = ctx.pending.setdefault(NAME, {"stopped": None, "start": ctx.clock()})
        if not uw.observed(as_of):
            state["stopped"] = "outside the observed window: no UW call"
            return self._raws(as_of)
        try:
            if self._calendar(ctx, as_of, state):
                self._screener(ctx, as_of, state)
        except base.CapReached as e:
            state["stopped"] = str(e)
        return self._raws(as_of)

    def _raws(self, session: date) -> list[Path]:
        """The session's calendar and screener files: small, so every run types the session's screener set again."""
        screener = sorted((base.raw_root() / "uw" / "screener" / session.isoformat()).glob("part-*.json.gz"))
        return [p for p in calendar_files(session) if p.exists()] + screener

    # ── type ──
    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        sessions = sorted({date.fromisoformat(p.parent.name) for p in raws})
        out: list[Path] = []
        for session in sessions:
            day_rows = screener_rows(session)
            if day_rows:
                out += tables.upsert(SOURCE, tables.TABLES["option_screener_daily"], day_rows)
        return out

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
        rows = screener_rows(as_of) or []
        note = [f"{len(cal)} names report in the next {DAYS_AHEAD} days" if cal_done
                else "the calendar is not complete yet",
                f"{sum(r['screened'] for r in rows)} screened, {sum(r['passes_gates'] for r in rows)} pass the gates"]
        left = not cal_done or len(asked) < sum(len(b) for b in parts)
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
