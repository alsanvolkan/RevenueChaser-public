"""Multi-source ETF holdings retrieval with fallback chain.

Primary: Unusual Whales API
Secondary: Financial Modeling Prep (FMP) — only when FMP_API_KEY is set
Tertiary: yfinance (free, no key) — funds_data.top_holdings

All sources normalize to [{ticker, name, weight}] where weight is a percentage.
"""
from __future__ import annotations

import logging
from typing import Optional

from RevenueChaser.core.config import FMP_API_KEY
from RevenueChaser.core import datahealth
from RevenueChaser.core.datahealth import Health

logger = logging.getLogger(__name__)


def _norm_weight(v) -> float:
    try:
        return round(float(v), 3)
    except (ValueError, TypeError):
        return 0.0


def fetch_from_unusual_whales(ticker: str, uw) -> list[dict]:
    """UW holdings (already deduped + sorted by the client)."""
    try:
        rows = uw.etf_holdings(ticker)
    except Exception:
        return []
    out = []
    for it in rows:
        t = it.get("ticker") or it.get("symbol", "")
        if not t:
            continue
        out.append({"ticker": str(t).strip().upper(),
                    "name": it.get("name") or "",
                    "weight": _norm_weight(it.get("weight"))})
    return out


def fetch_from_fmp(ticker: str, api_key: str) -> list[dict]:
    """FMP /api/v3/etf-holder/{ticker} — constituent list."""
    if not api_key:
        return []
    import httpx
    try:
        r = httpx.get(f"https://financialmodelingprep.com/api/v3/etf-holder/{ticker}",
                      params={"apikey": api_key}, timeout=15, follow_redirects=True)
        r.raise_for_status()
        items = r.json()
    except Exception as exc:
        logger.debug(f"[fmp] {ticker}: {exc}")
        return []
    out = []
    for it in items or []:
        t = it.get("asset") or it.get("symbol", "")
        if not t:
            continue
        # FMP weight may be absolute or percent; treat > 1 as basis-point-percent
        w = _norm_weight(it.get("weightPercentage", it.get("weight", 0)))
        if w > 100:
            w = w / 100
        out.append({"ticker": str(t).strip().upper(),
                    "name": it.get("name") or "",
                    "weight": w})
    out.sort(key=lambda x: x["weight"], reverse=True)
    return out


def fetch_from_yfinance(ticker: str) -> list[dict]:
    """yfinance funds_data.top_holdings (top ~10 by weight)."""
    import yfinance as yf
    try:
        t = yf.Ticker(ticker)
        top = t.funds_data.top_holdings
        if top is None or getattr(top, "empty", True):
            return []
    except Exception as exc:
        logger.debug(f"[yfinance] {ticker}: {exc}")
        return []
    out = []
    for symbol, row in top.iterrows():
        pct = _norm_weight(row.get("Holding Percent", 0.0))
        out.append({"ticker": str(symbol).strip().upper(),
                    "name": str(row.get("Name", symbol)),
                    "weight": round(pct * 100, 3)})
    out.sort(key=lambda x: x["weight"], reverse=True)
    return out


class HoldingsList(list):
    """A holdings answer that also says which provider gave it (``None`` when none did)."""

    def __init__(self, rows=(), provider: Optional[str] = None):
        super().__init__(rows)
        self.provider = provider


def get_etf_holdings(ticker: str, uw, fmp_api_key: Optional[str] = None) -> HoldingsList:
    """Resolve top-10 holdings via UW -> FMP -> yfinance, capped at 10; ``.provider`` names the source."""
    clean = ticker.strip().upper()

    holdings = fetch_from_unusual_whales(clean, uw)
    if holdings:
        return HoldingsList(holdings[:10], "uw")

    holdings = fetch_from_fmp(clean, fmp_api_key or FMP_API_KEY)
    if holdings:
        return HoldingsList(holdings[:10], "fmp")

    holdings = fetch_from_yfinance(clean)
    return HoldingsList(holdings[:10], "yfinance" if holdings else None)


