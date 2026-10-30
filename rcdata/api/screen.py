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
    """One dataset as the screen shows it. ``coverage_target`` of None means the dataset has no share to hit."""

    label: str
    cadence: str = "daily"
    coverage_target: float | None = None


#: The screen's card per dataset. A dataset the store knows and this map does not raises in CI.
DATASETS: dict[str, DatasetCard] = {
    'alpaca/bars': DatasetCard('Daily bars (Alpaca)', coverage_target=0.99),
    'alpaca/corporate_actions': DatasetCard('Corporate actions (Alpaca)'),
    'alphavantage/earnings_calendar': DatasetCard('Earnings calendar (Alpha Vantage)'),
    'finra/short_interest': DatasetCard('Short interest (FINRA)'),
    'fundamentals/tables': DatasetCard('Fundamentals (served tables, peers, build issues)'),
    'funds/index_holdings': DatasetCard('Index membership (ETF holdings files)'),
    'ibkr/iv_history': DatasetCard('One year of 30-day implied volatility per list name (IBKR)', coverage_target=None),
    'ibkr/shortable': DatasetCard('Borrow fee and shares available (IBKR file)'),
    'market_series': DatasetCard('Market series', coverage_target=None),
    'openfigi/map': DatasetCard('OpenFIGI map of the CUSIPs'),
    'ownership/serve': DatasetCard('13F totals per ticker and quarter'),
    'prices/serving': DatasetCard('Prices store (bars, actions, factors, shares, market cap)'),
    'regsho/threshold': DatasetCard('Reg SHO threshold lists'),
    'sec/13dg': DatasetCard('5% owners (Schedules 13D and 13G)'),
    'sec/13f': DatasetCard('13F holdings (SEC data sets, then forms)'),
    'sec/company_tickers_exchange': DatasetCard('SEC ticker and exchange file'),
    'sec/companyfacts': DatasetCard('SEC company facts (re-read on the filing day)'),
    'sec/cover_shares': DatasetCard('Shares per class (SEC filing covers)'),
    'sec/filings': DatasetCard('SEC filing lists, 8-K items and report texts'),
    'sec/form4': DatasetCard('Insider transactions (Form 4)'),
    'sec/fsds': DatasetCard('SEC Financial Statement Data Sets (the check)'),
    'sec/ftd': DatasetCard('Fails to deliver (SEC)'),
    'spine/security': DatasetCard('Identity spine (security, tiers, history)', coverage_target=0.98),
    'uw/financials': DatasetCard('Preliminary quarters (Unusual Whales, off by default)'),
    'uw/option_list': DatasetCard('Options of SPX and the option list (Unusual Whales)', coverage_target=0.95),
    'uw/option_screener': DatasetCard('Earnings options: calendar, screener and earnings set (Unusual Whales)', coverage_target=0.95),
    'uw/spot_expiry_strike': DatasetCard('SPX expiry-strike (Unusual Whales)', coverage_target=0.95),
    'uw/total_float': DatasetCard('Float check (Unusual Whales)'),
    'wrds/co_hgic': DatasetCard('Compustat GICS history (WRDS)'),
    'wrds/firm_ratio': DatasetCard('WRDS financial ratios (the debt check)'),
    'wrds/funda': DatasetCard('Compustat annual fundamentals (WRDS, the revision check)'),
    'wrds/fundq': DatasetCard('Compustat quarterly fundamentals (WRDS, the revision check)'),
    'wrds/idxcst_his': DatasetCard('Compustat index constituents (WRDS, the membership check)'),
    'wrds/prices': DatasetCard('Price history and actions (WRDS: Compustat, CRSP)'),
    'wrds/sec_idhist': DatasetCard('Compustat securities and identifier history (WRDS)'),
    'wrds/segments': DatasetCard('Compustat segments and customers (WRDS)'),
    'yfinance/estimates': DatasetCard('Analyst estimates (yfinance)'),
    'yfinance/float': DatasetCard('Float (Yahoo)'),
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
