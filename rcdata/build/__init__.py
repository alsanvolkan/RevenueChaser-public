"""The builder: the one process that writes the market store (D1). Run it with ``python -m RevenueChaser.rcdata.build``.

``DATASETS`` maps each dataset name to its module's ``DATASET``; ``base.run_request`` runs one queued request. A kind's
datasets are built in this order (``base.build_kind``): the spine's sources stage their Parquet before
``spine/security`` builds the served tables from them.
"""
from __future__ import annotations

from . import market_series, uw_spot_expiry_strike
from .spine import funds, openfigi_map, sec_tickers, security, wrds

DATASETS = {d.name: d for d in (uw_spot_expiry_strike.DATASET, market_series.DATASET, sec_tickers.DATASET,
                                wrds.SEC_IDHIST, wrds.CO_HGIC, wrds.IDXCST_HIS, openfigi_map.DATASET, funds.DATASET,
                                security.DATASET)}

# PR 7 (G2): the prices store's sources (the WRDS history last), then ``prices/serving``, which builds the served
# tables from their staging.
from .prices import actions as prices_actions, bars as prices_bars, cover_shares as prices_cover  # noqa: E402
from .prices import floats as prices_floats, serving as prices_serving, wrds_history as prices_wrds  # noqa: E402

DATASETS.update({d.name: d for d in (prices_bars.DATASET, prices_actions.DATASET, prices_cover.DATASET,
                                     prices_floats.YAHOO_FLOAT, prices_floats.UW_FLOAT, prices_wrds.DATASET,
                                     prices_serving.DATASET)})

# PR 10 (G4): the forecast ledger (``ledger.duckdb``); the freeze first, so a kind build reads its new rows, then the
# outcomes and the baselines derived from it.
from .ledger import derived as ledger_derived, freeze as ledger_freeze  # noqa: E402

DATASETS.update({d.name: d for d in (ledger_freeze.DATASET, ledger_derived.OUTCOMES_DATASET,
                                     ledger_derived.BASELINES_DATASET)})
