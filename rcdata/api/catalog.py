"""The need catalog: one record per piece of data a dashboard can ask for.

A need is a name and nothing more clever: which ``rcdata.read`` function answers it, which table that function reads
(so the dataset that owns the write and the serving kind come from ``read``'s own maps, not from a second list here),
which column carries the row's day, which column names the thing asked for, and how old the newest day may be before
the answer counts as stale.

``max_lag`` is the **only** place a freshness constant lives, and the catalog is its single owner (avo, 29 Oct
2026): a board that needs the number reads it from here, and whatever hashes a board's model version reads it from
here too, so the two cannot drift apart.

Each need carries **one number and its unit** (avo, same day): ``sessions`` for anything built per trading day, and
``days`` for a build with no session of its own — the weekly analyst estimates are 14 calendar days old by their own
reckoning, and converting that to sessions would make the number mean something else. The numbers below are the
defaults chosen with this PR except where a board already measured one; they are written here so that changing one
is a one-line change in one file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .. import read

TICKER = "ticker"
IDENTIFIER = "id"
SESSIONS = "sessions"
DAYS = "days"
UNITS = (SESSIONS, DAYS)


@dataclass(frozen=True)
class Need:
    """One need. ``reader`` is resolved from ``reader_name`` so the catalog cannot point at a function of its own."""

    name: str
    reader_name: str
    table: str
    day_column: str
    key_column: str
    max_lag: int
    max_lag_unit: str = SESSIONS
    ask_param: str | None = None
    normalize: str = IDENTIFIER
    providers: tuple[str, ...] = ()
    fallbacks: tuple[str, ...] = ()
    legacy: bool = False
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.max_lag_unit not in UNITS:
            raise ValueError(f"{self.name}: max_lag_unit must be one of {', '.join(UNITS)}")

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
    # ── The weekly analyst estimates: their build has no session of its own, so their age is calendar days
    # (PR 285 measured 14). Earnings rule R1 reads this number from here, not from its own reader.
    Need("fundamentals.estimates", "estimates", "estimate_latest", "source_as_of", "ticker", 14,
         max_lag_unit=DAYS, ask_param="tickers", normalize=TICKER, providers=("yfinance",)),
    # ── The macro series the boards read (three needs over one table: each has its own age) ──────────────────────
    # The numbers are the Earnings board's own, measured there: FRED posts the yield a day late, so a normal night
    # reads 1 or 2 sessions and 3 covers one missed night; the volatility indices are daily too; the CFTC publishes
    # on a Friday the positions held the Tuesday before, so a current night already reads a ~6-session-old row.
    Need("macro.dgs10", "market_series", "market_series", "obs_date", "series_id", 3, providers=("fred",)),
    Need("market.vix", "market_series", "market_series", "obs_date", "series_id", 3, providers=("yfinance",)),
    Need("market.cot", "market_series", "market_series", "obs_date", "series_id", 10, providers=("cftc",)),
    Need("market.series", "market_series", "market_series", "obs_date", "series_id", 3,
         providers=("fred", "yfinance")),
    Need("market.spx_expiry_strike", "spx_expiry_strike", "spx_gamma_expiry_strike", "session_date", "expiry", 1,
         providers=("uw",)),
    # ── Ownership ───────────────────────────────────────────────────────────────────────────────────────────────
    Need("ownership.short_interest", "finra_short_interest", "finra_short_interest", "settlement_date", "ticker", 3,
         ask_param="tickers", normalize=TICKER, providers=("finra",)),
    # ── The earnings calendar a screener card shows (step B) ────────────────────────────────────────────────────
    # The options layer's screener calendar is the first source; the stored D26 calendar fills the names it does not
    # cover. That fallback is not a need of its own yet: the dashboard, not the builder, writes ``raw.duckdb``, so it
    # cannot be filled through the queue (the legacy row of the design's section 6). ``core.earnings_dates`` holds
    # the union until the D26 sweep moves to the builder.
    # avo, 29 Oct 2026: the calendar's freshness is **one session**. It settles the contradiction the boards carried
    # (``earnings_dates.MAX_AGE_DAYS`` said 4 calendar days, ``stored_inputs.MAX_LAG`` said 1 session for the same
    # calendar); the stricter of the two wins, so a calendar older than the last session is not read at all.
    Need("earnings.next", "option_screener", "option_screener_daily", "session_date", "ticker", 1,
         providers=("uw",), fallbacks=("d26_calendar",)),
)

NEEDS: dict[str, Need] = {n.name: n for n in _NEEDS}
