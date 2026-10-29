"""One record of every dataset: tables, store file, source, writer, freshness, coverage (#124).

Claim rule, applied to ``rawstore.MANIFEST``:

- A ``*_changes`` table (MANIFEST ``changes_of``) is listed on the dataset that owns its base table.
- A history.duckdb table (``result_runs`` and every result-history ``*_history`` mirror) is listed on
  ``result_history``. Those 18 tables take freshness ``from_run_log`` until #82 and #83 make the run
  log trustworthy. ``earnings_history`` is not one of them: it is a raw change-history table.
- Every other MANIFEST table is listed on exactly one dataset.
- ``UNCLAIMED_ALLOWLIST`` is empty. A new MANIFEST table fails the claim test until a dataset owns it.

``db`` is the store file ``module_settings/inventory.py`` ``_db_of`` would pick (MANIFEST ``db``,
default ``raw``). ``writer`` is an ``automations.REGISTRY`` name, or None with ``writer_note``.
A freshness pair is a ``(table, column)`` ``_max_date`` can read. ``source`` is a provider named in
``core/providers`` or ``docs/data_architecture.md``; ``unknown`` means a single enum value would be a guess.

Does not read the store. Seven datasets have an existing summary: their ``coverage`` is a callable
returning that module's ``Health``, imported and called when a reader asks for the row (S4). It takes an
optional ``now``, passed on to a module whose ``health`` takes a clock (#150). S5 switches the inventory
reader to it.

``gap_kind`` and ``next_step`` (G11) are the written half of coverage: the measurement says how many
names are missing, these two say what would close the hole. ``gap_kind`` is one of ``GapKind``'s six
values, and a kind that closes only through work (``fixable``, ``in_build``, ``needs_decision``) or
through a fact about the source (``ceiling``, which carries its REASON) requires a ``next_step``.
"""
from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from importlib import import_module

from RevenueChaser.core.datahealth import Health

DBS = ("raw", "edgar", "prices", "history")

FROM_RUN_LOG = "from_run_log"
NO_CLAIM = "no_claim"
# Derived rows say this until the run log is trustworthy. S5b part 3.
FROM_RUN_LOG_CAVEAT = (
    "from the run log; not trustworthy until alsanvolkan/RevenueChaser-public#82 and alsanvolkan/RevenueChaser-public#83 are fixed (run_registry / result_runs)"
)

# history.duckdb: the header plus every ``*_history`` mirror. 18 tables. S5b part 3.
_HISTORY = (
    "result_runs",
    "ag_scores_history",
    "swing_scores_history",
    "swing_gates_history",
    "fundamentals_scores_history",
    "fundamentals_gates_history",
    "relatedness_scores_history",
    "value_chain_edges_history",
    "oipd_chain_history",
    "earnings_cards_history",
    "leaps_footprint_history",
    "oipd_fit_history",
    "rotation_holdings_history",
    "rotation_snapshots_history",
    "sector_tree_history",
    "money_flow_history",
    "sec13f_metrics_history",
    "financials_report_history",
)

# MANIFEST ``retired: True`` (test_manifest_planned_289.RETIRED). The plan text said 3; the flag is on 4.
_RETIRED = (
    "leaps_daily_scores",
    "sec13f_edgar_backfill",
    "sec13f_ticker_names",
    "sec_company_tickers",
)

# Store bookkeeping, not a dataset a board ages. No freshness claim (S5b part 4).
_BOOKKEEPING = (
    "store_meta",
    "migration_manifest",
    "run_registry",
    "job_runs",
    "store_schema",
)


class Source(str, Enum):
    """Provider of a dataset's rows. ``unknown`` is a refusal to pick one value, not a provider."""

    uw = "uw"
    yfinance = "yfinance"
    sec = "sec"
    finra_cftc = "finra/cftc"
    cboe = "cboe"
    fred = "fred"
    openfigi = "openfigi"
    derived = "derived"
    manual = "manual"
    store = "store"
    unknown = "unknown"


@dataclass(frozen=True)
class CoverageSpec:
    """Two SQL queries: who should have a row, and who does. S4 may fill these; S3 does not invent them."""

    population: str
    presence: str

    def __post_init__(self) -> None:
        if not (self.population and self.presence):
            raise ValueError("CoverageSpec needs a population query and a presence query")


