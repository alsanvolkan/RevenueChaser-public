"""What the news tape listener last knew about its gateway and its feeds, as a record the Settings page reads.

#727; avo, 12 Nov 2026: "Evet bir kart ekleyelim". On 11 Nov the two broad-tape feeds (BRFG, DJNL) were refused with IBKR
error 10197 ("No market data during competing live session") 32 times in a row, and the only place that said so was
the unit's journal. The listener (``tape.py``) is its own process and opens no DuckDB, so it writes this small JSON
file instead, whole and atomically; ``collect`` turns it into the words the Data health card shows. Like the login
check's record (``core/ibkr_login.py``) the card reads the file only: it opens no socket and asks the gateway nothing.

A feed has no "you are subscribed" answer from IBKR: the gateway either refuses the request or says nothing until
the first headline. So the record keeps three moments a feed (asked, refused, last headline) and ``collect`` reads
the state off them: refused, live (a headline since the last ask) or subscribed (asked, not refused, nothing yet).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from RevenueChaser.core import config
from RevenueChaser.core.ibkr_login import for_how_long

#: The listener writes at least every ``tape.TRIGGER_S`` (5 minutes); three missed beats and the record is stale.
STALE_MINUTES = 15
#: A headline is written through at most this often; an ask, a refusal and a connection change always are.
FLUSH_S = 60.0
#: A fresh ask is not called "subscribed" until the gateway has had this long to refuse it (it refuses in a second).
GRACE_S = 60.0
COMPETING_SESSION = 10197
#: Said once on the card when a feed is refused with 10197, not once a feed.
HINT_10197 = ("10197 means another session of this IBKR user holds the live market data: Trader Workstation, the "
              "Client Portal website or the IBKR mobile app, or the live user a paper user borrows its data from. "
              "Logging that session out frees the feeds; the listener asks again by itself.")

REFUSED, LIVE, SUBSCRIBED, WAITING, UNKNOWN = "refused", "live", "subscribed", "waiting", "unknown"
_FEED_KEYS = ("asked_at", "refused_at", "refused_since", "retry_at", "last_headline_at")


def state_path() -> Path:
    """Where the record lives. Read at call time, so a redirected cache root is followed."""
    return config.OUTPUT_DIR / "news_tape" / "state.json"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _parse(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def read(*, path: Path | None = None) -> dict:
    """The record as it stands. A missing, unreadable or malformed file reads as no record, never as a refusal."""
    out = {"written_at": None, "gateway": {"state": UNKNOWN, "since": None, "target": None, "detail": None},
           "feeds": {}}
    try:
        stored = json.loads(Path(path or state_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(stored, dict):
        return out
    out["written_at"] = _text(stored.get("written_at"))
    gw = stored.get("gateway") if isinstance(stored.get("gateway"), dict) else {}
    if gw.get("state") in ("connected", "down"):
        out["gateway"] = {"state": gw["state"], "since": _text(gw.get("since")), "target": _text(gw.get("target")),
                          "detail": _text(gw.get("detail"))}
    feeds = stored.get("feeds") if isinstance(stored.get("feeds"), dict) else {}
    for name, f in feeds.items():
        if isinstance(name, str) and isinstance(f, dict):
            row = {k: _text(f.get(k)) for k in _FEED_KEYS}
            code = f.get("code")
            row.update(code=code if isinstance(code, int) and not isinstance(code, bool) else None,
                       text=_text(f.get("text")), refusals=_count(f.get("refusals")),
                       headlines=_count(f.get("headlines")))
            out["feeds"][name] = row
    return out


class Recorder:
    """The listener's side: fold what happens into the record and write it. A write that fails is dropped: this
    is bookkeeping, and an unwritable file must never stop the tape."""

    def __init__(self, *, path: Path | None = None, clock=time.time):
        self.path, self.clock = Path(path or state_path()), clock
        self.rec = read(path=self.path)         # a restart keeps the streak: "refused since" is not reset by it
        self._flushed = 0.0

    def _feed(self, name: str) -> dict:
        return self.rec["feeds"].setdefault(name, {**{k: None for k in _FEED_KEYS}, "code": None, "text": None,
                                                   "refusals": 0, "headlines": 0})

    def write(self) -> None:
        now = self.clock()
        self.rec["written_at"], self._flushed = _iso(now), now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.rec, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def _gateway(self, state: str, target: str, detail: str | None) -> None:
        gw = self.rec["gateway"]
        if gw["state"] != state:
            gw["since"] = _iso(self.clock())
        # Coerced here, not at the call site: a value json cannot write would raise outside write()'s OSError guard.
        gw.update(state=state, target=str(target), detail=str(detail) if detail else None)
        self.write()

    def connected(self, target: str) -> None:
        self._gateway("connected", target, None)

    def down(self, target: str, detail: str) -> None:
        self._gateway("down", target, detail)

    def keep(self, names: list[str]) -> None:
        """Forget a feed the listener no longer asks for, so the card never shows a feed that was switched off."""
        self.rec["feeds"] = {n: f for n, f in self.rec["feeds"].items() if n in names}

    def asked(self, name: str) -> None:
        f, now = self._feed(name), _iso(self.clock())
        if not (f["refused_at"] and f["asked_at"] and f["refused_at"] >= f["asked_at"]):
            f.update(refused_since=None, refusals=0, code=None, text=None)      # the last ask was not refused
        f.update(asked_at=now, retry_at=None)
        self.write()

    def refused(self, name: str, code, text, retry_at: float) -> None:
        f, now = self._feed(name), _iso(self.clock())
        f.update(refused_at=now, refused_since=f["refused_since"] or now, refusals=f["refusals"] + 1,
                 code=code if isinstance(code, int) else None, text=str(text or "") or None, retry_at=_iso(retry_at))
        self.write()

    def headline(self, name: str) -> None:
        f = self._feed(name)
        was_refused = bool(f["refused_since"])
        f.update(last_headline_at=_iso(self.clock()), headlines=f["headlines"] + 1,
                 refused_since=None, refusals=0, code=None, text=None, retry_at=None)
        if was_refused or self.clock() - self._flushed >= FLUSH_S:
            self.write()

    def beat(self) -> None:
        self.write()


class Silent:
    """A recorder that records nothing: what ``tape.listen`` uses when it is given none."""

    def __getattr__(self, name):
        return lambda *a, **k: None


def _since(value, now: datetime) -> str:
    return f"{value[:16].replace('T', ' ')} UTC ({for_how_long(value, now)})" if _parse(value) else ""


def _feed_view(name: str, f: dict, now: datetime, connected: bool) -> dict:
    asked, refused, last = _parse(f["asked_at"]), _parse(f["refused_at"]), _parse(f["last_headline_at"])
    row = {"feed": name, "code": None, "since": "", "detail": "",
           "last_headline": (f"Last headline {for_how_long(f['last_headline_at'], now)} ago" if last else
                             "No headline has arrived yet")}
    fresh = asked is not None and (now - asked).total_seconds() < GRACE_S
    if not connected:
        return {**row, "state": WAITING, "text": "Waiting: the listener is not connected to the gateway"}
    if refused and f["refused_since"] and (asked is None or refused >= asked or fresh):
        code = f["code"]
        n = f["refusals"]
        why = f"IBKR error {code}: {f['text'] or 'no text'}" if code is not None else (f["text"] or "refused")
        detail = f"Refused {n} time{'' if n == 1 else 's'} in a row."
        if _parse(f["retry_at"]):
            detail += f" Asked again at {f['retry_at'][11:16]} UTC."
        return {**row, "state": REFUSED, "code": code, "text": f"Refused - {why}",
                "since": _since(f["refused_since"], now), "detail": detail}
    if asked is None:
        return {**row, "state": UNKNOWN, "text": "Not asked for yet"}
    if last and last >= asked:
        return {**row, "state": LIVE, "text": "Live", "since": _since(f["asked_at"], now)}
    return {**row, "state": SUBSCRIBED, "since": _since(f["asked_at"], now),
            "text": "Subscribed, not refused; no headline since (IBKR sends no confirmation of its own)"}


def collect(*, names: list[str], now: datetime | None = None, path: Path | None = None) -> dict:
    """What the Data health card shows for ``names``, the feeds the listener is set to ask for. Reads the record
    only - no socket, no gateway."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    rec = read(path=path)
    written = _parse(rec["written_at"])
    if written is None:
        return {"state": UNKNOWN, "stale": False, "feeds": [], "gateway": "", "gateway_state": UNKNOWN, "hint": "",
                "lead": "",
                "written_text": "No record yet: the listener has not written one. It writes when it connects, on "
                                "every ask and refusal, and every 5 minutes; a listener started before this card "
                                "existed writes nothing until it is restarted."}
    stale = (now - written).total_seconds() > STALE_MINUTES * 60
    ago = for_how_long(rec["written_at"], now)
    written_text = (f"The listener last wrote {ago} ago, longer than the {STALE_MINUTES} minutes expected: it is "
                    "probably not running, and the lines below are what it last knew." if stale
                    else f"The listener last wrote {ago} ago.")
    gw = rec["gateway"]
    connected = gw["state"] == "connected"
    where = gw["target"] or "the gateway"
    if connected:
        gateway = f"Connected to {where} since {_since(gw['since'], now)}"
    elif gw["state"] == "down":
        gateway = f"Not connected to {where} since {_since(gw['since'], now)}: {gw['detail'] or 'no reason given'}"
    else:
        gateway = "The record does not say whether the listener is connected."
    empty = {**{k: None for k in _FEED_KEYS}, "code": None, "text": None, "refusals": 0, "headlines": 0}
    feeds = [_feed_view(n, rec["feeds"].get(n, empty), now, connected) for n in names]
    states = {f["state"] for f in feeds}
    worst = (REFUSED if REFUSED in states else WAITING if WAITING in states or not connected
             else UNKNOWN if UNKNOWN in states else SUBSCRIBED if SUBSCRIBED in states else LIVE)
    hint = HINT_10197 if any(f["code"] == COMPETING_SESSION for f in feeds) else ""
    return {"state": worst, "stale": stale, "feeds": feeds, "gateway": gateway, "gateway_state": gw["state"],
            "written_text": written_text, "hint": hint, "lead": _lead(worst, feeds, ago if stale else "")}


def _lead(worst: str, feeds: list[dict], stale_ago: str) -> str:
    """The card's first line: the worst feed state in a few words, so a connected gateway with every feed refused
    does not open the card on a green line. A stale record says it is the last known state, not the present one."""
    refused = [f["feed"] for f in feeds if f["state"] == REFUSED]
    if not feeds:
        words = "The listener is set to ask for no feed"
    elif worst == REFUSED:
        words = f"{len(refused)} of {len(feeds)} feeds refused: {', '.join(refused)}"
    elif worst == WAITING:
        words = "No feed is arriving: the listener is not connected to the gateway"
    elif worst == UNKNOWN:
        words = "Not every feed has been asked for yet"
    elif worst == SUBSCRIBED:
        words = "No feed is refused; not every feed has sent a headline since it was asked for"
    else:
        words = "Every feed is live"
    return f"Last known, {stale_ago} ago: {words}" if stale_ago else words
