"""The WRDS worker: runs under the interpreter that has ``wrds`` (``RC_WRDS_PYTHON``), never inside the builder.

On the server ``wrds`` lives in a venv of its own (CPython 3.13: wrds 3.5.0 pins a pandas with no wheel for the
builder's Python 3.14). The builder (``spine/wrds.py``) starts this file once per process with
``<RC_WRDS_PYTHON> -P wrds_worker.py`` (``-P``: the script's own folder is not put on ``sys.path``, so the builder's
``spine/wrds.py`` cannot shadow the ``wrds`` package) and talks to it over stdin/stdout, one JSON object per line:

    request   {"sql": "<query>", "out": "<absolute path>.jsonl.gz"}
    answer    {"ok": true, "rows": <n>}
              {"ok": false, "login": true|false, "error": "<type: message>"}

The first request opens the one WRDS connection (``wrds.Connection``; the user from ``WRDS_USERNAME``, which picks
the line of that user's ``~/.pgpass``; WRDS may ask for a Duo push, which the person confirms). A failed login is
answered with ``login: true`` and the worker exits; a failed query is answered and the worker waits for the next.
End of stdin closes the connection. ``wrds`` prints to stdout and may prompt with ``input()``: the protocol runs on
private copies of the original stdin and stdout, stdout itself goes to stderr, and stdin is the terminal or
``/dev/null`` (``_isolate_streams``). An error sent back is capped and carries no user or host name.

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
import re
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


_PROTO = None                      # the protocol pipe (a copy of the original stdout), set by main()
_HOST = re.compile(r"[\w.-]+\.(?:upenn\.edu|wrds\.\w+)", re.I)


def _say(answer: dict) -> None:
    _PROTO.write(json.dumps(answer) + "\n")
    _PROTO.flush()


def _error(e: BaseException) -> str:
    """A driver error for the answer: without the user name or a host name, then capped (masked first, so the cap
    never leaves part of a name behind)."""
    text = f"{type(e).__name__}: {e}"
    user = (os.getenv("WRDS_USERNAME") or "").strip()
    if user:                                        # the name as a word, not inside one, in any case
        text = re.sub(rf"(?<![\w.-]){re.escape(user)}(?![\w-])", "<user>", text, flags=re.I)
    return _HOST.sub("<host>", text)[:300]


def _isolate_streams():
    """Keep ``wrds`` off the protocol: it prints ("Loading library list...", "Done") and may ``input()`` a prompt.

    The protocol keeps private copies of fds 0 and 1; fd 1 becomes stderr (whatever ``wrds`` prints reaches the person
    at the terminal, never the builder's parser) and fd 0 becomes the terminal when there is one, else ``/dev/null``
    (a prompt with no person is an EOFError, a failed login, never a hang). ``RC_WRDS_NO_TTY=1`` forces ``/dev/null``.
    """
    proto_in = os.fdopen(os.dup(0), "r", encoding="utf-8")
    proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    sys.stdout.flush()
    os.dup2(2, 1)
    tty = None
    if (os.getenv("RC_WRDS_NO_TTY") or "").strip() != "1":
        try:
            tty = os.open("/dev/tty", os.O_RDWR)
        except OSError:
            tty = None
    fd = tty if tty is not None else os.open(os.devnull, os.O_RDONLY)
    os.dup2(fd, 0)
    os.close(fd)
    sys.stdin = open(0, "r", encoding="utf-8", closefd=False)
    return proto_in, proto_out


def _connect():
    import wrds  # noqa: PLC0415 - only here: the import itself is part of the login
    return wrds.Connection(wrds_username=(os.getenv("WRDS_USERNAME") or "").strip())


def main() -> int:
    global _PROTO
    proto_in, _PROTO = _isolate_streams()
    con = None
    for line in proto_in:
        if not line.strip():
            continue
        req = json.loads(line)
        if con is None:
            try:
                con = _connect()
            except BaseException as e:  # noqa: BLE001 - every login failure ends the worker the same way
                _say({"ok": False, "login": True, "error": _error(e)})
                return 2
        try:
            _say({"ok": True, "rows": _write(con, req["sql"], req["out"])})
        except Exception as e:  # noqa: BLE001 - a failed query is answered; the connection stays
            _say({"ok": False, "login": False, "error": _error(e)})
    if con is not None:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
