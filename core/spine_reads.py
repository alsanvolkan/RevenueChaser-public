"""The spine reads one board render needs, kept until the spine file changes (Opus review S6, PR 6).

``rcdata.read`` opens the live ``spine.duckdb`` per call, by design (D4: a connection kept open across a swap goes on
reading the build it opened). On the spine default one render asked it for the same answers many times over: the
registry, the filter labels, the universe counts, the classification universe, the security types and the card
badges each read the 7.7k listed names again (16 opens, about 1 s, on the real-data scratch spine of 28 Oct). The
spine only changes when a build swaps the file, so an answer is kept under the file's identity (path, inode, mtime, size)
and read again as soon as that changes. Callers get copies: changing a returned row never changes the kept one.

The rows' ``status`` is the dataset check of when they were read; no board reads it from these.
"""
from __future__ import annotations

import threading

_LOCK = threading.Lock()
_KEPT: dict[tuple, list[dict]] = {}


def clear() -> None:
    with _LOCK:
        _KEPT.clear()


def _file_key(kind: str = "spine"):
    from ..rcdata import serving
    path = serving.live_path(kind)
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), st.st_ino, st.st_mtime_ns, st.st_size)


def _kept(name: str, args: tuple, fetch, kind: str = "spine") -> list[dict]:
    """``fetch()``'s rows, read once per ``kind`` file: kept under ``(name, args, file identity)``; a new file drops
    every older answer read from that file. No file yet is never kept (the first build must be seen at once)."""
    key = _file_key(kind)
    if key is None:
        return [dict(r) for r in fetch()]
    with _LOCK:
        rows = _KEPT.get((name, args, key))
    if rows is None:
        rows = fetch()
        with _LOCK:
            for old in [k for k in _KEPT if k[2][0] == key[0] and k[2] != key]:
                del _KEPT[old]
            _KEPT[(name, args, key)] = rows
    return [dict(r) for r in rows]


def universe(tiers=("A",)) -> list[dict]:
    from ..rcdata import read
    tiers = tuple(str(t) for t in tiers)
    return _kept("universe", tiers, lambda: read.universe(tiers=tiers))


def membership(index: str | None = None) -> list[dict]:
    """Current membership (``on`` None, the fund files), as ``read.membership(index)``."""
    from ..rcdata import read
    return _kept("membership", (index,), lambda: read.membership(index=index))


def identity_all() -> list[dict]:
    """Every ``security`` row, as ``read.identity()``."""
    from ..rcdata import read
    return _kept("identity", (), lambda: read.identity())


def latest_market(tickers) -> dict[str, dict]:
    """``read.latest_market(tickers)``, kept until the prices file changes (PR 7). No prices file yet: ``{}`` without a
    read, so a render before the first prices build opens nothing more than it did."""
    from ..rcdata import read
    if _file_key("prices") is None:
        return {}
    wanted = tuple(sorted({str(t).strip().upper() for t in tickers if str(t or "").strip()}))
    rows = _kept("latest_market", wanted, lambda: list(read.latest_market(wanted).values()), kind="prices")
    return {r["ticker"]: r for r in rows}
