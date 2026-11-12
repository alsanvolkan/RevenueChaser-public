"""``uw/far_contracts``: the market's far-dated contracts of a session and, the next morning, how much of each one's
volume became open interest (#746, avo's universe "A: Tum piyasa" of 12 Nov 2026). It replaces nothing yet:
``option_far_print`` and its dataset stay as they are.

The rule is the issue's (written before any data was looked at, #746 step 2); this module is that rule.

**Read 1, the sweep of session D.** ``/screener/option-contracts?min_dte=183&min_premium=50000&limit=250``, ``type``
``call`` then ``put``, ``page`` from 0 until a short page (UW sorts by volume; pages are 0-based). Only in D's
observed window (``uw.observed``). The row carries no session date, so the session is the clock's
(``calendar.latest_complete_session``), stamped by the run. Every row is checked against what was asked: the OCC
symbol's expiry at least ``SWEEP_MIN_DTE`` days after D, the premium at least ``MIN_PREMIUM``, the symbol's right the
side asked. UW drops a parameter it does not honour without an error (``page`` on ``/option-trades``, ``min_dte`` on
``/market/oi-change``), so **one row that breaks a filter fails the run** and no sweep file is written for D. A
symbol on two pages fails the side too (the answer moved while paging). Raw:
``raw/uw/far_contracts/<D>/sweep.json.gz``, written once both sides are whole.

**Read 2, the next session's open interest.** ``/stock/{ticker}/option-contracts?min_dte=175&limit=500``, paged, for
every ticker of D's sweep (the option-list names too: their ``option_eod`` of the next session is fetched only after
that session's close, which would tie this table to another dataset's timing). ``min_dte`` is 175 here, not 183,
because UW counts it from the day of the read, up to a long weekend after D. From ``OI_FROM`` New York on the trading
day after D (UW updates open interest about 06:45) until ``OI_UNTIL`` on the trading day after that, before UW's
next update would make the chain's ``prev_oi`` the next session's. A chain row is checked against its filter like
the sweep's; one that breaks it fails that ticker for the run (the sweep is never held up by a chain). A refused
chain (4xx) is not stored either, so it ends ``stale`` at the close, never ``missing``. Raw:
``raw/uw/far_contracts/<D>/oi/<TICKER>.json.gz``. The sweep is made before any chain, and only until ``OI_UNTIL``
on the morning after D, so its ``open_interest`` is never already the next session's.

- Per contract: the chain row counts only when its ``prev_oi`` equals the sweep row's ``open_interest`` (both are the
  open interest at the start of D); then ``oi_next`` is the chain row's ``open_interest``. Else the row is ``stale``.
- Per ticker, UW has not updated the name yet, so no file is written and the next run asks again inside the window,
  when either holds: one swept contract is provably not updated (its chain row has the sweep's ``open_interest``
  and ``prev_oi`` while those two differ), or ``STALE_TICKER_MIN`` or more swept contracts are in the chain and
  every one of them still has its sweep ``open_interest``.
- When the window ends, ``closed.json.gz`` marks the session; a ticker never read is then ``stale`` too.

**The numbers, raw, none clipped** (``contract_rows``): ``oi_change = oi_next - open_interest``,
``opened_share = oi_change / volume``, ``ask_share = ask_side_volume / volume``, ``multileg_share = multileg_volume /
volume`` (NULL for no volume). Trading alone moves open interest by at most the volume, so ``|oi_change| > volume``
sets ``out_of_range`` (an exercise, an adjustment or a data error); the value is kept. ``roll``: on the same session,
ticker and right, another matched contract has an ``opened_share`` of the opposite sign, both at least
``ROLL_MIN_SHARE`` in size, their volumes within ``ROLL_VOLUME_RATIO`` of each other; ``roll_partner`` is the one with
the closest volume. No label, no score, no tier.

``oi_status``: ``pending`` (sweep only, window open), ``matched``, ``stale``, ``missing`` (the ticker's chain was
read and the contract is not in it).

The UW calls of a session: 10 for the sweep (the 11 Nov 2026 session: 1,376 calls, 798 puts) and one or two a ticker
(406 tickers that session), about 420 to 435 in all. The builder has no UW day cap (``caps.py``).
"""
from __future__ import annotations

