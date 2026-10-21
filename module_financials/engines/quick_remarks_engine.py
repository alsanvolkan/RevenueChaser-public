"""Quick Remarks Engine — Momentum-Growth skoru, Asymmetric Growth Hunter ile aynı metodoloji.

3 sinyal (her biri X/3), toplam 0-9 puan — Hunter'ın ``asymmetric_growth_hunter``
ile birebir aynı eşikler/tanımlar. Dilution ve R&D skor kuralı değil, overlay
(uyarı/etiket). Veri: önce paylaşılan ``ticker_scores``, yoksa yfinance fallback.
"""
from __future__ import annotations

import logging

from RevenueChaser.core.providers import yfinance as yfinance_client
from RevenueChaser.core.providers.technical import fetch_technical

logger = logging.getLogger(__name__)


def _from_card(ticker: str, card: dict, source: str) -> dict:
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
    }


def run_quick_remarks(ticker: str, no_fetch: bool = False) -> dict:
    ticker = ticker.upper()

    # 1. Paylaşılan store (market-cap taraması zaten Hunter metodolojisiyle skorladı)
    try:
        from RevenueChaser.core import ticker_store
        card = ticker_store.get(ticker)
        if card:
            return _from_card(ticker, card, "ticker_scores")
    except Exception as exc:  # pragma: no cover - store hatası fallback'e düşer
        logger.debug("ticker_scores %s: %s", ticker, exc)

    # 2. yfinance fallback — Hunter'ın kendi score_ticker'ını kullan (birebir aynı metod).
    #    ``no_fetch`` (store-only re-render) skips this: no network, no card.
    if no_fetch:
        return _from_card(ticker, {}, "yfinance")
    try:
        from RevenueChaser.module_asymmetric_growth.engine import score_ticker
        financials = yfinance_client.get_financials(ticker)
        technical = fetch_technical(ticker)
        card = score_ticker(ticker, financials, technical)
        if card:
            return _from_card(ticker, card, "yfinance")
    except Exception as exc:  # pragma: no cover - network
        logger.debug("quick_remarks fallback %s: %s", ticker, exc)

    return _from_card(ticker, {}, "yfinance")
