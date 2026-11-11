"""Market headlines for the News board from the IBKR Gateway (avo, 9 Nov 2026 20:54 Amsterdam: "Ibkr kullanalim").

IBKR serves news per contract, not as one market feed (measured 9 Nov: Dow Jones live, 50+ headlines a name a week,
none for SPY). So the board asks for the Dow Jones headlines (``DJ-N`` news, ``DJ-RT`` real-time) of a fixed list:
the stock watch list first, then the largest US names, at most ``MAX_TICKERS``. Two requests a name (its contract id,
then its headlines), headlines only: the article body is never fetched.

Incremental (S16): a fetch asks only from the newest stored headline's time (less ``OVERLAP_S``), at most
``WINDOW_S`` back, and upserts by a headline id made from the story's words. The same story under two names or two providers is one row whose ``tickers`` lists every
name. A gateway that is down writes nothing, moves no mark and says why; a name it does not know is skipped.

``load_tape`` (#673) loads the market-wide headlines the listener (``module_news/tape.py``) appended since the last
load: same ids, ``tickers`` empty (the page's "Piyasa" lane). A story also met under a name keeps that name.

``load_x`` loads the Grok "X saatlik özet" mails the fetcher (``module_news/x_feed.py``) saved since the last load:
one row per tweet, ``provider`` ``grok-x`` (the page's "X" lane), with the tweet's ``kind`` and ``option``.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from RevenueChaser.core import config, ibkr_login, rawstore
from RevenueChaser.core.providers import ibkr_tws as ibkr
from RevenueChaser.module_news import tape, x_feed
from RevenueChaser.module_news.calendar import META

TABLE = "news_headlines"
MARK_KEY = "headlines_mark"
TAPE_MARK_KEY = "tape_mark"
X_MARK_KEY = "x_mark"
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


def story_id(text: str) -> str:
    """By the story's words, not IBKR's article id: DJ-N and DJ-RT carry one story under two ids."""
    key = re.sub(r"\W+", " ", text.lower()).strip()
    return "ibkr:" + hashlib.sha1(key.encode()).hexdigest()[:16]


def _merge_tickers(rows: list[dict]) -> None:
    """Each row's ``tickers`` (a list) joined with what is stored under its id, as the JSON text the table keeps."""
    stored = {r["headline_id"]: r for r in rawstore.query(
        f"SELECT headline_id, tickers FROM {TABLE} WHERE headline_id IN ({', '.join('?' for _ in rows)})",
        [r["headline_id"] for r in rows], db="news")} if rows else {}
    for r in rows:
        old = json.loads(stored[r["headline_id"]]["tickers"] or "[]") if r["headline_id"] in stored else []
        r["tickers"] = json.dumps(sorted(set(old) | set(r["tickers"])))


def _new_lines(folder: Path, key: str, first: str) -> tuple[list[bytes], dict | None, dict | None]:
    """The whole lines appended to ``folder/*.jsonl`` since the ``news_meta`` mark ``key``, and the old and new mark.

    The mark is ``{"file", "offset"}`` of the first byte not loaded; a line still being written (no newline yet)
    waits for the next load. With no mark, files from ``first`` (a file name) on are read."""
    rows_m = rawstore.query(f"SELECT value FROM {META} WHERE key = ?", [key], db="news")
    try:
        mark = json.loads(rows_m[0]["value"]) if rows_m else None
    except (TypeError, ValueError):
        mark = None
    files = sorted(folder.glob("*.jsonl")) if folder.exists() else []
    first = mark["file"] if mark else first
    lines: list[bytes] = []
    new_mark = mark
    for path in (f for f in files if f.name >= first):
        start = int(mark["offset"]) if mark and path.name == mark["file"] else 0
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read()
        done = data[:data.rfind(b"\n") + 1]
        lines.extend(done.splitlines())
        new_mark = {"file": path.name, "offset": start + len(done)}
    return lines, mark, new_mark


def _move_mark(key: str, mark: dict | None, new_mark: dict | None, now: float) -> None:
    if new_mark and new_mark != mark:
        rawstore.write_rows(META, [{"key": key, "value": json.dumps(new_mark), "run_at": now}])