class GapKind(str, Enum):
    """What stands between a dataset and its full population (#124 G11).

    ``fixable`` we can close now, ``in_build`` something already being built closes, ``needs_decision``
    waits on a choice, ``ceiling`` cannot be closed on the route we use (its ``next_step`` says why, as
    a fact about the source), ``self_heals`` fills in over time, ``none`` means there is no hole.
    """

    fixable = "fixable"
    in_build = "in_build"
    needs_decision = "needs_decision"
    ceiling = "ceiling"
    self_heals = "self_heals"
    none = "none"


# The kinds whose gap only closes through written words. ``ceiling`` is here because it must carry
# its REASON (why the source cannot give us the rest), not a schedule.
_GAP_KINDS_WITH_STEP = (
    GapKind.fixable, GapKind.in_build, GapKind.needs_decision, GapKind.ceiling,
)

Freshness = tuple[str, str] | tuple[tuple[str, str], ...] | str | None


@dataclass(frozen=True)
class DatasetSpec:
    """One dataset. ``coverage`` is None, a ``CoverageSpec`` of two SQL queries, or a callable returning ``Health``."""

    key: str
    label: str
    tables: tuple[str, ...]
    db: str
    source: Source
    writer: str | None
    freshness: Freshness
    writer_note: str | None = None
    freshness_note: str | None = None
    coverage: CoverageSpec | Callable[..., Health] | None = None
    coverage_note: str | None = None
    gap_kind: GapKind | None = None
    next_step: str | None = None

    def __post_init__(self) -> None:
        if not self.key or not self.label:
            raise ValueError("a dataset needs a key and a label")
        if not self.tables or len(set(self.tables)) != len(self.tables):
            raise ValueError(f"{self.key}: tables must be a non-empty tuple of unique names")
        if self.db not in DBS:
            raise ValueError(f"{self.key}: db {self.db!r} is not one of {DBS}")
        if not isinstance(self.source, Source):
            raise ValueError(f"{self.key}: source must be a Source, not {self.source!r}")
        if self.writer is None:
            if not (self.writer_note and self.writer_note.strip()):
                raise ValueError(f"{self.key}: writer None needs a non-empty writer_note")
        elif not str(self.writer).strip():
            raise ValueError(f"{self.key}: writer is blank")
        if self.freshness == FROM_RUN_LOG:
            note = self.freshness_note or FROM_RUN_LOG_CAVEAT
            if "#82" not in note or "#83" not in note:
                raise ValueError(f"{self.key}: from_run_log must carry the #82/#83 caveat")
            object.__setattr__(self, "freshness_note", note)
        pairs = self.freshness_pairs()
        for table, column in pairs:
            if table not in self.tables or not column:
                raise ValueError(f"{self.key}: freshness {table}.{column} is not one of its tables")
        if self.coverage is not None and not (
            isinstance(self.coverage, CoverageSpec) or callable(self.coverage)
        ):
            raise ValueError(f"{self.key}: coverage must be None, a CoverageSpec, or a callable")
        if self.gap_kind is not None and not isinstance(self.gap_kind, GapKind):
            raise ValueError(
                f"{self.key}: gap_kind must be a GapKind or None, not {self.gap_kind!r}; "
                f"one of {[kind.value for kind in GapKind]}")
        if self.next_step is not None and not (
            isinstance(self.next_step, str) and self.next_step.strip()
        ):
            raise ValueError(f"{self.key}: next_step must be None or non-empty text")
        if self.gap_kind in _GAP_KINDS_WITH_STEP and not (self.next_step or "").strip():
            needs = ("its reason, a fact about the source" if self.gap_kind is GapKind.ceiling
                     else "a next_step that says what would close it")
            raise ValueError(f"{self.key}: gap_kind {self.gap_kind.value!r} needs {needs} in next_step")

    def freshness_pairs(self) -> tuple[tuple[str, str], ...]:
        """``(table, column)`` pairs ``_max_date`` can read. Empty for None, from_run_log, no_claim."""
        f = self.freshness
        if f is None or f == FROM_RUN_LOG or f == NO_CLAIM:
            return ()
        if (
            isinstance(f, tuple) and len(f) == 2
            and isinstance(f[0], str) and isinstance(f[1], str)
        ):
            return (f,)
        if (
            isinstance(f, tuple) and f
            and all(isinstance(p, tuple) and len(p) == 2 and isinstance(p[0], str) for p in f)
        ):
            return f
        raise ValueError(
            f"{self.key}: freshness {f!r} is not a (table, column) pair, {FROM_RUN_LOG}, or {NO_CLAIM}"
        )


