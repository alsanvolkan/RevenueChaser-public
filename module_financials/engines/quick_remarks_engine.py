"""Quick Remarks Engine — Momentum-Growth skoru, Asymmetric Growth Hunter ile aynı metodoloji.

3 sinyal (her biri X/3), toplam 0-9 puan — Hunter'ın ``asymmetric_growth_hunter``
ile birebir aynı eşikler/tanımlar. Dilution ve R&D skor kuralı değil, overlay
(uyarı/etiket). Veri: önce paylaşılan ``ticker_scores``, yoksa yfinance fallback.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from RevenueChaser.core.providers import yfinance as yfinance_client
from RevenueChaser.core.providers.technical import fetch_technical

logger = logging.getLogger(__name__)


def _session(stamp) -> str | None:
    """The New York session ``stamp`` belongs to, the day the header and the comps name (``history._as_of``)."""
    from RevenueChaser.module_financials import history as fin_history
    return fin_history._as_of(stamp, None) if stamp else None


def _from_card(ticker: str, card: dict, source: str, as_of: str | None = None, as_of_note: str | None = None) -> dict:
    """The section's fields; ``as_of`` / ``as_of_note`` say when the score was made and by what (review S5)."""
    return {
        "ticker": ticker,
        "company": card.get("name") or ticker,
        "price": card.get("price"),
        "sector": card.get("sector"),
        "source": source,
        "score": card.get("total_points"),
        "max": 9,
        "signals": card.get("signals") or [],
        "dilution_warning": card.get("dilution_warning"),
        "dilution_yoy": card.get("dilution_yoy"),
        "tech_breakthrough": card.get("tech_breakthrough"),
        "rd_intensity": card.get("rd_intensity"),
        "as_of": as_of,
        "as_of_note": as_of_note,
    }


def run_quick_remarks(ticker: str, no_fetch: bool = False, financials: dict | None = None) -> dict:
    """``financials`` is the ``get_financials`` payload the caller already holds (the Fetch it just read, or the
    rows it read back from the store): the score is built from it, so this makes no second ``get_financials`` call
    (FIN-5, #385). Only a caller that passes none falls back to a fetch, and ``no_fetch`` (store-only render) never
    fetches at all."""
    ticker = ticker.upper()

    # 1. Paylaşılan store (market-cap taraması zaten Hunter metodolojisiyle skorladı)
    try:
        from RevenueChaser.core import ticker_store
        card = ticker_store.get(ticker)
        if card:
            # A stored scan card: dated by that scan, which may be days older than this report (review S5).
            return _from_card(ticker, card, "ticker_scores", _session(ticker_store.asof()),
                              "the last Asymmetric Growth Fetch")
    except Exception as exc:  # pragma: no cover - store hatası fallback'e düşer
        logger.debug("ticker_scores %s: %s", ticker, exc)

    # 2. yfinance fallback — Hunter'ın kendi score_ticker'ını kullan (birebir aynı metod).
    #    ``no_fetch`` (store-only re-render) fetches nothing: it shows what the last stored Fetch scored, dated by
    #    that Fetch, instead of an empty card (review S5).
    if no_fetch:
        from RevenueChaser.module_financials import history as fin_history
        return fin_history.last_quick_remarks(ticker) or _from_card(ticker, {}, "yfinance")
    try:
        from RevenueChaser.module_asymmetric_growth.engine import score_ticker
        if financials is None:
            financials = yfinance_client.get_financials(ticker)
        technical = fetch_technical(ticker)
        card = score_ticker(ticker, financials, technical)
        if card:
            stamp = financials.get("_fetched_at_iso") or datetime.now(timezone.utc).isoformat()
            return _from_card(ticker, card, "yfinance", _session(stamp), "this Fetch")
    except Exception as exc:  # pragma: no cover - network
        logger.debug("quick_remarks fallback %s: %s", ticker, exc)

    return _from_card(ticker, {}, "yfinance")