def load_tape(now: float | None = None, run_id: str | None = None) -> dict:
    """Load the tape lines appended since the mark into ``news_headlines``. Returns ``read`` and ``stored``.
    With no mark, only the files of the last ``WINDOW_S`` are read."""
    now = time.time() if now is None else now
    run_id = run_id or f"news-{int(now)}"
    lines, mark, new_mark = _new_lines(tape.tape_dir(), TAPE_MARK_KEY, tape.day_file(now - WINDOW_S).name)
    found: dict[str, dict] = {}
    for line in lines:
        try:
            it = json.loads(line)
            at = float(it["time_ms"]) / 1000.0
        except (ValueError, TypeError, KeyError):
            continue
        text = clean(it.get("headline"))
        if not text:
            continue
        found.setdefault(story_id(text), {
            "headline_id": story_id(text), "provider": "ibkr", "published_at": at, "headline": text,
            "source": SOURCE if str(it.get("provider", "")).startswith("DJ") else str(it.get("provider") or ""),
            "tickers": [], "url": None, "fetched_at": now, "run_id": run_id, "run_at": now})
    rows = list(found.values())
    _merge_tickers(rows)
    if rows:
        rawstore.write_rows(TABLE, rows)
    _move_mark(TAPE_MARK_KEY, mark, new_mark, now)
    return {"read": len(lines), "stored": len(rows)}


def load_x(now: float | None = None, run_id: str | None = None) -> dict:
    """Load the Grok X mails saved since the mark into ``news_headlines``. Returns ``mails``, ``stored``,
    ``refused`` (rows ``x_feed.parse`` turned down) and ``cut`` (mails that declared more rows than they carried).
    With no mark, every saved mail is read (the first load is the backfill)."""
    now = time.time() if now is None else now
    run_id = run_id or f"news-{int(now)}"
    lines, mark, new_mark = _new_lines(x_feed.x_dir(config.MARKET_RAW_DIR), X_MARK_KEY, "")
    found: dict[str, dict] = {}
    mails = refused = cut = 0
    for line in lines:
        try:
            text = str(json.loads(line)["text"])
        except (ValueError, TypeError, KeyError):
            continue
        mails += 1
        rows, bad = x_feed.parse(text, now, run_id)
        refused += len(bad)
        declared = x_feed.declared(text)
        cut += declared is not None and declared > len(rows) + len(bad)
        for r in rows:
            found[r["headline_id"]] = {**r, "tickers": json.dumps(r["tickers"])}
    if found:
        rawstore.write_rows(TABLE, list(found.values()))
    _move_mark(X_MARK_KEY, mark, new_mark, now)
    return {"mails": mails, "stored": len(found), "refused": refused, "cut": int(cut)}


# Contract ids do not change; kept for the dashboard process's life so a 5-minute pass asks one request a name.
_CON_IDS: dict[str, int] = {}


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
                    if name not in _CON_IDS:
                        _CON_IDS[name] = client.con_id(req, ibkr.contract_for(name))
                    con_id = _CON_IDS[name]
                    req += 1
                    items = client.news(req, con_id, PROVIDERS, _ib_time(since), "", PER_TICKER)
                except ibkr.HistoryError:
                    skipped.append(name)
                    continue
                for it in items:
                    text, at = clean(it["headline"]), _epoch(it["time"])
                    if not text or at is None:
                        continue
                    row = found.setdefault(story_id(text), {
                        "headline_id": story_id(text), "provider": "ibkr", "published_at": at,
                        "headline": text, "source": SOURCE, "tickers": [], "url": None, "fetched_at": now,
                        "run_id": run_id, "run_at": now})
                    if name not in row["tickers"]:
                        row["tickers"].append(name)
    except ibkr.TWSError as e:                 # no session, or the gateway went away: nothing is written
        return {"tickers": len(names), "stored": 0, "skipped": skipped, "error": str(e)}
    rows = list(found.values())
    _merge_tickers(rows)
    rawstore.write_rows(TABLE, rows)
    # The mark is the newest headline stored, on IBKR's own clock, and moves only when every name answered: a name
    # skipped now has its gap asked for again next time (the upsert makes the overlap harmless).
    if rows and not skipped:
        rawstore.write_rows(META, [{"key": MARK_KEY, "value": repr(max(r["published_at"] for r in rows)),
                                    "run_at": now}])
    return {"tickers": len(names), "stored": len(rows), "skipped": skipped, "error": None}
