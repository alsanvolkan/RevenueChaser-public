"""SIC per CIK from SEC EDGAR — a cache the identity master reads.

Yahoo's screener returns no ``sector``/``industry`` (verified), so the universe
cannot be classified from its own payload. SEC submissions carry ``sic`` +
``sicDescription`` for every filer; this module fetches and caches them in
``sec_company_sic``.

Since R3 (#86) nothing here writes ``equity_universe``: ``security_master`` (built by
``core.identity.refresh`` from the UW and Yahoo sector and industry rows) is the single
writer of its sector and industry columns, via ``core.identity.apply``. The SIC backfill
that used to live here, and the ``_RANGES`` rules it applied, are gone.

Cache-first and resumable: a ticker already in ``sec_company_sic`` is skipped, so
an interrupted run continues where it stopped. Rows are written every
``_FLUSH_EVERY`` fetched tickers and once more in a ``finally`` (also on Ctrl-C),
so an interrupt keeps what was fetched. ``no_fetch=True`` never touches the
network: it counts pending tickers from the stored ``sec_exchange_file`` rows (#301 D21).

    python -m RevenueChaser.core.sic_registry --refresh
    python -m RevenueChaser.core.sic_registry --refresh --limit 200
"""
from __future__ import annotations

import json
import logging
import time

import httpx

from . import rawstore
from .providers import sec_edgar

logger = logging.getLogger(__name__)

SIC_TABLE = "sec_company_sic"
_UNIVERSE = "equity_universe"
_FLUSH_EVERY = 200


def _quote_obj(value):
    """Normalise the ``quote`` cell to a dict.

    ``rawstore.query`` returns a JSON column as a *string*; writing that string
    back into the JSON column double-encodes it (the AG engine then reads a str
    where it expects a dict). Parse until a dict, or fall back to ``{}``.
    """
    for _ in range(3):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def submissions_url(cik) -> str:
    return f"{sec_edgar.SEC_DATA_BASE}/submissions/CIK{int(cik):010d}.json"


def parse_sic(ticker: str, cik, payload: dict) -> dict:
    """One ``sec_company_sic`` row from a submissions payload."""
    return {
        "ticker": str(ticker).strip().upper(),
        "cik": str(cik),
        "sic": str((payload or {}).get("sic") or ""),
        "sic_description": str((payload or {}).get("sicDescription") or ""),
        "fetched_at": time.time(),
    }


def cached() -> dict[str, dict]:
    """Cached SIC rows keyed by ticker."""
    return {r["ticker"]: r for r in rawstore.get_all(SIC_TABLE)}


def _universe_tickers() -> list[str]:
    return [r["ticker"] for r in
            rawstore.query(f"SELECT ticker FROM {_UNIVERSE} ORDER BY ticker")]


def _stored_cik_map() -> dict[str, dict]:
    """The stored SEC exchange file rows, read only (never a network call)."""
    from . import sec_files
    return {r["ticker"]: {"cik": r["cik"]} for r in sec_files.latest("exchange") if r.get("cik")}


def fetch_missing(tickers: list[str] | None = None, limit: int | None = None,
                  no_fetch: bool = False, client=None) -> dict:
    """Fetch SIC for universe tickers that have a CIK and no cached SIC.

    Cache-first: existing rows are never refetched. ``no_fetch`` returns the
    pending count from the stored ticker map without creating a client or making
    a request (``cik_map_loaded`` False means the map is not stored yet, so
    ``pending`` 0 is "unknown", not "none"). A real run writes every
    ``_FLUSH_EVERY`` rows and again on the way out, interrupt included;
    ``fetched`` is the number of rows written.
    """
    cmap = _stored_cik_map() if no_fetch else sec_edgar._cik_map()
    have = cached()
    if tickers is None:
        tickers = _universe_tickers()

    todo: list[tuple[str, str]] = []
    for t in tickers:
        t = str(t).strip().upper()
        cik = (cmap.get(t) or {}).get("cik")
        if not cik or t in have:
            continue
        todo.append((t, str(cik)))
    if limit is not None:
        todo = todo[:limit]

    if no_fetch:
        return {"cached": len(have), "pending": len(todo), "fetched": 0,
                "failed": 0, "no_fetch": True, "cik_map_loaded": bool(cmap)}
    if not todo:
        return {"cached": len(have), "pending": 0, "fetched": 0,
                "failed": 0, "no_fetch": False}

    own = client is None
    if own:
        client = httpx.Client(headers=sec_edgar._headers(), timeout=60,
                              follow_redirects=True)
    rows: list[dict] = []
    written = failed = 0
    try:
        for t, cik in todo:
            try:
                payload = sec_edgar.get_json(submissions_url(cik), client)
                rows.append(parse_sic(t, cik, payload))
            except Exception as exc:  # noqa: BLE001 — per-ticker network failure
                failed += 1
                logger.warning("sic %s (cik %s): %s", t, cik, exc)
            if len(rows) >= _FLUSH_EVERY:
                written += rawstore.write_rows(SIC_TABLE, rows)
                rows = []
    finally:
        try:
            if rows:
                written += rawstore.write_rows(SIC_TABLE, rows)
        finally:
            if own:
                client.close()
    return {"cached": len(have), "pending": len(todo), "fetched": written,
            "failed": failed, "no_fetch": False}


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="SEC SIC cache")
    p.add_argument("--refresh", action="store_true",
                   help="fetch missing SIC for universe tickers")
    p.add_argument("--limit", type=int, default=None, help="cap tickers fetched")
    args = p.parse_args(argv)

    if args.refresh:
        print(fetch_missing(limit=args.limit))
    else:
        print(cached.__doc__ or "SIC registry")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
