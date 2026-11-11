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

import html
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
    contracts, ``first``/``last`` (epoch) and ``pts``, each print as ``[epoch, size, premium, side]`` (at most UW's
    50). ``trades`` 0 when nothing did."""
    out = {"trades": 0, "contracts": 0, "premium": 0.0, "ask": 0, "bid": 0, "mid": 0, "first": None, "last": None,
           "pts": []}
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
        out["pts"].append([round(at, 1), size, round(_num(p.get("premium")) or 0.0), side])   # the card's chart
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


def session_day(published_at: float) -> str:
    return datetime.fromtimestamp(published_at, NY).date().isoformat()


def _row_ticker(row: dict, state: dict) -> str:
    if state.get("ticker"):
        return state["ticker"]
    try:
        got = json.loads(row.get("tickers") or "[]")
    except ValueError:
        return ""
    return str(got[0]).upper() if isinstance(got, list) and got else ""


def bars_line(candles: list[dict]) -> list[list]:
    """UW intraday candles as ``[[epoch, close], ...]``, oldest first (pre-market and after-hours kept)."""
    out = {}
    for c in candles or []:
        at, close = _epoch(c.get("start_time")), _num(c.get("close"))
        if at is not None and close is not None:
            out[round(at)] = round(close, 4)
    return [[t, out[t]] for t in sorted(out)]


def _close_of(session: str) -> float:
    d = date.fromisoformat(session)
    return datetime(d.year, d.month, d.day, 16, 20, tzinfo=NY).timestamp()


def _px_due(state: dict, session: str, now: float) -> bool:
    """The price chart is asked until a fetch lands after the session's close, at most hourly."""
    if state.get("px_tries", 0) >= MAX_TRIES:
        return False
    at = state.get("px_at")
    return at is None or (at < _close_of(session) and now - at >= OI_EVERY_S)


def check(uw, row: dict, now: float, px_cache: dict | None = None) -> tuple[dict, int]:
    """The row's next ``x_check`` and the UW calls it took. A failed call keeps the step for the next pass (at most
    ``MAX_TRIES``). Every X row with a ticker also gets its session's 5-minute closes (``px``), one call a ticker and
    session a pass (``px_cache``)."""
    px_cache = {} if px_cache is None else px_cache
    state = json.loads(row["x_check"]) if row.get("x_check") else {}
    calls = 0
    if not state:
        c = None
        if row.get("kind") == "option":
            c = contract(row.get("option") or "", row["published_at"]) or contract(row.get("headline") or "",
                                                                                    row["published_at"])
        state = ({**c, "session": session_day(row["published_at"]), "status": "pending", "tries": 0} if c else
                 {"status": "no_contract" if row.get("kind") == "option" else "x_only"})
    if state["status"] == "pending":
        calls += 1
        got = uw.option_contract_flow_result(state["occ"], date=state["session"], min_premium=MIN_PREMIUM)
        if got.status != "ok":
            state["tries"] = state.get("tries", 0) + 1
            if state["tries"] >= MAX_TRIES:
                state["status"] = "failed"
        else:
            s = prints_summary(got.data, row["published_at"])
            state.update(s, status="seen" if s["trades"] else "not_seen", checked_at=now)
    if state["status"] in ("seen", "not_seen") and state.get("oi_change") is None and session_day(now) > state["session"]:
        calls += 1
        state["oi_asked_at"] = now
        got = uw.option_contract_historic_result(state["occ"])
        if got.status == "ok":
            state["oi_change"] = oi_change(got.data, state["session"])
    tk, session = _row_ticker(row, state), session_day(row["published_at"])
    if tk and _px_due(state, session, now):
        if (tk, session) not in px_cache:
            calls += 1
            got = uw.intraday_bars_result(tk, session)
            px_cache[(tk, session)] = bars_line(got.data) if got.status == "ok" else None
        if px_cache[(tk, session)] is None:
            state["px_tries"] = state.get("px_tries", 0) + 1
        else:
            state.update(px=px_cache[(tk, session)], px_at=now, px_tk=tk)
    return state, calls