import time
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base
from . import KIND, rows, tables, uw

NAME = "uw/far_contracts"
SOURCE = tables.FAR_CONTRACT_SOURCE
OWNED = ("option_far_contract",)

SWEEP_MIN_DTE = 183             # the LEAPS floor (module_leaps), counted from D
MIN_PREMIUM = 50_000
SWEEP_LIMIT = 250               # the screener's documented maximum ("Default 50, Max 250")
SIDES = ("call", "put")
CHAIN_MIN_DTE = 175             # 183 less a long weekend: UW counts min_dte from the day of the read
CHAIN_LIMIT = uw.PAGE_LIMIT     # 500
OI_FROM = dtime(7, 30)          # New York; UW's open-interest update is about 06:45
OI_UNTIL = dtime(6, 0)          # New York, two trading days on: before UW's next update moves prev_oi on
STALE_TICKER_MIN = 3
ROLL_MIN_SHARE = 0.25            # the issue's NVDA roll: the closing leg's share was -0.39
ROLL_VOLUME_RATIO = 1.25
OPEN_SESSIONS = 5               # sweep folders looked at for open interest still to read or close
TICKER_FIELDS = ("ticker_symbol", "ticker", "underlying_symbol")
STATUSES = ("pending", "matched", "stale", "missing")


class FilterBroken(base.SourceError):
    """UW answered rows the request's filter excludes: the filter was not honoured, so nothing of it is kept."""


def session_root(session: date) -> Path:
    return base.raw_root() / "uw" / "far_contracts" / session.isoformat()


def sweep_path(session: date) -> Path:
    return session_root(session) / "sweep.json.gz"


def oi_path(session: date, ticker: str) -> Path:
    return session_root(session) / "oi" / f"{ticker}.json.gz"


def closed_path(session: date) -> Path:
    return session_root(session) / "closed.json.gz"


def oi_window(session: date) -> tuple[datetime, datetime]:
    """``[start, end)`` in New York of the open-interest read of ``session``."""
    first = calendar.next_trading_day(session)
    second = calendar.next_trading_day(first)
    return (datetime.combine(first, OI_FROM, tzinfo=calendar.NY),
            datetime.combine(second, OI_UNTIL, tzinfo=calendar.NY))


def sweep_open(session: date) -> bool:
    """True while ``session`` can be swept: its observed window, and before ``OI_UNTIL`` on the next trading day
    (after UW's open-interest update the screener's ``open_interest`` would be the next session's already)."""
    return uw.observed(session) and calendar.now_ny() < datetime.combine(
        calendar.next_trading_day(session), OI_UNTIL, tzinfo=calendar.NY)


def ticker_of(item: dict) -> str:
    """The underlying of a screener row: UW's ticker field, else the OCC root."""
    for f in TICKER_FIELDS:
        value = str(item.get(f) or "").strip().upper()
        if value:
            return value
    m = rows.OCC.match(str(item.get("option_symbol") or "").replace(" ", "").upper())
    return m["root"] if m else ""


def check_sweep_row(session: date, side: str, item) -> str | None:
    """Why a sweep row breaks the filter it was asked with, or None."""
    if not isinstance(item, dict):
        return "a row that is not an object"
    symbol = item.get("option_symbol")
    occ = rows.parse_occ(symbol)
    if occ is None:
        return f"{symbol!r}: not an OCC symbol"
    expiry, right, _ = occ
    if right != side:
        return f"{symbol}: a {right} on the {side} side"
    if (expiry - session).days < SWEEP_MIN_DTE:
        return f"{symbol}: {(expiry - session).days} days to expiry, under {SWEEP_MIN_DTE}"
    premium = tables.to_float(item.get("premium"))
    if premium is None or premium < MIN_PREMIUM:
        return f"{symbol}: premium {item.get('premium')!r}, under {MIN_PREMIUM}"
    if not ticker_of(item):
        return f"{symbol}: no underlying"
    return None


