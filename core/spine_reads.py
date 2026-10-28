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


def _file_key():
    from ..rcdata import serving
    path = serving.live_path("spine")
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), st.st_ino, st.st_mtime_ns, st.st_size)


def _kept(name: str, args: tuple, fetch) -> list[dict]:
    """``fetch()``'s rows, read once per spine file: kept under ``(name, args, file identity)``; a new file drops
    every older answer. No file yet is never kept (the first build must be seen at once)."""
    key = _file_key()
    if key is None:
        return [dict(r) for r in fetch()]
    with _LOCK:
        rows = _KEPT.get((name, args, key))
    if rows is None:
        rows = fetch()
        with _LOCK:
            if any(k[2] != key for k in _KEPT):
                _KEPT.clear()
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
