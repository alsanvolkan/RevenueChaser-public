"""The builder: the one process that writes the market store (D1). Run it with ``python -m RevenueChaser.rcdata.build``.

``DATASETS`` maps each dataset name to its module's ``DATASET``; ``base.run_request`` runs one queued request.
"""
from __future__ import annotations

from . import market_series, uw_spot_expiry_strike

DATASETS = {d.name: d for d in (uw_spot_expiry_strike.DATASET, market_series.DATASET)}
