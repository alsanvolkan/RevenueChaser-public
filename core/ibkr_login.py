"""Is the IBKR Gateway logged in, and if not, since when? (#162)

The gateway holds a session that expires and is logged in again by hand. Nothing else in
RevenueChaser can say whether that session is up, so this module answers it with the smallest
read-only question there is, and keeps a record of the answers so the Data Health card can say
**when** it dropped and when it was last up.

**The probe is market-data safe and deliberately tiny.** It opens a TCP connection to the configured
host and port, sends the TWS API version handshake, and reads the reply. That is all. It never asks
for an account, a position, an order or an execution, and it sends nothing past the handshake: those
requests have no code here to make them.

The handshake is nine lines of ``socket`` and ``struct``, so no IBKR library becomes a dependency.
Its shape was read off ``ib_async`` 2.1.0 (``ib_async/client.py:218-224`` for the request,
``:353-355`` for the length prefix, ``:365-395`` for the reply): the client sends ``b"API\\0"``, then a
big-endian 4-byte length, then ``b"v<min>..<max>"``; the gateway answers one length-prefixed message
of NUL-separated fields whose first two are the server version and the connection time.

**Three readings**, and they are the three states the card shows:

- ``DOWN``   - the connection was refused, timed out or could not be made: the gateway is not
  listening, so it is not running or is sitting on its login screen.
- ``SILENT`` - the port accepted the connection but the handshake brought nothing back: the port is
  up and the API session is not usable.
- ``UP``     - the handshake was answered. The server version is kept as the evidence.

A reply that arrives but cannot be parsed still counts as ``UP`` with the version left unknown: a
mistake in the framing here must degrade to "answered, version unknown" and never to a false "down".

**The record is a JSON file, not a store table** (``.cache/ibkr_login/state.json``, written
``tmp`` + ``replace`` like the SEC pull receipts in ``core/sec_files.py``). The dashboard process
holds the single DuckDB writer, and a probe meant to run every few minutes must never queue behind
it or hold it; a file also needs no ``_SCHEMA_VERSION`` bump. It survives a restart, which is what is
required. It does not survive a ``.cache/`` wipe, and the card then reads "no check has run yet"
rather than inventing a history. Writing is bookkeeping: a record that cannot be written never fails
the job.

**Only the job probes.** ``collect`` - what the Data Health card reads - opens no socket: a Refresh
must stay free of side effects and inside its 2.5 s budget. So the card's freshness is the job's, and
a reading older than ``STALE_MINUTES`` is called out, because "nothing has checked recently" means the
schedule is not running and is itself the useful fact.

**Off until it is switched on.** ``RC_IBKR_LOGIN_CHECK`` is empty by default: nothing is probed,
nothing is written, and the card says so. ``RC_IBKR_HOST`` and ``RC_IBKR_PORT`` then default to
loopback and 4001, because the gateway's API port should not be reachable off its own machine.
"""
from __future__ import annotations

import json
import os
import socket
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from RevenueChaser.core import config   # noqa: F401 - imported for its load_dotenv() on .env

# The three readings the probe can make, the two the record can be in before one is made, and the
# one the card shows when the check is switched off.
UP = "up"
SILENT = "silent"
DOWN = "down"
UNKNOWN = "unknown"
NOT_SET = "not set"
_REAL_STATES = (UP, SILENT, DOWN)

# The client-version range the handshake offers, as ``ib_async`` 2.1.0 offers it
# (``ib_async/client.py:88-89``). The gateway picks a version inside it and answers with its own.
MIN_CLIENT_VERSION = 157
MAX_CLIENT_VERSION = 178

# A loopback connect and one small reply: a second is already generous, three leaves room for a
# gateway busy with its own startup. Short on purpose - the job runs often and waits for nobody.
TIMEOUT_SECONDS = 3.0
# How much of the reply is read. The handshake's answer is a few dozen bytes; this is a ceiling, not
# an expectation, and nothing past the first message is looked at.
_READ_BYTES = 4096
# A sane frame length for the handshake reply. Past this the bytes are not a frame of ours, so the
# version stays unknown (the reading is still "answered").
_MAX_FRAME = 1000

