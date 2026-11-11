"""The X lane's option tweets checked against our own option data (avo, 11 Nov 2026 08:55 Amsterdam: "these are raw,
I need curated processed informational. When it says spx option bla bla, we need to show that option flow from our
sources").

A Grok row of kind ``option`` names a contract in its ``option`` field (``SPY 759C 11/20exp $1.8M ...``). ``contract``
turns that into the OCC symbol; ``check`` asks Unusual Whales for that contract's prints of the tweet's session
(``/option-contract/{id}/flow``, the 50 newest at or above ``MIN_PREMIUM``) and keeps what traded around the tweet
(``BEFORE_S`` before it, ``AFTER_S`` after it): trades, contracts, premium, the side (a print at or above the ask is a
buy, at or below the bid a sell) and the first and last time. Once the next session is open it asks the contract's
daily history (``/historic``) once more for the open-interest change across the session, which says whether the
position was opened (OI up) or closed (OI down).

The result is the row's ``x_check`` (JSON text). ``run`` is the dashboard pass: it checks at most ``MAX_CALLS`` rows a
pass and builds the UW client only when a row is waiting, so the 5-minute News pass costs no call on a quiet hour.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from RevenueChaser.core import rawstore
from RevenueChaser.module_news import x_feed

NY = ZoneInfo("America/New_York")
TABLE = "news_headlines"
MIN_PREMIUM = 25_000          # a tweet is about a block print; this keeps UW's 50 newest prints to the large ones
BEFORE_S, AFTER_S = 4 * 3600, 15 * 60
MAX_CALLS = 20
MAX_TRIES = 3
OI_EVERY_S = 3600             # the next day's open interest is asked at most hourly until UW has it
RECHECK_DAYS = 3              # a row older than this is no longer checked (its OI change is asked within this)
# Index options trade under their weekly root; the tweets write the index name.
_ROOTS = {"SPX": "SPXW", "NDX": "NDXP", "RUT": "RUTW"}
_CONTRACT = re.compile(r"\$?([A-Z]{1,6})\s+\$?(\d+(?:\.\d+)?)\s*([CP])\b(?:[^\d/]{0,6}(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?)?",
                       re.I)


def contract(text: str, published_at: float) -> dict | None:
    """``{"ticker", "strike", "right", "expiry", "occ"}`` from text like ``SPY 759C 11/20exp``, or None without an
    expiry. A month/day before the tweet's own day is next year's."""
    m = _CONTRACT.search(text or "")
    if not m or not m.group(4):
        return None
    ticker, strike, right = m.group(1).upper(), float(m.group(2)), m.group(3).upper()
    day = datetime.fromtimestamp(published_at, NY).date()
    year = int(m.group(6)) % 100 + 2000 if m.group(6) else day.year
    try:
        expiry = date(year, int(m.group(4)), int(m.group(5)))
        if not m.group(6) and expiry < day:
            expiry = date(year + 1, expiry.month, expiry.day)
    except ValueError:
        return None
    occ = f"{_ROOTS.get(ticker, ticker)}{expiry:%y%m%d}{right}{round(strike * 1000):08d}"
    return {"ticker": ticker, "strike": strike, "right": right, "expiry": expiry.isoformat(), "occ": occ}


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _epoch(text) -> float | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def prints_summary(prints: list[dict], published_at: float) -> dict:
    """What traded in the contract around the tweet: ``trades``, ``contracts``, ``premium``, ``ask``/``bid``/``mid``
    contracts, ``first``/``last`` (epoch). ``trades`` 0 when nothing did."""
    out = {"trades": 0, "contracts": 0, "premium": 0.0, "ask": 0, "bid": 0, "mid": 0, "first": None, "last": None}
    for p in prints or []:
        at = _epoch(p.get("executed_at"))
        if at is None or p.get("canceled") or not published_at - BEFORE_S <= at <= published_at + AFTER_S:
            continue
        size = int(_num(p.get("size")) or 0)
        price, ask, bid = _num(p.get("price")), _num(p.get("nbbo_ask")), _num(p.get("nbbo_bid"))
        side = "ask" if price is not None and ask and price >= ask else (
            "bid" if price is not None and bid and price <= bid else "mid")
        out["trades"] += 1
        out["contracts"] += size
        out["premium"] += _num(p.get("premium")) or 0.0
        out[side] += size
        out["first"] = min(at, out["first"] or at)
        out["last"] = max(at, out["last"] or at)
    return out


def oi_change(chains: list[dict], session: str) -> int | None:
    """Open interest on the first day after ``session`` less that on ``session`` (UW's daily row of a day carries the
    open interest at its start), or None while the next day is not there."""
    by_day = {str(r.get("date")): _num(r.get("open_interest")) for r in chains or []}
    later = sorted(d for d in by_day if d > session)
    if session not in by_day or not later or by_day[session] is None or by_day[later[0]] is None:
        return None
    return int(by_day[later[0]] - by_day[session])


def _session(published_at: float) -> str:
    return datetime.fromtimestamp(published_at, NY).date().isoformat()


def check(uw, row: dict, now: float) -> tuple[dict, int]:
    """The row's next ``x_check`` and the UW calls it took. A failed call keeps the step for the next pass (at most
    ``MAX_TRIES``)."""
    state = json.loads(row["x_check"]) if row.get("x_check") else {}
    calls = 0
    if not state:
        c = contract(row.get("option") or "", row["published_at"]) or contract(row.get("headline") or "",
                                                                                row["published_at"])
        if c is None:
            return {"status": "no_contract"}, 0
        state = {**c, "session": _session(row["published_at"]), "status": "pending", "tries": 0}
    if state["status"] == "pending":
        calls += 1
        got = uw.option_contract_flow_result(state["occ"], date=state["session"], min_premium=MIN_PREMIUM)
        if got.status != "ok":
            state["tries"] = state.get("tries", 0) + 1
            if state["tries"] >= MAX_TRIES:
                state["status"] = "failed"
            return state, calls
        s = prints_summary(got.data, row["published_at"])
        state.update(s, status="seen" if s["trades"] else "not_seen", checked_at=now)
    if state["status"] in ("seen", "not_seen") and state.get("oi_change") is None and _session(now) > state["session"]:
        calls += 1
        state["oi_asked_at"] = now
        got = uw.option_contract_historic_result(state["occ"])
        if got.status == "ok":
            state["oi_change"] = oi_change(got.data, state["session"])
    return state, calls


def _waiting(now: float) -> list[dict]:
    rows = rawstore.query(
        f"SELECT * FROM {TABLE} WHERE provider = ? AND kind = 'option' AND published_at >= ? "
        f"ORDER BY published_at DESC", [x_feed.PROVIDER, now - RECHECK_DAYS * 86400], db="news")
    out = []
    for r in rows:
        state = json.loads(r["x_check"]) if r.get("x_check") else {}
        if not state or state.get("status") == "pending" or (
                state.get("status") in ("seen", "not_seen") and state.get("oi_change") is None
                and _session(now) > state.get("session", "") and now - state.get("oi_asked_at", 0) >= OI_EVERY_S):
            out.append(r)
    return out


def run(make_uw, now: float | None = None) -> dict:
    """Check the option rows still waiting. Returns ``checked`` and ``calls``. ``make_uw()`` builds the client, only
    when a row waits; the client is closed after."""
    now = time.time() if now is None else now
    rows = _waiting(now)
    if not rows:
        return {"checked": 0, "calls": 0}
    uw = make_uw()
    calls, done = 0, []
    try:
        for r in rows:
            if calls >= MAX_CALLS:
                break
            state, n = check(uw, r, now)
            calls += n
            done.append({**r, "x_check": json.dumps(state)})
    finally:
        getattr(uw, "close", lambda: None)()
    if done:
        rawstore.write_rows(TABLE, done)
    return {"checked": len(done), "calls": calls}


def _int(n: float) -> str:
    return f"{int(round(n)):,}".replace(",", ".")


def money(v: float) -> str:
    """``$1,82M`` / ``$450 bin`` / ``$900``, Turkish decimals."""
    v = float(v or 0)
    if v >= 1e6:
        return f"${v / 1e6:.2f}M".replace(".", ",")
    if v >= 1e3:
        return f"${v / 1e3:.0f} bin"
    return f"${v:.0f}"


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(x_feed.AMS).strftime("%H:%M")


def facts(state: dict | None) -> str:
    """The one Turkish line the board shows under an option tweet, or "" when there is nothing to say."""
    s = state or {}
    status = s.get("status")
    if status == "seen":
        side = max(("ask", "bid", "mid"), key=lambda k: s.get(k) or 0)
        share = round(100 * (s.get(side) or 0) / max(1, s.get("contracts") or 0))
        word = {"ask": "ask (alım)", "bid": "bid (satım)", "mid": "orta"}[side]
        when = _hm(s["first"]) if s["first"] == s["last"] else f"{_hm(s['first'])}–{_hm(s['last'])}"
        line = (f"UW teyidi: {s['trades']} işlem · {_int(s['contracts'])} kontrat · {money(s['premium'])} · "
                f"%{share} {word} · {when}")
        if s.get("oi_change") is not None:
            oi = s["oi_change"]
            line += f" · ertesi gün OI {'+' if oi >= 0 else '−'}{_int(abs(oi))} ({'açılış' if oi > 0 else 'kapanış'})"
        return line
    if status == "not_seen":
        return f"UW'de tweet saatinde bu kontratta {money(MIN_PREMIUM)} üstü işlem yok"
    if status == "pending":
        return "UW teyidi bekliyor"
    if status == "failed":
        return "UW teyidi alınamadı"
    return ""


def chip(state: dict | None) -> str:
    """``SPY 759C 20.11`` for a parsed contract, else ""."""
    s = state or {}
    if not s.get("occ"):
        return ""
    strike = f"{s['strike']:g}"
    return f"{s['ticker']} {strike}{s['right']} {s['expiry'][8:10]}.{s['expiry'][5:7]}"
