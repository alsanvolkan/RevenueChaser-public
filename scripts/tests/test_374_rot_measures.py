"""#374 Rotation-on-D01 measurement script (market plan §6, measurements 7, 10, 11): synthetic stores only.

The script copies the snapshots it is given and opens only the copies, read-only and offline; a live store path is
refused. The stores here are tiny DuckDB files built from the MANIFEST column lists.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import date
from pathlib import Path

import duckdb
import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_sector_rotation import engine
from RevenueChaser.module_sector_rotation.tests.d01_seed import sessions

RESEARCH = Path(__file__).resolve().parents[1] / "research"
SESSION = date(2026, 10, 7)


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("rot374_measures", RESEARCH / "374_rot_d01_measures.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _store(path: Path, tables: dict[str, list[dict]]) -> Path:
    con = duckdb.connect(str(path))
    for name, rows in tables.items():
        con.execute(f"CREATE TABLE {name} ({', '.join(rawstore.MANIFEST[name]['columns'])})")
        for r in rows:
            keys = list(r)
            cols = ", ".join('"group"' if k == "group" else k for k in keys)
            con.execute(f"INSERT INTO {name} ({cols}) VALUES ({', '.join('?' * len(keys))})", [r[k] for k in keys])
    con.close()
    return path


def _bars(ticker: str, start: date, step: float, volume: float = 1e6) -> list[dict]:
    return [{"ticker": ticker, "session_date": d, "open": 100 + i * step, "high": 100 + i * step,
             "low": 100 + i * step, "close": 100 + i * step, "volume": volume + (i % 7) * 1e4, "source": "uw",
             "fetched_at": 0.0, "run_id": "s", "run_at": 0.0}
            for i, d in enumerate(sessions(start, SESSION))]


@pytest.fixture
def snapshots(tmp_path):
    tickers = engine.rrg_tickers()[:4]
    bars = _bars("SPY", date(2025, 3, 3), 0.10)
    for k, t in enumerate(tickers):
        bars += _bars(t, date(2025, 3, 3) if k else date(2026, 5, 4), 0.05 * (k + 1))
    stored = [{"as_of_date": "2026-10-07", "ticker": t, "name": t, "group": "sector", "close_price": 1.0,
               "rs_1w": 0.0, "rs_1m": 0.0, "rs_3m": 0.0, "rs_ratio": 0.0, "rs_momentum": 0.0, "rs_ratio_z": 0.0,
               "rs_momentum_z": 0.0, "quadrant": "LEADING", "benchmark": "SPY", "generated_at": "x", "source": "live"}
              for t in tickers]
    stored += [dict(r, as_of_date="2026-10-01", source="backfill") for r in stored]
    raw = _store(tmp_path / "raw_20261008T000000Z.duckdb", {"rotation_snapshots": stored})
    prices = _store(tmp_path / "prices_20261008T000000Z.duckdb", {"price_bars_daily": bars})
    return tickers, raw, prices


def test_measurement_7_recomputes_the_newest_live_snapshot_from_d01(mod, snapshots, tmp_path):
    tickers, raw, prices = snapshots
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (raw, prices)}
    out = tmp_path / "out"
    result = mod.main(["--raw", str(raw), "--prices", str(prices), "--out", str(out)])
    m7 = result["m7"]
    assert m7["stored"] == {"as_of_date": "2026-10-07", "rows": 4}
    assert m7["session"] == "2026-10-07" and m7["stored_date_is_a_session"] is True
    assert m7["depth"]["SPY"]["sessions"] == len(sessions(date(2025, 3, 3), SESSION))
    assert m7["depth"][tickers[0]]["sessions"] < 240 and tickers[0] in m7["under_floor"]
    assert m7["too_short"] == [tickers[0]]                     # ~22 weeks: left off the map, a warning
    assert m7["only_67"]["compared"] == 3 and m7["only_67"]["only_before"] == [tickers[0]]
    assert m7["only_67"]["quadrant_changes"] == len(m7["only_67"]["changed"])
    assert m7["only_67"]["median_abs_d_ratio_z"] is not None
    assert {(x["as_of_date"], x["source"]) for x in result["m10"]} == {("2026-10-01", "backfill"),
                                                                       ("2026-10-07", "live")}
    assert json.loads((out / "374_measures.json").read_text())["m7"]["session"] == "2026-10-07"
    assert (out / "374_measures.txt").read_text().startswith("# 374")
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (raw, prices)} == before   # never written


def test_a_live_store_path_is_refused(mod, tmp_path):
    live = tmp_path / ".store"
    live.mkdir()
    (live / "raw_20261008T000000Z.duckdb").write_bytes(b"")
    (live / "prices_20261008T000000Z.duckdb").write_bytes(b"")
    with pytest.raises(mod.cm.Refused):
        mod.main(["--backups", str(live), "--out", str(tmp_path / "out")])


def test_measurement_11_compares_money_flow_and_names_the_holdings_that_turn_n_a(mod, tmp_path):
    from RevenueChaser.module_sector_rotation import money_flow
    ranked = money_flow.ranked_tickers()
    bars = _bars("SPY", date(2026, 3, 2), 0.10)
    for k, t in enumerate(ranked[1:]):                         # ranked[0] has no D01 series
        bars += _bars(t, date(2026, 6, 1), (k - len(ranked) / 2) / 10.0)
    bars += _bars("AAA", date(2026, 3, 2), 0.2)
    raw = _store(tmp_path / "raw_20261008T000000Z.duckdb", {
        "rotation_snapshots": [],
        "etf_holdings": [{"ticker": "AIQ", "entry": json.dumps([{"ticker": "AAA", "name": "A", "weight": 9},
                                                                 {"ticker": "ZZZ.L", "name": "Z", "weight": 5}]),
                          "provider": "uw", "fetched_at": 0.0}]})
    prices = _store(tmp_path / "prices_20261008T000000Z.duckdb", {"price_bars_daily": bars})
    before_in = [{"run_id": "r1", "side": "inflow", "rank": 1, "etf": ranked[-1], "name": "", "flow_score": 50.0,
                  "as_of": SESSION}, {"run_id": "r1", "side": "outflow", "rank": 1, "etf": ranked[1], "name": "",
                                      "flow_score": -40.0, "as_of": SESSION}]
    hist = _store(tmp_path / "history_20261008T000000Z.duckdb", {
        "result_runs": [{"board": "module_money_flow", "run_id": "r1", "session_date": SESSION, "run_at": 1.0,
                         "quality": "complete", "session_snapshot": True,
                         "context": json.dumps({"as_of": SESSION.isoformat()})},
                        {"board": "module_sector_rotation", "run_id": "r2", "session_date": SESSION, "run_at": 2.0,
                         "quality": "complete", "session_snapshot": True}],
        "money_flow_history": before_in,
        "rotation_holdings_history": [{"run_id": "r2", "etf": "AIQ", "holding": "AAA", "position": 1,
                                       "rs_1m": 1.0}]})
    paths = (raw, prices, hist)
    sums = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    result = mod.main(["--raw", str(raw), "--prices", str(prices), "--history", str(hist),
                       "--out", str(tmp_path / "out")])
    m11 = result["m11"]
    assert m11["session"] == SESSION.isoformat() and m11["money_flow"]["missing"] == [ranked[0]]
    c = m11["money_flow"]["compare"]
    assert c["inflows"]["before"] == [ranked[-1]] and c["outflows"]["before"] == [ranked[1]] and c["compared"] == 2
    assert len(c["inflows"]["after"]) == len(c["outflows"]["after"]) == 10
    assert c["median_abs_d_flow_score"] is not None and c["side_flips"] == []
    h = m11["holdings"]
    assert h["theme_etfs"] == 1 and h["turn_na"] == [{"etf": "AIQ", "holding": "ZZZ.L"}]
    assert h["not_in_the_last_board"] == [{"etf": "AIQ", "holding": "ZZZ.L"}] and h["rs_compared"] == 1
    assert "Measurement 11" in (tmp_path / "out" / "374_measures.txt").read_text()
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths} == sums       # never written


def test_the_reads_apply_the_splits_daily_bars_applies(mod, tmp_path):
    """Review 2026-10-16: a split inside the window (e.g. the 2025-12-05 sector SPDR splits) reads as the engine reads
    it through ``core.prices.daily_bars``, not as a jump in plain SQL closes."""
    ex = date(2026, 8, 3)
    plain = _bars("XLK", date(2026, 3, 2), 0.1)
    served = [dict(b, close=b["close"] * 2, volume=b["volume"] / 2) if b["session_date"] < ex else b for b in plain]
    split = [{"ticker": "XLK", "ex_date": ex, "action": "split", "value": 2.0, "session_date": ex,
              "source": "yfinance", "fetched_at": 0.0, "run_id": "s", "run_at": 0.0}]
    a = duckdb.connect(str(_store(tmp_path / "a.duckdb", {"price_bars_daily": plain})), read_only=True)
    b = duckdb.connect(str(_store(tmp_path / "b.duckdb", {"price_bars_daily": served, "price_actions": split})),
                       read_only=True)
    start = date(2026, 3, 2)
    assert mod.d01_points(b, ["XLK"], start, SESSION) == mod.d01_points(a, ["XLK"], start, SESSION)
    assert mod.money_flow_after(b, ["XLK"], SESSION)["scores"] == mod.money_flow_after(a, ["XLK"], SESSION)["scores"]