def check_chain_row(today: date, item) -> str | None:
    """Why a chain row breaks ``min_dte``, or None (counted from the day of the read, as UW counts it)."""
    if not isinstance(item, dict):
        return "a row that is not an object"
    occ = rows.parse_occ(item.get("option_symbol"))
    if occ is None:
        return f"{item.get('option_symbol')!r}: not an OCC symbol"
    if (occ[0] - today).days < CHAIN_MIN_DTE:
        return f"{item.get('option_symbol')}: {(occ[0] - today).days} days to expiry, under {CHAIN_MIN_DTE}"
    return None


def swept(session: date) -> dict[str, dict]:
    """The sweep rows of ``session`` by option symbol (empty without a sweep file)."""
    if not sweep_path(session).exists():
        return {}
    doc = base.read_raw_json(sweep_path(session))
    return {str(r["option_symbol"]).upper(): r for side in SIDES for r in doc["body"].get(side) or []}


def tickers_of(sweep: dict[str, dict]) -> dict[str, list[str]]:
    """``{ticker: [option symbols]}`` of a sweep."""
    out: dict[str, list[str]] = {}
    for symbol, item in sweep.items():
        out.setdefault(ticker_of(item), []).append(symbol)
    return out


def not_updated(contracts: list[str], sweep: dict[str, dict], chain: dict[str, dict]) -> bool:
    """True when UW has not moved a ticker's open interest to the next session yet (see the module doc)."""
    found = [s for s in contracts if s in chain]

    def same(s: str, f: str) -> bool:
        return tables.to_int(chain[s].get(f)) == tables.to_int(sweep[s].get(f))
    for s in found:
        oi, prev = tables.to_int(sweep[s].get("open_interest")), tables.to_int(sweep[s].get("prev_oi"))
        if oi is not None and prev is not None and oi != prev and same(s, "open_interest") and same(s, "prev_oi"):
            return True
    return len(found) >= STALE_TICKER_MIN and all(same(s, "open_interest") for s in found)


def _share(part, whole) -> float | None:
    return None if part is None or not whole else part / whole


def contract_rows(session: date, sweep: dict[str, dict], chains: dict[str, dict], closed: bool,
                  prov: dict) -> list[dict]:
    """``option_far_contract``: one row per swept contract of ``session``. ``chains`` is ``{ticker: {"rows":
    {symbol: chain row}, "fetched_at": ...}}`` of the tickers read; ``closed`` is the end of the read window."""
    out: list[dict] = []
    for symbol, item in sorted(sweep.items()):
        expiry, right, strike = rows.parse_occ(symbol)
        ticker = ticker_of(item)
        row = {"session_date": session, "option_symbol": symbol, "ticker": ticker, "option_type": right,
               "expiry": expiry, "strike": strike, "days_to_expiry": (expiry - session).days,
               "issue_type": None if item.get("issue_type") in (None, "") else str(item["issue_type"]),
               "sector": None if item.get("sector") in (None, "") else str(item["sector"]),
               **tables.numbers(item, tables.FAR_CONTRACT_DOUBLES, tables.FAR_CONTRACT_COUNTS),
               "last_fill": None if item.get("last_fill") in (None, "") else str(item["last_fill"]),
               "next_earnings_date": tables.to_day(item.get("next_earnings_date")),
               "oi_next": None, "chain_prev_oi": None, "oi_change": None, "opened_share": None,
               "out_of_range": None, "roll": None, "roll_partner": None, "oi_fetched_at": None, **prov}
        volume = row["volume"]
        row["ask_share"] = _share(row["ask_side_volume"], volume)
        row["multileg_share"] = _share(row["multileg_volume"], volume)
        chain = chains.get(ticker)
        if chain is None:
            row["oi_status"] = "stale" if closed else "pending"
        elif symbol not in chain["rows"]:
            row["oi_status"], row["oi_fetched_at"] = "missing", chain["fetched_at"]
        else:
            got = chain["rows"][symbol]
            row["oi_fetched_at"] = chain["fetched_at"]
            row["chain_prev_oi"] = tables.to_int(got.get("prev_oi"))
            if row["chain_prev_oi"] is None or row["chain_prev_oi"] != row["open_interest"]:
                row["oi_status"] = "stale"
            else:
                row["oi_status"] = "matched"
                row["oi_next"] = tables.to_int(got.get("open_interest"))
                if row["oi_next"] is not None:
                    row["oi_change"] = row["oi_next"] - row["open_interest"]
                    row["opened_share"] = _share(row["oi_change"], volume)
                    row["out_of_range"] = volume is not None and abs(row["oi_change"]) > volume
        out.append(row)
    _rolls(out)
    return out