def _summary_health(dotted: str) -> Callable[[], Health]:
    """The ``Health`` of the module that owns this dataset's summary, read when the row is read (S4).

    A callable, not a call: the registry is imported by pages that never open this dataset, and these modules
    read the store (and, for four of them, the calendar). One argument-free call per row is all a reader needs;
    a reader with a clock passes ``now``, which reaches the module's ``health`` when that takes one, so the
    coverage and the row's own summary are one question at one time (#150).
    """

    def coverage(now=None) -> Health:
        module = import_module(f"RevenueChaser.{dotted}")
        health = getattr(module, "health")
        if "now" in inspect.signature(health).parameters:
            return health(now=now)
        return health()

    coverage.__name__ = f"{dotted}.health"
    coverage.__doc__ = f"The dataset's Health from ``{dotted}.health()``."
    return coverage


def _runlog(key: str, label: str, tables: tuple[str, ...], db: str, source: Source,
            writer: str | None, writer_note: str | None = None) -> DatasetSpec:
    """A derived dataset whose freshness is the producing run, not a row timestamp."""
    return DatasetSpec(
        key=key, label=label, tables=tables, db=db, source=source,
        writer=writer, writer_note=writer_note, freshness=FROM_RUN_LOG,
        freshness_note=FROM_RUN_LOG_CAVEAT,
    )


_LEDGER_MOVED = "moved to the builder's ledger.duckdb in PR 10 (rcdata ledger/*); no writer of this table any more"


