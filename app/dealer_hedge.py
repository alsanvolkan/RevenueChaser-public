"""Dealer Hedge (demo) tab (``/view/dealer_hedge``): avo's teaching page on how dealers delta-hedge and how
gamma, vanna and charm move that hedge (#376).

A static page. It reads no store, calls no provider, runs no job and sends no request: the live book is
Black-Scholes (r = 0, q = 0) computed in the page's own script, with four preset books and five scenarios.
The numbers are illustrative; the page says so in its footer. Plan: #376.
"""
from __future__ import annotations

from RevenueChaser.core import templating

# Named in the page footer. The one value the renderer passes in.
SOURCE = "avo'nun paylaştığı Dealer Hedge Mekaniği sayfası, 2026-10-10"


def render_page(source: str = SOURCE) -> str:
    return templating.render("dealer_hedge.html.j2", source=source)
