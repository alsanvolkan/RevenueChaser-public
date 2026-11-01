"""The Step 0 report command on fixture files: no network, no store (Relatedness v2, #341)."""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness.validation import __main__ as cli
from RevenueChaser.module_relatedness.validation import metrics

BANKS = ["JPM", "BAC", "C", "WFC", "GS", "MS", "USB", "PNC", "TFC"]
RAIL = ["UNP", "CSX", "NSC", "CNI", "CP"]
SEMI = ["NVDA", "AMD", "AVGO", "TSM", "MU", "QCOM"]


def _fixtures(tmp_path: Path) -> dict[str, Path]:
    tickers = BANKS + RAIL + SEMI
    group = {t: g for g, names in (("bank", BANKS), ("rail", RAIL), ("semi", SEMI)) for t in names}
    n = len(tickers)
    rng = np.random.default_rng(0)
    s = rng.random((n, n)) * 0.3
    for i, a in enumerate(tickers):
        for j, b in enumerate(tickers):
            if group[a] == group[b]:
                s[i, j] += 0.6
    np.fill_diagonal(s, np.nan)
    scores = {"tickers": tickers, "matrix": [[None if np.isnan(v) else float(v) for v in row] for row in s]}
    themes = {
        "fine": {"f1": {"label": "Banks", "members": BANKS}, "f2": {"label": "Rail", "members": RAIL},
                 "f3": {"label": "Chips", "members": SEMI}},
        "medium": {"m1": {"label": "Finance", "members": BANKS},
                   "m2": {"label": "Industry and chips", "members": RAIL + SEMI}},
    }
    industries = {**{t: "Banks" for t in BANKS}, **{t: "Ground Transportation" for t in RAIL},
                  **{t: "Semiconductors" for t in SEMI}}
    seeds = [{t: group[t] for t in tickers}, {t: group[t].upper() for t in tickers}]
    holdings = {"SMH": SEMI + ["ASML"], "KRE": ["USB", "PNC", "TFC", "ZION"], "IYT": RAIL + ["FDX"]}
    out = {}
    for name, obj in (("scores", scores), ("themes", themes), ("industries", industries), ("seeds", seeds),
                      ("holdings", holdings)):
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(obj), encoding="utf-8")
        out[name] = p
    return out


@pytest.fixture
def no_store(monkeypatch):
    """Any store access during the report is a failure."""
    def boom(*a, **k):
        raise AssertionError("the report touched the store")
    for name in ("connect", "query", "get", "get_all", "write_rows", "ensure_table", "ensure_all_tables"):
        monkeypatch.setattr(rawstore, name, boom)


def test_report_runs_on_fixtures_without_store_or_network(tmp_path, capsys, no_store):
    f = _fixtures(tmp_path)
    rc = cli.main(["report", "--scores", str(f["scores"]), "--themes", str(f["themes"]),
                   "--industries", str(f["industries"]), "--seeds", str(f["seeds"]),
                   "--etf-holdings", str(f["holdings"])])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hand_sets_version"] == 1 and out["etf_sets_version"] == 1
    # Of the hand sets only banks and railroads are in the fixture universe.
    assert set(out["hand_precision"]["per_set"]) == {"us_large_banks", "railroads"}
    assert out["purity"]["purity"] == pytest.approx(1.0)
    assert out["etf_recall"]["per_etf"]["SMH"]["recall"] == pytest.approx(1.0)
    assert out["etf_recall"]["per_etf"]["KRE"]["recall"] == pytest.approx(1.0)
    assert out["seed_ari"] == pytest.approx(1.0)
    assert tuple(out["theme_runs"]) == metrics.THEME_RUNS_COLUMNS
    rep = {r["set"]: r for r in out["peer_set_report"]["etf"]}
    assert rep["KRE"]["dropped"] == ["ZION"] and rep["JETS"]["excluded"] == "no holdings"
    hand = {r["set"]: r for r in out["peer_set_report"]["hand"]}
    assert hand["us_airlines"]["excluded"]


