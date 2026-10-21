"""Index membership registry — single source of truth for ``ticker -> index``.

Covers the three headline US indexes (all sourced from Unusual Whales), cache-first:

  - S&P 500           UW screener (``is_s_p_500=true``)
  - Dow Jones         UW ETF holdings of DIA (30 members)
  - Nasdaq Composite  UW ticker-exchanges (``exchange == nasdaq``)

Russell 2000 is DEFERRED: no free membership source. `core/index_lists.py` is where a source would go.

The S&P SmallCap 600 (slug ``sp600``, #302) is stored from the identity cache's last
accepted S&P 1500 snapshot (no request of its own). It has a label in ``LABELS`` (#214), so a
stored member shows the badge and the filter lists it once it has rows, but it never joins
``INDEXES``: ``fetch_all_indexes`` (through ``build_registry``; neither has a caller outside tests today) walks
``INDEXES``, and the first fill needs the user's OK for that run (``FIRST_FILL_GATED``, checked in
``fetch_index`` too). The weekly step ``refresh_lists`` is the only route that writes sp600.

Each index is cached to the ``index_constituents`` table. The ``index_tickers``
VIEW (see ``rawstore._VIEWS``) joins membership to ``equity_universe`` for
``name`` / ``sector``; the screeners read it to badge/filter by index.

A partial fetch is never written to cache (guarded by a completeness floor), so a
rate-limited run can't shrink the registry.
"""
from __future__ import annotations

import logging
from typing import Iterable

from RevenueChaser.core import job_lock, rawstore

logger = logging.getLogger(__name__)

# slug -> display label for every stored slug (#214). Display order matters: the badge shows the first match,
# and the filter lists the labels in this order. A SmallCap 600 member listed on Nasdaq reads "S&P SmallCap 600".
LABELS: dict[str, str] = {
    "sp500": "S&P 500",
    "dow": "Dow Jones",
    "sp600": "S&P SmallCap 600",
    "nasdaq": "Nasdaq Composite",
}
# The filter's short text for a label whose full name misleads (Nasdaq Composite ~ listed on the exchange).
SHORT_LABELS: dict[str, str] = {"Nasdaq Composite": "Nasdaq-listed"}

# (slug, display label) of the lists ``fetch_all_indexes`` fetches cache-first (no caller outside tests). sp600 is
# not one of them: it is written only by the weekly step (``refresh_lists``), never filled from a board.
INDEXES: list[tuple[str, str]] = [
    ("sp500", LABELS["sp500"]),
    ("dow", LABELS["dow"]),
    ("nasdaq", LABELS["nasdaq"]),
]
INDEX_LABELS: list[str] = [label for _, label in INDEXES]

# Completeness floors — below these a fetch is treated as partial and discarded.
MIN_SP500 = 400
MIN_DOW = 25
MIN_NASDAQ = 1000
MIN_SP600 = 480         # about 80% of the 601 members, the share the S&P 500 floor allows (#302; inferred)
MIN_INDEX: dict[str, int] = {
    "sp500": MIN_SP500,
    "dow": MIN_DOW,
    "nasdaq": MIN_NASDAQ,
    "sp600": MIN_SP600,
}

# The slugs the weekly step writes (D24), and the provider each list comes from (rule A's ``source``).
STORED: tuple[str, ...] = ("sp500", "dow", "nasdaq", "sp600")
SOURCE: dict[str, str] = {"sp500": "uw", "dow": "uw", "nasdaq": "uw", "sp600": "sp1500"}
# A slug whose first fill needs the user's OK for that run (phase2/05_decisions.md:16): the weekly step skips it
# until a run names it in ``first_fill``; once it has rows, every run writes it.
FIRST_FILL_GATED = frozenset({"sp600"})

class IndexFetchError(RuntimeError):
    """A UW read for an index list failed (no key, rejected key, provider error): the cause, not an empty list."""


def _uw_client():
    from RevenueChaser.core.providers.unusual_whales import UWClient
    return UWClient()


