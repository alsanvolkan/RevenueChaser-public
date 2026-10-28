"""The pin and squeeze on the old and the new basis, side by side, for stored days (PR 3, homesrv step S3).

Read-only and with no network: each day's raw files are read from ``<data root>/raw/<day>/`` (the board's own UW
snapshot) and the market-store inputs through ``store_reads`` (``rcdata.read``: DuckDB ``read_only=True``, one
connection per read). Nothing is written into the board's data root. ``rcdata.read`` counts each read in its read log
(``ops.sqlite``), as every reader does.

For each day, one line: the magnet strike, its 0-1 DTE dollar gamma (the new basis, review S4) and its all-expiry
dollar gamma (the old basis, ``/spot-exposures/strike``), the pin flag and the squeeze trigger on each basis, and the
VIX, COT and SPX close the board now shows beside what the raw day itself held (an older Fetch wrote
``vix_family.json``, ``cftc_tff.json`` and ``ohlc_8wk.json``; a day fetched after PR 3 has none, so ``old=n/a``).

Usage: python -m RevenueChaser.module_spx_gamma.tools.compare_pin --days 2026-10-26 2026-10-23 [--data-root PATH]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from RevenueChaser.module_spx_gamma.config import DATA_ROOT, net_sum
from RevenueChaser.module_spx_gamma.engines.prediction_engine import scenario_extraction
from RevenueChaser.module_spx_gamma.normalize import normalize_snapshot
from RevenueChaser.module_spx_gamma.raw_day import load_raw_day
from RevenueChaser.module_spx_gamma.run import with_store_inputs


def _billions(value) -> str:
    return f"${value / 1e9:.2f}B" if value is not None else "n/a"


def _level(value) -> str:
    return f"{value:,.0f}" if value is not None else "-"


def _trigger(scen: dict) -> str:
    strike = scen.get("upside_trigger")
    if strike is None:
        return "-"
    dollars = scen.get("upside_trigger_net_gex") if scen.get("_dollars_known") else None
    return f"{_level(strike)} ({_billions(dollars)})"


def _scenarios(snapshot: dict, dollars: dict | None) -> dict:
    scen = scenario_extraction({**snapshot, "zero_one_dte_dollars": dollars})
    strike = scen.get("upside_trigger")
    scen["_dollars_known"] = bool(dollars) and strike is not None and dollars.get(strike) is not None
    return scen


def _file(day_dir: Path, name: str):
    try:
        return json.loads((day_dir / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _old_inputs(day_dir: Path) -> dict[str, str]:
    """What the raw day itself held for the three inputs that moved to the store (an older Fetch's files)."""
    vix = ((_file(day_dir, "vix_family.json") or {}).get("tickers") or {}).get("^VIX")
    cot = (_file(day_dir, "cftc_tff.json") or {}).get("report_date")
    window = (_file(day_dir, "ohlc_8wk.json") or {}).get("data") or []
    last = window[-1] if window else {}
    return {"vix": f"{vix:.2f}" if vix is not None else "n/a",
            "cot": str(cot).split("T", 1)[0] if cot else "n/a",
            "spx": f"{last['close']:,.2f}@{last.get('date')}" if last.get("close") is not None else "n/a"}


def compare_day(day: str, data_root: Path) -> str:
    day_dir = data_root / "raw" / day
    try:
        raw = load_raw_day(day_dir)
    except FileNotFoundError:
        return f"{day} no complete raw day (manifest.json missing)"
    effective = raw["_meta"]["effective_date"]
    full = with_store_inputs(raw, effective)
    snapshot = normalize_snapshot(full, effective)
    zero_one = full["zero_one_dte"]
    all_expiry = {r["strike"]: net_sum(r.get("call_gamma_oi"), r.get("put_gamma_oi"))
                  for r in snapshot.get("spot_exposures") or [] if r.get("strike") is not None}
    new = _scenarios(snapshot, zero_one.get("dollars") if zero_one.get("stored") else None)
    old = _scenarios(snapshot, all_expiry)
    magnet = new.get("pin_magnet")

    inputs = full["store_inputs"]
    vix = (full.get("vix") or {}).get("tickers", {}).get("^VIX")
    cot = (full.get("cftc") or {}).get("report_date")
    closes = (full.get("ohlc") or {}).get("data") or []
    was = _old_inputs(day_dir)
    vix_now = f"{vix:.2f}@{inputs['vix'].get('obs_date')}" if vix is not None else "not stored"
    spx_now = f"{closes[-1]['close']:,.2f}@{closes[-1]['date']}" if closes else "not stored"
    parts = [
        f"{effective} magnet={_level(magnet)}",
        f"0-1DTE={_billions(new.get('pin_dollars'))}",
        f"all-expiry={_billions(old.get('pin_dollars'))}",
        f"pin new={new.get('pin_flag') or '-'} old={old.get('pin_flag') or '-'}",
        f"squeeze new={_trigger(new)} old={_trigger(old)}",
        f"0-1DTE source: {zero_one.get('label')}",
        f"vix new={vix_now} old={was['vix']}",
        f"cot new={cot or 'not stored'} old={was['cot']}",
        f"spx new={spx_now} old={was['spx']}",
    ]
    return " | ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--days", nargs="+", required=True, help="stored days, YYYY-MM-DD")
    parser.add_argument("--data-root", default=str(DATA_ROOT), help="the board's data root (default: DATA_ROOT)")
    args = parser.parse_args(argv)
    root = Path(args.data_root)
    for day in args.days:
        print(compare_day(day, root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