def _rolls(out: list[dict]) -> None:
    """Set ``roll`` and ``roll_partner`` on the matched rows of one session (see the module doc)."""
    groups: dict[tuple, list[dict]] = {}
    for r in out:
        if r["oi_status"] == "matched" and r["opened_share"] is not None:
            r["roll"] = False
            groups.setdefault((r["ticker"], r["option_type"]), []).append(r)
    for group in groups.values():
        for a in group:
            if abs(a["opened_share"]) < ROLL_MIN_SHARE:
                continue
            partners = [b for b in group if b is not a and abs(b["opened_share"]) >= ROLL_MIN_SHARE
                        and (a["opened_share"] > 0) != (b["opened_share"] > 0) and a["opened_share"] != 0
                        and b["opened_share"] != 0
                        and max(a["volume"], b["volume"]) <= ROLL_VOLUME_RATIO * min(a["volume"], b["volume"])]
            if partners:
                best = min(partners, key=lambda b: (abs(b["volume"] - a["volume"]), b["option_symbol"]))
                a["roll"], a["roll_partner"] = True, best["option_symbol"]


class UWFarContracts:
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

    def _sweep(self, ctx: base.RunContext, session: date, state: dict) -> None:
        """Both sides of ``session``'s sweep, checked row by row, written once whole."""
        if sweep_path(session).exists() or self._over_budget(ctx, state):
            return
        before = ctx.calls.get("uw", [0])[0]
        body: dict[str, list] = {}
        for side in SIDES:
            errors: dict[str, str] = {}
            params = {"min_dte": SWEEP_MIN_DTE, "min_premium": MIN_PREMIUM, "type": side}
            answer = uw.paged(ctx, "/screener/option-contracts", params, side, errors,
                              key=lambda r: str(r["option_symbol"]).upper(), limit=SWEEP_LIMIT)
            if answer is None:
                raise base.SourceError(f"UW refused the {side} sweep of {session}: {errors.get(side)}")
            for item in answer["data"]:
                why = check_sweep_row(session, side, item)
                if why:
                    raise FilterBroken(f"the {side} sweep of {session} broke its filter: {why}")
            body[side] = answer["data"]
            state["pages"][side] = answer["pages"]
        if not any(body.values()):
            # Every session has far-dated contracts above the floor (2,174 on 11 Nov 2026); none at all is an answer
            # that went wrong (a 200 without a list reads as no rows), and a sweep file is never asked again.
            raise base.SourceError(f"UW answered no far-dated contract for the sweep of {session}")
        self._write(ctx, sweep_path(session), {"session_date": session.isoformat(), "body": body,
                                               "pages": dict(state["pages"])}, before)
        state["written"].add(session)

    def _open_interest(self, ctx: base.RunContext, session: date, state: dict) -> None:
        """The chains of ``session``'s tickers not read yet, inside the read window; the close marker after it."""
        start, end = oi_window(session)
        now = calendar.now_ny()
        if now < start or closed_path(session).exists():
            return
        if now >= end:
            base.write_raw_json(closed_path(session), {"session_date": session.isoformat(), "closed_at": time.time(),
                                                      "run_id": ctx.run_id})
            state["written"].add(session)
            return
        sweep = swept(session)
        today = calendar.today_ny()
        for ticker, contracts in sorted(tickers_of(sweep).items()):
            path = oi_path(session, ticker)
            if path.exists():
                continue
            if self._over_budget(ctx, state):
                return
            errors: dict[str, str] = {}
            before = ctx.calls.get("uw", [0])[0]
            try:
                answer = uw.paged(ctx, uw.stock(ticker, "option-contracts"), {"min_dte": CHAIN_MIN_DTE}, "chain",
                                  errors, key=lambda r: str(r["option_symbol"]).upper(), limit=CHAIN_LIMIT)
            except uw.NameFailed as e:
                state["failed"].append(ticker)
                ctx.issue("fetch_failed", f"{session} {ticker}", str(e)[:300])
                continue
            if answer is None:
                state["failed"].append(ticker)
                ctx.issue("part_empty", f"{session} {ticker}", f"UW refused the chain: {errors.get('chain')}")
                continue
            data = answer.get("data") or []
            broken = next((why for why in map(lambda r: check_chain_row(today, r), data) if why), None)
            if broken:
                state["failed"].append(ticker)
                ctx.issue("filter_broken", f"{session} {ticker}", f"the chain broke its filter: {broken}"[:300])
                continue
            chain = {str(r["option_symbol"]).upper(): r for r in data}
            if not_updated(contracts, sweep, chain):
                state["waiting"].append(ticker)
                continue
            self._write(ctx, path, {"session_date": session.isoformat(), "ticker": ticker,
                                    "read_on": today.isoformat(), "body": data}, before)
            state["written"].add(session)

    def open_sessions(self, as_of: date) -> list[date]:
        """The newest ``OPEN_SESSIONS`` swept sessions up to ``as_of`` still open: no close marker, and a ticker whose
        chain is not read yet."""
        root = base.raw_root() / "uw" / "far_contracts"
        days = sorted(date.fromisoformat(p.parent.name) for p in root.glob("*/sweep.json.gz")
                      if p.parent.name <= as_of.isoformat()) if root.exists() else []
        return [d for d in days[-OPEN_SESSIONS:] if not closed_path(d).exists()
                and any(not oi_path(d, t).exists() for t in tickers_of(swept(d)))]

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        state = ctx.pending.setdefault(NAME, {"stopped": None, "start": ctx.clock(), "failed": [], "waiting": [],
                                              "pages": {}, "written": set()})
        try:
            # The sweep first: it can be made only this evening, a chain can be read again by the next run.
            if sweep_open(as_of):
                self._sweep(ctx, as_of, state)
            elif not sweep_path(as_of).exists():
                state["stopped"] = "outside the sweep window: no sweep"
            for session in self.open_sessions(as_of):
                self._open_interest(ctx, session, state)
        except base.CapReached as e:
            state["stopped"] = str(e)
        return self._raws(as_of, state)

    def _raws(self, as_of: date, state: dict) -> list[Path]:
        """The sweep file of every session to type: written this run, or newer than its Parquet partition (a run
        killed between its fetch and its typing)."""
        out = []
        root = base.raw_root() / "uw" / "far_contracts"
        for p in sorted(root.glob("*/sweep.json.gz")) if root.exists() else []:
            session = date.fromisoformat(p.parent.name)
            part = tables.partition_path(SOURCE, OWNED[0], session)
            newest = max(f.stat().st_mtime for f in p.parent.rglob("*.json.gz"))
            if session in state.get("written", ()) or not part.exists() or newest > part.stat().st_mtime:
                out.append(p)
        return out

    # ── type ──
    def session_rows(self, ctx: base.RunContext, session: date) -> list[dict]:
        doc = base.read_raw_json(sweep_path(session))
        sweep = swept(session)
        chains: dict[str, dict] = {}
        fetched = [doc["fetched_at"]]
        for path in sorted((session_root(session) / "oi").glob("*.json.gz")):
            oi = base.read_raw_json(path)
            chains[oi["ticker"]] = {"rows": {str(r.get("option_symbol") or "").upper(): r for r in oi.get("body") or []
                                             if isinstance(r, dict)}, "fetched_at": oi["fetched_at"]}
            fetched.append(oi["fetched_at"])
        closed = closed_path(session).exists()
        if closed:
            fetched.append(base.read_raw_json(closed_path(session))["closed_at"])
        # The newest file of the session stamps every row, so a re-typed session wins its keys in the Parquet.
        prov = {"source": "uw", "source_as_of": session, "fetched_at": max(fetched), "run_id": ctx.run_id,
                "observed": bool(doc.get("observed"))}
        return contract_rows(session, sweep, chains, closed, prov)

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        out: list[Path] = []
        for session in sorted({date.fromisoformat(p.parent.name) for p in raws}):
            day_rows = self.session_rows(ctx, session)
            if day_rows:
                out += tables.upsert(SOURCE, tables.TABLES[OWNED[0]], day_rows)
        return out

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        ctx.notes.append("no backfill: the sweep is the session's own volume, which UW serves for the latest session")

    # ── build and check ──
    def dirty(self, ctx: base.RunContext):
        from .. import dirty
        return dirty.keyed(ctx, self, "date")

    def full_rebuild_reason(self, ctx: base.RunContext) -> str | None:
        return tables.full_rebuild_reason(ctx)

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        return tables.build_tables(con, OWNED, ctx)

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        as_of = ctx.as_of or self.default_as_of()
        state = ctx.pending.get(NAME) or {}
        added, removed = tables.compare_by_day(con, tables.TABLES[OWNED[0]])
        have_sweep = sweep_path(as_of).exists()
        note = []
        if have_sweep:
            sweep = swept(as_of)
            note.append(f"{as_of}: {len(sweep)} contracts in {len(tickers_of(sweep))} names swept")
        else:
            note.append(f"{as_of}: no sweep yet")
        counts = dict(con.execute("SELECT oi_status, count(*) FROM option_far_contract WHERE session_date = "
                                  "(SELECT max(session_date) FROM option_far_contract WHERE oi_status <> 'pending') "
                                  "GROUP BY oi_status").fetchall())
        if counts:
            note.append("open interest of the newest read session: "
                        + ", ".join(f"{counts.get(s, 0)} {s}" for s in STATUSES if s != "pending"))
        if state.get("waiting"):
            note.append("UW not updated yet: " + ", ".join(state["waiting"][:10]))
        if state.get("failed"):
            note.append("failed: " + ", ".join(state["failed"][:10]))
        if state.get("stopped"):
            note.append(f"stopped: {state['stopped']}")
        # Again the same session while its sweep can still be made or a session still has a chain to read (the
        # morning read waits for 07:30, and ``due.py`` compares days only, so the hourly pass is what reaches it).
        again = (not have_sweep and sweep_open(as_of)) or bool(self.open_sessions(as_of))
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": as_of.isoformat(),
                "rows": int(con.execute("SELECT count(*) FROM option_far_contract").fetchone()[0]),
                "added": added, "removed": removed, "coverage": 1.0 if have_sweep else 0.0,
                "next_check_due": as_of.isoformat() if again else base.next_due(as_of), "note": "; ".join(note)}


DATASET = UWFarContracts()