def _waiting(now: float) -> list[dict]:
    rows = rawstore.query(
        f"SELECT * FROM {TABLE} WHERE provider = ? AND kind <> 'other' AND published_at >= ? "
        f"ORDER BY published_at DESC", [x_feed.PROVIDER, now - RECHECK_DAYS * 86400], db="news")
    out = []
    for r in rows:
        state = json.loads(r["x_check"]) if r.get("x_check") else {}
        if not state or state.get("status") == "pending" or (
                state.get("status") in ("seen", "not_seen") and state.get("oi_change") is None
                and session_day(now) > state.get("session", "") and now - state.get("oi_asked_at", 0) >= OI_EVERY_S) or (
                _row_ticker(r, state) and _px_due(state, session_day(r["published_at"]), now)):
            out.append(r)
    return out


def run(make_uw, now: float | None = None) -> dict:
    """Check the X rows still waiting. Returns ``checked`` and ``calls``. ``make_uw()`` builds the client, only
    when a row waits; the client is closed after."""
    now = time.time() if now is None else now
    rows = _waiting(now)
    if not rows:
        return {"checked": 0, "calls": 0}
    uw = make_uw()
    calls, done, px_cache = 0, [], {}
    try:
        for r in rows:
            if calls >= MAX_CALLS:
                break
            state, n = check(uw, r, now, px_cache)
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


def chart_svg(state: dict | None, published_at: float) -> str:
    """The card's print chart: one bar a print over time, its height the premium, a buy (ask) above the line and a
    sell (bid) below it, mid prints as short grey ticks, and a dashed line at the tweet. Each bar's ``<title>`` is its
    hover text; the times under it are the chart's two ends. "" when there are no prints."""
    pts = (state or {}).get("pts") or []
    if not pts:
        return ""
    lo = min(min(p[0] for p in pts), published_at)
    hi = max(max(p[0] for p in pts), published_at)
    pad = max(300.0, (hi - lo) * 0.06)
    lo, hi = lo - pad, hi + pad
    top = max(p[2] for p in pts) or 1.0
    w, h, mid = 600.0, 72.0, 36.0

    def x(t):
        return round((t - lo) / (hi - lo) * w, 1)

    bars = []
    for at, size, prem, side in sorted(pts, key=lambda p: p[2]):          # big prints drawn last, on top
        tip = f"{_hm(at)} · {_int(size)} kontrat · {money(prem)} · " + {"ask": "ask (alım)", "bid": "bid (satım)",
                                                                       "mid": "orta"}[side]
        if side == "mid":
            bars.append(f'<rect class="pm" x="{x(at) - 1.5}" y="{mid - 4}" width="3" height="8" rx="1.5">'
                        f'<title>{tip}</title></rect>')
            continue
        bh = max(3.0, (mid - 4) * prem / top)
        y = mid - 1 - bh if side == "ask" else mid + 1
        bars.append(f'<rect class="{"pa" if side == "ask" else "pb"}" x="{x(at) - 2}" y="{round(y, 1)}" width="4" '
                    f'height="{round(bh, 1)}" rx="2"><title>{tip}</title></rect>')
    tx = x(published_at)
    return (f'<figure class="xc"><svg viewBox="0 0 {w:.0f} {h:.0f}" preserveAspectRatio="none" role="img" '
            f'aria-label="Kontratın işlemleri: üstte alım, altta satım, kesik çizgi tweet anı">'
            f'<line class="xb" x1="0" y1="{mid}" x2="{w:.0f}" y2="{mid}"/>'
            f'<line class="xt" x1="{tx}" y1="0" x2="{tx}" y2="{h:.0f}"/>{"".join(bars)}</svg>'
            f'<figcaption><span>{_hm(lo)}</span><span class="lg"><i class="pa"></i>alım <i class="pb"></i>satım '
            f'<i class="xt"></i>tweet</span><span>{_hm(hi)}</span></figcaption></figure>')


#: The row's verification pill: (class, icon, label, hover text). Icon and word carry it, never the color alone.
_BADGES = {
    "seen": ("ok", "✓", "UW teyitli", "Unusual Whales'ta tweet saatinde bu kontratta işlem var"),
    "not_seen": ("no", "✗", "UW'de yok", f"Unusual Whales'ta tweet saatinde bu kontratta {money(MIN_PREMIUM)} üstü "
                                          "işlem yok"),
    "pending": ("wait", "◷", "UW bekliyor", "Unusual Whales henüz sorulmadı"),
    "failed": ("warn", "!", "UW alınamadı", "Unusual Whales üç denemede cevap vermedi"),
    "no_contract": ("wait", "–", "kontrat okunamadı", "Tweet'te vade ve strike birlikte yok; UW'de aranamadı"),
}
_X_ID = ("id", "✓", "X kimliği", "Tweet'in X kimliğindeki zaman, mailin saatiyle eşleşti")


