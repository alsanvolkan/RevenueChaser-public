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
    #: The column a row's day comes from, or None for a table with no date of its own (a peer group, a symbol
    #: mapping): such a need can be present or absent, never stale.
    day_column: str | None
    key_column: str
    max_lag: int
    max_lag_unit: str = SESSIONS
    #: The table's own key column, when the reader renames it on the way out (``latest_market`` returns ``ticker``
    #: for a table keyed ``security_id``). A summary counts this one; a row's key stays ``key_column``.
    table_key_column: str | None = None
    ask_param: str | None = None
    normalize: str = IDENTIFIER
    providers: tuple[str, ...] = ()
    fallbacks: tuple[str, ...] = ()
    legacy: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def summary_key(self) -> str:
        return self.table_key_column or self.key_column

    def __post_init__(self):
        if self.max_lag_unit not in UNITS:
            raise ValueError(f"{self.name}: max_lag_unit must be one of {', '.join(UNITS)}")

    @property
    def reader(self) -> Callable:
        return getattr(read, self.reader_name)

    @property
    def dataset(self) -> str | None:
        """The dataset that owns writing this need's table (``rcdata.read``'s own map).

        None for a table several datasets write: there the status is each row's own source's, as ``read`` returns
        it, and a dataset-level status would have to pick one of them.
        """
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
    # ── The fundamentals layer ──────────────────────────────────────────────────────────────────────────────────
    # The filings-driven needs are as old as the newest filing, so their window is wide: a company that has not
    # filed in a month is not stale data, it is a company that has not filed.
    Need("fundamentals.statements", "statements", "fundamental_fact", "period_end", "cik", 65, providers=("sec",)),
    Need("fundamentals.companies", "fundamental_companies", "fundamental_company", "facts_as_of", "cik", 5,
         ask_param="tickers", normalize=TICKER, providers=("sec",)),
    Need("fundamentals.earnings_event", "earnings_events", "earnings_event", "report_date", "cik", 1,
         providers=("sec", "uw")),
    Need("fundamentals.filings", "filing_items", "filing_item", "filing_date", "cik", 1, providers=("sec",)),
    Need("fundamentals.segments", "segments", "segment_revenue", "period_end", "cik", 65, providers=("sec",)),
    Need("fundamentals.customers", "customers", "customer_share", "period_end", "cik", 65, providers=("sec",)),
    # A peer group carries no date of its own: it is present or absent, never stale.
    Need("fundamentals.peers", "peers", "peer_group", None, "cik", 65, providers=("sec",)),
    Need("fundamentals.revision", "revisions", "fundamental_revision", "datadate", "cik", 65, providers=("wrds",)),
    # ── Ownership ───────────────────────────────────────────────────────────────────────────────────────────────
    # The 13F period tables are quarterly by construction: a filing deadline 45 days after a quarter end means the
    # newest period is routinely a quarter old, which is the data, not staleness.
    Need("ownership.holders_13f", "holders_13f", "sec13f_ticker_period", "period", "ticker", 65,
         ask_param="tickers", normalize=TICKER, providers=("sec",)),
    Need("ownership.periods_13f", "periods_13f", "sec13f_periods", "filed_through", "period", 65,
         providers=("sec",)),
    Need("ownership.shortable", "shortable", "ibkr_shortable", "session_date", "ticker", 1,
         ask_param="tickers", normalize=TICKER, providers=("ibkr",)),
    Need("ownership.threshold", "threshold_list", "regsho_threshold", "list_date", "ticker", 1,
         providers=("nasdaqtrader", "nyse")),
    Need("ownership.ftd", "fails_to_deliver", "sec_ftd", "settlement_date", "ticker", 10,
         ask_param="tickers", normalize=TICKER, providers=("sec",)),
    Need("ownership.insider", "insider_transactions", "insider_transactions", "filed", "ticker", 1,
         ask_param="tickers", normalize=TICKER, providers=("sec",)),
    Need("ownership.beneficial", "beneficial_ownership", "beneficial_ownership", "filed", "ticker", 5,
         ask_param="tickers", normalize=TICKER, providers=("sec",)),
    # ── Options ─────────────────────────────────────────────────────────────────────────────────────────────────
    Need("options.chain", "option_chain", "option_eod", "session_date", "ticker", 1, providers=("uw",)),
    Need("options.strikes", "option_strikes", "option_strike", "session_date", "ticker", 1, providers=("uw",)),
    Need("options.expiries", "option_expiries", "option_expiry", "session_date", "ticker", 1, providers=("uw",)),
    Need("options.daily", "option_daily", "option_daily", "session_date", "ticker", 1,
         ask_param="tickers", normalize=TICKER, providers=("uw",)),
    Need("options.prints", "option_prints", "option_print", "session_date", "ticker", 1, providers=("uw",)),
    Need("options.iv", "iv_history", "iv_daily", "session_date", "ticker", 1,
         ask_param="tickers", normalize=TICKER, providers=("uw", "ibkr")),
    Need("options.screener", "option_screener", "option_screener_daily", "session_date", "ticker", 1,
         providers=("uw",)),
    Need("earnings.options", "earnings_options", "er_options", "session_date", "ticker", 1,
         ask_param="tickers", normalize=TICKER, providers=("uw",)),
    # ── The prices store's remaining reads ──────────────────────────────────────────────────────────────────────
    Need("prices.actions", "actions", "corporate_action", "ex_date", "security_id", 1,
         ask_param="security_ids", providers=("alpaca",)),
    # ``latest_market`` returns a row keyed ``ticker``; the table itself is keyed ``security_id``.
    Need("prices.latest", "latest_market", "market_cap_daily", "session_date", "ticker", 1,
         table_key_column="security_id", ask_param="tickers", normalize=TICKER, providers=("alpaca",)),
    # The two second-source reports (Karar 32-B): the WRDS comparison is a monthly attended run, so its freshness
    # is counted in calendar days - a month-old report is not stale, and in sessions the number would mean
    # something else. The revision log is written by every serving build, so it is counted in sessions like the
    # bars it revises.
    Need("prices.wrds_diff", "price_source_diff", "wrds_price_diff", "as_of", "comparison", 35, DAYS,
         ask_param="comparison", providers=("wrds",)),
    Need("prices.revision", "price_revisions", "price_revision", "session_date", "security_id", 1,
         ask_param="security_ids"),
    Need("prices.security_ids", "security_ids", "security_symbol", "valid_from", "symbol", 5,
         ask_param="tickers", normalize=TICKER, providers=("alpaca",)),
)

NEEDS: dict[str, Need] = {n.name: n for n in _NEEDS}
