"""The need catalog: one record per piece of data a dashboard can ask for.

A need is a name and nothing more clever: which ``rcdata.read`` function answers it, which table that function reads
(so the dataset that owns the write and the serving kind come from ``read``'s own maps, not from a second list here),
which column carries the row's day, which column names the thing asked for, and how old the newest day may be before
the answer counts as stale.

``max_lag_sessions`` is the **only** place a freshness constant lives. The numbers below are the defaults chosen
with this PR, not avo's measured ones: the daily stores get one session, the weekly spine five, the share counts a
quarter (they come from filings). They are written here so that changing one is a one-line change in one file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .. import read

TICKER = "ticker"
IDENTIFIER = "id"


@dataclass(frozen=True)
class Need:
    """One need. ``reader`` is resolved from ``reader_name`` so the catalog cannot point at a function of its own."""

    name: str
    reader_name: str
    table: str
    day_column: str
    key_column: str
    max_lag_sessions: int
    ask_param: str | None = None
    normalize: str = IDENTIFIER
    providers: tuple[str, ...] = ()
    fallbacks: tuple[str, ...] = ()
    legacy: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def reader(self) -> Callable:
        return getattr(read, self.reader_name)

    @property
    def dataset(self) -> str:
        """The dataset that owns writing this need's table (``rcdata.read``'s own map)."""
        return read.DATASET_OF_TABLE[self.table]

    @property
    def kind(self) -> str:
        """The serving file the need is read from (``rcdata.read``'s own map)."""
        return read.KIND_OF_TABLE[self.table]


_NEEDS = (
    # ── The identity spine (serving kind ``spine``; weekly sources) ──────────────────────────────────────────────
    Need("identity.universe", "universe", "security", "source_as_of", "ticker", 5, providers=("sec",)),
    Need("identity.security", "identity", "security", "source_as_of", "ticker", 5,
         ask_param="tickers", normalize=TICKER, providers=("sec",)),
    Need("identity.membership", "membership", "index_membership", "source_as_of", "ticker", 5,
         providers=("ssga", "ishares", "vanguard", "nasdaq", "invesco")),
    # ── The prices store (serving kind ``prices``; daily bars, share counts from filings) ────────────────────────
    Need("prices.bars", "bars", "price_bar_daily", "session_date", "security_id", 1,
         ask_param="security_ids", providers=("alpaca",)),
    Need("prices.market_cap", "market_cap", "market_cap_daily", "session_date", "security_id", 1,
         ask_param="security_ids", providers=("alpaca",)),
    Need("prices.shares", "shares", "shares_class", "as_of", "security_id", 65,
         ask_param="security_ids", providers=("sec",)),
)

NEEDS: dict[str, Need] = {n.name: n for n in _NEEDS}
