"""``company_profile`` — one row per ``equity_universe`` ticker, with the AG, Fundamentals and Swing scalars.

Scalar joins only; no N-valued joins and no scalar theme (themes live in ``ticker_themes``). It used to be a VIEW
in raw.duckdb; the three score tables now live in their boards' own files (DECISIONS S9: no ATTACH, one query per
file), so the join is done here, by ticker.
"""
from __future__ import annotations

from . import rawstore

_UNIVERSE = ("ticker", "name", "sector", "industry", "industry_group", "sub_industry", "market_cap", "price")
#: (table, {column: name in the profile})
_PARTS = (
    ("ticker_scores", {"total_points": "total_points", "tech_breakthrough": "tech_breakthrough",
                       "dilution_warning": "dilution_warning", "earnings_date": "earnings_date"}),
    ("fundamentals_scores", {"ro40": "ro40", "rev_growth_ttm": "rev_growth_ttm", "fcf_margin": "fcf_margin",
                             "gross_margin": "gross_margin", "roic": "roic", "pe": "pe", "pb": "pb",
                             "multiplier": "fundamentals_multiplier"}),
    ("swing_scores", {"quality": "swing_quality", "rs_rating": "rs_rating", "ma_alignment": "ma_alignment",
                      "multiplier": "swing_multiplier"}),
)


def ensure() -> None:
    """Make sure the base tables exist."""
    rawstore.ensure_all_tables()


def _part(table: str, cols: dict, tickers: list[str] | None) -> dict[str, dict]:
    sel = ", ".join(["ticker"] + [f"{c} AS {a}" for c, a in cols.items()])
    where, args = "", []
    if tickers:
        where, args = f" WHERE ticker IN ({', '.join(['?'] * len(tickers))})", tickers
    return {r.pop("ticker"): r for r in rawstore.query(f"SELECT {sel} FROM {table}{where}", args,
                                                       db=rawstore._db_of(table))}


def query(tickers: list[str] | None = None) -> list[dict]:
    want = [t.upper() for t in tickers] if tickers else None
    where, args = "", []
    if want:
        where, args = f" WHERE ticker IN ({', '.join(['?'] * len(want))})", want
    rows = rawstore.query(f"SELECT {', '.join(_UNIVERSE)} FROM equity_universe{where} "
                          "ORDER BY market_cap DESC NULLS LAST", args)
    parts = [(_part(t, cols, want), list(cols.values())) for t, cols in _PARTS]
    for r in rows:
        for found, names in parts:
            r.update(found.get(r["ticker"]) or dict.fromkeys(names))
    return rows


def get(ticker: str) -> dict | None:
    rows = query([ticker])
    return rows[0] if rows else None


def count() -> int:
    return rawstore.query("SELECT count(*) n FROM equity_universe")[0]["n"]
