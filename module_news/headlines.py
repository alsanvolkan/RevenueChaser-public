"""Market headlines for the News board from the IBKR Gateway (avo, 9 Nov 2026 20:54 Amsterdam: "Ibkr kullanalim").

IBKR serves news per contract, not as one market feed (measured 9 Nov: Dow Jones live, 50+ headlines a name a week,
none for SPY). So the board asks for the Dow Jones headlines (``DJ-N`` news, ``DJ-RT`` real-time) of a fixed list:
the stock watch list first, then the largest US names, at most ``MAX_TICKERS``. Two requests a name (its contract id,
then its headlines), headlines only: the article body is never fetched.

Incremental (S16): a fetch asks only from the newest stored headline's time (less ``OVERLAP_S``), at most
``WINDOW_S`` back, and upserts by a headline id made from the story's words. The same story under two names or two providers is one row whose ``tickers`` lists every
name. A gateway that is down writes nothing, moves no mark and says why; a name it does not know is skipped.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone

from RevenueChaser.core import ibkr_login, rawstore
from RevenueChaser.core.providers import ibkr_tws as ibkr
from RevenueChaser.module_news.calendar import META

TABLE = "news_headlines"
MARK_KEY = "headlines_mark"
# A client id of its own: the IV history client is 73, and a second client with the same id is refused (326).
CLIENT_ID = 74
PROVIDERS = "DJ-N+DJ-RT"
SOURCE = "Dow Jones"
PER_TICKER = 20
MAX_TICKERS = 25
WINDOW_S = 24 * 3600
OVERLAP_S = 600
#: The largest US names, after the watch list, so the board has market news even with an empty list.
BASE_TICKERS = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "BRK.B", "LLY", "XOM")

_META_PREFIX = re.compile(r"^\{[^}]*\}!?")
_IB_TIME = "%Y-%m-%d %H:%M:%S.0"


def tickers(watchlist) -> list[str]:
    """Watch-list names (sorted) first, then ``BASE_TICKERS``, no repeats, at most ``MAX_TICKERS``."""
    out: list[str] = []
    for t in [*sorted(str(x).upper() for x in watchlist or ()), *BASE_TICKERS]:
        if t and t not in out:
            out.append(t)
    return out[:MAX_TICKERS]


def clean(headline: str) -> str:
    """The headline without Dow Jones' ``{A:…:C:…}!`` metadata prefix."""
    return _META_PREFIX.sub("", str(headline or "")).strip()


def _ib_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime(_IB_TIME)


def _epoch(text: str) -> float | None:
    try:
        return datetime.strptime(text.strip(), _IB_TIME).replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, AttributeError):
        return None


def _mark() -> float | None:
    rows = rawstore.query(f"SELECT value FROM {META} WHERE key = ?", [MARK_KEY], db="news")
    try:
        return float(rows[0]["value"]) if rows else None
    except (TypeError, ValueError):
        return None


def fetch(names: list[str], now: float | None = None, run_id: str | None = None,
          target: tuple[str, int] | None = None) -> dict:
    """One incremental pass over ``names``. Returns ``tickers``, ``stored``, ``skipped`` and ``error`` (or None)."""
    now = time.time() if now is None else now
    run_id = run_id or f"news-{int(now)}"
    mark = _mark()
    since = max(now - WINDOW_S, (mark - OVERLAP_S) if mark else now - WINDOW_S)
    host, port = target or ibkr_login.target()
    found: dict[str, dict] = {}            # story key -> row
    skipped: list[str] = []
    try:
        with ibkr.TWSClient(host, port, client_id=CLIENT_ID) as client:
            req = 0
            for name in names:
                req += 1
                try:
                    con_id = client.con_id(req, ibkr.contract_for(name))
                    req += 1
                    items = client.news(req, con_id, PROVIDERS, _ib_time(since), "", PER_TICKER)
                except ibkr.HistoryError:
                    skipped.append(name)
                    continue
                for it in items:
                    text, at = clean(it["headline"]), _epoch(it["time"])
                    if not text or at is None:
                        continue
                    key = re.sub(r"\W+", " ", text.lower()).strip()
                    row = found.setdefault(key, {
                        # By the story's words, not IBKR's article id: DJ-N and DJ-RT carry one story under two ids,
                        # and a later run may meet it first under the other one.
                        "headline_id": "ibkr:" + hashlib.sha1(key.encode()).hexdigest()[:16], "provider": "ibkr", "published_at": at,
                        "headline": text, "source": SOURCE, "tickers": [], "url": None, "fetched_at": now,
                        "run_id": run_id, "run_at": now})
                    if name not in row["tickers"]:
                        row["tickers"].append(name)
    except ibkr.TWSError as e:                 # no session, or the gateway went away: nothing is written
        return {"tickers": len(names), "stored": 0, "skipped": skipped, "error": str(e)}
    rows = list(found.values())
    stored = {r["headline_id"]: r for r in rawstore.query(
        f"SELECT headline_id, tickers FROM {TABLE} WHERE headline_id IN ({', '.join('?' for _ in rows) or 'NULL'})",
        [r["headline_id"] for r in rows], db="news")} if rows else {}
    for r in rows:
        old = json.loads(stored[r["headline_id"]]["tickers"] or "[]") if r["headline_id"] in stored else []
        r["tickers"] = json.dumps(sorted(set(old) | set(r["tickers"])))
    rawstore.write_rows(TABLE, rows)
    # The mark is the newest headline stored, on IBKR's own clock, and moves only when every name answered: a name
    # skipped now has its gap asked for again next time (the upsert makes the overlap harmless).
    if rows and not skipped:
        rawstore.write_rows(META, [{"key": MARK_KEY, "value": repr(max(r["published_at"] for r in rows)),
                                    "run_at": now}])
    return {"tickers": len(names), "stored": len(rows), "skipped": skipped, "error": None}
