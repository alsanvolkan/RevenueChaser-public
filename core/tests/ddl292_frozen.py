"""The 22 new raw/edgar MANIFEST entries of the G1 bump (#292), as proposed in ddl_292.md.

One source for the markdown blocks and for the validation run (validate_ddl292.py).
"""

STAMP = ["source TEXT", "fetched_at DOUBLE", "run_id TEXT", "run_at DOUBLE"]
STAMP_NO_SOURCE = ["fetched_at DOUBLE", "run_id TEXT", "run_at DOUBLE"]  # tables whose key holds `source`
CHANGE = ["valid_from DATE", "status TEXT"]

ENTRIES = {
    # ── D01 dividends and splits (#156) ──
    "price_actions": {
        "key": ["ticker", "ex_date", "action"],
        "columns": ["ticker TEXT", "ex_date DATE", "action TEXT", "value DOUBLE", "session_date DATE",
                    *STAMP, "PRIMARY KEY (ticker, ex_date, action)"],
        # "db": "prices" (#156, avo 2026-10-09): the file moved before the fill; the DDL above is unchanged
        "kind": "tabular", "write": "append", "history_mode": "event", "db": "prices", "sources": [],
    },
    # ── D03 SPX daily level (#293) ──
    "index_levels_daily": {
        "key": ["symbol", "session_date"],
        "columns": ["symbol TEXT", "session_date DATE", "open DOUBLE", "high DOUBLE", "low DOUBLE",
                    "close DOUBLE", "volume DOUBLE", *STAMP, "PRIMARY KEY (symbol, session_date)"],
        # "db": "prices" (#293, avo 2026-10-10): beside D01 before its writer; the DDL above is unchanged
        "kind": "tabular", "write": "append", "history_mode": "event", "db": "prices", "sources": [],
    },
    # ── D04 quotes and market cap (#294) ──
    "quote_snapshot": {
        "key": ["ticker", "session_date", "source"],
        "columns": ["ticker TEXT", "session_date DATE", "source TEXT", "market_cap DOUBLE", "price DOUBLE",
                    "shares_outstanding DOUBLE", "week_52_high DOUBLE", "week_52_low DOUBLE", "payload BLOB",
                    *STAMP_NO_SOURCE, "PRIMARY KEY (ticker, session_date, source)"],
        "compress": {"payload": "json"},
        "kind": "tabular", "write": "append", "history_mode": "session_snapshot", "sources": [],
    },
    # ── D10 short interest (#296) ──
    "short_interest": {
        "key": ["ticker", "session_date"],
        "columns": ["ticker TEXT", "session_date DATE", "market_date DATE", "short_interest DOUBLE",
                    "si_float DOUBLE", "total_float DOUBLE", "days_to_cover DOUBLE", "fee_rate DOUBLE",
                    "rebate_rate DOUBLE", "short_shares_available DOUBLE", "payload JSON",
                    *STAMP, "PRIMARY KEY (ticker, session_date)"],
        "kind": "tabular", "write": "append", "history_mode": "session_snapshot", "sources": [],
    },
    # ── D12 vendor statements (#297) ──
    "vendor_financials": {
        "key": ["ticker", "source", "statement", "report_type", "period_end", "run_at"],
        "columns": ["ticker TEXT", "source TEXT", "statement TEXT", "report_type TEXT", "period_end DATE",
                    *CHANGE, "revenue DOUBLE", "gross_profit DOUBLE", "operating_income DOUBLE", "ebitda DOUBLE",
                    "net_income DOUBLE", "operating_cash_flow DOUBLE", "total_assets DOUBLE", "inventory DOUBLE",
                    "receivables DOUBLE", "deferred_revenue DOUBLE", "rpo DOUBLE", "total_debt DOUBLE",
                    "interest_expense DOUBLE", "payload JSON", *STAMP_NO_SOURCE,
                    "PRIMARY KEY (ticker, source, statement, report_type, period_end, run_at)"],
        "kind": "tabular", "write": "append", "history_mode": "change_history", "sources": [],
    },
    # ── D12 vendor valuation (#297) ──
    "vendor_valuation": {
        "key": ["ticker", "source", "session_date"],
        "columns": ["ticker TEXT", "source TEXT", "session_date DATE", "enterprise_value DOUBLE",
                    "ebitda DOUBLE", "total_revenue DOUBLE", "net_income_to_common DOUBLE", "trailing_pe DOUBLE",
                    "beta DOUBLE", "currency TEXT", "long_business_summary TEXT", "payload BLOB",
                    *STAMP_NO_SOURCE, "PRIMARY KEY (ticker, source, session_date)"],
        "compress": {"payload": "json"},
        "kind": "tabular", "write": "append", "history_mode": "session_snapshot", "sources": [],
    },
    # ── D13 earnings history per print (#298) ──
    "earnings_history": {
        "key": ["ticker", "endpoint", "report_date", "run_at"],
        "columns": ["ticker TEXT", "endpoint TEXT", "report_date DATE", *CHANGE, "report_time TEXT",
                    "actual_eps DOUBLE", "eps_estimate DOUBLE", "surprise_pct DOUBLE", "expected_move_perc DOUBLE",
                    "post_earnings_move_1d DOUBLE", "fiscal_period_end DATE", "report_type TEXT", "payload JSON",
                    *STAMP, "PRIMARY KEY (ticker, endpoint, report_date, run_at)"],
        "kind": "tabular", "write": "append", "history_mode": "change_history", "sources": [],
    },
    # ── D14 estimate snapshots (#199) ──
    "estimate_snapshots": {
        "key": ["ticker", "session_date", "period"],
        "columns": ["ticker TEXT", "session_date DATE", "period TEXT",
                    "eps_est_analysts DOUBLE", "eps_est_avg DOUBLE", "eps_est_low DOUBLE", "eps_est_high DOUBLE",
                    "eps_est_year_ago DOUBLE", "eps_est_growth DOUBLE",
                    "rev_est_analysts DOUBLE", "rev_est_avg DOUBLE", "rev_est_low DOUBLE", "rev_est_high DOUBLE",
                    "rev_est_year_ago DOUBLE", "rev_est_growth DOUBLE",
                    "eps_trend_current DOUBLE", "eps_trend_7d DOUBLE", "eps_trend_30d DOUBLE",
                    "eps_trend_60d DOUBLE", "eps_trend_90d DOUBLE",
                    "eps_rev_up_7d DOUBLE", "eps_rev_up_30d DOUBLE", "eps_rev_down_7d DOUBLE",
                    "eps_rev_down_30d DOUBLE", "payload JSON",
                    *STAMP, "PRIMARY KEY (ticker, session_date, period)"],
        "kind": "tabular", "write": "append", "history_mode": "session_snapshot", "sources": [],
    },
    # ── D19 CUSIP -> ticker/FIGI map (#207) ──
    "identity_cusip": {
        "key": ["cusip", "run_at"],
        "columns": ["cusip TEXT", *CHANGE, "ticker TEXT", "figi TEXT", "share_class_figi TEXT", "share_class TEXT",
                    "name TEXT", "exch_code TEXT", "security_type TEXT", *STAMP, "PRIMARY KEY (cusip, run_at)"],
        "kind": "tabular", "write": "append", "history_mode": "change_history", "sources": [],
    },
    # ── D20 ownership snapshot (#300) ──
    "ownership_snapshot": {
        "key": ["ticker", "session_date", "source"],
        "columns": ["ticker TEXT", "session_date DATE", "source TEXT", "shares_outstanding DOUBLE",
                    "insider_pct DOUBLE", "institutional_pct DOUBLE", *STAMP_NO_SOURCE,
                    "PRIMARY KEY (ticker, session_date, source)"],
        "kind": "tabular", "write": "append", "history_mode": "session_snapshot", "sources": [],
    },
    # ── D8 SEC fund file (#301) ──
    "sec_mf_file": {
        "key": ["class_id", "run_at"],
        "columns": ["class_id TEXT", *CHANGE, "cik TEXT", "series_id TEXT", "symbol TEXT", *STAMP,
                    "PRIMARY KEY (class_id, run_at)"],
        "kind": "tabular", "write": "append", "history_mode": "change_history", "sources": [],
    },
    # ── D21 SEC exchange file (#301) ──
    "sec_exchange_file": {
        "key": ["ticker", "run_at"],
        "columns": ["ticker TEXT", *CHANGE, "cik TEXT", "name TEXT", "exchange TEXT", *STAMP,
                    "PRIMARY KEY (ticker, run_at)"],
        "kind": "tabular", "write": "append", "history_mode": "change_history", "sources": [],
    },
    # ── D26 earnings calendar (#304) ──
    "earnings_calendar": {
        "key": ["session_date", "earnings_date", "ticker", "report_session"],
        "columns": ["session_date DATE", "earnings_date DATE", "ticker TEXT", "report_session TEXT",
                    "sector TEXT", "full_name TEXT", "marketcap DOUBLE", "expected_move_perc DOUBLE",
                    "street_mean_est DOUBLE", "pre_earnings_close DOUBLE", "post_earnings_close DOUBLE",
                    "report_time TEXT", "item JSON", *STAMP,
                    "PRIMARY KEY (session_date, earnings_date, ticker, report_session)"],
        "kind": "tabular", "write": "append", "history_mode": "session_snapshot", "sources": [],
    },
    # ── D27 macro series (#305) ──
    "macro_series": {
        "key": ["series_id", "obs_date", "source"],
        "columns": ["series_id TEXT", "obs_date DATE", "source TEXT", "value DOUBLE", "frequency TEXT",
                    "proxy_for TEXT", "session_date DATE", *STAMP_NO_SOURCE,
                    "PRIMARY KEY (series_id, obs_date, source)"],
        "kind": "tabular", "write": "append", "history_mode": "event", "sources": [],
    },
    # ── D28 volatility indexes (#305) ──
    "vol_index_daily": {
        "key": ["symbol", "session_date", "source"],
        "columns": ["symbol TEXT", "session_date DATE", "source TEXT", "open DOUBLE", "high DOUBLE", "low DOUBLE",
                    "close DOUBLE", "operator TEXT", *STAMP_NO_SOURCE,
                    "PRIMARY KEY (symbol, session_date, source)"],
        "kind": "tabular", "write": "append", "history_mode": "event", "sources": [],
    },
    # ── D29 CFTC COT (#305) ──
    "cftc_cot": {
        "key": ["report_type", "market", "report_date"],
        "columns": ["report_type TEXT", "market TEXT", "report_date DATE", "release_date DATE",
                    "open_interest DOUBLE",
                    "asset_mgr_long DOUBLE", "asset_mgr_short DOUBLE", "asset_mgr_change_long DOUBLE",
                    "lev_money_long DOUBLE", "lev_money_short DOUBLE", "lev_money_change_long DOUBLE",
                    "dealer_long DOUBLE", "dealer_short DOUBLE", "dealer_change_long DOUBLE",
                    "raw_row JSON", "session_date DATE", *STAMP,
                    "PRIMARY KEY (report_type, market, report_date)"],
        "kind": "tabular", "write": "append", "history_mode": "event", "sources": [],
    },
    # ── D37 economic calendar (#307) ──
    "economic_calendar": {
        "key": ["session_date", "event_ts", "event", "type"],
        "columns": ["session_date DATE", "event_ts DOUBLE", "event TEXT", "type TEXT", "forecast TEXT",
                    "prev TEXT", "reported_period TEXT", "raw_row JSON", *STAMP,
                    "PRIMARY KEY (session_date, event_ts, event, type)"],
        "kind": "tabular", "write": "append", "history_mode": "session_snapshot", "sources": [],
    },
    # ── rule A per-key "last checked" stamps ──
    "source_checks": {
        "key": ["dataset", "source", "key"],
        "columns": ["dataset TEXT", "source TEXT", "key TEXT", "checked_at DOUBLE", "run_id TEXT",
                    "PRIMARY KEY (dataset, source, key)"],
        "kind": "tabular", "write": "upsert", "history_mode": "operational", "sources": [],
    },
    # ── 13F filer names (#191, #208) ──
    "sec13f_filers": {
        "key": ["accession"],
        "columns": ["accession TEXT PRIMARY KEY", "cik TEXT", "period TEXT", "name TEXT", *STAMP],
        "kind": "tabular", "write": "append", "history_mode": "event", "sources": [],
    },
    # ── D11 SEC companyfacts (#161), edgar.duckdb ──
    "edgar_companyfacts": {
        "key": ["cik", "taxonomy", "concept", "unit", "period_start", "period_end", "accn"],
        "columns": ["cik TEXT", "taxonomy TEXT", "concept TEXT", "unit TEXT", "period_start TEXT",
                    "period_end DATE", "accn TEXT", "value DOUBLE", "form TEXT", "fy INTEGER", "fp TEXT",
                    "filed DATE", *STAMP,
                    "PRIMARY KEY (cik, taxonomy, concept, unit, period_start, period_end, accn)"],
        "kind": "tabular", "write": "append", "history_mode": "event", "db": "edgar", "sources": [],
    },
    # ── D15 SEC filings (edgar.duckdb) ──
    "sec_submissions": {
        "key": ["cik", "accession"],
        "columns": ["cik TEXT", "accession TEXT", "form TEXT", "filing_date DATE", "primary_document TEXT",
                    "items TEXT", *STAMP, "PRIMARY KEY (cik, accession)"],
        "kind": "tabular", "write": "append", "history_mode": "event", "db": "edgar", "sources": [],
    },
    # ── D15 SEC company header (edgar.duckdb) ──
    "sec_submissions_cik": {
        "key": ["cik", "run_at"],
        "columns": ["cik TEXT", *CHANGE, "name TEXT", "sic TEXT", "sic_description TEXT", "entity_type TEXT",
                    "tickers JSON", "exchanges JSON", *STAMP, "PRIMARY KEY (cik, run_at)"],
        "kind": "tabular", "write": "append", "history_mode": "change_history", "db": "edgar", "sources": [],
    },
}
