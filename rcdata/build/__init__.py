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

# The options data (G8, serving kind ``options``), in a block of its own.
from .options import uw_list as _options_uw_list, uw_screener as _options_uw_screener  # noqa: E402

DATASETS.update({d.name: d for d in (_options_uw_list.DATASET, _options_uw_screener.DATASET)})