# The newest reading may be this old before the card says nothing has checked recently. Tolerant of a
# missed run or two at a quarter-hour cadence; past it, the schedule itself is the problem.
STALE_MINUTES = 90
# How many state changes the record keeps. Enough to show a few weeks of a weekly login cycle.
MAX_CHANGES = 20


@dataclass(frozen=True)
class Reading:
    """One probe's answer: the state, the words behind it, and the server version when it answered."""
    state: str
    detail: str
    server_version: int | None = None


# ── the switch and the target ───────────────────────────────────────────────────────────────────

_YES = ("1", "true", "yes", "on")


def enabled() -> bool:
    """Is the login check switched on? Off (and silent) until ``RC_IBKR_LOGIN_CHECK`` says a yes word."""
    return (os.getenv("RC_IBKR_LOGIN_CHECK") or "").strip().lower() in _YES


def target() -> tuple[str, int]:
    """``(host, port)`` from the environment; loopback and 4001 when unset or unparsable."""
    host = (os.getenv("RC_IBKR_HOST") or "").strip() or "127.0.0.1"
    try:
        port = int((os.getenv("RC_IBKR_PORT") or "").strip())
    except ValueError:
        port = 4001
    return host, port


def state_path() -> Path:
    """Where the readings are recorded. Read at call time, so a redirected cache root is followed."""
    return config.OUTPUT_DIR / "ibkr_login" / "state.json"


# ── the probe ───────────────────────────────────────────────────────────────────────────────────

def _handshake() -> bytes:
    body = f"v{MIN_CLIENT_VERSION}..{MAX_CLIENT_VERSION}".encode()
    return b"API\0" + struct.pack(">I", len(body)) + body


def _server_version(data: bytes) -> int | None:
    """The server version out of the handshake reply, or None when these bytes are not that frame."""
    if len(data) < 5:
        return None
    length = struct.unpack(">I", data[:4])[0]
    if not 0 < length <= _MAX_FRAME or len(data) < 4 + length:
        return None
    try:
        return int(data[4:4 + length].split(b"\0")[0])
    except (ValueError, IndexError):
        return None


def probe(host: str, port: int, *, timeout: float = TIMEOUT_SECONDS, connect=None) -> Reading:
    """Open the port, send the version handshake, read the reply. Never raises; never asks for more.

    ``connect`` is the seam the tests use (``socket.create_connection`` by default), so the suite
    opens no socket at all.
    """
    connect = connect or socket.create_connection
    try:
        sock = connect((host, port), timeout=timeout)
    except OSError as exc:
        return Reading(DOWN, f"Could not connect to {host}:{port} - {exc}")
    try:
        sock.settimeout(timeout)
        sock.sendall(_handshake())
        data = sock.recv(_READ_BYTES)
    except OSError as exc:
        return Reading(SILENT, f"{host}:{port} accepted the connection but the API handshake failed - {exc}")
    finally:
        try:
            sock.close()
        except OSError:
            pass
    if not data:
        return Reading(SILENT, f"{host}:{port} accepted the connection but answered the API handshake "
                               "with nothing")
    version = _server_version(data)
    if version is None:
        return Reading(UP, f"{host}:{port} answered the API handshake; its version could not be read "
                           f"from the {len(data)} bytes it sent")
    return Reading(UP, f"{host}:{port} answered the API handshake, server version {version}", version)


# ── the record ──────────────────────────────────────────────────────────────────────────────────

def _empty() -> dict:
    return {"state": UNKNOWN, "since": None, "last_up": None, "last_down": None,
            "checked_at": None, "checked_detail": None, "server_version": None, "changes": []}


