"""The builder: the one process that writes the market store (D1). Run it with ``python -m RevenueChaser.rcdata.build``.

``DATASETS`` maps each dataset name to its module's ``DATASET``; ``base.run_request`` runs one queued request. A kind's
datasets are built in this order (``base.build_kind``): the spine's sources stage their Parquet before
``spine/security`` builds the served tables from them.

The ownership store's datasets (PR 16) are their own block, added after the spine's.
"""
from __future__ import annotations

from . import market_series, uw_spot_expiry_strike
from .ownership import finra, form4, ftd, ibkr, regsho, sched13dg, sec13f, serve as ownership_serve
from .spine import etf_holdings as spine_etf_holdings, funds, openfigi_map, sec_tickers, security, wrds

DATASETS = {d.name: d for d in (uw_spot_expiry_strike.DATASET, market_series.DATASET, sec_tickers.DATASET,
                                wrds.SEC_IDHIST, wrds.CO_HGIC, wrds.IDXCST_HIS, openfigi_map.DATASET, funds.DATASET,
                                spine_etf_holdings.DATASET, security.DATASET)}
# The ownership store (PR 16): its sources, then ``ownership/serve``, which derives from their tables (last).
DATASETS.update({d.name: d for d in (finra.DATASET, regsho.DATASET, ibkr.DATASET, ftd.DATASET, sec13f.DATASET,
                                     form4.DATASET, sched13dg.DATASET, ownership_serve.DATASET)})

# PR 7 (G2): the prices store's sources (the WRDS history last), then ``prices/serving``, which builds the served
# tables from their staging.
from .prices import actions as prices_actions, bars as prices_bars, cover_shares as prices_cover  # noqa: E402
from .prices import floats as prices_floats, serving as prices_serving, wrds_history as prices_wrds  # noqa: E402

DATASETS.update({d.name: d for d in (prices_bars.DATASET, prices_actions.DATASET, prices_cover.DATASET,
                                     prices_floats.YAHOO_FLOAT, prices_floats.UW_FLOAT, prices_wrds.DATASET,
                                     prices_serving.DATASET)})

# ── Fundamentals (build plan PR 12): the sources stage their Parquet, ``fundamentals/tables`` builds the served
# file last (``base.build_kind`` builds a kind's datasets in this order).
from .fundamentals import companyfacts as fundamentals_companyfacts  # noqa: E402
from .fundamentals import earnings as fundamentals_earnings  # noqa: E402
from .fundamentals import estimates as fundamentals_estimates  # noqa: E402
from .fundamentals import filings as fundamentals_filings  # noqa: E402
from .fundamentals import fsds as fundamentals_fsds  # noqa: E402
from .fundamentals import tables as fundamentals_tables  # noqa: E402
from .fundamentals import uw_financials as fundamentals_uw_financials  # noqa: E402
from .fundamentals import wrds_fund as fundamentals_wrds  # noqa: E402

DATASETS.update({d.name: d for d in (fundamentals_companyfacts.DATASET, fundamentals_filings.DATASET,
                                     fundamentals_earnings.DATASET, fundamentals_uw_financials.DATASET,
                                     fundamentals_estimates.DATASET, fundamentals_fsds.DATASET,
                                     *fundamentals_wrds.DATASETS,
                                     fundamentals_tables.DATASET)})

# The options data (G8, serving kind ``options``), in a block of its own.
from .options import ibkr_iv as _options_ibkr_iv, uw_list as _options_uw_list  # noqa: E402
from .options import uw_screener as _options_uw_screener  # noqa: E402

DATASETS.update({d.name: d for d in (_options_uw_list.DATASET, _options_uw_screener.DATASET,
                                     _options_ibkr_iv.DATASET)})
