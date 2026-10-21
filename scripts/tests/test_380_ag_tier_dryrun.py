"""#380 AG W1 dry run (M5, M6): synthetic snapshots only, read-only, offline."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import zlib
from pathlib import Path

import duckdb

from RevenueChaser.core import rawstore

RESEARCH = Path(__file__).resolve().parents[1] / "research"
FETCHED = 1_792_000_000.0            # 2026-10-14


def _load(name):
    spec = importlib.util.spec_from_file_location("r_" + name, RESEARCH / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sig(*passed):
    return json.dumps([{"key": k, "passed": p, "known": 3, "total": 3} for k, p in zip("abc", passed)])


def _rec(year, end, revenue, gp=None, diluted=None, basic=None):
    out = {"fiscal_year": year, "revenue": revenue, "gross_profit": gp if gp is not None else revenue * 0.5,
           "period_ends": {"revenue": end}}
    if diluted is not None:
        out["diluted_shares"] = diluted
    if basic is not None:
        out["basic_shares"] = basic
    return out


def _stores(tmp_path):
    raw, edgar = tmp_path / "raw_20261014_040000_000000.duckdb", tmp_path / "edgar_20261014_040000_000000.duckdb"
    con = duckdb.connect(str(raw))
    con.execute("CREATE TABLE ticker_scores (ticker TEXT, sector TEXT, total_points INTEGER, signals TEXT)")
    cards = [  # ticker, sector, points, signals
        ("BAL", "Technology", 9, _sig(3, 3, 3)),        # tier 3
        ("STR", "Technology", 7, _sig(3, 3, 1)),        # 3-3-1 -> tier 2
        ("LOW", "Technology", 3, _sig(1, 1, 1)),        # none
        ("REIT", "Real Estate", 6, _sig(2, 2, 2)),      # tier 3 by signals, flagged by sector
        ("OLD", "Technology", 5, _sig(2, 2, 1)),        # tier 2, stale anchor
        ("BAD", "Technology", 4, _sig(2, 1, 1)),        # prior <= 0, gp > revenue, basic vs diluted
    ]
    con.executemany("INSERT INTO ticker_scores VALUES (?, ?, ?, ?)", cards)
    con.close()
    con = duckdb.connect(str(edgar))
    con.execute("CREATE TABLE edgar_financials (ticker TEXT, annual BLOB, fetched_at DOUBLE)")
    fresh = [_rec(2024, "2024-12-31", 100.0, diluted=10), _rec(2025, "2025-12-31", 150.0, diluted=11)]
    annual = {
        "BAL": fresh, "STR": fresh, "LOW": fresh, "REIT": fresh,
        "OLD": [_rec(2022, "2022-06-30", 100.0, diluted=10), _rec(2023, "2023-06-30", 150.0, diluted=11)],
        "BAD": [_rec(2024, "2024-12-31", -5.0, diluted=10), _rec(2025, "2025-12-31", 150.0, gp=900.0, basic=11)],
    }
    for t, a in annual.items():
        con.execute("INSERT INTO edgar_financials VALUES (?, ?, ?)", [t, zlib.compress(json.dumps(a).encode()), FETCHED])
    con.close()
    return raw, edgar


def _digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_counts_tiers_gates_sector_and_stale(tmp_path):
    mod = _load("380_ag_tier_dryrun")
    raw, edgar = _stores(tmp_path)
    before = (_digest(raw), _digest(edgar))
    assert mod.cm.run_main(mod.main, ["--raw", str(raw), "--edgar", str(edgar), "--out", str(tmp_path / "o")]) == 0
    r = json.loads((tmp_path / "o" / "dryrun.json").read_text())
    assert r["cards"] == 6 and r["as_of"] == "2026-10-14"
    assert r["points_buckets"] == {"3": 1, "4": 1, "5": 1, "6": 1, "7": 1, "9": 1}
    assert r["tiers"] == {"balanced_3": 2, "strong_2": 2, "none": 2}      # BAL+REIT, STR+OLD, LOW+BAD
    assert r["tier_by_ratio"]["1.000"]["3"] == 1                          # only 3-3-3 is strong everywhere
    assert r["gates"]["prior_nonpositive"] == 1 and r["gates"]["gross_profit_exceeds_revenue"] == 1
    assert r["gates"]["mixed_share_basis"] == 1 and r["cards_with_any_gate"] == 1
    assert r["spine_sector_flagged"] == {"Real Estate": 1}
    assert r["stale_annual"] == 1 and r["stale_annual_by_tier"] == {"2": 1}
    assert r["no_recent_revenue"] == 0
    assert (tmp_path / "o" / "dryrun.txt").read_text().startswith("# 380 AG W1 dry run")
    assert (_digest(raw), _digest(edgar)) == before                        # opened read-only


def test_a_fresh_filer_with_an_old_revenue_figure_is_not_stale(tmp_path):
    """#380 O-2 fix (PC measurement on 4d2782a): the newest record decides, as in the engine. GAP's last revenue
    figure is FY2017's but its newest record ended 2025-12-31: counted as ``no_recent_revenue``, not stale."""
    mod = _load("380_ag_tier_dryrun")
    raw, edgar = _stores(tmp_path)
    con = duckdb.connect(str(raw))
    con.execute("INSERT INTO ticker_scores VALUES ('GAP', 'Technology', 6, ?)", [_sig(0, 3, 3)])
    con.close()
    gap = [_rec(2017, "2017-12-31", 100.0)] + [{"fiscal_year": y, "total_assets": 1000.0,
                                                 "period_ends": {"total_assets": f"{y}-12-31"}} for y in range(2018, 2026)]
    con = duckdb.connect(str(edgar))
    con.execute("INSERT INTO edgar_financials VALUES (?, ?, ?)", ["GAP", zlib.compress(json.dumps(gap).encode()), FETCHED])
    con.close()
    mod.cm.run_main(mod.main, ["--raw", str(raw), "--edgar", str(edgar), "--out", str(tmp_path / "o")])
    r = json.loads((tmp_path / "o" / "dryrun.json").read_text())
    assert r["stale_annual"] == 1 and r["examples"]["stale"] == ["OLD"]
    assert r["no_recent_revenue"] == 1 and r["examples"]["no_recent_revenue"] == ["GAP"]