# ── Stored holdings (#303, D25) ─────────────────────────────────────────────────
# The holdings job (``app/runners.run_etf_holdings_sync``) is the one writer of ``etf_holdings``; the Rotation Fetch
# reads it here instead of fetching. The store keeps the change history (``etf_holdings_changes``).

def synced_etfs() -> list[str]:
    """The ETFs the holdings job resolves: every sector and theme ETF of the Rotation universe."""
    from .universe import ROTATION_ETFS
    return [t for t, info in ROTATION_ETFS.items() if info.get("group") in ("sector", "theme")]


def holdings_row(ticker: str, rows: list[dict], fetched_at: float) -> dict:
    """The ``etf_holdings`` row the holdings job writes: the list, its provider (``None`` if unknown), the fetch time."""
    return {"ticker": ticker, "entry": list(rows), "provider": getattr(rows, "provider", None),
            "fetched_at": fetched_at}


def stored_holdings(ticker: str) -> list[dict]:
    """An ETF's stored top holdings (no network); ``[]`` when the job has stored none."""
    from RevenueChaser.core import rawstore
    row = rawstore.get("etf_holdings", ticker.strip().upper())
    return list((row or {}).get("entry") or [])


@datahealth.scoped
def health_summary() -> dict:
    """What Data Health says about the stored holdings (store only).

    ``newest_session`` is the session (rule B) of the newest fetch; ``in_newest`` counts the ETFs fetched in it,
    ``providers`` the provider of every stored list, ``missing`` the synced ETFs with no row or an empty list (every
    provider failed and nothing was stored before), ``rs_stored`` the theme ETFs with stored RS rows
    (``rotation_holdings``)."""
    from RevenueChaser.core import calendar, rawstore
    from .universe import ROTATION_ETFS
    expected = synced_etfs()
    rows = {r["ticker"]: r for r in rawstore.query(
        "SELECT ticker, provider, fetched_at FROM etf_holdings WHERE json_array_length(entry) > 0")}
    sessions = {t: calendar.session_for(r["fetched_at"]).session_date
                for t, r in rows.items() if r.get("fetched_at") is not None}
    newest = max(sessions.values()) if sessions else None
    providers: dict[str, int] = {}
    for r in rows.values():
        if r.get("provider"):
            providers[r["provider"]] = providers.get(r["provider"], 0) + 1
    themes = [t for t, info in ROTATION_ETFS.items() if info.get("group") == "theme"]
    with_rs = {r["ticker"] for r in rawstore.query(
        "SELECT ticker FROM rotation_holdings WHERE rows IS NOT NULL AND json_array_length(rows) > 0")}
    return {
        "expected": len(expected),
        "stored": sum(1 for t in expected if t in rows),
        "newest_session": newest.isoformat() if newest else None,
        "in_newest": sum(1 for t in expected if newest is not None and sessions.get(t) == newest),
        "providers": providers,
        "missing": sorted(t for t in expected if t not in rows),
        "themes": len(themes),
        "rs_stored": sum(1 for t in themes if t in with_rs),
    }


def health() -> Health:
    """The numbers :func:`health_summary` reports, typed (#124 S4).

    ``covered`` is the ETFs with stored holdings against the ``expected`` the holdings job resolves;
    ``source_share`` is the provider of each stored list as a share of the stored lists (a list stored without a
    provider is in no numerator, so the shares can sum below 1). ``newest`` is the session of the newest fetch;
    the summary reads no calendar, so it has no ``expected_as_of``.
    """
    n = health_summary()
    stored = n["stored"]
    return Health(
        covered=stored,
        expected=n["expected"],
        newest=n["newest_session"],
        expected_as_of=None,
        source_share={p: c / stored for p, c in n["providers"].items()} if stored else None,
    )