def _raise_failed(ep: str, error: dict | None) -> None:
    err = error or {}
    status = f" (status {err['status']})" if err.get("status") is not None else ""
    raise IndexFetchError(f"UW {ep}: {err.get('error') or 'request failed'}{status}")


def _uw_get(ep: str, params: dict | None = None) -> list | dict:
    """GET a UW endpoint through ``UWClient.get_result`` (retry, breaker, ``auth_error``, health record).

    Raises :class:`IndexFetchError` naming the cause when no key is set or the request finally fails, so a
    rejected key is an ``error`` row in :func:`refresh_lists`, not an empty list that reads as below floor
    (B5, DF100). An empty answer from the provider is an empty list, not an error."""
    from RevenueChaser.core import credentials
    with _uw_client() as client:
        if not client.active:
            raise IndexFetchError(credentials.missing_message("unusual_whales"))
        res = client.get_result(ep, params)
    if res.status != "ok":
        _raise_failed(ep, res.error)
    data = res.data
    return data if isinstance(data, dict) else data or []


def _symbols(items: list[dict], key: str = "symbol") -> set[str]:
    out = set()
    for it in items:
        t = str((it or {}).get(key) or "").strip().upper()
        if t:
            out.add(t)
    return out


def fetch_sp500() -> set[str]:
    """S&P 500 members via UW screener (is_s_p_500=true)."""
    data = _uw_get("/screener/stocks", {"is_s_p_500": "true", "limit": 500})
    rows = data.get("data", []) if isinstance(data, dict) else data or []
    return _symbols(rows, key="ticker")


def fetch_dow() -> set[str]:
    """Dow Jones 30 members via UW ETF holdings of DIA (full 30)."""
    res = _uw_client().etf_holdings_result("DIA")
    if res.status != "ok":
        _raise_failed(res.endpoint or "/etfs/DIA/holdings", res.error)
    return {str(r.get("ticker") or "").upper() for r in res.data if r.get("ticker")}


def fetch_nasdaq_composite() -> set[str]:
    """Nasdaq Composite ~ all tickers whose UW exchange mapping is 'nasdaq'."""
    data = _uw_get("/stock-directory/ticker-exchanges")
    rows = data.get("data", []) if isinstance(data, dict) else data or []
    return {str(r.get("ticker") or "").upper() for r in rows
            if str(r.get("exchange") or "").lower() == "nasdaq"}


def fetch_sp600() -> set[str]:
    """S&P SmallCap 600 members from the last accepted S&P 1500 snapshot in the identity cache.

    No request: the identity job fetches the Wikipedia list and gates it (#272); a rejected or failed
    snapshot is never cached, and a name that left the list keeps an old row, so only the newest
    snapshot counts (``refresh._last_snapshot``).
    """
    from RevenueChaser.core.identity.refresh import _last_snapshot
    return {str(r.get("ticker") or "").upper() for r in _last_snapshot("sp1500")
            if r.get("index") == "sp600" and r.get("ticker")}


_FETCHERS = {
    "sp500": fetch_sp500,
    "dow": fetch_dow,
    "nasdaq": fetch_nasdaq_composite,
    "sp600": fetch_sp600,
}


def load_index(slug: str) -> set[str] | None:
    rows = rawstore.query("SELECT ticker FROM index_constituents WHERE slug = ?", [slug])
    if not rows:
        return None
    return {str(r["ticker"]).upper() for r in rows if r.get("ticker")}


def save_index(slug: str, tickers: set[str]) -> None:
    rows = [{"slug": slug, "ticker": t} for t in sorted(tickers)]
    if rows:
        rawstore.write_rows("index_constituents", rows)


