"""Versioned peer sets for the Step 0 validation package (Relatedness v2, #341).

Two files under ``sets/`` (not ``data/``, which the repo ignores as a module cache):

- ``hand_sets.json``: 10 hand-written peer sets, ids 1-10; 1-6 are clear peers, 7-10 are borderline by design (a
  diversified or mixed name beside pure-play rivals), so precision is not inflated by easy cases. Claude proposed
  them and avo reviewed them on 1 Nov 2026. **Hand sets alone never gate a cutover**: they began from a model's
  judgement, as the profiles do, so they can share its blind spots. The ETF sets and the pair sheet are the
  independent checks.
- ``etf_sets.json``: the 29 narrow ETFs whose holdings are the ETF peer sets. Holdings are not shipped; they come
  from the store's ``etf_holdings`` at run time (``etf_holdings_from_store``) or from a file the caller passes.

**Versioning.** Each file carries an integer ``version``, and ``FINGERPRINTS`` pins the content of every version.
Editing a set without bumping the version (and adding the new fingerprint here) is refused on load, so a metric
reported against ``hand v1`` always means the same sets.

**Loading.** Every ticker was written from memory and is unchecked. The loader keeps the names the given universe
holds and drops the rest, reporting them per set; an ETF with no holdings and a set left with fewer than two names
are excluded and reported. A miss is a report line, never a failure.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

SETS_DIR = Path(__file__).resolve().parent / "sets"
HAND_SETS_PATH = SETS_DIR / "hand_sets.json"
ETF_SETS_PATH = SETS_DIR / "etf_sets.json"
_AUTO_ENSURE = "RC_STORE_AUTO_ENSURE"

# (kind, version) -> sha256 of the canonical set content. A new version adds a line; an old line never changes.
FINGERPRINTS: dict[tuple[str, int], str] = {
    ("hand", 1): "c893f55bab8c2abfc001f833715a11a8fc3dbf3f594f34c5d5cda4899d901acf",
    ("etf", 1): "6c27fc0abef430cb07cfff48bc277e71ed5cd88064f88f904619408dbf529346",
}

MIN_MEMBERS = 2   # a set needs two names for any member to have a peer in it


class PeerSetVersionError(ValueError):
    """A peer-set file whose content does not match the fingerprint pinned for its version."""


@dataclass(frozen=True)
class PeerSet:
    id: object
    name: str
    tickers: tuple[str, ...]
    borderline: bool
    kind: str               # "hand" or "etf"


@dataclass
class LoadedSets:
    version: int
    sets: list[PeerSet]
    report: list[dict] = field(default_factory=list)   # one line per set: kept, dropped, excluded


def fingerprint(content) -> str:
    """sha256 of ``content`` as canonical JSON (sorted keys, no whitespace)."""
    blob = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _read(path: Path, kind: str, content_key: str):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    version = data.get("version")
    if not isinstance(version, int):
        raise PeerSetVersionError(f"{path}: no integer 'version' field")
    content = data[content_key]
    want = FINGERPRINTS.get((kind, version))
    got = fingerprint(content)
    if want != got:
        raise PeerSetVersionError(
            f"{path}: the {kind} sets do not match the content pinned for version {version}. A changed set needs a "
            f"version bump and a new FINGERPRINTS entry ({(kind, version + 1)!r}: {got!r} if this content is v"
            f"{version + 1}); a pinned version never changes.")
    return version, content


def read_hand_sets(path: Path | None = None) -> tuple[int, list[PeerSet]]:
    """``(version, sets)`` as written in the hand-set file, unfiltered."""
    version, raw = _read(path or HAND_SETS_PATH, "hand", "sets")
    sets = [PeerSet(id=s["id"], name=s["name"], tickers=tuple(t.upper() for t in s["tickers"]),
                    borderline=bool(s.get("borderline")), kind="hand") for s in raw]
    return version, sets


def read_etf_list(path: Path | None = None) -> tuple[int, list[str]]:
    """``(version, ETF tickers)`` from the ETF list file."""
    version, raw = _read(path or ETF_SETS_PATH, "etf", "etfs")
    return version, [t.upper() for t in raw]


def _filter(s: PeerSet, universe: set[str]) -> tuple[PeerSet | None, dict]:
    kept = tuple(t for t in dict.fromkeys(s.tickers) if t in universe)
    dropped = [t for t in dict.fromkeys(s.tickers) if t not in universe]
    line = {"set": s.name, "id": s.id, "kept": len(kept), "dropped": dropped, "excluded": None}
    if len(kept) < MIN_MEMBERS:
        line.update(kept=0, excluded=f"fewer than {MIN_MEMBERS} names in the universe")
        return None, line
    return PeerSet(id=s.id, name=s.name, tickers=kept, borderline=s.borderline, kind=s.kind), line


def _universe(universe: Iterable[str]) -> set[str]:
    return {str(t).upper() for t in universe}


def hand_sets_for(universe: Iterable[str], path: Path | None = None) -> LoadedSets:
    """The hand sets restricted to ``universe``, with a per-set report of what was dropped."""
    version, sets = read_hand_sets(path)
    uni = _universe(universe)
    out, report = [], []
    for s in sets:
        kept, line = _filter(s, uni)
        report.append(line)
        if kept:
            out.append(kept)
    return LoadedSets(version, out, report)


def etf_sets_for(holdings: Mapping[str, Iterable[str]], universe: Iterable[str],
                 path: Path | None = None) -> LoadedSets:
    """One peer set per listed ETF from ``holdings`` (ETF -> holding tickers), restricted to ``universe``.

    An ETF of the list with no holdings is excluded and reported (``"no holdings"``); ETFs outside the list are
    ignored. The report has one line per listed ETF.
    """
    version, etfs = read_etf_list(path)
    uni = _universe(universe)
    held = {str(k).upper(): [str(t).upper() for t in v] for k, v in holdings.items()}
    out, report = [], []
    for etf in etfs:
        h = held.get(etf) or []
        if not h:
            report.append({"set": etf, "id": etf, "kept": 0, "dropped": [], "excluded": "no holdings"})
            continue
        kept, line = _filter(PeerSet(id=etf, name=etf, tickers=tuple(h), borderline=False, kind="etf"), uni)
        report.append(line)
        if kept:
            out.append(kept)
    return LoadedSets(version, out, report)


def etf_holdings_from_store(path: Path | None = None) -> dict[str, set[str]]:
    """Holding tickers per listed ETF from the store's ``etf_holdings`` (read only; empty set when none).

    The same table and the same entry parser as ``themes.match_theme_etfs``; that function itself writes
    ``relatedness_theme_etfs`` and covers only the rotation ETFs, so it is not called here.

    **Never a store write** (review S1, #342): a store call with ``RC_STORE_AUTO_ENSURE`` unset applies the whole
    schema manifest first, which on a live store would run a migration as a side effect of this read. The variable
    is held at ``0`` for the read and then put back as the caller had it, so the caller's process keeps its own
    auto-ensure behaviour.
    """
    _, etfs = read_etf_list(path)
    prev = os.environ.get(_AUTO_ENSURE)
    os.environ[_AUTO_ENSURE] = "0"
    try:
        from RevenueChaser.core import rawstore
        from RevenueChaser.module_relatedness.themes import _etf_holding_tickers

        out: dict[str, set[str]] = {}
        for etf in etfs:
            row = rawstore.get("etf_holdings", etf)
            out[etf] = _etf_holding_tickers(row.get("entry")) if row else set()
        return out
    finally:
        if prev is None:
            os.environ.pop(_AUTO_ENSURE, None)
        else:
            os.environ[_AUTO_ENSURE] = prev