def test_a_live_store_is_refused(tmp_path, capsys):
    mod = _load("380_ag_tier_dryrun")
    raw, edgar = _stores(tmp_path)
    live = tmp_path / "raw.duckdb"
    live.write_bytes(raw.read_bytes())
    assert mod.cm.run_main(mod.main, ["--raw", str(live), "--edgar", str(edgar), "--out", str(tmp_path / "o")]) == 2
    assert "refused" in capsys.readouterr().err


def test_as_of_moves_the_stale_count(tmp_path):
    mod = _load("380_ag_tier_dryrun")
    raw, edgar = _stores(tmp_path)
    mod.cm.run_main(mod.main, ["--raw", str(raw), "--edgar", str(edgar), "--out", str(tmp_path / "o"),
                               "--as-of", "2027-06-01"])
    r = json.loads((tmp_path / "o" / "dryrun.json").read_text())
    assert r["stale_annual"] == 6                      # 2025-12-31 is 517 days before 2027-06-01


def test_the_default_tier_is_the_engines():
    """The dry run's tier at the default ratio is ``engine.tier_of`` (2 of 3 sub-criteria passed), so the PC's
    M5 counts are what the code will show (#380 S2)."""
    from itertools import product

    from RevenueChaser.module_asymmetric_growth import engine
    mod = _load("380_ag_tier_dryrun")
    for passed in product(range(4), repeat=3):
        for known in (3, 2, 1):
            sigs = [{"passed": min(p, known), "known": known, "total": 3} for p in passed]
            assert mod.tier_of(sigs) == engine.tier_of(sigs), sigs


def test_a_basic_share_count_in_both_years_is_no_mixed_basis(tmp_path):
    """Anchor diluted + basic, prior basic only: the engine compares basic with basic, no gate (#380 S1)."""
    mod = _load("380_ag_tier_dryrun")
    raw, edgar = _stores(tmp_path)
    con = duckdb.connect(str(edgar))
    annual = [_rec(2024, "2024-12-31", 100.0, basic=10), _rec(2025, "2025-12-31", 150.0, diluted=12, basic=11)]
    con.execute("UPDATE edgar_financials SET annual = ? WHERE ticker = 'BAD'", [zlib.compress(json.dumps(annual).encode())])
    con.close()
    mod.cm.run_main(mod.main, ["--raw", str(raw), "--edgar", str(edgar), "--out", str(tmp_path / "o")])
    r = json.loads((tmp_path / "o" / "dryrun.json").read_text())
    assert "mixed_share_basis" not in r["gates"]
