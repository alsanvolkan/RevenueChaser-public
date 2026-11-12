"""Which SEC CIK a company used to file under (#749).

When a company reorganises under a holding company or redomiciles, the SEC gives the new registrant a new CIK: the
ticker moves to it, the filing history stays on the old one, and filers go on citing the old CIK for years. Nothing
in a single SEC snapshot says the two are one company, so every store keyed by CIK loses the history at the change.

Two sources, read together:

- **The spine's own ``security_history``** (the CIK per ticker per stored SEC snapshot): true for every company,
  but only as far back as our snapshots go. The ownership naming reads it (``ownership/common.py``).
- **The seed file** ``successor_ciks.csv``, this module's data: hand-reviewed pairs that predate our snapshots.
  It is edgartools' ``edgar/reference/data/successor_registrants.csv`` at upstream ``479d350`` (MIT), copied
  column for column, and the note of each row is theirs. It is a short list of known pairs, never a census, and
  nothing in this repository has to be kept in step with it.

A pair is ``(predecessor cik -> successor cik)``, so a ticker is never named here: who holds the successor CIK is
the spine's business, and a reused ticker cannot be inherited by mistake. Pairs chain (a company that moved twice),
and :func:`predecessors` follows the chain.
"""
from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
from typing import Iterable

SEED_FILE = Path(__file__).with_name("successor_ciks.csv")
#: Where the seed's rows come from, named wherever the pairs are used (#749).
SEED_SOURCE = "edgartools successor_registrants.csv @479d350 (MIT)"
#: A chain longer than this is a loop or a mistake in the seed, and is cut.
MAX_CHAIN = 8


def _cik(value) -> str | None:
    text = str(value if value is not None else "").strip()
    return str(int(text)) if text.isdigit() else None


@lru_cache(maxsize=1)
def seed() -> dict[str, str]:
    """``{predecessor cik: successor cik}`` of the seed file; empty when the file is gone.

    A row whose two CIKs do not both read as numbers, or that points a CIK at itself, is left out. A predecessor
    that the file gives two successors for is left out as well: an ambiguous pair would move history to the wrong
    company, and no guess is better than none here.
    """
    if not SEED_FILE.exists():
        return {}
    seen: dict[str, set[str]] = {}
    with SEED_FILE.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pre, suc = _cik(row.get("predecessor_cik")), _cik(row.get("successor_cik"))
            if pre and suc and pre != suc:
                seen.setdefault(pre, set()).add(suc)
    return {pre: next(iter(sucs)) for pre, sucs in seen.items() if len(sucs) == 1}


def successor(cik: str, pairs: dict[str, str] | None = None) -> str | None:
    """The CIK ``cik``'s history ends at, following a chain of pairs; None when it is nobody's predecessor."""
    pairs = seed() if pairs is None else pairs
    here, out, seen = cik, None, {cik}
    for _ in range(MAX_CHAIN):
        nxt = pairs.get(here)
        if nxt is None or nxt in seen:
            break
        seen.add(nxt)
        here = out = nxt
    return out


def predecessors(ciks: Iterable[str], pairs: dict[str, str] | None = None) -> dict[str, list[str]]:
    """``{cik: [the CIKs whose history it inherited]}`` for the CIKs of ``ciks`` that have any, oldest-first by
    number. A chain is flattened: every CIK upstream of ``cik`` is listed under it, not only the one before."""
    pairs = seed() if pairs is None else pairs
    wanted = {c for c in ciks if c}
    out: dict[str, list[str]] = {}
    for pre in pairs:
        end = successor(pre, pairs)
        if end in wanted:
            out.setdefault(end, []).append(pre)
    return {c: sorted(p, key=int) for c, p in out.items()}


def former_ciks(current: Iterable[str], pairs: dict[str, str] | None = None) -> dict[str, str]:
    """``{predecessor cik: the current cik it belongs to}`` for the CIKs of ``current``: :func:`predecessors`
    turned around, for a caller that starts from the old CIK a filing names."""
    return {pre: cik for cik, pres in predecessors(current, pairs).items() for pre in pres}
