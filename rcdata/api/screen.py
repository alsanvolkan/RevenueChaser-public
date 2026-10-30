"""The Settings / Data screen's own lists, kept beside the need catalog.

The screen shows one card per **dataset** — what it is called in plain words, how often it is meant to run, what
share of the names it should cover, which column dates its newest write — and the catalog records one row per
**need**. Those are two grains, so the screen's facts live here, in the catalog's own package, rather than in the
screen: a need added to the catalog and a dataset added to the screen are then one edit in one place, and
``module_settings/newdata.py`` reads these maps instead of keeping a copy (a write to its copy is refused).

Two columns, not one: ``Need.day_column`` is the day a board asks about (a quarter's ``period_end``, an earnings
``report_date``), while ``WRITTEN_COLUMN`` is the column that says how recently the dataset was written
(``filed``, ``source_as_of``) — which is what a freshness card shows. Seven tables differ, and
``test_the_rows_day_and_the_written_column_are_allowed_to_differ`` holds that difference open on purpose.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DatasetCard:
    """One dataset as the screen shows it. ``coverage_target`` of None means the dataset has no share to hit.

    ``step`` is its line in the build plan (1-7 the spine, 8 prices, 9 fundamentals, 10 ownership, 11 options and the
    market series), and ``role`` what it is to the data it writes: the ``first`` source, the ``only source``, one
    that ``fills`` what the first leaves open, a ``cross-check`` that is compared and not served, or the ``build``
    that writes the served tables from the others' Parquet. The control board's journey table shows both.
    """

    label: str
    cadence: str = "daily"
    coverage_target: float | None = None
    step: int = 0
    role: str = "first"


#: The roles a card may name (``DatasetCard.role``).
ROLES = ("first", "only source", "fills", "cross-check", "build")


#: The screen's card per dataset. A dataset the store knows and this map does not raises in CI.
DATASETS: dict[str, DatasetCard] = {
    'alpaca/bars': DatasetCard('Daily bars (Alpaca)', coverage_target=0.99, step=8, role='first'),
    'alpaca/corporate_actions': DatasetCard('Corporate actions (Alpaca)', step=8, role='first'),
    'alphavantage/earnings_calendar': DatasetCard('Earnings calendar (Alpha Vantage)', step=9, role='first'),
    'finra/short_interest': DatasetCard('Short interest (FINRA)', step=10, role='first'),
    'fundamentals/tables': DatasetCard('Fundamentals (served tables, peers, build issues)', step=9, role='build'),
    'funds/index_holdings': DatasetCard('Index membership (ETF holdings files)', step=6, role='first'),
    'funds/etf_holdings': DatasetCard('ETF holdings, whole (SPDR, iShares, Unusual Whales)', step=6, role='only source'),
    'ibkr/iv_history': DatasetCard('One year of 30-day implied volatility per list name (IBKR)', coverage_target=None, step=11, role='first'),
    'ibkr/shortable': DatasetCard('Borrow fee and shares available (IBKR file)', step=10, role='first'),
    'market_series': DatasetCard('Market series', coverage_target=None, step=11, role='first'),
    'openfigi/map': DatasetCard('OpenFIGI map of the CUSIPs', step=4, role='only source'),
    'ownership/serve': DatasetCard('13F totals per ticker and quarter', step=10, role='build'),
    'prices/serving': DatasetCard('Prices store (bars, actions, factors, shares, market cap)', step=8, role='build'),
    'regsho/threshold': DatasetCard('Reg SHO threshold lists', step=10, role='first'),
    'sec/13dg': DatasetCard('5% owners (Schedules 13D and 13G)', step=10, role='first'),
    'sec/13f': DatasetCard('13F holdings (SEC data sets, then forms)', step=10, role='first'),
    'sec/company_tickers_exchange': DatasetCard('SEC ticker and exchange file', step=1, role='first'),
    'sec/companyfacts': DatasetCard('SEC company facts (re-read on the filing day)', step=9, role='first'),
    'sec/cover_shares': DatasetCard('Shares per class (SEC filing covers)', step=8, role='first'),
    'sec/filings': DatasetCard('SEC filing lists, 8-K items and report texts', step=9, role='first'),
    'sec/form4': DatasetCard('Insider transactions (Form 4)', step=10, role='first'),
    'sec/fsds': DatasetCard('SEC Financial Statement Data Sets (the check)', step=9, role='fills'),
    'sec/ftd': DatasetCard('Fails to deliver (SEC)', step=10, role='first'),
    'spine/security': DatasetCard('Identity spine (security, tiers, history)', coverage_target=0.98, step=7, role='build'),
    'uw/financials': DatasetCard('Preliminary quarters (Unusual Whales, off by default)', step=9, role='fills'),
    'uw/option_list': DatasetCard('Options of SPX and the option list (Unusual Whales)', coverage_target=0.95, step=11, role='first'),
    'uw/option_screener': DatasetCard('Earnings options: calendar, screener and earnings set (Unusual Whales)', coverage_target=0.95, step=11, role='first'),
    'uw/spot_expiry_strike': DatasetCard('SPX expiry-strike (Unusual Whales)', coverage_target=0.95, step=11, role='first'),
    'uw/total_float': DatasetCard('Float check (Unusual Whales)', step=8, role='cross-check'),
    'wrds/co_hgic': DatasetCard('Compustat GICS history (WRDS)', step=5, role='fills'),
    'wrds/firm_ratio': DatasetCard('WRDS financial ratios (the debt check)', step=9, role='cross-check'),
    'wrds/funda': DatasetCard('Compustat annual fundamentals (WRDS, the revision check)', step=9, role='cross-check'),
    'wrds/fundq': DatasetCard('Compustat quarterly fundamentals (WRDS, the revision check)', step=9, role='cross-check'),
    'wrds/idxcst_his': DatasetCard('Compustat index constituents (WRDS, the membership check)', step=6, role='cross-check'),
    'wrds/prices': DatasetCard('Price history and actions (WRDS: Compustat, CRSP)', step=8, role='cross-check'),
    'wrds/sec_idhist': DatasetCard('Compustat securities and identifier history (WRDS)', step=3, role='fills'),
    'wrds/segments': DatasetCard('Compustat segments and customers (WRDS)', step=9, role='fills'),
    'yfinance/estimates': DatasetCard('Analyst estimates (yfinance)', step=9, role='first'),
    'yfinance/float': DatasetCard('Float (Yahoo)', step=8, role='fills'),
}

#: The column whose newest value dates a serving table's last write. Never ``Need.day_column``; see the note above.
WRITTEN_COLUMN: dict[str, str] = {
    'adj_factor': 'valid_to',
    'beneficial_ownership': 'filed',
    'corporate_action': 'ex_date',
    'customer_share': 'document_date',
    'earnings_event': 'source_as_of',
    'er_options': 'session_date',
    'estimate_latest': 'source_as_of',
    'etf_holdings': 'as_of',
    'filing_item': 'filing_date',
    'finra_short_interest': 'settlement_date',
    'fundamental_company': 'facts_as_of',
    'fundamental_fact': 'filed',
    'fundamental_revision': 'new_as_of',
    'gics_history': 'valid_from',
    'ibkr_shortable': 'session_date',
    'index_membership': 'valid_from',
    'insider_transactions': 'filed',
    'iv_daily': 'session_date',
    'market_cap_daily': 'session_date',
    'market_series': 'obs_date',
    'option_daily': 'session_date',
    'option_eod': 'session_date',
    'option_expiry': 'session_date',
    'option_print': 'session_date',
    'option_screener_daily': 'session_date',
    'option_strike': 'session_date',
    'price_bar_daily': 'session_date',
    'price_revision': 'session_date',
    'regsho_threshold': 'list_date',
    'sec13f_cusip_map': 'period',
    'sec13f_filer_period': 'period',
    'sec13f_filings': 'filed',
    'sec13f_periods': 'period',
    'sec13f_ticker_period': 'period',
    'sec_ftd': 'settlement_date',
    'security': 'source_as_of',
    'security_history': 'valid_from',
    'security_symbol': 'valid_from',
    'segment_revenue': 'document_date',
    'shares_class': 'as_of',
    'spx_gamma_expiry_strike': 'session_date',
    'wrds_price_diff': 'as_of',
}

#: Old ``.store`` tables a new dataset replaces, and the table that replaces each. Their reads in ``read_log`` are
#: the retirement count, read for ``RETIREMENT_DAYS`` days before the old table goes.
RETIRING: dict[str, str] = {
    'cftc_cot': 'market_series',
    'economic_calendar': 'market_series',
    'estimate_snapshots': 'estimate_latest',
    'macro_series': 'market_series',
    'vol_index_daily': 'market_series',
}
RETIREMENT_DAYS = 14
#: How many days a weekly or a monthly dataset may go without a run before its card says "late".
CYCLE_DAYS = {"weekly": 8, "monthly": 35}
#: Worst first, so a sort on this puts what needs attention at the top of the screen.
STATUS_ORDER = ("stale", "never", "late", "unchanged", "fresh")


def labels() -> dict[str, str]:
    return {name: card.label for name, card in DATASETS.items()}


def cadences() -> dict[str, str]:
    return {name: card.cadence for name, card in DATASETS.items()}


def coverage_targets() -> dict[str, float | None]:
    """Every dataset, with its share or None — a card with no target is in the map, not missing from it."""
    return {name: card.coverage_target for name, card in DATASETS.items()}


def written_columns() -> dict[str, str]:
    return dict(WRITTEN_COLUMN)


# ── The control board (design v28, avo 30 Oct 2026) ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FileCard:
    """One serving file as the control board groups it: its steps in the build plan, and the one sentence that
    says which names it runs on (the universe above, or none, because it is not keyed by a name)."""

    steps: str
    words: str
    runs_on_universe: bool


#: The serving files in build order, the order the board's Fetch and Data sections list them in.
FILES: dict[str, FileCard] = {
    "spine": FileCard("Steps 1–7 · the spine", "sets the universe above: the tier sets come from this file", False),
    "prices": FileCard("Step 8 · prices and corporate actions", "runs on the universe at the top of this section",
                       True),
    "fundamentals": FileCard("Step 9 · fundamentals and earnings", "runs on the universe at the top of this section",
                             True),
    "ownership": FileCard("Step 10 · the short side and ownership",
                          "not tied to a tier: every filing is read, its CUSIP or symbol is matched to the spine",
                          False),
    "options": FileCard("Step 11 · options", "the option list (SPX and the watch list), not a tier set", False),
    "market": FileCard("Step 11 · market-wide series",
                       "not tied to the spine: series are keyed by series_id, not by name", False),
}

#: Why a dataset has no "Fill by tier set" row: a dataset fills by tier only when its fetch asks the names a
#: request's scope names (``rcdata.build.base.honours_scope``). The kind's words, then a dataset's own.
NO_TIER_WORDS: dict[str, str] = {
    "spine": "the spine is the list the tier sets come from.",
    "prices": "this dataset reads its own list of names, so a request's tiers are noted on its check, not used.",
    "fundamentals": "this dataset reads its own list of names, so a request's tiers are noted on its check, not used.",
    "ownership": "whole-market files (13F sets, FINRA, Reg SHO, fails to deliver, Form 4): every name at once.",
    "options": "the option list (SPX and the watch list), not a tier set.",
    "market": "series, not names.",
}
NO_TIER_DATASET_WORDS: dict[str, str] = {
    "alphavantage/earnings_calendar": "one call brings the whole calendar, every name at once.",
    "sec/fsds": "one file a quarter holds every filer at once.",
}
#: A hand-run (attended) dataset is never filled by tier.
NO_TIER_ATTENDED = "a check source run by hand, not filled by tier."


@dataclass(frozen=True)
class UniverseSet:
    """One segment of the universe bar: a tier, or a part of one. ``offered`` False is drawn, hatched, never picked."""

    key: str
    label: str
    tier: str
    words: str
    offered: bool = True


#: The universe's sets (avo, 30 Oct 2026: the universe is Tier A + B; C and D are shown, not offered). Tier B is two
#: sets: the listed funds (``etp`` and ``cef``) and the rest of B (SPACs, preferreds, warrants, units).
UNIVERSE_SETS: tuple[UniverseSet, ...] = (
    UniverseSet("A", "A · operating", "A", "listed and an operating company (or not typed yet)"),
    UniverseSet("E", "B · ETF + CEF", "B", "listed funds: ETFs and closed-end funds"),
    UniverseSet("R", "B · the rest", "B", "listed, other excluded types: SPAC, preferred, warrant, unit"),
    UniverseSet("C", "C · over the counter", "C", "not offered: an over-the-counter name", offered=False),
    UniverseSet("D", "D · no exchange", "D", "not offered: no exchange listing", offered=False),
)
#: The security types of the "B · ETF + CEF" set (``core.identity.types``).
FUND_TYPES = ("etp", "cef")
#: The named groups a narrowed request asks for besides its tiers and names (``rcdata.build.base.SCOPE_GROUPS``,
#: avo's Karar 82), as the universe control words them.
INCLUDE_WORDS = {"anchors": "the benchmarks and the rotation ETFs", "watchlist": "the stock watch list",
                 "cover_classes": "the other listed classes of a Tier A issuer"}

#: The switch that turns the Fill buttons on. Off unless set: no fill starts until avo's word ("başla"), and a test
#: or a fresh install never fetches from a press it did not mean (avo's standing rule of 29 Oct 2026).
FILL_ENV = "RC_FILL_ENABLED"
FILL_OFF_WHY = "off until your word: no fill starts before it (RC_FILL_ENABLED is not set on this server)"


def fill_enabled() -> bool:
    """True only when ``RC_FILL_ENABLED`` is ``1``, ``true``, ``yes`` or ``on``."""
    import os
    return os.environ.get(FILL_ENV, "").strip().lower() in ("1", "true", "yes", "on")


#: The build plan's steps as the flow diagram's middle column names them (``DatasetCard.step``). Step 2 has no dataset
#: of its own: the tiers are worked out by ``spine/security`` from what steps 1 and 3-6 brought.
STEPS: dict[int, str] = {
    1: "Security", 2: "Tiers", 3: "CIK ↔ CUSIP", 4: "FIGI", 5: "Sector classification", 6: "Index membership",
    7: "Build and health check", 8: "Prices and corporate actions", 9: "Fundamentals and earnings",
    10: "Short side and ownership", 11: "Options and market-wide series",
}


@dataclass(frozen=True)
class ProviderWords:
    """One provider as the control board names it: its name, what it brings, our own pace and the provider's rule.
    The day cap is not here: it is ``rcdata.caps.DAY_CAPS``, read where it is shown."""

    name: str
    what: str
    pace: str = "none"
    rule: str = "none quoted in the code"


#: Every provider a dataset names (``Dataset.providers``). A provider a dataset names and this map does not raises in
#: CI, so the screen never shows a bare id.
PROVIDERS: dict[str, ProviderWords] = {
    "sec": ProviderWords("SEC", "the ticker file, company facts, filings, 13F, Form 4, 13D/G, fails to deliver",
                         "4.5 a second (ownership) · 0.25 s apart (fundamentals)",
                         "10 a second per User-Agent (fair access)"),
    "uw": ProviderWords("Unusual Whales", "options, SPX, float, financials", "a budget a run · 500 rows a page"),
    "alpaca": ProviderWords("Alpaca", "bars, corporate actions", "190 a minute",
                            "200 a minute (free plan) · no SIP data of the last 15 minutes"),
    "yfinance": ProviderWords("yfinance", "float, estimates, MOVE", "float lookups 1.5 s apart · a 429 stops the run",
                              "unofficial · a 429 is its limit"),
    "alphavantage": ProviderWords("Alpha Vantage", "the earnings calendar", "one request a run", "25 a day (free tier)"),
    "openfigi": ProviderWords("OpenFIGI", "the FIGI of each CUSIP", "24 a minute · up to 100 CUSIPs a request",
                              "keyed limit, not quoted in the code"),
    "fred": ProviderWords("FRED", "DGS10 and the macro series", "110 a minute", "120 a minute"),
    "finra": ProviderWords("FINRA", "short interest", "5,000 rows a page"),
    "nasdaqtrader": ProviderWords("Nasdaq Trader", "Reg SHO threshold list", "60 a minute"),
    "nyse": ProviderWords("NYSE", "Reg SHO threshold list", "60 a minute"),
    "ibkr": ProviderWords("IBKR", "shortable file, IV year", "IV: 6 a minute", "60 historical requests in 10 minutes"),
    "ishares": ProviderWords("iShares", "index fund holdings", "one request a file and run"),
    "ssga": ProviderWords("SSGA", "index fund holdings", "one request a file and run"),
    "vanguard": ProviderWords("Vanguard", "index fund holdings", "one request a file and run"),
    "nasdaq": ProviderWords("Nasdaq", "the Nasdaq-100 list", "one request a file and run"),
    "invesco": ProviderWords("Invesco", "index fund holdings", "one request a file and run"),
    "wrds": ProviderWords("WRDS", "Compustat and CRSP", "run by hand: a login and Duo each time"),
    "treasury": ProviderWords("Treasury", "yields and auctions"),
    "cboe": ProviderWords("Cboe", "VIX and the index series"),
    "cftc": ProviderWords("CFTC", "futures positioning"),
    "fed": ProviderWords("Fed", "policy rates"),
    "bls": ProviderWords("BLS", "CPI and jobs"),
}

#: The first day each Backfill calendar offers: the provider's own first day, where the code has no constant of its
#: own for it. Alpaca's daily bars start in 2016 (its market-data docs); the SPX expiry-strike set starts at
#: ``uw_spot_expiry_strike.FIRST_SESSION`` and is read from there.
CALENDAR_FIRST: dict[str, str] = {"alpaca/bars": "2016-01-04"}
