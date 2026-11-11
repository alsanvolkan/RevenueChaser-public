"""Read a stored raw day back into the ``raw`` dict ``normalize_snapshot`` takes.

``snapshot.fetch_snapshot`` stores one day in the board file (its manifest and every answer, ``store.put_raw_day``);
this is its inverse, so a board can be rebuilt with no network (Refresh, #177 / G15, G10).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import store

# file stem -> key in the raw dict. The VIX family, the CFTC COT and the 8-week window are not files of the day
# any more (PR 3): run.build_day reads them from the market store, and an older day's copies of them are ignored.
FILE_KEYS = {
    "gex_levels": "gex_levels",
    "gex_levels_vol": "gex_levels_vol",
    "greek_exposure_expiry": "expiry_exposure",
    "greek_exposure_strike": "strike_exposure",
    "greek_exposure_strike_prev": "strike_exposure_prev",
    "spot_exposures_strike": "spot_exposure",
    "interpolated_iv": "interpolated_iv",
    "max_pain": "max_pain",
    "iv_rank": "iv_rank",
    "options_volume": "options_volume",
    "vol_term_structure": "vol_term_structure",
    "variance_risk_premium": "variance_risk_premium",
    "realized_vol": "realized_vol",
    "option_chain": "option_chain",
    "target_chain": "target_chain",
    "economic_calendar": "economic_calendar",
    "spy_ohlc_30m": "spy_ohlc_30m",
    "greek_flow": "greek_flow",
    "vix_greeks": "vix_greeks",
    "flow_per_strike": "flow_per_strike",
}
ZERO_DTE_STEM = "zero_dte_strike_expiry"
ALL_EXPIRY_PREFIX = "strike_expiry_"
SKEW_CHAIN_PREFIX = "skew_chain_"


def load_raw_day(day) -> dict:
    """The raw dict of one stored day (``day`` is the date, or an old ``.../raw/<date>`` path, whose name is used).
    Raises FileNotFoundError when the day is not stored (a partial or missing fetch is not a day)."""
    day = Path(str(day)).name
    manifest = store.raw_manifest(day)
    if manifest is None:
        raise FileNotFoundError(f"no stored raw day {day}")
    texts = store.raw_files(day)
    effective = manifest.get("effective_date") or manifest.get("asof_date") or day

    def _read(name: str):
        return json.loads(texts[name])

    raw: dict = {}
    for name in sorted(texts):
        key = FILE_KEYS.get(Path(name).stem)
        if key:
            raw[key] = _read(name)
    raw["strike_expiry"] = {}
    zero = f"{ZERO_DTE_STEM}.json"
    if zero in texts:
        raw["strike_expiry"][manifest.get("zero_dte_expiry") or effective] = _read(zero)
    raw["strike_expiry_all"] = {}
    for name in sorted(n for n in texts if n.startswith(ALL_EXPIRY_PREFIX)):
        raw["strike_expiry_all"][Path(name).stem[len(ALL_EXPIRY_PREFIX):]] = _read(name)
    # F3: the 25-delta skew chains near 30 and 90 days
    raw["skew_chains"] = {}
    for key in ("1m", "3m"):
        name = f"{SKEW_CHAIN_PREFIX}{key}.json"
        if name in texts:
            raw["skew_chains"][key] = _read(name)
    raw["_meta"] = {
        "requested_date": manifest.get("requested_date") or effective,
        "effective_date": effective,
        "api_snapshot_time": manifest.get("api_snapshot_time"),
    }
    # The Fetch's own spot, when it recorded one (#293): a day fetched during an open session used a live
    # level, and a Refresh must show that level, not the stored window's last close.
    if manifest.get("spot"):
        raw["spot"] = manifest["spot"]
    return raw