def test_report_writes_to_a_file(tmp_path, no_store):
    f = _fixtures(tmp_path)
    dest = tmp_path / "report.json"
    assert cli.main(["report", "--scores", str(f["scores"]), "--themes", str(f["themes"]),
                     "--out", str(dest)]) == 0
    out = json.loads(dest.read_text(encoding="utf-8"))
    assert out["purity"] is None and out["seed_ari"] is None   # no inputs for them, no number
    assert out["theme_runs"]["val_gics_purity"] is None


def test_report_accepts_score_pairs(tmp_path, capsys, no_store):
    f = _fixtures(tmp_path)
    m = json.loads(f["scores"].read_text(encoding="utf-8"))
    pairs = [{"ticker": a, "peer": b, "score": v}
             for a, row in zip(m["tickers"], m["matrix"]) for b, v in zip(m["tickers"], row) if v is not None]
    f["scores"].write_text(json.dumps({"pairs": pairs}), encoding="utf-8")
    assert cli.main(["report", "--scores", str(f["scores"])]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["peer_recall"]["per_set"]["railroads"] == pytest.approx(1.0)


def test_pairs_generate_then_score(tmp_path, capsys, no_store):
    tickers = [f"T{i:02d}" for i in range(40)]
    s = np.random.default_rng(1).random((40, 40))
    s = (s + s.T) / 2
    np.fill_diagonal(s, np.nan)
    sp = tmp_path / "scores.json"
    sp.write_text(json.dumps({"tickers": tickers,
                              "matrix": [[None if np.isnan(v) else float(v) for v in r] for r in s]}),
                  encoding="utf-8")
    sheet, key = tmp_path / "sheet.csv", tmp_path / "key.json"
    assert cli.main(["pairs-generate", "--scores", str(sp), "--seed", "5", "--sheet", str(sheet),
                     "--key", str(key), "--method", "toy"]) == 0
    k = json.loads(key.read_text(encoding="utf-8"))
    with sheet.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r["related"] = "y" if k["pairs"][r["pair_no"]]["band"] == "high" else "n"
    with sheet.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["pair_no", "a", "b", "related"])
        w.writeheader()
        w.writerows(rows)
    capsys.readouterr()
    assert cli.main(["pairs-score", "--sheet", str(sheet), "--key", str(key)]) == 0
    assert json.loads(capsys.readouterr().out)["agreement"] == pytest.approx(1.0)


def test_module_runs_as_a_command(tmp_path):
    f = _fixtures(tmp_path)
    root = Path(__file__).resolve().parents[3]
    proc = subprocess.run([sys.executable, "-m", "RevenueChaser.module_relatedness.validation", "report",
                           "--scores", str(f["scores"])], capture_output=True, text=True, timeout=120,
                          cwd=root.parent, env={**__import__("os").environ, "RC_STORE_AUTO_ENSURE": "0"})
    assert proc.returncode == 0, proc.stderr
    assert "peer_recall" in json.loads(proc.stdout)


def test_report_upper_cases_input_keys_and_counts_the_unmatched(tmp_path, capsys, no_store):
    """Review N4 (#342): lower-case theme members, industries and seed tickers still match the scores."""
    f = _fixtures(tmp_path)
    ind = json.loads(f["industries"].read_text(encoding="utf-8"))
    ind = {k.lower(): v for k, v in ind.items()}
    ind["nosuch"] = "Banks"
    f["industries"].write_text(json.dumps(ind), encoding="utf-8")
    th = json.loads(f["themes"].read_text(encoding="utf-8"))
    for level in th.values():
        for t in level.values():
            t["members"] = [m.lower() for m in t["members"]]
    th["fine"]["f1"]["members"].append("ghost")
    f["themes"].write_text(json.dumps(th), encoding="utf-8")
    seeds = [{k.lower(): v for k, v in s.items()} for s in json.loads(f["seeds"].read_text(encoding="utf-8"))]
    f["seeds"].write_text(json.dumps(seeds), encoding="utf-8")
    assert cli.main(["report", "--scores", str(f["scores"]), "--themes", str(f["themes"]),
                     "--industries", str(f["industries"]), "--seeds", str(f["seeds"])]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["purity"]["purity"] == pytest.approx(1.0)
    assert out["seed_ari"] == pytest.approx(1.0)
    assert out["unmatched"] == {"themes": 1, "industries": 1, "seeds": 0}
