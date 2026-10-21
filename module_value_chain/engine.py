"""Value Chain engine — corpus -> mentions -> edges, plus 2-hop.

Fetch pulls the latest 10-K/20-F per issuer and slices Item 1/4; the model-only
(``no_fetch``) path reads the cached corpus and never touches the network. Edges
are persisted so a Refresh re-renders with zero network.
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from RevenueChaser.core import ledger, rawstore
from RevenueChaser.core.providers import sec_edgar

from . import aliases
from . import concentration
from . import config
from . import edgar_text
from . import mentions as mentions_mod
from . import precision as precision_mod

logger = logging.getLogger(__name__)

_TABLE = "chain_filings"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Everything the board and the cache check need; ``item_text`` (~50 KB per issuer) is not.
_LIGHT_COLUMNS = ["ticker", "form", "filed", "quality", "primary_doc"]


def _cached_filings() -> dict[str, dict]:
    return {r["ticker"]: r for r in rawstore.get_all(_TABLE, columns=["ticker"])}


# ── the one write path for chain_filings (#116 C6) ──────────────────────────
_DB = rawstore.MANIFEST[_TABLE].get("db", "raw")   # chain_filings and chain_meta share it
# A slice is replaced only by one of the same rank or better. ``overrun`` still
# holds all of Item 1, so it is protected too; ``short`` and ``missing`` rank 0.
_RANK = {"ok": 2, "overrun": 1}


def store_slices(rows: list[dict]) -> tuple[int, int]:
    """Write sliced filings; return ``(written, kept)``.

    A stored slice is never replaced by a worse one (``ok`` > ``overrun`` >
    ``short`` / ``missing``), whatever the caller (sync, ``fetch_corpus``,
    ``force``); between two of the same rank the new filing wins. The stored quality is read and
    the rows written in one transaction. A written row clears its ``miss:`` key.
    """
    if not rows:
        return 0, 0
    tickers = [r["ticker"] for r in rows]

    def _write(tx):
        tx.ensure_table(_TABLE)
        tx.ensure_table("chain_meta")
        marks = ", ".join("?" * len(tickers))
        stored = {t: (q, acc) for t, q, acc in tx.execute(
            f"SELECT ticker, quality, accession FROM {_TABLE} WHERE ticker IN ({marks})",
            tickers).fetchall()}
        write, kept = [], []
        for r in rows:
            old_q, old_acc = stored.get(r["ticker"], (None, None))
            if _RANK.get(r.get("quality"), 0) < _RANK.get(old_q, 0):
                kept.append((r, old_q, old_acc))
            else:
                write.append(r)
        tx.write_rows(_TABLE, write)
        if write:
            keys = [_MISS_PREFIX + r["ticker"] for r in write]
            tx.execute(f"DELETE FROM chain_meta WHERE key IN ({', '.join('?' * len(keys))})", keys)
        return write, kept

    write, kept = rawstore.atomic(_write, db=_DB)
    for r, old_q, old_acc in kept:
        logger.warning("value_chain %s: kept %s slice %s; new %s is %s",
                       r["ticker"], old_q, old_acc, r.get("accession"), r.get("quality"))
    return len(write), len(kept)


# ── recorded misses (#116 C26): chain_meta ``miss:<TICKER>`` keys ───────────
# Only a CIK with no usable annual filing is recorded; a missing CIK costs no
# request (the CIK map is local), so it is counted, never stored.
_MISS_PREFIX = "miss:"
MISS_NOTES = {"no_filing": "no 10-K / 20-F", "form_40f": "40-F, not supported yet"}


def _cik_str(cik) -> str:
    try:
        return str(int(cik))
    except (TypeError, ValueError):
        return str(cik or "")


def miss_row(ticker: str, reason: str, cik) -> dict:
    return {"key": _MISS_PREFIX + ticker.upper(), "value": json.dumps({
        "reason": reason, "note": MISS_NOTES.get(reason, reason),
        "cik": _cik_str(cik), "checked": _now()})}


def record_misses(rows: list[dict]) -> None:
    if rows:
        rawstore.ensure_table("chain_meta")
        rawstore.write_rows("chain_meta", rows)


def stored_misses() -> dict[str, dict]:
    """``{ticker: {reason, note, cik, checked}}`` from ``chain_meta`` (read only)."""
    out: dict[str, dict] = {}
    for r in rawstore.get_all("chain_meta"):
        key = str(r.get("key") or "")
        if not key.startswith(_MISS_PREFIX):
            continue
        v = r.get("value")
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                continue
        if isinstance(v, dict):
            out[key[len(_MISS_PREFIX):]] = v
    return out


def miss_is_current(miss: dict | None, cik, now: datetime | None = None) -> bool:
    """A recorded miss still holds: same CIK, checked less than MISS_RETRY_DAYS ago."""
    if not miss or _cik_str(miss.get("cik")) != _cik_str(cik):
        return False
    try:
        checked = datetime.fromisoformat(str(miss.get("checked")))
    except ValueError:
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return now - checked < timedelta(days=config.MISS_RETRY_DAYS)


def corpus_miss_summary(tickers: list[str]) -> dict | None:
    """Recorded misses for ``tickers`` by reason, plus the no-CIK count (read only).

    Store reads only, no network: the CIK map comes from ``sec_company_tickers``
    as stored, never through ``sec_edgar._cik_map`` (which fetches when empty).
    A ``miss:`` key is deleted only when a slice is written, so a ticker that left
    the universe keeps its key; only ``tickers`` are counted. ``due`` is the
    misses the next sync asks SEC about again (``not miss_is_current``).

    Unknown is not zero (F4): ``None`` when the store cannot be read, and
    ``no_cik`` is ``None`` when no CIK map is stored, so a missing map never
    reads as "every ticker has no CIK".
    """
    try:
        ciks = {str(r["ticker"]).upper(): r.get("cik")
                for r in rawstore.get_all("sec_company_tickers") if r.get("cik")}
        misses = stored_misses()
    except Exception:  # noqa: BLE001 - unreadable store reads as "unknown"
        return None
    out = {"recorded": 0, "no_filing": 0, "form_40f": 0, "due": 0,
           "no_cik": (sum(1 for t in tickers if t.upper() not in ciks) if ciks else None)}
    for t in tickers:
        miss = misses.get(t.upper())
        if not miss:
            continue
        out["recorded"] += 1
        reason = miss.get("reason")
        if reason in ("no_filing", "form_40f"):
            out[reason] += 1
        if not miss_is_current(miss, ciks.get(t.upper())):
            out["due"] += 1
    return out


def _stored_ciks() -> dict[str, str]:
    """The stored SEC ticker map, read only (``_cik_map`` may fill it from SEC)."""
    return {r["ticker"]: r["cik"] for r in rawstore.get_all(
        "sec_company_tickers", columns=["ticker", "cik"]) if r.get("cik")}


def fetch_corpus(tickers: list[str] | None = None, no_fetch: bool = False,
                 limit: int | None = None, client=None, force: bool = False,
                 workers: int = 8) -> dict:
    """Fetch + slice filings for tickers with no cached row (cache-first).

    ``pending`` leaves out tickers with no CIK (``no_cik``) and recorded misses
    (``misses``). ``no_fetch`` computes it from reads only and writes nothing.
    """
    if tickers is None:
        if config.USE_SHARED_UNIVERSE:
            from RevenueChaser.core.universe import classification_universe
            tickers = classification_universe()
        else:
            tickers = list(config.SEED)
    else:
        tickers = list(tickers)
    have = {} if force else _cached_filings()
    todo = [t for t in tickers if t not in have]
    if limit is not None:
        todo = todo[:limit]
    misses = stored_misses()
    if no_fetch:
        ciks = _stored_ciks()
        # An empty map means "not loaded yet", not "no CIK anywhere".
        no_cik = [t for t in todo if ciks and not ciks.get(t.upper())]
        missed = [t for t in todo if t.upper() in misses and t not in no_cik]
        return {"requested": len(tickers), "cached": len(tickers) - len(todo),
                "pending": len(todo) - len(no_cik) - len(missed), "fetched": 0,
                "failed": 0, "no_cik": len(no_cik), "misses": len(missed), "no_fetch": True}
    if not todo:
        return {"requested": len(tickers), "cached": len(tickers),
                "pending": 0, "fetched": 0, "failed": 0, "no_cik": 0, "misses": 0,
                "no_fetch": False}

    cmap = sec_edgar._cik_map()
    now = datetime.now(timezone.utc)
    counts = {"failed": 0, "no_cik": 0, "known_miss": 0, "no_filing": 0, "form_40f": 0}
    pairs: list[tuple[str, str]] = []
    for t in todo:
        cik = (cmap.get(t.upper()) or {}).get("cik")
        if not cik:
            counts["no_cik"] += 1
            continue
        if not force and miss_is_current(misses.get(t.upper()), cik, now):
            counts["known_miss"] += 1
            continue
        pairs.append((t, cik))

    rows: list[dict] = []
    miss_rows: list[dict] = []
    if pairs:
        # Parallel fetch; sec_edgar._throttle is shared and lock-guarded, so the
        # aggregate request rate stays within SEC's 10 req/s regardless of workers.
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(edgar_text.fetch_filing, t, cik, client): (t, cik)
                    for t, cik in pairs}
            for f in as_completed(futs):
                t, cik = futs[f]
                try:
                    res = f.result()
                except Exception as exc:  # noqa: BLE001 - per-ticker network failure
                    logger.warning("value_chain %s: %s", t, exc)
                    res = None
                if res and res.get("miss"):
                    counts[res["miss"]] = counts.get(res["miss"], 0) + 1
                    miss_rows.append(miss_row(t, res["miss"], cik))
                elif res and not res.get("error"):
                    rows.append(res)
                else:  # a network error is retried next run, never recorded
                    counts["failed"] += 1
    record_misses(miss_rows)
    written, kept = store_slices(rows)
    missed = counts["known_miss"] + counts["no_filing"] + counts["form_40f"]
    return {"requested": len(tickers), "cached": len(tickers) - len(todo),
            "pending": len(todo) - counts["no_cik"] - missed, "fetched": written,
            "kept": kept, **counts, "misses": missed, "no_fetch": False}


def filing_edges(ticker: str, text: str, filed: str = "") -> list[dict]:
    """Edges one filing states: concentration sentences first, then keyword mentions.

    A concentration sentence ("Apple accounted for 29.8% of our net sales")
    states relation and direction, so it replaces any keyword-window guess for
    the same counterparty, and its share rides on the edge as ``share_pct``
    (#387 D7; None on a keyword edge). A company naming itself (or its other
    share class) is not an edge.
    """
    me = aliases.issuer(ticker)
    stated = [c for c in concentration.find(text, filed) if aliases.issuer(c["dst_ticker"]) != me]
    stated_dst = {c["dst_ticker"] for c in stated}
    out = [{"src": ticker, "dst_name": c["dst_name"], "dst_ticker": c["dst_ticker"],
            "relation": c["relation"], "snippet": c["snippet"], "filed": c["filed"],
            "share_pct": c.get("share_pct")}
           for c in stated]
    for m in mentions_mod.find_mentions(text, filed):
        if aliases.issuer(m["dst_ticker"]) == me:
            continue
        if m["dst_ticker"] in stated_dst and m["relation"] in ("supplier", "customer"):
            continue
        out.append({"src": ticker, "dst_name": m["dst_name"], "dst_ticker": m["dst_ticker"],
                    "relation": m["relation"], "snippet": m["snippet"], "filed": m["filed"],
                    "share_pct": None})
    # chain_mentions is keyed (src, dst_name, relation): keep the first per key.
    seen, uniq = set(), []
    for x in out:
        k = (x["src"], x["dst_name"], x["relation"])
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq


def build_edges(info: dict | None = None) -> list[dict]:
    """Recompute mentions from the cached corpus and persist them.

    A caller that passes ``info`` (the result history, #387) learns how many filings were read (``filings``) and
    whether the stored edges were kept because none came out (``kept``)."""
    rows = rawstore.get_all(_TABLE)
    edges: list[dict] = []
    for f in rows:
        if f.get("quality") == "missing" or not f.get("item_text"):
            continue
        edges.extend(filing_edges(f["ticker"], f["item_text"], f.get("filed") or ""))
    rawstore.ensure_table("chain_mentions")
    if info is not None:
        info.update(filings=len(rows), kept=not edges)
    if not edges:
        # No filing yielded an edge (every row missing or text-less, an empty corpus): replace_all
        # would delete the stored edges and write none. Keep them and return what the store holds, so
        # the board, ``n_mentions`` and a later Refresh agree (#284).
        kept = rawstore.get_all("chain_mentions")
        logger.warning("value_chain: no edges came out of %d filings; kept the %d stored chain_mentions",
                       len(rows), len(kept))
        return kept
    rawstore.replace_all("chain_mentions", edges)
    return edges


def _board_filings(filings: list[dict]) -> list[dict]:
    """Filing rows plus the fields the universal dashboard filters need."""
    from RevenueChaser.core.gics_map import sector_or_unclassified, sector_path
    from RevenueChaser.core.universe import filter_fields
    meta = filter_fields([f["ticker"] for f in filings])
    out = []
    for f in sorted(filings, key=lambda r: r["ticker"]):
        m = meta.get(f["ticker"]) or {}
        out.append({
            "ticker": f["ticker"], "form": f.get("form"), "filed": f.get("filed"),
            "quality": f.get("quality"), "primary_doc": f.get("primary_doc"),
            "name": m.get("name") or f["ticker"],
            "sector": sector_or_unclassified(m.get("sector")),
            "industry": m.get("industry") or "",
            "market_cap": m.get("market_cap"),
            "earnings_date": m.get("earnings_date"),
            "index": m.get("index") or [],
            # The shared filter row (core.universe.filter_fields, #383 C1b).
            "gics": m.get("gics") or sector_path(sector_or_unclassified(m.get("sector")), m.get("industry") or ""),
            "theme_ids": m.get("theme_ids") or [],
        })
    return out


def _board_payload(filings: list[dict], edges: list[dict], as_of: str,
                   fetch: dict | None) -> dict:
    """The board's payload. Filings and edges of an excluded security type (Identity
    R4, #87: funds, SPACs, preferreds, ...) are left out of it; the stored rows stay.

    Only the filing's own ticker and an edge's ``src`` are filtered — a counterparty
    (``dst``) is a name in a filing's text, whatever type it is. Below-floor names
    that were synced earlier are not filtered here (out of scope: type only).
    """
    from RevenueChaser.core.universe import classification_universe, excluded_tickers, theme_catalog
    excluded = excluded_tickers()
    filings = [f for f in filings if f["ticker"] not in excluded]
    edges = [e for e in edges if e.get("src") not in excluded]
    universe = classification_universe()
    universe_total = len(universe)
    # Universe tickers with no filing row that are known not to be fetchable:
    # a recorded miss, or no CIK in the stored map (reads only).
    have = {f["ticker"] for f in filings}
    misses, ciks = stored_misses(), _stored_ciks()
    unfiled = [t for t in universe if t not in have and t not in excluded]
    no_cik = {t for t in unfiled if ciks and not ciks.get(t)}
    missed = {t for t in unfiled if t in misses} - no_cik
    # Pending is always read from the store (D4, DF58): a Fetch's own ``pending`` is its to-do list before it
    # ran, so it still holds the names it just wrote; it stays in ``fetch`` for the job message. Counting the
    # unfiled universe names, not ``universe_total - len(filings)``, keeps a stored filing of a name now
    # below the floor from hiding a pending one; it is the count a Refresh's ``fetch_corpus`` gives.
    pending = len(unfiled) - len(no_cik) - len(missed)
    return {
        "as_of": as_of,
        "filings": _board_filings(filings),
        "edges": edges,
        "fetch": fetch,
        "universe_total": universe_total,
        "pending": pending,
        "misses": len(missed),
        "no_cik": len(no_cik),
        # Hand-labelled fixture precision (file read only: no store, no network).
        "precision": precision_mod.gate(served=edges),
        # The Theme facet's vocabulary: the pinned theme run (#383 C1b), a store read.
        "theme_catalog": theme_catalog(),
    }


def run(no_fetch: bool = False, tickers: list[str] | None = None,
        limit: int | None = None, force: bool = False, run_id: str | None = None) -> dict:
    """The board's payload. A Fetch records its edges and payload in result history (#387) under ``run_id`` (the
    dashboard job's id, else ``ledger.new_run_id()``); a Refresh writes nothing."""
    run_at = time.time()
    fetch = fetch_corpus(tickers=tickers, no_fetch=no_fetch, limit=limit, force=force)
    if no_fetch:
        # Refresh is pure: it re-renders what the store holds and writes nothing,
        # so ``chain_meta.last_sync`` never claims a sync that did not happen.
        # Rescanning Item 1/4 for the live universe would hang the request thread.
        rawstore.ensure_table("chain_mentions")
        edges = rawstore.get_all("chain_mentions")
        filings = rawstore.get_all(_TABLE, columns=_LIGHT_COLUMNS)
        return _board_payload(filings, edges, _stored_sync() or _now(), fetch)
    info: dict = {}
    edges = build_edges(info)
    filings = rawstore.get_all(_TABLE, columns=_LIGHT_COLUMNS)
    as_of = _now()
    rawstore.ensure_table("chain_meta")
    # write_rows, not replace_all: chain_meta is a "replace" table keyed by
    # ``key``, so only these keys are rewritten and the ``miss:`` keys survive.
    rawstore.write_rows("chain_meta", [
        {"key": "last_sync", "value": as_of},
        {"key": "n_filings", "value": str(len(filings))},
        {"key": "n_mentions", "value": str(len(edges))},
    ])
    payload = _board_payload(filings, edges, as_of, fetch)
    record_history(edges, info, run_id=run_id, run_at=run_at, payload=payload, counts=fetch)
    return payload


def record_history(edges: list[dict], info: dict, *, run_id: str | None, run_at: float, counts: dict,
                   payload: dict | None = None) -> None:
    """Result history of a ``chain_mentions`` write (#387): a Value Chain Fetch or an EDGAR sync. A failure is a job
    note, never a failed run: the latest table and the board do not depend on it."""
    from . import history
    try:
        history.record(edges, run_id=run_id or ledger.new_run_id(), run_at=run_at, kept=bool(info.get("kept")),
                       counts=counts, filings=info.get("filings"), payload=payload)
    except Exception as exc:
        logger.exception("Value Chain result history failed")
        ledger.add_note(f"Value Chain result history was not recorded: {type(exc).__name__}: {exc}")


def _stored_sync() -> str | None:
    meta = {r["key"]: r["value"] for r in rawstore.get_all("chain_meta")}
    return meta.get("last_sync")


def load_payload() -> dict:
    """Board payload from the store (zero network)."""
    filings = rawstore.get_all(_TABLE, columns=_LIGHT_COLUMNS)
    edges = rawstore.get_all("chain_mentions")
    return _board_payload(filings, edges, _stored_sync() or _now(), None)


def _norm(edges: list[dict]) -> dict[str, list[dict]]:
    """Index edges by source ticker."""
    out: dict[str, list[dict]] = {}
    for e in edges:
        out.setdefault(e["src"], []).append(e)
    return out


def edge_index(edges: list[dict]) -> dict[str, dict]:
    """Edges by source and by resolved target, built once for many ``focal_graph``
    calls (#118 C19: a per-ticker rebuild made the board O(tickers x edges))."""
    by_dst: dict = {}
    for e in edges:
        by_dst.setdefault(e.get("dst_ticker"), []).append(e)
    return {"by_src": _norm(edges), "by_dst": by_dst}


def focal_graph(edges: list[dict], ticker: str, depth: int = 2,
                max_hop2: int = config.MAX_HOP2, index: dict | None = None) -> dict:
    """Upstream/downstream edges for ``ticker`` plus one more hop on resolved names.

    Hop 2 only follows resolved supplier/customer edges (never ``other``, never a
    private name), and is capped so the sankey stays readable. ``index`` is an
    optional prebuilt ``edge_index(edges)``; without it one is built per call.
    """
    ticker = (ticker or "").upper()
    index = index or edge_index(edges)
    by_src = index["by_src"]
    seen = {ticker}
    links: list[dict] = []

    def _add(e, hop, incoming=False):
        # ``share_pct``: the share of the filer's (``src``'s) sales or purchases a concentration sentence states
        # (#387 D7); None on a keyword edge and on an edge stored before the column.
        links.append({"src": e["src"], "dst": e.get("dst_ticker") or e["dst_name"],
                      "dst_ticker": e.get("dst_ticker") or "",
                      "relation": e["relation"], "snippet": e.get("snippet") or "",
                      "share_pct": e.get("share_pct"),
                      "hop": hop, "incoming": incoming})

    first = [e for e in by_src.get(ticker, []) if e["relation"] in ("supplier", "customer")]
    for e in first:
        _add(e, 1)
        if depth >= 2 and e.get("dst_ticker"):
            seen.add(e["dst_ticker"])
    # Incoming: other issuers' filings that name this company. The link keeps the
    # filing's direction (src names dst); it never seeds a second hop.
    for e in index["by_dst"].get(ticker, []):
        if e["src"] != ticker and e["relation"] in ("supplier", "customer"):
            _add(e, 1, incoming=True)
    if depth >= 2:
        hop2 = 0
        for e in first:
            dst = e.get("dst_ticker")
            if not dst or hop2 >= max_hop2:
                continue
            for e2 in by_src.get(dst, []):
                if e2["relation"] not in ("supplier", "customer"):
                    continue
                _add(e2, 2)
                hop2 += 1
                if hop2 >= max_hop2:
                    break
    return {"ticker": ticker, "links": links}