def _iso(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _changes(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    out = []
    for c in value:
        if isinstance(c, dict) and _iso(c.get("at")):
            out.append({"from": str(c.get("from") or UNKNOWN), "to": str(c.get("to") or UNKNOWN),
                        "at": c["at"]})
    return out[-MAX_CHANGES:]


def read(*, path: Path | None = None) -> dict:
    """The record as it stands. A missing, unreadable or malformed file reads ``UNKNOWN``, never ``DOWN``."""
    try:
        stored = json.loads(Path(path or state_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(stored, dict):
        return _empty()
    out = _empty()
    state = stored.get("state")
    if state in _REAL_STATES:
        out["state"] = state
        out["since"] = _iso(stored.get("since"))
    out["last_up"] = _iso(stored.get("last_up"))
    out["last_down"] = _iso(stored.get("last_down"))
    out["checked_at"] = _iso(stored.get("checked_at"))
    out["checked_detail"] = _iso(stored.get("checked_detail"))
    version = stored.get("server_version")
    out["server_version"] = version if isinstance(version, int) and not isinstance(version, bool) else None
    out["changes"] = _changes(stored.get("changes"))
    return out


def record(reading: Reading, *, path: Path | None = None, now: datetime | None = None) -> dict:
    """Fold one reading into the record and write it. Returns the record as it now stands.

    A change is entered only when a real earlier state differs from this one, so the first reading
    opens the state instead of reading as a change out of nowhere.
    """
    path = Path(path or state_path())
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    stamp = now.isoformat()

    out = read(path=path)
    before = out["state"]
    if before != reading.state:
        if before in _REAL_STATES:
            out["changes"] = (out["changes"] + [{"from": before, "to": reading.state, "at": stamp}])[-MAX_CHANGES:]
        out["since"] = stamp
    out["state"] = reading.state
    out["checked_at"] = stamp
    out["checked_detail"] = reading.detail
    out["server_version"] = reading.server_version
    if reading.state == UP:
        out["last_up"] = stamp
    else:
        out["last_down"] = stamp

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass        # bookkeeping: an unwritable record never fails the job
    return out


# ── the words the card shows ────────────────────────────────────────────────────────────────────

def _parse(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def for_how_long(since, now: datetime) -> str:
    """"3 days", "4 hours", "7 minutes" - the biggest unit that is not zero, in plain words."""
    start = _parse(since)
    if start is None:
        return ""
    seconds = max(0, int((now - start).total_seconds()))
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        n = seconds // size
        if n:
            return f"{n} {unit}{'' if n == 1 else 's'}"
    return "under a minute"


_WORDS = {
    UP: "Logged in",
    DOWN: "Login needed",
    SILENT: "Port open, the API is not answering",
}


def collect(*, now: datetime | None = None, path: Path | None = None) -> dict:
    """What the Data Health card shows. Reads the record only - this never probes."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    host, port = target()
    where = f"{host}:{port}"
    if not enabled():
        return {"state": NOT_SET, "target": where, "stale": False, "since": None, "for_text": "",
                "last_up": None, "last_down": None, "checked_at": None, "changes": [],
                "server_version": None, "checked_text": "",
                "text": "Not set up: RC_IBKR_LOGIN_CHECK is empty, so nothing is checked and nothing is "
                        "recorded. Set it, with RC_IBKR_HOST and RC_IBKR_PORT when the gateway is not at "
                        f"{where}, and the check starts reporting."}

    out = read(path=path)
    checked = _parse(out["checked_at"])
    stale = checked is not None and (now - checked).total_seconds() > STALE_MINUTES * 60
    if checked is None:
        checked_text = "No check has run yet."
    elif stale:
        checked_text = (f"Last checked {for_how_long(out['checked_at'], now)} ago, so no check has run "
                        f"for longer than the {STALE_MINUTES} minutes expected: the state below may be out "
                        "of date and the schedule is probably not running.")
    else:
        checked_text = f"Last checked {for_how_long(out['checked_at'], now)} ago."

    if out["state"] == UNKNOWN:
        text = f"Unknown: no check has run yet against {where}."
    else:
        text = f"{_WORDS[out['state']]} ({where})"
    return {**out, "target": where, "stale": stale, "text": text, "checked_text": checked_text,
            "for_text": for_how_long(out["since"], now)}
