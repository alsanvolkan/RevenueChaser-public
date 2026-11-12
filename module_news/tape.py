"""The News board's live listener (#673; avo, 10 Nov 2026 20:04 Amsterdam: "evet bu sekilde yapalim").

One long-lived process (``python -m RevenueChaser.module_news.tape``, the ``life-rc-news-tape`` unit on homesrv). It
holds one IBKR Gateway connection (client id ``CLIENT_ID``), subscribes to the broad-tape news feeds in ``feeds()``
and appends every headline, as it arrives, to ``tape_dir()/<UTC day>.jsonl``. It opens no DuckDB: the dashboard is
the one writer of ``boards/news.duckdb``, so every ``TRIGGER_S`` the listener queues the dashboard job
``news_headlines`` (``POST /run/news_headlines``), which loads the new lines and runs the per-ticker fetch.

A feed the gateway refuses is dropped with a log line and asked again after ``RESUB_S``; the rest go on. With no feed
left the listener still queues the job every ``TRIGGER_S``, so the per-ticker headlines keep their pace. A gateway
that goes away (its daily restart, or 1101 "data lost", which drops every subscription) is retried after
``RETRY_S``, doubling up to ``RETRY_MAX_S``; the 5-minute schedule carries over the reconnect.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from RevenueChaser.core import config, ibkr_login
from RevenueChaser.core.providers import ibkr_tws as ibkr
from RevenueChaser.module_news import tape_state

log = logging.getLogger(__name__)

# A client id of its own: 73 IV history, 74 the board's per-ticker fetch, 75 the builder's (326 refuses a repeat).
CLIENT_ID = 76
#: Broad-tape feeds asked for when ``RC_NEWS_TAPE_FEEDS`` is not set. Both stream on the paper user (11 Nov 2026).
#: BRFUPDN (Briefing.com Analyst Actions) is free but has no broad tape: IBKR answers 200 "no security definition"
#: for BRFUPDN:BRFUPDN_ALL, so its headlines come only per ticker (module_news.headlines).
DEFAULT_FEEDS = ("BRFG", "DJNL")
TRIGGER_S = 300                      # avo: "5 dk cok yerinde"
READ_WAIT_S = 5.0                    # a quiet socket returns this often, so the trigger is never late by more
RETRY_S, RETRY_MAX_S = 30.0, 300.0
RESUB_S = 1800                       # a refused feed (e.g. 10197, a competing live session) is asked again after this
DASHBOARD = "http://127.0.0.1:8000"  # app.dashboard.main's port
_HEADERS = {"X-RC-Panel": "cli"}     # every non-GET request needs it (CLAUDE.md)


def feeds() -> list[str]:
    raw = os.getenv("RC_NEWS_TAPE_FEEDS", "")
    got = [f.strip().upper() for f in raw.split(",") if f.strip()] if raw.strip() else list(DEFAULT_FEEDS)
    return list(dict.fromkeys(got))


def tape_dir() -> Path:
    return Path(config.MARKET_RAW_DIR) / "raw" / "ibkr" / "news_tape"


def day_file(ts: float) -> Path:
    return tape_dir() / f"{datetime.fromtimestamp(ts, timezone.utc):%Y-%m-%d}.jsonl"


def append(item: dict, feed: str, now: float) -> None:
    """One headline as one JSON line; the line is written whole, so a reader never sees half of one."""
    line = json.dumps({"received_at": now, "feed": feed, "time_ms": item.get("time_ms"),
                       "provider": item.get("provider"), "article_id": item.get("article_id"),
                       "headline": item.get("headline")}, ensure_ascii=False) + "\n"
    path = day_file(now)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def trigger(post=httpx.post) -> str:
    """Queue the dashboard's ``news_headlines`` job; the outcome as a word, never an exception."""
    try:
        r = post(f"{DASHBOARD}/run/news_headlines", headers=_HEADERS, timeout=30)
    except httpx.HTTPError as e:
        return f"dashboard_down: {e}"
    return "queued" if r.status_code == 202 else f"refused {r.status_code}"


def listen(client, names: list[str], *, clock=time.time, post=httpx.post, until=None, state: dict | None = None,
           rec=None) -> None:
    """Subscribe ``names`` on ``client`` and run until the connection fails (``TWSError``) or ``until()`` is true.

    ``state["next_trigger"]`` outlives a reconnect, so a gateway that keeps dropping the socket does not turn the
    5-minute pass into one per reconnect. A refused feed is asked again after ``RESUB_S`` (a competing session ends);
    an error for a request id no longer held is logged and ignored. ``rec`` (``tape_state.Recorder``) is told every
    ask, refusal and headline, and beats with the trigger, so the Settings card can say which feed is refused."""
    state = {} if state is None else state
    rec = tape_state.Silent() if rec is None else rec
    state.setdefault("next_trigger", clock())
    by_req: dict[int, str] = {}
    retry: dict[str, float] = {name: 0.0 for name in names}      # feed -> when to (re)subscribe
    req = 0
    while not (until and until()):
        now = clock()
        for name, at in list(retry.items()):
            if now >= at:
                req += 1
                client.subscribe_news(req, ibkr.news_contract(name))
                by_req[req] = name
                del retry[name]
                rec.asked(name)
        if now >= state["next_trigger"]:
            log.info("news tape: %s (%d feeds live)", trigger(post), len(by_req))
            state["next_trigger"] = now + TRIGGER_S
            rec.beat()
        try:
            item = client.next_news()
        except ibkr.HistoryError as e:
            feed = by_req.pop(getattr(e, "req_id", None), None)
            if feed is None:
                log.info("news tape: error for a request no longer held: %s", e)
            else:
                log.warning("news tape: feed %s refused (%s); asked again in %d min", feed, e, RESUB_S // 60)
                client.cancel_news(e.req_id)        # if the gateway kept it, asking again must not add a second line
                retry[feed] = clock() + RESUB_S
                rec.refused(feed, getattr(e, "code", None), getattr(e, "text", str(e)), retry[feed])
            continue
        if item and item.get("headline") and item["req_id"] in by_req:
            append(item, by_req[item["req_id"]], clock())
            rec.headline(by_req[item["req_id"]])


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    names, wait, state = feeds(), RETRY_S, {}
    rec = tape_state.Recorder()
    rec.keep(names)
    while True:
        host, port = ibkr_login.target()
        opened = time.time()
        try:
            with ibkr.TWSClient(host, port, client_id=CLIENT_ID, timeout=READ_WAIT_S) as client:
                log.info("news tape: connected to %s:%s, feeds %s", host, port, ",".join(names))
                rec.connected(f"{host}:{port}")
                listen(client, names, state=state, rec=rec)
        except ibkr.TWSError as e:
            log.warning("news tape: gateway %s:%s: %s; retry in %.0f s", host, port, e, wait)
            rec.down(f"{host}:{port}", str(e))
        if time.time() - opened >= RETRY_MAX_S:      # a connection that held a while starts the backoff over
            wait = RETRY_S
        time.sleep(wait)
        wait = min(wait * 2, RETRY_MAX_S)


if __name__ == "__main__":
    raise SystemExit(main())
