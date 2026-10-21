"""Liquid assets of one EDGAR annual record: cash plus short-term investments (#187).

One shared pure helper, so the AG board (net debt, runway) and the Fundamentals board
(net debt / EBITDA) cannot define "cash" differently.

Rules:
- Both figures come from ONE record. A caller must never take the investments from an older
  year than the cash: the company may have sold them since.
- A missing ``short_term_investments`` counts as 0 **only when cash is present**. That
  understates liquidity, never inflates it, and equals the cash-only number of before.
- Investments never stand in for a missing cash: no cash, no liquidity.
- Noncurrent securities and ``long_term_investments`` are stored on the record for visibility
  and are never counted here.
- Only the EDGAR field is read. yfinance records have no ``short_term_investments`` key and
  their ``cash`` may already include investments, so nothing is added to them.
"""
from __future__ import annotations

from .config import to_float


def liquid_assets(rec: dict | None) -> tuple[float | None, str | None]:
    """``(value, basis)`` for one annual record: ``cash`` plus ``short_term_investments``.

    ``basis`` is ``"cash+sti"`` or ``"cash_only"``; ``(None, None)`` when there is no cash.
    The basis is for the dry run's count; it is not rendered anywhere.
    """
    if not rec:
        return None, None
    cash = to_float(rec.get("cash"))
    if cash is None:
        return None, None
    sti = to_float(rec.get("short_term_investments"))
    if sti is None or sti < 0:
        return cash, "cash_only"
    return cash + sti, "cash+sti"