def fetch_index(slug: str, refresh: bool = False) -> set[str]:
    """Return the cached constituents; fetch fresh only if empty or ``refresh``.

    A fresh fetch is written back only if it clears the completeness floor, so a
    partial/rate-limited run can never shrink an existing cache. A ``FIRST_FILL_GATED``
    slug with no rows yet is neither fetched nor written: its first fill is a run the
    user names (``refresh_lists(first_fill=...)``), never a side effect of a board.
    """
    cached = load_index(slug)
    if cached is not None and not refresh:
        return cached
    if cached is None and slug in FIRST_FILL_GATED:
        return set()

    try:
        fresh = _FETCHERS[slug]()
    except IndexFetchError as exc:
        logger.warning("[index_registry] %s fetch failed (%s); keeping cache", slug, exc)
        return cached if cached is not None else set()
    floor = MIN_INDEX.get(slug, 1)
    if len(fresh) >= floor:
        save_index(slug, fresh)
        return fresh

    if fresh:
        logger.warning("[index_registry] %s partial (%d < %d); keeping cache", slug, len(fresh), floor)
    return cached if cached is not None else fresh


def _change_rows(slug: str) -> int:
    return int(rawstore.query("SELECT count(*) AS n FROM index_constituents_changes WHERE slug = ?",
                              [slug])[0]["n"])


def refresh_lists(run_id: str, first_fill: Iterable[str] = ()) -> dict:
    """The weekly index lists step (#302, D24; task RC-Index-Lists, Saturday 10:00, and the ``index_lists`` job).

    Fetches each slug of ``STORED`` once (three UW requests; sp600 reads the identity cache) and writes a
    list only when it clears its floor, so a partial or failed fetch keeps the stored list. The store records
    the change history in the write's transaction (``index_constituents_changes``): a new member gets a
    'present' row, a member that left one 'absent' row, an unchanged list nothing. The rows carry ``run_id``
    and the provider as ``source``. A slug of ``FIRST_FILL_GATED`` with no rows yet is skipped unless named in
    ``first_fill``. One slug's failure never stops the others.

    Returns ``{"run_id", "locked", "slugs": {slug: {"status", "members", "floor", "appended"[, "error"]}}}``;
    status is ``written``, ``below_floor``, ``awaiting_first_fill`` or ``error``.
    """
    wanted = set(first_fill)
    res: dict = {"run_id": run_id, "locked": False, "slugs": {}}
    with job_lock.acquire("index_lists", wait=0) as got:
        if not got:
            res["locked"] = True
            return res
        for slug in STORED:
            floor = MIN_INDEX[slug]
            out = {"status": "", "members": 0, "floor": floor, "appended": 0}
            res["slugs"][slug] = out
            try:
                if slug in FIRST_FILL_GATED and slug not in wanted and load_index(slug) is None:
                    out["status"] = "awaiting_first_fill"
                    continue
                fresh = _FETCHERS[slug]()
                out["members"] = len(fresh)
                if len(fresh) < floor:
                    out["status"] = "below_floor"
                    logger.warning("[index_registry] %s partial (%d < %d); keeping the stored list",
                                   slug, len(fresh), floor)
                    continue
                before = _change_rows(slug)
                with rawstore.change_label(SOURCE[slug], run_id=run_id):
                    save_index(slug, fresh)
                out["appended"] = _change_rows(slug) - before
                out["status"] = "written"
            except Exception as exc:  # noqa: BLE001 - one list's failure is its own row, the others still run
                logger.warning("[index_registry] %s failed", slug, exc_info=True)
                out.update(status="error", error=f"{type(exc).__name__}: {exc}")
    return res


def lists_ok(res: dict) -> bool:
    """True when no list failed or fell under its floor (a gated first fill is waiting, not failing)."""
    return all(r["status"] in ("written", "awaiting_first_fill") for r in res.get("slugs", {}).values())


def summary_line(res: dict) -> str:
    parts = []
    for slug, r in res.get("slugs", {}).items():
        st = r["status"]
        if st == "written":
            parts.append(f"{slug} {r['members']:,} (+{r['appended']:,})")
        elif st == "below_floor":
            parts.append(f"{slug} below floor ({r['members']:,} < {r['floor']:,}, kept)")
        elif st == "awaiting_first_fill":
            parts.append(f"{slug} awaiting first fill")
        else:
            parts.append(f"{slug} error ({r.get('error', '')})")
    return "Index lists: " + (", ".join(parts) or "nothing ran")


