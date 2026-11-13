"""A watch-list symbol: avo's own spelling, split into its parts, and where its data comes from (#786).

A symbol is written the way TradingView writes it, ``EXCHANGE:TICKER`` — ``NASDAQ:NVDA``, ``BIST:THYAO``,
``COMEX:GC1!``, ``TVC:US10Y-TVC:US02Y``. That spelling is what goes back out when a list is exported, so it is
stored exactly as given and never normalised beyond trimming and upper-casing.

``coverage`` says which part of this repository can answer for a symbol. It is decided from the spelling alone, so
it needs no store and no network, and it is the whole reason an unsupported symbol can sit in a list without
breaking it (avo, 12 Nov 2026: the list stays whole, the row shows no data):

- ``us_equity``  — a plain US listing we carry: Nasdaq, NYSE, AMEX, Cboe, OTC (``rcdata/build/spine/security.py``).
- ``market_series`` — a market-wide series we already read (``rcdata/build/market_series.py``); the map below is
  avo's spelling on the left and our ``series_id`` on the right.
- ``derived`` — an expression over two symbols (``A/B``, ``A-B``), evaluated when the row is rendered.
- ``unsupported`` — no source here yet; issue #785 is the hunt for one. Foreign listings, futures, index CFDs,
  crypto, FX and the macro series we do not read are all this.

``etf`` is deliberately **not** decided here: an ETF is a US listing like any other and the spine says which names
are funds. A reader that needs the distinction asks the spine, not the spelling.
"""
from __future__ import annotations

from typing import NamedTuple

#: Exchange prefixes of the US listings the spine carries (``security.exchange``). OTC is included because avo's
#: lists hold a few (``OTC:EVVTY``); the spine's Tier D covers them.
US_EXCHANGES = frozenset({"NASDAQ", "NYSE", "AMEX", "CBOE", "OTC", "BATS", "ARCA", "NYSEARCA"})

#: avo's spelling of a market series -> the ``series_id`` of ``market_series``. Only what we actually read today;
#: everything else is ``unsupported`` until #785 finds it a source.
SERIES_ALIASES = {
    "TVC:VIX": "VIX",
    "TVC:VVIX": "VVIX",
    "CBOE:VVIX": "VVIX",
    "TVC:US02Y": "DGS2",
    "TVC:US05Y": "DGS5",
    "TVC:US10Y": "DGS10",
    "TVC:US20Y": "DGS20",
    "TVC:US30Y": "DGS30",
    "TVC:US10Y-TVC:US02Y": "T10Y2Y",
    "FRED:RRPONTSYD": "RRPONTSYD",
    "FRED:BAMLH0A0HYM2": "BAMLH0A0HYM2",
}

#: Symbols that carry a US exchange prefix but are **not** a listing: an index the exchange publishes. Without
#: this they would read as ``us_equity`` and the board would promise data the spine has no row for. Cboe's own
#: index symbols are the whole of it today; the ones we do serve are in ``SERIES_ALIASES`` above and win, because
#: that map is checked first.
NOT_LISTED = frozenset({"CBOE:VXN", "CBOE:SPX", "CBOE:VIX", "CBOE:VIX9D", "CBOE:VIX3M", "CBOE:OVX", "INDEX:S5FD"})

#: The spellings that look like an expression. Checked before the series map, except that a series alias naming a
#: spread of its own (``TVC:US10Y-TVC:US02Y`` -> ``T10Y2Y``) wins, because we serve that spread as one series.
_OPERATORS = ("/", "-")

COVERAGES = ("us_equity", "market_series", "derived", "unsupported")


class Symbol(NamedTuple):
    """One watch-list symbol, as stored."""

    raw: str
    exchange: str
    ticker: str
    coverage: str
    series_id: str | None = None

    @property
    def is_supported(self) -> bool:
        return self.coverage != "unsupported"


def split(symbol: str) -> tuple[str, str]:
    """``"NASDAQ:NVDA"`` -> ``("NASDAQ", "NVDA")``. A symbol with no prefix has an empty exchange, and one with
    several colons keeps everything after the first as the ticker (``TVC:US10Y-TVC:US02Y``)."""
    text = (symbol or "").strip()
    if ":" not in text:
        return "", text.upper()
    exchange, _, ticker = text.partition(":")
    return exchange.strip().upper(), ticker.strip().upper()


def parse(symbol: str) -> Symbol:
    """Read one symbol as avo wrote it. Never raises: an empty or unreadable symbol is ``unsupported``."""
    raw = (symbol or "").strip()
    if not raw:
        return Symbol("", "", "", "unsupported")
    raw = raw.upper()
    exchange, ticker = split(raw)

    series = SERIES_ALIASES.get(raw)
    if series:
        return Symbol(raw, exchange, ticker, "market_series", series)

    if any(op in ticker for op in _OPERATORS) or raw.count(":") > 1:
        return Symbol(raw, exchange, ticker, "derived")

    if raw in NOT_LISTED:
        return Symbol(raw, exchange, ticker, "unsupported")

    if exchange in US_EXCHANGES:
        return Symbol(raw, exchange, ticker, "us_equity")

    return Symbol(raw, exchange, ticker, "unsupported")


def coverage_of(symbol: str) -> str:
    return parse(symbol).coverage
