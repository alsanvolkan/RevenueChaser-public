"""UW answers typed into the rows of the options tables (``tables.py``). Pure functions: no call, no file.

Each takes the session, the ticker, the set (``list`` or ``earnings``), the answer as UW sent it, and the provenance
of the raw file (``source``, ``source_as_of``, ``fetched_at``, ``run_id``, ``observed``). A row UW stamps on another
day than the session is left out: the answers without a ``date`` parameter are only fetched in the observed window,
and a stray row is the one way a wrong day could still get in.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import Callable

from ....core import calendar
from ....core.option_prints import SIDE_TAGS, is_package, print_side  # noqa: F401 - SIDE_TAGS for readers
from ..uw_spot_expiry_strike import snapshot_day
from . import tables as t
from .uw import rows_of

# An OCC option symbol: root, expiry YYMMDD, C or P, strike times 1,000 in eight digits (``AAPL261120C00200000``).
OCC = re.compile(r"^(?P<root>[A-Z0-9.]+?)(?P<ymd>\d{6})(?P<cp>[CP])(?P<k>\d{8})$")
PRINTS_KEPT = 50        # the plan's "last 50 prints" per name and session

Issue = Callable[[str, str, str], None]


def parse_occ(symbol) -> tuple[date, str, float] | None:
    """``(expiry, "call" | "put", strike)`` of an OCC symbol, or None when it is not one."""
    m = OCC.match(str(symbol or "").replace(" ", "").upper())
    if not m:
        return None
    ymd = m["ymd"]
    try:
        expiry = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
    except ValueError:
        return None
    return expiry, "call" if m["cp"] == "C" else "put", int(m["k"]) / 1000.0


def on_session(row: dict, session: date) -> bool:
    """True when the row names no day, or names the session."""
    day = row.get("date")
    return day in (None, "") or t.to_day(day) == session


def _session_rows(body, session: date) -> list[dict]:
    return [r for r in rows_of(body) if isinstance(r, dict) and on_session(r, session)]


# ── option_eod ─────────────────────────────────────────────────────────────────────────────────────────────────

def eod_rows(session: date, ticker: str, chain: dict, prov: dict, issue: Issue) -> list[dict]:
    """One row per contract of ``chain`` (``{expiry: {"contracts": {"data": rows}, "greeks": body}}``)."""
    out, seen, bad = [], set(), 0
    for expiry_text in sorted(chain):
        part = chain[expiry_text] or {}
        greeks = {}
        for g in _session_rows(part.get("greeks"), session):
            strike = t.to_float(g.get("strike"))
            if strike is not None:
                greeks[strike] = g
        for item in rows_of(part.get("contracts")):
            if not isinstance(item, dict):
                continue
            parsed = parse_occ(item.get("option_symbol"))
            if parsed is None:
                bad += 1
                continue
            symbol = str(item["option_symbol"]).replace(" ", "").upper()
            if symbol in seen:
                issue("duplicate_key", f"{session} {symbol}", "second row of the contract dropped")
                continue
            seen.add(symbol)
            expiry, option_type, strike = parsed
            side = "call" if option_type == "call" else "put"
            g = greeks.get(strike, {})
            row = {"session_date": session, "ticker": ticker, "option_symbol": symbol, "expiry": expiry,
                   "strike": strike, "option_type": option_type, "days_to_expiry": (expiry - session).days,
                   "charm": t.to_float(g.get(f"{side}_charm")), "vanna": t.to_float(g.get(f"{side}_vanna")),
                   "greeks_iv": t.to_float(g.get(f"{side}_volatility")),
                   "last_tape_time": None if item.get("last_tape_time") in (None, "") else str(item["last_tape_time"]),
                   **t.numbers(item, t.CONTRACT_DOUBLES, t.CONTRACT_COUNTS), **prov}
            oi, prev = row["open_interest"], row["prev_oi"]
            row["oi_change"] = oi - prev if oi is not None and prev is not None else None
            out.append(row)
    if bad:
        issue("bad_row", f"{session} {ticker}", f"{bad} contracts without a readable OCC option_symbol")
    return out


# ── option_strike and option_expiry ────────────────────────────────────────────────────────────────────────────

def strike_rows(session: date, ticker: str, set_name: str, spot, exposure, prov: dict, issue: Issue) -> list[dict]:
    """One row per strike: the spot exposures (``spot``) and the greek exposure by strike (``exposure``)."""
    by_strike: dict[float, dict] = {}
    wrong = 0
    for item in rows_of(spot):
        strike = t.to_float(item.get("strike")) if isinstance(item, dict) else None
        if strike is None:
            continue
        if snapshot_day(item.get("time")) not in (session, None):
            wrong += 1
            continue
        by_strike.setdefault(strike, {}).update(
            {"price": t.to_float(item.get("price")), "snapshot_time": item.get("time"),
             **t.numbers(item, t.SPOT_FIELDS)})
    for item in _session_rows(exposure, session):
        strike = t.to_float(item.get("strike"))
        if strike is not None:
            by_strike.setdefault(strike, {}).update(t.numbers(item, t.EXPOSURE_FIELDS))
    if wrong:
        issue("wrong_session", f"{session} {ticker}", f"{wrong} spot-exposure rows stamped on another day")
    return [{"session_date": session, "ticker": ticker, "set_name": set_name, "strike": k, **v, **prov}
            for k, v in sorted(by_strike.items())]


def expiry_of(item: dict) -> date | None:
    """A UW row's expiry: ``expiry``, else ``expires``. ``expiry-breakdown`` sends ``expires`` (the first real sample,
    2026-10-28), though ``docs/openapi.yaml`` names it ``expiry``; read by its spec name only, it gave no expiry, so
    no chain was asked for."""
    return t.to_day(item.get("expiry") or item.get("expires"))


def expiry_rows(session: date, ticker: str, set_name: str, prov: dict, *, breakdown=None, exposure=None, flow=None,
                term=None) -> list[dict]:
    """One row per expiry: the chain's size (``breakdown``), greek exposure, the day's flow and the term structure."""
    by_expiry: dict[date, dict] = {}

    def add(body, fields: dict):
        for item in _session_rows(body, session):
            expiry = expiry_of(item)
            if expiry is not None:
                by_expiry.setdefault(expiry, {}).update({k: f(item) for k, f in fields.items()})

    add(breakdown, {"chains": lambda r: t.to_int(r.get("chains")),
                    "open_interest": lambda r: t.to_int(r.get("open_interest")),
                    "volume": lambda r: t.to_int(r.get("volume"))})
    add(exposure, {f: (lambda r, f=f: t.to_float(r.get(f))) for f in t.EXPOSURE_FIELDS})
    add(flow, {**{f: (lambda r, f=f: t.to_float(r.get(f))) for f in t.FLOW_EXPIRY_DOUBLES},
               **{f: (lambda r, f=f: t.to_int(r.get(f))) for f in t.FLOW_EXPIRY_COUNTS}})
    add(term, {f: (lambda r, f=f: t.to_float(r.get(f))) for f in t.TERM_FIELDS})
    return [{"session_date": session, "ticker": ticker, "set_name": set_name, "expiry": e,
             "days_to_expiry": (e - session).days, **v, **prov} for e, v in sorted(by_expiry.items())]


# ── One row per name and day ───────────────────────────────────────────────────────────────────────────────────

def gex_levels(body) -> dict:
    data = body.get("data") if isinstance(body, dict) else None
    data = data if isinstance(data, dict) else {}
    return {f: t.to_float(data.get(f)) for f in t.GEX_LEVEL_FIELDS}


def volume_fields(body, session: date) -> dict:
    """The options-volume row of the session (its fields empty when UW has none for it)."""
    rows = [r for r in _session_rows(body, session) if t.to_day(r.get("date")) == session]
    return t.numbers(rows[0] if rows else {}, t.VOLUME_DOUBLES, t.VOLUME_COUNTS)


def iv_rank_fields(body, session: date) -> dict:
    rows = [r for r in _session_rows(body, session) if t.to_day(r.get("date")) == session]
    row = rows[-1] if rows else {}
    return {"iv_rank_1y": t.to_float(row.get("iv_rank_1y")), "iv_rank_volatility": t.to_float(row.get("volatility")),
            "close": t.to_float(row.get("close"))}


def iv_rows(session: date, ticker: str, body, prov: dict) -> list[dict]:
    """UW's interpolated IV of the session: one row per tenor (``days``)."""
    out = {}
    for item in _session_rows(body, session):
        days = t.to_int(item.get("days"))
        if days is not None:
            out[days] = {"session_date": session, "ticker": ticker, "days": days,
                         "volatility": t.to_float(item.get("volatility")),
                         "implied_move_perc": t.to_float(item.get("implied_move_perc")),
                         "percentile": t.to_float(item.get("percentile")), **prov}
    return [out[d] for d in sorted(out)]