def badge(state: dict | None) -> tuple[str, str, str, str]:
    """Every stored X row passed the X-id time check (``x_feed``); an option row says what UW found instead."""
    return _BADGES.get((state or {}).get("status"), _X_ID)


def _price(v: float) -> str:
    return f"{v:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")


def price_at(px: list, at: float) -> float | None:
    """The close of the last bar that started at or before ``at``."""
    got = None
    for t, close in px or []:
        if t > at:
            break
        got = close
    return got


def price_move(state: dict | None, at: float) -> str:
    """``fiyat 712,40 → 714,10 (+0,24%)``: at the tweet and the session's last bar, or "" without bars."""
    px = (state or {}).get("px") or []
    then = price_at(px, at)
    if not px or then is None or px[-1][0] <= at:
        return f"fiyat {_price(then)}" if then else ""
    last = px[-1][1]
    pct = (last / then - 1) * 100
    sign = "+" if pct >= 0 else "−"
    return (f"fiyat {_price(then)} → {_price(last)} ({sign}" + f"{abs(pct):.2f}".replace(".", ",") + "%)")


def price_svg(px: list, marks: list[tuple[float, str]]) -> str:
    """A ticker's session as a line of 5-minute closes, regular hours plus any tweet outside them, each tweet a dot on
    the line (``marks``: epoch and hover text). "" without bars."""
    if not px:
        return ""
    day = datetime.fromtimestamp(px[-1][0], NY)
    open_, close = (datetime(day.year, day.month, day.day, h, m, tzinfo=NY).timestamp() for h, m in ((9, 30), (16, 0)))
    lo = min([open_] + [t - 900 for t, _ in marks])
    hi = max([close] + [t + 900 for t, _ in marks])
    pts = [p for p in px if lo <= p[0] <= hi]
    if len(pts) < 2:
        return ""
    lo, hi = pts[0][0], pts[-1][0]
    pmin, pmax = min(p[1] for p in pts), max(p[1] for p in pts)
    span = (pmax - pmin) or 1.0
    w, h, pad = 600.0, 96.0, 8.0

    def x(t):
        return round((t - lo) / ((hi - lo) or 1) * w, 1)

    def y(v):
        return round(pad + (pmax - v) / span * (h - 2 * pad), 1)

    line = " ".join(f"{x(t)},{y(v)}" for t, v in pts)
    step = w / max(1, len(pts) - 1)
    hits = "".join(f'<rect class="hit" x="{round(x(t) - step / 2, 1)}" y="0" width="{round(step, 1)}" height="{h:.0f}">'
                   f'<title>{_hm(t)} · {_price(v)}</title></rect>' for t, v in pts)
    dots = []                                  # HTML over the stretched SVG, so a dot stays round at any width
    for at, tip in marks:
        v = price_at(pts, at)
        if v is not None and lo <= at <= hi + 300:
            dots.append(f'<i class="tw" style="left:{x(min(at, hi)) / w * 100:.2f}%;top:{y(v):.1f}px" '
                        f'title="{html.escape(tip, quote=True)} · {_price(v)}"></i>')
    first, last = pts[0][1], pts[-1][1]
    trend = "up" if last >= first else "down"
    return (f'<figure class="xp"><svg viewBox="0 0 {w:.0f} {h:.0f}" preserveAspectRatio="none" role="img" '
            f'aria-label="Seansın 5 dakikalık fiyatı, noktalar tweet anları">'
            f'<polyline class="ln {trend}" points="{line}"/>{hits}</svg>{"".join(dots)}'
            f'<figcaption><span>{_hm(lo)}</span><span>düşük {_price(pmin)} · yüksek {_price(pmax)}</span>'
            f'<span class="lg"><i class="tw"></i>tweet</span><span>{_hm(hi)}</span></figcaption></figure>')
