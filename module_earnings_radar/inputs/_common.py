"""What the RC-Earnings-Inputs sweeps share (E0, #372): value readers, the sweep outcome and the stored ticker sets."""
from __future__ import annotations

import math
from datetime import date, datetime

from RevenueChaser.core import rawstore

CALL_CAP = "call_cap"      # the error a capped client gives a request it refused (core/nightly_inputs.py)


def num(value) -> float | None:
    """A finite float, or None: a missing or unreadable field stays NULL, never 0. A Yahoo ``{"raw": x}`` cell is
    read as ``x``."""
    if isinstance(value, dict):
        value = value.get("raw")
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def text(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def json_safe(value):
    """``value`` with every non-finite float as None and dates as ISO text, so a JSON column accepts it."""
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def error_of(res) -> str:
    return str((res.error or {}).get("error") or "failed")


def outcome(asked: int, errors: list[str], **extra) -> dict:
    """A sweep's result from its requests: ``failed`` when every request failed and one not for the cap, ``partial``
    when some failed (``note`` says why), else ``ok``."""
    hard = [e for e in errors if e != CALL_CAP]
    res = {"asked": asked, "failed_requests": len(errors), **extra}
    if asked and len(errors) == asked and hard:
        res.update(status="failed", error=f"all {asked} requests failed (first: {hard[0]})")
    elif errors:
        note = [f"{len(hard)} of {asked} requests failed (first: {hard[0]})"] if hard else []
        if len(hard) < len(errors):
            note.append(CALL_CAP)
        res.update(status="partial", note="; ".join(note))
    else:
        res["status"] = "ok"
    return res


def calendar_tickers(session: date) -> list[str]:
    """The tickers of the newest stored calendar (D26) with an earnings date on or after ``session``, nearest first.

    Read from the store, not from tonight's D26 sweep, so a failed calendar sweep still leaves the last good one."""
    rows = rawstore.query(
        "SELECT ticker, min(earnings_date) AS d FROM earnings_calendar "
        "WHERE session_date = (SELECT max(session_date) FROM earnings_calendar WHERE session_date <= ?) "
        "AND earnings_date >= ? GROUP BY ticker ORDER BY d, ticker", [session, session])
    return [r["ticker"] for r in rows if r.get("ticker")]


def stored_tickers(table: str, session: date) -> set[str]:
    """Tickers ``table`` already holds for ``session`` (a second run of the night does not ask for them again)."""
    return {r["ticker"] for r in rawstore.query(f"SELECT DISTINCT ticker FROM {table} WHERE session_date = ?",
                                                [session])}
