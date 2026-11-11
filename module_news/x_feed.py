"""X (Twitter) records from avo's Grok automation "X saatlik özet" for the News board (avo, 10 Nov 2026 22:27
Amsterdam: "benim hedefim bunlari news boardina tutturmak"; 22:28: the raw records must be processed first).

The automation sends one mail an hour whose ``KAYITLAR`` part holds one row per tweet::

    YYYY-MM-DD HH:MM | @account | status_id | TICKER1,TICKER2 | kind | option | text

The mails are not machine-clean (measured on 4 mails, 4-10 Nov): rows run together on one line or sit on their own
lines, the block may be wrapped in Markdown ``*``, the text can run past 100 characters, and the body is cut with
"..." so the last row may be incomplete. ``parse`` takes the plain-text body and returns rows ready for
``news_headlines`` plus the rows it refused and why.

A row is kept only when its tweet id is real for its stated time: an X status id carries its own creation time
(snowflake), and it must fall within ``MAX_SKEW_S`` of the row's Amsterdam minute. That one check refuses a made-up
or mistyped id without asking X. ``published_at`` is the id's own time, exact to the millisecond.

Two halves, like the IBKR tape (``tape.py``). ``python -m RevenueChaser.module_news.x_feed`` is the hourly fetcher
(the ``life-rc-x-mail`` unit on homesrv, under ``life-with-keys gmail-imap``, so the system python: this file imports
the standard library only). It reads the label's new mails read-only, appends each as one JSON line to
``x_dir()/<UTC day>.jsonl`` and queues the dashboard job ``news_headlines``. It opens no DuckDB. The dashboard's
``headlines.load_x`` then parses those lines into ``news_headlines`` (the board's X lane).
"""
from __future__ import annotations

import email
import email.policy
import html
import imaplib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

AMS = ZoneInfo("Europe/Amsterdam")
PROVIDER = "grok-x"
# The IMAP folder of avo's Gmail label (measured on homesrv 11 Nov 2026: 275 mails; the web search name 00_invest-grok is not a folder).
LABEL = "00_invest/Grok"
KINDS = frozenset({"news", "level", "option", "macro", "position", "other"})
TEXT_MAX = 280
MAX_SKEW_S = 120
_TWEPOCH_MS = 1288834974657
IMAP_HOST = "imap.gmail.com"
DASHBOARD = "http://127.0.0.1:8000"  # app.dashboard.main's port, as tape.DASHBOARD

_ROW = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*\|\s*@(\w{1,15})\s*\|\s*(\d{15,20})\s*\|"
    r"([^|]*)\|([^|]*)\|([^|]*)\|"
)
# Where a row's text stops when no next row follows: the summary part, the mail footer, or a cut body.
_TEXT_END = re.compile(r"\s*(?:\*?\s*BÖLÜM 2|\*?ÖZET\b|\|\s*Continue (?:reading|conversation)|\n\s*\n|\n\s*SON\b)")
_TICKER = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_COUNT = re.compile(r"KAYIT\s+n\s*=\s*(\d+)")
_TAG = re.compile(r"<[^>]+>")


def status_time(status_id: int) -> float:
    """The creation time (epoch seconds) an X status id carries."""
    return ((int(status_id) >> 22) + _TWEPOCH_MS) / 1000.0


def _minute(stamp: str) -> float:
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=AMS).timestamp()


def _tickers(field: str) -> list[str]:
    out: list[str] = []
    for t in field.replace("$", "").split(","):
        t = t.strip().upper()
        if _TICKER.match(t) and t not in out:
            out.append(t)
    return out


def _clean(field: str) -> str:
    return field.strip().strip("*").strip()


def parse(body: str, fetched_at: float, run_id: str) -> tuple[list[dict], list[dict]]:
    """``(rows, refused)`` from one mail's plain-text body. Each refused item has ``status_id`` and ``reason``."""
    body = (body or "").replace("\r", "")
    matches = list(_ROW.finditer(body))
    rows: list[dict] = []
    refused: list[dict] = []
    seen: set[str] = set()
    for i, m in enumerate(matches):
        stamp, account, sid, tick, kind, option = (g.strip() for g in m.groups())
        stop = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        tail = body[m.end():stop]
        end = _TEXT_END.search(tail)
        text = " ".join(_clean(tail[:end.start()] if end else tail).split())
        kind = _clean(kind).lower()
        option = _clean(option)
        published = status_time(int(sid))
        if sid in seen:
            continue
        if abs(published - _minute(stamp)) > MAX_SKEW_S:
            refused.append({"status_id": sid, "reason": f"id time {published:.0f} is not {stamp} Amsterdam"})
            continue
        if kind not in KINDS:
            refused.append({"status_id": sid, "reason": f"unknown kind {kind!r}"})
            continue
        if not text:
            refused.append({"status_id": sid, "reason": "no text"})
            continue
        seen.add(sid)
        rows.append({
            "headline_id": f"x:{sid}",
            "provider": PROVIDER,
            "published_at": published,
            "headline": text[:TEXT_MAX],
            "source": f"@{account}",
            "tickers": _tickers(tick),
            "url": f"https://x.com/{account}/status/{sid}",
            "kind": kind,
            "option": "" if option in ("", "-") else option,
            "fetched_at": fetched_at,
            "run_id": run_id,
            "run_at": fetched_at,
        })
    return rows, refused


def declared(body: str) -> int | None:
    """The row count the prompt's ``KAYIT n=K`` line declares, or None for a mail without one (older prompt).
    Fewer parsed rows than this means the mail was cut."""
    m = _COUNT.search(body or "")
    return int(m.group(1)) if m else None


