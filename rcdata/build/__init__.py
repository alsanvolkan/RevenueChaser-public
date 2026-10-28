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

# ── Fundamentals (build plan PR 12): the sources stage their Parquet, ``fundamentals/tables`` builds the served
# file last (``base.build_kind`` builds a kind's datasets in this order).
from .fundamentals import companyfacts as fundamentals_companyfacts  # noqa: E402
from .fundamentals import filings as fundamentals_filings  # noqa: E402
from .fundamentals import tables as fundamentals_tables  # noqa: E402

DATASETS.update({d.name: d for d in (fundamentals_companyfacts.DATASET, fundamentals_filings.DATASET,
                                     fundamentals_tables.DATASET)})
