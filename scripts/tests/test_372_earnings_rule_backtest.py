"""#372 B1 earnings rule backtest: synthetic snapshots, read-only, offline."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import zlib
from datetime import date, timedelta
from pathlib import Path

import pytest

import duckdb

RESEARCH = Path(__file__).resolve().parents[1] / "research"

pytestmark = pytest.mark.skipif(not RESEARCH.is_dir(),
                                reason="scripts/research/ is not in this tree")


def _load(name):
    spec = importlib.util.spec_from_file_location("r_" + name, RESEARCH / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _quarters(net_income, ocf):
    out, end = [], date(2024, 3, 31)
    for _ in range(9):
        filed = (end + timedelta(days=35)).isoformat()
        out.append({"period": end.isoformat(), "revenue": 100.0, "gross_profit": 50.0, "ebit": 10.0,
                    "net_income": net_income, "operating_cash_flow": ocf, "total_assets": 200.0,
                    "filed": {"revenue": filed, "net_income": filed}})
        end += timedelta(days=91)
    return out


def _session_days(start, n):
    d, out = start, []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _prices(quarters, jump):
    """Flat 100, then ``jump`` at the first session on/after each filing date."""
    days = _session_days(date(2024, 1, 1), 900)
    events = sorted(q["filed"]["revenue"] for q in quarters)
    price, rows = 100.0, []
    for d in days:
        while events and events[0] <= d.isoformat():
            price *= 1.0 + jump
            events.pop(0)
        rows.append((d, price))
    return rows


def _stores(tmp_path):
    edgar, prices = tmp_path / "edgar_20261014_040000_000000.duckdb", tmp_path / "prices_20261014_040000_000000.duckdb"
    con = duckdb.connect(str(edgar))
    con.execute("CREATE TABLE edgar_financials (ticker TEXT, quarterly BLOB)")
    up, down = _quarters(10.0, 12.0), _quarters(40.0, 0.0)           # accruals -1 % -> R7 +20; +20 % -> R7 -30
    for t, q in (("UP", up), ("DOWN", down)):
        con.execute("INSERT INTO edgar_financials VALUES (?, ?)", [t, zlib.compress(json.dumps(q).encode())])
    con.close()
    con = duckdb.connect(str(prices))
    con.execute("CREATE TABLE price_bars_daily (ticker TEXT, session_date DATE, close DOUBLE, adj_close DOUBLE)")
    for t, q, jump in (("UP", up, 0.05), ("DOWN", down, -0.05)):
        con.executemany("INSERT INTO price_bars_daily VALUES (?, ?, ?, ?)",
                        [(t, d, p, p) for d, p in _prices(q, jump)])
    con.close()
    return edgar, prices


def _digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_r7_hits_when_the_stock_moves_with_the_score(tmp_path):
    mod = _load("372_earnings_rule_backtest")
    edgar, prices = _stores(tmp_path)
    before = (_digest(edgar), _digest(prices))
    assert mod.cm.run_main(mod.main, ["--edgar", str(edgar), "--prices", str(prices), "--out", str(tmp_path / "o"),
                                      "--quarters", "4"]) == 0
    r = json.loads((tmp_path / "o" / "backtest.json").read_text())
    assert r["events"] == 8 and r["tickers_without_prices"] == 0
    r7 = r["rules"]["R7@0"]
    assert (r7["n"], r7["score_pos"], r7["score_neg"]) == (8, 4, 4)
    assert r7["hit_rate"] == 1.0 and r7["hit_n"] == 8
    assert abs(r7["mean_move_pos"] - 0.05) < 1e-6 and abs(r7["mean_move_neg"] + 0.05) < 1e-6
    assert r7["spread_pos_minus_neg"] > 0.09 and r7["spearman"] > 0.9
    assert r["rules"]["R6@0"]["n"] == 0                      # no DGS10 without a raw snapshot: not scored, not guessed
    assert "R2" in r["not_run"]
    assert (tmp_path / "o" / "backtest.txt").read_text().startswith("# 372 earnings rule backtest")
    assert (_digest(edgar), _digest(prices)) == before


def test_the_rule_rows_use_only_filings_up_to_the_event(tmp_path):
    mod = _load("372_earnings_rule_backtest")
    qs = _quarters(10.0, 12.0)
    ev = mod.event_date(qs[3])
    hist = mod.history_at(qs, 3, ev)
    assert len(hist) == 4 and hist[0]["fiscal_date"] == qs[3]["period"]
    late = [dict(q) for q in qs]
    late[1] = {**qs[1], "filed": {"revenue": "2099-01-01"}}  # filed after the event: not visible at it
    assert len(mod.history_at(late, 3, ev)) == 3


def test_move_needs_a_close_before_and_a_session_after(tmp_path):
    mod = _load("372_earnings_rule_backtest")
    dates = ["2026-01-05", "2026-01-06", "2026-01-07"]
    closes = [100.0, 110.0, 121.0]
    assert abs(mod.move(dates, closes, "2026-01-06", 0) - 0.10) < 1e-9
    assert abs(mod.move(dates, closes, "2026-01-06", 1) - 0.21) < 1e-9
    assert mod.move(dates, closes, "2026-01-05", 0) is None        # no close before the first bar
    assert mod.move(dates, closes, "2026-01-07", 1) is None        # no session after


def test_r6_flags_a_rate_sensitive_name_from_dgs10(tmp_path):
    """A stock that moves -3x the 10Y yield change (decimal) is 'high rate sensitivity' (-15) at every event."""
    import math
    mod = _load("372_earnings_rule_backtest")
    edgar, prices = _stores(tmp_path)
    raw = tmp_path / "raw_20261014_040000_000000.duckdb"
    days = _session_days(date(2024, 1, 1), 900)
    y, rows_y, price, rows_p = 4.0, [], 100.0, []
    for k, d in enumerate(days):
        dy = 0.2 * math.sin(k / 3.0)                       # percentage points
        y += dy
        price *= 1.0 - 3.0 * dy / 100.0 if k else 1.0
        rows_y.append(("DGS10", d, "fred", y))
        rows_p.append(("UP", d, price, price))
    con = duckdb.connect(str(raw))
    con.execute("CREATE TABLE macro_series (series_id TEXT, obs_date DATE, source TEXT, value DOUBLE)")
    con.executemany("INSERT INTO macro_series VALUES (?, ?, ?, ?)", rows_y)
    con.close()
    con = duckdb.connect(str(prices))
    con.execute("DELETE FROM price_bars_daily WHERE ticker = 'UP'")
    con.executemany("INSERT INTO price_bars_daily VALUES (?, ?, ?, ?)", rows_p)
    con.close()
    assert mod.cm.run_main(mod.main, ["--edgar", str(edgar), "--prices", str(prices), "--raw", str(raw),
                                      "--out", str(tmp_path / "o"), "--quarters", "4"]) == 0
    r6 = json.loads((tmp_path / "o" / "backtest.json").read_text())["rules"]["R6@0"]
    # UP (4 events) is flagged; DOWN has no yield link (beta ~ 0), so it is scored 0 rather than flagged
    assert r6["score_neg"] == 4 and r6["score_pos"] == 0 and r6["score_zero"] == 4
    assert "spearman" not in r6                              # a negative-only rule is read as flagged vs not