# Seeded from module_settings/inventory.py ``_collect`` (the 19 rows that builder returns), then
# extended so every base MANIFEST table has one owner. ``*_changes`` companions are on the base's dataset.
REGISTRY: tuple[DatasetSpec, ...] = (
    # Live table has no fetch stamp (inventory last_date is a dash). The pull time is universe_provenance.
    # source unknown: membership is the SEC exchange file, caps are yfinance. One value would drop one.
    DatasetSpec(
        key="universe", label="Equity universe",
        tables=("equity_universe", "equity_universe_changes"),
        db="raw", source=Source.unknown, writer="RC-Screeners-Daily", freshness=None,
        writer_note="caps-only fill is the dashboard job POST /run/universe-stats, not this task",
    ),
    DatasetSpec(
        key="universe_provenance", label="Universe provenance",
        tables=("equity_universe_provenance",),
        db="raw", source=Source.store, writer="RC-Screeners-Daily",
        freshness=("equity_universe_provenance", "generated_at"),
    ),
    # source unknown: the spine merges sec, uw, openfigi, fdic and spdr answers (identity_source_cache).
    DatasetSpec(
        key="identity", label="Identity spine",
        tables=("security_master", "identity_conflicts", "identity_source_cache", "identity_runs",
                "security_master_changes", "identity_source_cache_changes"),
        db="raw", source=Source.unknown, writer=None,
        writer_note="dashboard job POST /run/identity (app/dashboard.py); not an automations.REGISTRY entry",
        freshness=("identity_runs", "finished_at"),
    ),
    DatasetSpec(
        key="corpus", label="10-K / 20-F corpus",
        tables=("chain_filings", "edgar_documents"),
        db="edgar", source=Source.sec, writer="RC-ValueChain-Sync",
        freshness=(("chain_filings", "filed"), ("edgar_documents", "filed")),
        writer_note="RC-Relatedness-OffPeak also writes chain_filings and edgar_documents",
    ),
    DatasetSpec(
        key="sec_submissions", label="SEC submissions",
        tables=("sec_submissions", "sec_submissions_cik"),
        db="edgar", source=Source.sec, writer="RC-ValueChain-Sync",
        freshness=("sec_submissions", "filing_date"),
        coverage=_summary_health("core.sec_submissions"),
        coverage_note="expected is None: the universe CIK count is the inventory's query, not the summary's",
        gap_kind=GapKind.fixable,
        next_step=("The summary counts the CIKs it has answered for but not the universe's CIKs, so "
                   "there is no population. The inventory already counts the universe's CIKs; joining "
                   "the two would give one."),
    ),
    DatasetSpec(
        key="sec_files", label="SEC exchange & fund files",
        tables=("sec_exchange_file", "sec_mf_file"),
        db="raw", source=Source.sec, writer="RC-Daily-Bars",
        freshness=(("sec_exchange_file", "run_at"), ("sec_mf_file", "run_at")),
        coverage=_summary_health("core.sec_files"),
        coverage_note="both files summed; expected is None until a good pull records file_rows",
        gap_kind=GapKind.self_heals,
        next_step=("The population comes from the last good pull's own row count, and each good pull "
                   "tops the stored rows up to it; a store with no receipt yet shows no population until "
                   "one is written."),
    ),
    DatasetSpec(
        key="companyfacts", label="Companyfacts",
        tables=("edgar_financials",),
        db="edgar", source=Source.sec, writer="RC-Screeners-Daily",
        freshness=("edgar_financials", "fetched_at"),
    ),
    DatasetSpec(
        key="ohlc", label="Legacy daily open/high/low/close cache (SPY fallback)",
        tables=("swing_ohlc",),
        db="raw", source=Source.yfinance, writer="RC-Screeners-Daily",
        freshness=("swing_ohlc", "ts"),
    ),
    # Lead source uw (RC-Daily-Bars: UW ohlc/1d, yfinance fallback). price_checks is the watermark, same file.
    DatasetSpec(
        key="price_bars", label="Daily price bars",
        tables=("price_bars_daily", "price_actions", "price_checks"),
        db="prices", source=Source.uw, writer="RC-Daily-Bars",
        freshness=("price_bars_daily", "session_date"),
        coverage=_summary_health("core.prices"),
        gap_kind=GapKind.needs_decision,
        next_step=("Most of the names with no bar are warrants, preferred shares and SPAC units, which "
                   "the boards' universe drops by design (alsanvolkan/RevenueChaser#87), so this "
                   "population counts names no board scores. Deciding whether coverage should be "
                   "measured on the boards' universe would close it."),
    ),
    DatasetSpec(
        key="index_levels", label="SPX daily levels",
        tables=("index_levels_daily",),
        db="prices", source=Source.yfinance, writer="RC-Daily-Bars",
        freshness=("index_levels_daily", "session_date"),
        coverage=_summary_health("core.index_levels"),
        gap_kind=GapKind.none,
    ),
    # source unknown: yfinance via the screeners Fetch and uw via the Earnings Fetch are both writers.
    DatasetSpec(
        key="vendor", label="Vendor statements & valuation",
        tables=("vendor_financials", "vendor_valuation"),
        db="raw", source=Source.unknown, writer="RC-Screeners-Daily",
        freshness=(("vendor_financials", "fetched_at"), ("vendor_valuation", "session_date")),
        writer_note="Earnings Fetch also appends Unusual Whales statements (docs/data_architecture.md Stage 2)",
        coverage=_summary_health("core.vendor_data"),
        coverage_note="expected is None: the universe is not read by the summary",
        gap_kind=GapKind.fixable,
        next_step=("The summary counts the tickers with a valuation row but not the universe, so there is "
                   "no population. Reading the universe count, as the quotes summary already does, would "
                   "give one."),
    ),
    # Yahoo leads (inventory._quotes_row). UW rows are stored too.
    DatasetSpec(
        key="quotes", label="Quotes and market cap",
        tables=("quote_snapshot",),
        db="raw", source=Source.yfinance, writer="RC-Screeners-Daily",
        freshness=("quote_snapshot", "session_date"),
        coverage=_summary_health("core.quotes"),
        gap_kind=GapKind.needs_decision,
        next_step=("Most of the names with no quote are warrants, preferred shares and SPAC units, which "
                   "the boards' universe drops by design (alsanvolkan/RevenueChaser#87), and a "
                   "rate-limited night misses more (#100). Deciding whether coverage should be "
                   "measured on the boards' universe would close most of it."),
    ),
    # Lead source uw. sp600 is copied from the last accepted identity snapshot, not a UW call.
    DatasetSpec(
        key="index", label="Index constituents",
        tables=("index_constituents", "index_constituents_changes"),
        db="raw", source=Source.uw, writer="RC-Index-Lists",
        freshness=("index_constituents_changes", "run_at"),
    ),
    # Ingest family the architecture groups with the two tables the inventory row already lists.
    # last_date already reads sec13f_submissions.period, so that table belongs here.
    DatasetSpec(
        key="sec13f", label="13F",
        tables=("sec13f_totals", "sec13f_holdings", "sec13f_submissions",
                "sec13f_cusip_names", "sec13f_ingested_zips"),
        db="raw", source=Source.sec, writer=None,
        writer_note="dashboard job POST /run/module_sec_13f; rebuild is sec13f_rebuild. Not a REGISTRY entry",
        freshness=("sec13f_submissions", "period"),
    ),
    DatasetSpec(
        key="oipd", label="Implied Probability chain",
        tables=("oipd_chain", "oipd_meta"),
        db="raw", source=Source.uw, writer=None,
        writer_note="dashboard job POST /run/module_oipd; not an automations.REGISTRY entry",
        freshness=("oipd_meta", "as_of"),
    ),
    # UW, then yfinance (module_sector_rotation/etf_holdings.py get_etf_holdings).
    DatasetSpec(
        key="etf_holdings", label="Rotation ETF holdings",
        tables=("etf_holdings", "etf_holdings_changes"),
        db="raw", source=Source.uw, writer=None,
        writer_note="dashboard job POST /run/holdings; not an automations.REGISTRY entry",
        freshness=("etf_holdings", "fetched_at"),
        coverage=_summary_health("module_sector_rotation.etf_holdings"),
        gap_kind=GapKind.ceiling,
        next_step=("The gold, silver and oil funds GLD, SLV and USO hold metal or futures rather than "
                   "company shares, so the holdings providers return no list for them."),
    ),
    DatasetSpec(
        key="relatedness", label="Relatedness profiles",
        tables=("relatedness_profiles", "relatedness_scores", "relatedness_attributes"),
        db="raw", source=Source.derived, writer="RC-Relatedness-OffPeak",
        freshness=("relatedness_profiles", "fetched_at"),
    ),
    # The 18 history-file tables. session_date exists on result_runs; it is not the claim until #82/#83.
    DatasetSpec(
        key="result_history", label="Result history",
        tables=_HISTORY,
        db="history", source=Source.store, writer=None,
        writer_note=("core.history.record_run, called from board Fetches; no single REGISTRY entry "
                     "owns every history table"),
        freshness=FROM_RUN_LOG, freshness_note=FROM_RUN_LOG_CAVEAT,
    ),
    # The two watch lists moved to ops.sqlite in PR 4 (rcdata.ops, not a store file here): the raw watchlist tables
    # stay claimed while the manifest has them, unwritten, until their retirement (PR 21).
    DatasetSpec(
        key="inputs", label="Your inputs (watchlist, board settings)",
        tables=("watchlist", "board_settings", "watchlist_changes", "board_settings_changes"),
        db="raw", source=Source.manual, writer=None,
        writer_note=("the user enters it (board settings save); nothing fetches it. The watch lists are in "
                     "ops.sqlite since PR 4; watchlist and watchlist_changes here are unwritten"),
        freshness=("board_settings_changes", "run_at"),
    ),

    # --- extended: base tables the 19 rows do not own ---
    DatasetSpec(
        key="value_chain", label="Value Chain edges",
        tables=("chain_mentions", "chain_meta"),
        db="edgar", source=Source.sec, writer="RC-ValueChain-Sync",
        freshness=("chain_mentions", "filed"),
    ),
    DatasetSpec(
        key="earnings_calendar", label="Earnings calendar",
        tables=("earnings_calendar",),
        db="raw", source=Source.uw, writer="RC-Earnings-Inputs",
        freshness=("earnings_calendar", "session_date"),
    ),
    # UW, then yfinance (docs/data_architecture.md short_interest).
    DatasetSpec(
        key="short_interest", label="Short interest",
        tables=("short_interest",),
        db="raw", source=Source.uw, writer="RC-Earnings-Inputs",
        freshness=("short_interest", "session_date"),
    ),
    DatasetSpec(
        key="economic_calendar", label="Economic calendar",
        tables=("economic_calendar",),
        db="raw", source=Source.uw, writer="RC-Earnings-Inputs",
        freshness=("economic_calendar", "session_date"),
    ),
    DatasetSpec(
        key="macro_series", label="FRED macro series",
        tables=("macro_series",),
        db="raw", source=Source.fred, writer="RC-Earnings-Inputs",
        freshness=("macro_series", "obs_date"),
    ),
    DatasetSpec(
        key="estimates", label="Analyst estimates",
        tables=("estimate_snapshots",),
        db="raw", source=Source.yfinance, writer="RC-Earnings-Inputs",
        freshness=("estimate_snapshots", "session_date"),
    ),
    # Cboe for the VIX family; MOVE is yfinance (docs/data_architecture.md vol_index_daily).
    DatasetSpec(
        key="vol_index", label="Volatility indices",
        tables=("vol_index_daily",),
        db="raw", source=Source.cboe, writer="RC-Earnings-Inputs",
        freshness=("vol_index_daily", "session_date"),
    ),
    # Reader ages by session_date (stored_inputs.cot_row, MAX_LAG). report_date is the report's own date.
    DatasetSpec(
        key="cftc", label="CFTC E-mini Commitments of Traders",
        tables=("cftc_cot",),
        db="raw", source=Source.finra_cftc, writer="RC-Earnings-Inputs",
        freshness=("cftc_cot", "session_date"),
        gap_kind=GapKind.needs_decision,
        next_step=("The Commitments of Traders table is fresh but shallow: the nightly writer asks for the "
                   "newest few weeks and the first write per report wins, so it only grows forward. #119 "
                   "asks how deep to backfill."),
    ),
    DatasetSpec(
        key="earnings_history", label="Earnings history",
        tables=("earnings_history",),
        db="raw", source=Source.uw, writer=None,
        writer_note="Earnings Fetch (module_earnings_radar/earnings_store.record); not a REGISTRY entry",
        freshness=("earnings_history", "valid_from"),
    ),
    # source unknown: yfinance on the screeners path and uw_info on the 13F Fetch are both writers.
    DatasetSpec(
        key="ownership", label="Ownership snapshot",
        tables=("ownership_snapshot",),
        db="raw", source=Source.unknown, writer="RC-Screeners-Daily",
        writer_note="13F Fetch also appends source uw_info (docs/data_architecture.md Stage 2)",
        freshness=("ownership_snapshot", "session_date"),
    ),
    DatasetSpec(
        key="ticker_payload", label="yfinance payload cache",
        tables=("ticker_payload_cache",),
        db="raw", source=Source.yfinance, writer="RC-Screeners-Daily",
        freshness=("ticker_payload_cache", "fetched_at"),
    ),
    DatasetSpec(
        key="swing_info", label="Swing info cache",
        tables=("swing_info",),
        db="raw", source=Source.yfinance, writer="RC-Screeners-Daily",
        freshness=("swing_info", "cached_at"),
    ),
    _runlog(
        "ag_scores", "Asymmetric Growth scores", ("ticker_scores",),
        "raw", Source.derived, "RC-Screeners-Daily",
    ),
    _runlog(
        "swing_scores", "Swing scores", ("swing_scores", "swing_gates"),
        "raw", Source.derived, "RC-Screeners-Daily",
    ),
    _runlog(
        "fundamentals_scores", "Fundamentals scores", ("fundamentals_scores", "fundamentals_gates"),
        "raw", Source.derived, "RC-Screeners-Daily",
    ),
    # PR 10 (G4): the three forecast tables moved to the builder's ledger kind (``ledger.duckdb``: ``ledger/freeze``,
    # ``ledger/outcomes``, ``ledger/baselines``, shown with the other builder datasets). These raw.duckdb tables have no
    # writer any more and stay empty until the retirement group drops them from the manifest.
    DatasetSpec(
        key="forecast_ledger", label="Forecast ledger (old raw.duckdb table)",
        tables=("forecast_ledger",),
        db="raw", source=Source.derived, writer=None, writer_note=_LEDGER_MOVED,
        freshness=("forecast_ledger", "forecast_date"),
    ),
    _runlog(
        "forecast_outcomes", "Forecast outcomes (old raw.duckdb table)", ("forecast_outcomes",),
        "raw", Source.derived, None, _LEDGER_MOVED,
    ),
    _runlog(
        "forecast_baselines", "Forecast baselines (old raw.duckdb table)", ("forecast_baselines",),
        "raw", Source.derived, None, _LEDGER_MOVED,
    ),
    DatasetSpec(
        key="comovement", label="Co-movement runs",
        tables=("comov_runs", "comov_nodes", "comov_members", "comov_lag"),
        db="raw", source=Source.derived, writer=None,
        writer_note="dashboard job POST /run/module_comovement; not an automations.REGISTRY entry",
        freshness=("comov_runs", "as_of"),
    ),
    DatasetSpec(
        key="rotation", label="Sector rotation",
        tables=("rotation_snapshots", "rotation_holdings"),
        db="raw", source=Source.derived, writer=None,
        writer_note="dashboard job Sector Rotation Fetch (module_sector_rotation/store.py); not a REGISTRY entry",
        freshness=(("rotation_snapshots", "as_of_date"), ("rotation_holdings", "date")),
    ),
    DatasetSpec(
        key="themes", label="Fitted themes",
        tables=("relatedness_themes", "relatedness_theme_tags", "relatedness_theme_members",
                "relatedness_theme_etfs", "theme_runs", "ticker_themes"),
        db="raw", source=Source.derived, writer=None,
        writer_note="module_relatedness/themes.py; not listed on RC-Relatedness-OffPeak",
        freshness=("theme_runs", "session_date"),
    ),
    DatasetSpec(
        key="leaps", label="LEAPS flow",
        tables=("leaps_flow_prints", "leaps_cache"),
        db="raw", source=Source.uw, writer=None,
        writer_note="dashboard job POST /run/module_leaps; not an automations.REGISTRY entry",
        freshness=("leaps_flow_prints", "session_date"),
    ),
    DatasetSpec(
        key="identity_cusip", label="OpenFIGI CUSIP map",
        tables=("identity_cusip",),
        db="raw", source=Source.openfigi, writer=None,
        writer_note="13F map step core/identity_cusip.py, dashboard job sec13f_map; not a REGISTRY entry",
        freshness=("identity_cusip", "valid_from"),
        gap_kind=GapKind.ceiling,
        next_step=("OpenFIGI maps a CUSIP to a name, so a name with no CUSIP cannot get one on this route; "
                   "the spine's CUSIP column has the same ceiling."),
    ),
    _runlog(
        "cusip_map", "CUSIP to ticker map", ("cusip_ticker",),
        "raw", Source.derived, None,
        "rebuilt by the 13F derive (engine.recompute_totals); not a REGISTRY entry",
    ),
    DatasetSpec(
        key="sec_sic", label="SEC industry code cache",
        tables=("sec_company_sic",),
        db="raw", source=Source.sec, writer=None,
        writer_note="CLI core.edgar_bulk and core/sic_registry.py; not an automations.REGISTRY entry",
        freshness=("sec_company_sic", "fetched_at"),
    ),
    DatasetSpec(
        key="edgar_facts", label="EDGAR fact spine",
        tables=("edgar_facts",),
        db="edgar", source=Source.sec, writer=None,
        writer_note="CLI core.edgar_bulk companyfacts.zip ingest; not an automations.REGISTRY entry",
        freshness=("edgar_facts", "period"),
    ),
    DatasetSpec(
        key="edgar_companyfacts", label="EDGAR companyfacts (planned)",
        tables=("edgar_companyfacts",),
        db="edgar", source=Source.sec, writer=None,
        writer_note="planned (alsanvolkan/RevenueChaser#289): MANIFEST only, no writer yet",
        freshness=("edgar_companyfacts", "fetched_at"),
    ),
    # source unknown: biotech catalysts; the writer is not in the tree and no enum value names the provider.
    DatasetSpec(
        key="catalysts", label="Catalyst events (planned)",
        tables=("catalyst_events", "catalyst_events_changes"),
        db="raw", source=Source.unknown, writer=None,
        writer_note=("planned (alsanvolkan/RevenueChaser#289, alsanvolkan/RevenueChaser#186 to "
                     "alsanvolkan/RevenueChaser#192): writer not in the tree"),
        freshness=("catalyst_events", "fetched_at"),
    ),
    # source unknown: FDA OOPD is not in the provider enum.
    DatasetSpec(
        key="orphan", label="FDA orphan designations (planned)",
        tables=("orphan_designations", "orphan_lookups", "orphan_designations_changes"),
        db="raw", source=Source.unknown, writer=None,
        writer_note=("planned (alsanvolkan/RevenueChaser#289, alsanvolkan/RevenueChaser#190): OOPD writer not in "
                     "the tree"),
        freshness=(("orphan_designations", "fetched_at"), ("orphan_lookups", "session_date")),
    ),
    DatasetSpec(
        key="sec13f_filers", label="13F filers (planned)",
        tables=("sec13f_filers",),
        db="raw", source=Source.sec, writer=None,
        writer_note="planned (alsanvolkan/RevenueChaser#289): MANIFEST only, no writer yet",
        freshness=("sec13f_filers", "fetched_at"),
    ),
    DatasetSpec(
        key="sec13f_security_meta", label="13F security names",
        tables=("sec13f_security_meta",),
        db="raw", source=Source.manual, writer=None,
        writer_note="manual shared ticker meta (docs/data_architecture.md Stage 2); no REGISTRY writer",
        freshness=None,
    ),
    # Not MANIFEST retired. Writer is gone and the table has no date, so there is nothing to claim.
    DatasetSpec(
        key="sec13f_enrichment", label="13F enrichment (writer retired)",
        tables=("sec13f_enrichment",),
        db="raw", source=Source.unknown, writer=None,
        writer_note=("writer retired (alsanvolkan/RevenueChaser#300); not flagged MANIFEST retired; no reader since "
                     "alsanvolkan/RevenueChaser#375"),
        freshness=None,
    ),
    # source unknown: DeepSeek and Grok are not in the provider enum.
    DatasetSpec(
        key="llm_cache", label="Language-model prompt cache",
        tables=("llm_cache",),
        db="raw", source=Source.unknown, writer="RC-Relatedness-OffPeak",
        freshness=("llm_cache", "fetched_at"),
    ),
    DatasetSpec(
        key="source_checks", label="Source check watermarks",
        tables=("source_checks",),
        db="raw", source=Source.store, writer=None,
        writer_note=("stamped by the fetches that record a check (core/quotes.py, core/sec_submissions.py); "
                     "not one REGISTRY entry"),
        freshness=("source_checks", "checked_at"),
    ),
    DatasetSpec(
        key="bookkeeping", label="Store bookkeeping",
        tables=_BOOKKEEPING,
        db="raw", source=Source.store, writer=None,
        writer_note=("store_meta, migration_manifest, store_schema (one stamp per store file, alsanvolkan/RevenueChaser-public#26 PL7), "
                     "run_registry and job_runs are the store's own pins and job log; no freshness claim"),
        freshness=NO_CLAIM,
    ),
    DatasetSpec(
        key="retired", label="Retired tables",
        tables=_RETIRED,
        db="raw", source=Source.unknown, writer=None,
        writer_note="MANIFEST retired=True: only a past writer or the legacy import; no freshness claim",
        freshness=NO_CLAIM,
    ),
)

