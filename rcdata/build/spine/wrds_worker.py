"""The WRDS worker: runs under the interpreter that has ``wrds`` (``RC_WRDS_PYTHON``), never inside the builder.

On the server ``wrds`` lives in a venv of its own (CPython 3.13: wrds 3.5.0 pins a pandas with no wheel for the
builder's Python 3.14). The builder (``spine/wrds.py``) starts this file once per process with
``<RC_WRDS_PYTHON> -P wrds_worker.py`` (``-P``: the script's own folder is not put on ``sys.path``, so the builder's
``spine/wrds.py`` cannot shadow the ``wrds`` package) and talks to it over stdin/stdout, one JSON object per line:

    request   {"sql": "<query>", "out": "<absolute path>.jsonl.gz"}
    answer    {"ok": true, "rows": <n>}
              {"ok": false, "login": true|false, "error": "<type: message>"}

The first request opens the one WRDS connection (``wrds.Connection``; the user from ``WRDS_USERNAME`` when set, the
password from that user's ``~/.pgpass``; WRDS may ask for a Duo push, which the person confirms). A failed login is
answered with ``login: true`` and the worker exits; a failed query is answered and the worker waits for the next.
End of stdin closes the connection.

Each answer is streamed in chunks (``raw_sql(..., chunksize=CHUNK, return_iter=True)`` where the installed ``wrds``
has them, else one frame) to gzip JSON lines, every value a string or null (dates as ``YYYY-MM-DD``, whole floats
as integers), written to ``<out>.tmp`` and renamed, so a file under its real name is whole. Standard library and
``wrds`` (with its pandas) only: this file must not import the RevenueChaser package.
"""
from __future__ import annotations

import gzip
import json
import math
import os
import sys
from datetime import date, datetime

CHUNK = 200_000


def _text(v):
    """A value as the builder reads it: None, or text (a date as ``YYYY-MM-DD``, a whole float as an integer)."""
    if v is None:
        return None
    if isinstance(v, float):
        if math.isnan(v):
            return None
        return str(int(v)) if v.is_integer() else repr(v)
    if isinstance(v, (datetime, date)) or type(v).__name__ in ("Timestamp", "NaTType"):
        try:
            return v.isoformat()[:10] if str(v) != "NaT" else None
        except ValueError:
            return None
    if hasattr(v, "item"):                                     # a numpy scalar
        return _text(v.item())
    return str(v)


def _frames(con, sql: str):
    try:
        result = con.raw_sql(sql, chunksize=CHUNK, return_iter=True)
    except TypeError:                                          # an older wrds without chunked reads
        result = con.raw_sql(sql)
    if hasattr(result, "columns") or isinstance(result, list):
        return [result]
    return result


def _write(con, sql: str, out: str) -> int:
    tmp, rows = out + ".tmp", 0
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            for frame in _frames(con, sql):
                records = frame.to_dict("records") if hasattr(frame, "to_dict") else frame
                for rec in records:
                    fh.write(json.dumps({str(k).lower(): _text(v) for k, v in rec.items()}) + "\n")
                    rows += 1
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    os.replace(tmp, out)
    return rows


def _say(answer: dict) -> None:
    sys.stdout.write(json.dumps(answer) + "\n")
    sys.stdout.flush()


def _connect():
    import wrds  # noqa: PLC0415 - only here: the import itself is part of the login
    user = (os.getenv("WRDS_USERNAME") or "").strip()
    return wrds.Connection(wrds_username=user) if user else wrds.Connection()


def main() -> int:
    con = None
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        if con is None:
            try:
                con = _connect()
            except BaseException as e:  # noqa: BLE001 - every login failure ends the worker the same way
                _say({"ok": False, "login": True, "error": f"{type(e).__name__}: {str(e)[:300]}"})
                return 2
        try:
            _say({"ok": True, "rows": _write(con, req["sql"], req["out"])})
        except Exception as e:  # noqa: BLE001 - a failed query is answered; the connection stays
            _say({"ok": False, "login": False, "error": f"{type(e).__name__}: {str(e)[:300]}"})
    if con is not None:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