def mail_text(raw: bytes) -> str:
    """The plain text of one RFC 822 mail: its text/plain part, else its HTML with the tags removed."""
    return _text(email.message_from_bytes(raw, policy=email.policy.default))


def _text(msg) -> str:
    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        return part.get_content()
    part = msg.get_body(preferencelist=("html",))
    return html.unescape(_TAG.sub(" ", part.get_content())) if part is not None else ""


def read_mails(imap, label: str, after_uid: int) -> list[tuple[int, bytes]]:
    """``(uid, raw mail)`` for every Grok mail in ``label`` with a UID above ``after_uid``, oldest first.

    ``imap`` is a logged-in ``imaplib.IMAP4_SSL``. The label is opened read-only and bodies are fetched with
    ``BODY.PEEK``, so nothing is marked read, moved or deleted."""
    status, _ = imap.select(f'"{label}"', readonly=True)
    if status != "OK":
        raise RuntimeError(f"cannot open label {label}")
    status, data = imap.uid("SEARCH", None, f"UID {after_uid + 1}:*", "FROM", '"noreply@x.ai"')
    if status != "OK":
        raise RuntimeError("IMAP search failed")
    out = []
    # IMAP answers "N:*" with the newest UID even when it is below N, so the bound is checked again here.
    for uid in sorted(int(u) for u in (data[0] or b"").split() if int(u) > after_uid):
        status, parts = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
        raw = next((p[1] for p in parts or () if isinstance(p, tuple)), None)
        if status == "OK" and raw:
            out.append((uid, raw))
    return out


def published_local(row: dict) -> str:
    """The row's time as ``YYYY-MM-DD HH:MM`` in Amsterdam (for the board and for checks)."""
    return datetime.fromtimestamp(row["published_at"], timezone.utc).astimezone(AMS).strftime("%Y-%m-%d %H:%M")


def x_dir(root) -> Path:
    """Where the fetcher keeps the mails: ``<market raw root>/raw/grok_x``."""
    return Path(root) / "raw" / "grok_x"


def _raw_root() -> Path:
    # core.config.MARKET_RAW_DIR's rule, repeated: config imports dotenv, which the system python does not have.
    env = (os.getenv("RC_MARKET_RAW_DIR") or "").strip()
    return Path(env).expanduser().resolve() if env else Path(__file__).resolve().parents[1] / ".market_raw"


def uid_mark(folder: Path) -> int:
    """The newest mail UID already saved (0 before the first run)."""
    try:
        return int(json.loads((folder / "uid_mark.json").read_text())["uid"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def save(mails: list[tuple[int, bytes]], folder: Path, now: float) -> None:
    """Append each mail as one JSON line (``uid``, ``fetched_at``, ``subject``, ``date``, ``text``), then move the UID
    mark. A crash between the two saves a mail twice; the loader's upsert by tweet id keeps one row."""
    if not mails:
        return
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{datetime.fromtimestamp(now, timezone.utc):%Y-%m-%d}.jsonl", "a", encoding="utf-8") as f:
        for uid, raw in mails:
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            f.write(json.dumps({"uid": uid, "fetched_at": now, "subject": str(msg["Subject"] or ""),
                                "date": str(msg["Date"] or ""), "text": _text(msg)}, ensure_ascii=False) + "\n")
    tmp = folder / "uid_mark.json.tmp"
    tmp.write_text(json.dumps({"uid": max(u for u, _ in mails)}))
    os.replace(tmp, folder / "uid_mark.json")


def trigger(urlopen=urllib.request.urlopen) -> str:
    """Queue the dashboard's ``news_headlines`` job; the outcome as a word, never an exception."""
    req = urllib.request.Request(f"{DASHBOARD}/run/news_headlines", method="POST", headers={"X-RC-Panel": "cli"})
    try:
        with urlopen(req, timeout=30) as r:
            return "queued" if r.status == 202 else f"refused {r.status}"
    except urllib.error.HTTPError as e:
        return f"refused {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return f"dashboard_down: {e}"


def run(imap_factory=imaplib.IMAP4_SSL, folder: Path | None = None, now: float | None = None,
        urlopen=urllib.request.urlopen) -> int:
    """One hourly pass. The login comes from ``GMAIL_IMAP_USER`` / ``GMAIL_IMAP_APP_PASSWORD``, which only
    ``life-with-keys gmail-imap`` sets; their values are never printed. Exit code 0, or 2 without them."""
    user, password = os.getenv("GMAIL_IMAP_USER"), os.getenv("GMAIL_IMAP_APP_PASSWORD")
    if not user or not password:
        print("x_feed: GMAIL_IMAP_USER / GMAIL_IMAP_APP_PASSWORD not set (run under life-with-keys gmail-imap)",
              file=sys.stderr)
        return 2
    folder = x_dir(_raw_root()) if folder is None else folder
    now = time.time() if now is None else now
    after = uid_mark(folder)
    # shortcut: the mark is a bare UID; if Gmail ever changes the folder's UIDVALIDITY, reset uid_mark.json by hand.
    with imap_factory(IMAP_HOST, timeout=60) as imap:
        imap.login(user, password)
        mails = read_mails(imap, LABEL, after)
    save(mails, folder, now)
    print(f"x_feed: {len(mails)} new mails after uid {after}" + (f"; {trigger(urlopen)}" if mails else ""))
    return 0


if __name__ == "__main__":
    sys.exit(run())