UNCLAIMED_ALLOWLIST: frozenset[str] = frozenset()


def _index() -> tuple[dict[str, DatasetSpec], dict[str, tuple[str, ...]]]:
    by_key: dict[str, DatasetSpec] = {}
    owners: dict[str, list[str]] = {}
    for spec in REGISTRY:
        if spec.key in by_key:
            raise ValueError(f"dataset key {spec.key!r} is listed twice")
        by_key[spec.key] = spec
        for table in spec.tables:
            owners.setdefault(table, []).append(spec.key)
    return by_key, {table: tuple(keys) for table, keys in owners.items()}


_BY_KEY, _OWNERS = _index()


def by_key(key: str) -> DatasetSpec:
    """The dataset with this key. Raises KeyError if there is none."""
    try:
        return _BY_KEY[key]
    except KeyError:
        raise KeyError(f"unknown dataset {key!r}") from None


def dataset_of_table(table: str) -> DatasetSpec:
    """The one dataset that claims ``table``. Raises KeyError if it is unclaimed or claimed twice."""
    owners = _OWNERS.get(table, ())
    if len(owners) != 1:
        raise KeyError(f"{table!r} is claimed by {owners or 'nobody'}")
    return _BY_KEY[owners[0]]


def tables_claimed() -> frozenset[str]:
    """Every table name listed on a dataset, including a name listed twice."""
    return frozenset(_OWNERS)


def unclaimed(manifest_keys: Iterable[str]) -> frozenset[str]:
    """Keys in ``manifest_keys`` that no dataset claims, minus ``UNCLAIMED_ALLOWLIST``.

    The allowlist is empty. A name here is a MANIFEST table the registry forgot.
    """
    return frozenset(manifest_keys) - tables_claimed() - UNCLAIMED_ALLOWLIST