def fetch_all_indexes(refresh: bool = False) -> dict[str, set[str]]:
    return {slug: fetch_index(slug, refresh=refresh) for slug, _ in INDEXES}


def _index_memberships(indexes: dict[str, set[str]]) -> dict[str, list[str]]:
    """Invert ``{slug -> tickers}`` into ``{ticker -> [labels]}`` (display order)."""
    memberships: dict[str, list[str]] = {}
    for slug, label in LABELS.items():
        for t in indexes.get(slug, set()):
            memberships.setdefault(t, []).append(label)
    return memberships


def build_registry(universe_rows: list[dict] | None = None,
                   refresh: bool = False) -> dict:
    """Fetch indexes (cache-first) into ``index_constituents`` and return
    ``{ticker: {index, name, sector}}``.

    Membership is persisted in ``index_constituents``; the ``index_tickers``
    VIEW supplies ``name`` / ``sector`` from ``equity_universe``. ``universe_rows``
    is accepted for backwards compatibility but no longer written here.
    """
    indexes = fetch_all_indexes(refresh=refresh)
    memberships = _index_memberships(indexes)

    names: dict[str, str] = {}
    sectors: dict[str, str] = {}
    for r in rawstore.query("SELECT ticker, name, sector FROM index_tickers"):
        t = str(r["ticker"]).upper()
        names[t] = r.get("name") or ""
        sectors[t] = r.get("sector") or ""

    registry: dict = {}
    for t in sorted(memberships):
        registry[t] = {
            "index": memberships[t],
            "name": names.get(t, ""),
            "sector": sectors.get(t, ""),
        }
    return registry


def load_registry() -> dict | None:
    rows = rawstore.query("SELECT slug, ticker, name, sector FROM index_tickers")
    if not rows:
        return None
    label = LABELS
    order = {lbl: i for i, lbl in enumerate(LABELS.values())}
    out: dict[str, dict] = {}
    for r in rows:
        t = str(r["ticker"]).upper()
        entry = out.setdefault(t, {"index": [], "name": r.get("name") or "",
                                   "sector": r.get("sector") or ""})
        lbl = label.get(r["slug"])
        if lbl and lbl not in entry["index"]:
            entry["index"].append(lbl)
    for entry in out.values():
        entry["index"].sort(key=lambda l: order.get(l, 99))
    return out or None


def filter_labels() -> list[str]:
    """The index labels the shared filter offers, in display order (K1, #214).

    A ``FIRST_FILL_GATED`` slug is listed only once it has rows, so the filter never offers an index no card
    can carry; the other slugs are always listed."""
    try:
        have = {r["slug"] for r in rawstore.query("SELECT DISTINCT slug FROM index_constituents")}
    except Exception:  # noqa: BLE001 - a fresh store without the table lists the ungated slugs
        have = set()
    return [lbl for slug, lbl in LABELS.items() if slug not in FIRST_FILL_GATED or slug in have]


def index_for(ticker: str, registry: dict | None = None) -> list[str]:
    reg = registry if registry is not None else (load_registry() or {})
    return list(reg.get((ticker or "").upper(), {}).get("index", []))


def annotate_cards(cards: list[dict], registry: dict | None = None) -> list[dict]:
    """Set ``card["index"]`` (list of labels) for each card, cache-first."""
    reg = registry if registry is not None else (load_registry() or {})
    for c in cards:
        c["index"] = list(reg.get((c.get("ticker") or "").upper(), {}).get("index", []))
    return cards


def _main(argv: list[str]) -> int:
    """``--refresh`` runs the weekly step (``scripts/index_lists.py --weekly``: lock, floors, run id and provider on
    the change rows); without it the stored registry is only read."""
    if "--refresh" in argv:
        from RevenueChaser.scripts import index_lists
        return index_lists.main(["--weekly"])
    reg = load_registry() or {}
    print(f"registry: {len(reg)} tickers -> rawstore.index_tickers (view)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
