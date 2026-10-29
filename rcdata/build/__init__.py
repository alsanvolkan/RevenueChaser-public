"""The builder: the one process that writes the market store (D1). Run it with ``python -m RevenueChaser.rcdata.build``.

``DATASETS`` maps each dataset name to its module's ``DATASET``; ``base.run_request`` runs one queued request. A kind's
datasets are built in this order (``base.build_kind``): the spine's sources stage their Parquet before
``spine/security`` builds the served tables from them.

The ownership store's datasets (PR 16) are their own block, added after the spine's.
"""
from __future__ import annotations

from . import market_series, uw_spot_expiry_strike
from .ownership import finra, form4, ftd, ibkr, regsho, sched13dg, sec13f, serve as ownership_serve
from .spine import funds, openfigi_map, sec_tickers, security, wrds

DATASETS = {d.name: d for d in (uw_spot_expiry_strike.DATASET, market_series.DATASET, sec_tickers.DATASET,
                                wrds.SEC_IDHIST, wrds.CO_HGIC, wrds.IDXCST_HIS, openfigi_map.DATASET, funds.DATASET,
                                security.DATASET)}
# The ownership store (PR 16): its sources, then ``ownership/serve``, which derives from their tables (last).
DATASETS.update({d.name: d for d in (finra.DATASET, regsho.DATASET, ibkr.DATASET, ftd.DATASET, sec13f.DATASET,
                                     form4.DATASET, sched13dg.DATASET, ownership_serve.DATASET)})

# The options data (G8, serving kind ``options``), in a block of its own.
from .options import ibkr_iv as _options_ibkr_iv, uw_list as _options_uw_list  # noqa: E402
from .options import uw_screener as _options_uw_screener  # noqa: E402

DATASETS.update({d.name: d for d in (_options_uw_list.DATASET, _options_uw_screener.DATASET,
                                     _options_ibkr_iv.DATASET)})
