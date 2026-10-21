"""Shared per-ticker store — Asymmetric Growth Hunter verisini diğer board'lara açar.

DuckDB-backed (``.store/raw.duckdb`` → ``ticker_scores``). Tek yazıcı = market-cap
taraması (``runners.run_asymmetric_growth``); diğer board'lar salt-okur
(``get`` / ``list_tickers``).
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from . import rawstore

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "3"     # 3: tier, flags, model_version, anchor_fy, prior_fy, period_ends (#380 AG W1 S2, S3)

# Kart -> sütun adları. JSON olarak saklananlar ``_JSON_COLS``.
_SCALAR_COLS = [
    "ticker", "name", "sector", "industry", "industry_group", "sub_industry",
    "price", "market_cap", "ps_ratio",
    "rev_growth", "gross_margin", "fcf", "runway_years", "rd_intensity",
    "dilution_yoy", "earnings_date", "total_points", "tech_breakthrough",
    "dilution_warning", "us_listed", "source",
    "inst_holders", "inst_new", "inst_exited", "inst_buy_flow",
    "inst_sell_flow", "inst_shares", "inst_shares_delta_pct", "inst_ownership_pct",
    "tier", "model_version", "anchor_fy", "prior_fy",
]
_JSON_COLS = ["themes", "signals", "flags", "period_ends"]
_JSON_DICT_COLS = ("period_ends",)          # a JSON object; the other JSON columns hold lists
_BOOL_COLS = ("tech_breakthrough", "dilution_warning", "us_listed")


def _empty(col: str):
    return {} if col in _JSON_DICT_COLS else []


def _meta(key: str) -> Optional[str]:
    row = rawstore.get("store_meta", key)
    return row.get("value") if row else None


def _set_meta(key: str, value: str) -> None:
    rawstore.write_rows("store_meta", [{"key": key, "value": value}])


def rows_from_cards(cards: list[dict]) -> list[dict]:
    """The ``ticker_scores`` rows of a card list. The one mapping the latest table and the result history share."""
    rows = []
    for c in cards:
        t = c.get("ticker")
        if not t:
            continue
        row = {}
        for col in _SCALAR_COLS:
            v = c.get(col)
            if isinstance(v, bool):
                v = int(v)
            row[col] = v
        for col in _JSON_COLS:
            row[col] = c.get(col) or _empty(col)
        rows.append(row)
    return rows


def _col_types() -> dict[str, str]:
    """``ticker_scores`` column -> its base SQL type (``DOUBLE``, ``INTEGER``, ``TEXT``)."""
    out = {}
    for c in rawstore.MANIFEST["ticker_scores"]["columns"]:
        name, _, rest = c.partition(" ")
        out[name] = rest.split()[0].upper() if rest else ""
    return out


def card_view(card: dict) -> dict:
    """The card as the board embeds it: the ``ticker_scores`` columns, typed as the table stores them, plus the
    card's index labels. A Fetch (cards in memory) and a Refresh (the rows read back, K4 #383 R1) embed the same
    card; a field the table does not keep (``fiscal_year``) is not shown."""
    types = _col_types()
    out = {}
    for col in _SCALAR_COLS:
        v = card.get(col)
        kind = types.get(col)
        if v is not None and not isinstance(v, str):
            if col in _BOOL_COLS:
                v = bool(v)
            elif kind == "DOUBLE":
                v = float(v)
            elif kind == "INTEGER":
                v = int(round(float(v)))
        out[col] = v
    for col in _JSON_COLS:
        v = card.get(col) or _empty(col)
        out[col] = dict(v) if isinstance(v, dict) else list(v)
    out["index"] = list(card.get("index") or [])
    return out


def all_cards() -> list[dict]:
    """Every stored card (the last Fetch's ``ticker_scores`` rows), unsorted."""
    return [_row_to_card(r) for r in rawstore.get_all("ticker_scores")]


def write_from_cards(cards: list[dict], asof: str | None = None):
    """Kart listesini ticker-keyed store'a yazar (tam yenileme).

    ``replace_all`` — clear + insert in ONE transaction. Two separate calls
    would leave the store empty if the insert failed, and would expose an empty
    window to the dashboard's concurrent readers.
    """
    rows = rows_from_cards(cards)
    if not rows:
        # Never let an empty write wipe a populated store. A model-only
        # (``no_fetch``) scan with a cold cache produces no cards; the board
        # should show its empty state while the durable ticker data survives.
        logger.warning("ticker_store.write_from_cards: empty card list — store left unchanged")
        return
    rawstore.replace_all("ticker_scores", rows)
    if asof:
        _set_meta("asof", asof)
    _set_meta("schema_version", SCHEMA_VERSION)


def _row_to_card(row: dict) -> dict:
    d = dict(row)
    for col in _JSON_COLS:
        raw = d.get(col)
        if isinstance(raw, (list, dict)):
            continue  # already parsed back by rawstore
        try:
            d[col] = json.loads(raw) if raw else _empty(col)
        except (json.JSONDecodeError, TypeError):
            d[col] = _empty(col)
    for col in _BOOL_COLS:
        if d.get(col) is not None:
            d[col] = bool(d[col])
    return d


def get(ticker: str) -> Optional[dict]:
    row = rawstore.get("ticker_scores", ticker.upper())
    return _row_to_card(row) if row else None


def list_tickers() -> list[dict]:
    rows = rawstore.query(
        "SELECT ticker, name, sector, market_cap, total_points FROM ticker_scores ORDER BY ticker")
    return [dict(r) for r in rows]


def count() -> int:
    return rawstore.count("ticker_scores")


def asof() -> Optional[str]:
    return _meta("asof")