# ── option_print ───────────────────────────────────────────────────────────────────────────────────────────────

def _executed(item: dict) -> datetime | None:
    text = str(item.get("executed_at") or "").strip()
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _texts(value) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def print_rows(session: date, ticker: str, set_name: str, body, prov: dict, keep: int = PRINTS_KEPT) -> list[dict]:
    """The newest ``keep`` prints executed on the session (New York date), newest first."""
    prints = []
    for item in rows_of(body):
        when = _executed(item) if isinstance(item, dict) else None
        if when is None or when.astimezone(calendar.NY).date() != session:
            continue
        prints.append((when, item))
    prints.sort(key=lambda p: p[0], reverse=True)
    out, seen = [], set()
    for when, item in prints:
        pid = str(item.get("id") or "") or hashlib.sha1(
            json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()[:20]
        if pid in seen:
            continue
        seen.add(pid)
        parsed = parse_occ(item.get("option_chain_id"))
        option_type = str(item.get("option_type") or "").lower() or (parsed[1] if parsed else None)
        out.append({"session_date": session, "ticker": ticker, "set_name": set_name, "print_id": pid,
                    "executed_at": when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "option_symbol": item.get("option_chain_id"),
                    "expiry": t.to_day(item.get("expiry")) or (parsed[0] if parsed else None),
                    "strike": t.to_float(item.get("strike")) if item.get("strike") is not None
                    else (parsed[2] if parsed else None),
                    "option_type": option_type, "exchange": item.get("exchange"),
                    "tags": _texts(item.get("tags")), "report_flags": _texts(item.get("report_flags")),
                    "upstream_condition_detail": item.get("upstream_condition_detail"),
                    "canceled": t.to_bool(item.get("canceled")), "side": print_side(item),
                    "package": is_package(item), **t.numbers(item, t.PRINT_DOUBLES, t.PRINT_COUNTS), **prov})
        if len(out) >= keep:
            break
    return out
