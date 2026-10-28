"""Read a stored raw day back into the ``raw`` dict ``normalize_snapshot`` takes.

``snapshot.fetch_snapshot`` writes one folder per day (files plus a manifest); this is its
inverse, so a board can be rebuilt from disk with no network (Refresh, #177 / G15, G10).
"""
from __future__ import annotations

import json
from pathlib import Path

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
    "flow_per_strike": "flow_per_strike",
}
ZERO_DTE_STEM = "zero_dte_strike_expiry"
ALL_EXPIRY_PREFIX = "strike_expiry_"


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_raw_day(day_dir) -> dict:
    """The raw dict of one stored day. Raises FileNotFoundError when the day has no manifest
    (a partial or missing fetch is not a day)."""
    d = Path(day_dir)
    manifest_path = d / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"no complete raw day at {d} (manifest.json missing)")
    manifest = _read(manifest_path)
    effective = manifest.get("effective_date") or manifest.get("asof_date") or d.name

    raw: dict = {}
    for f in sorted(d.glob("*.json")):
        key = FILE_KEYS.get(f.stem)
        if key:
            raw[key] = _read(f)
    raw["strike_expiry"] = {}
    zero = d / f"{ZERO_DTE_STEM}.json"
    if zero.exists():
        raw["strike_expiry"][manifest.get("zero_dte_expiry") or effective] = _read(zero)
    raw["strike_expiry_all"] = {}
    for f in sorted(d.glob(f"{ALL_EXPIRY_PREFIX}*.json")):
        raw["strike_expiry_all"][f.stem[len(ALL_EXPIRY_PREFIX):]] = _read(f)
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
