"""Step 0 pair check sheet: generator and scorer (Relatedness v2, #341). Offline, no store."""
from __future__ import annotations

import csv

import numpy as np
import pytest

from RevenueChaser.module_relatedness.validation import pair_sheet


def _matrix(n=40, seed=3):
    rng = np.random.default_rng(seed)
    s = rng.random((n, n))
    s = (s + s.T) / 2
    np.fill_diagonal(s, np.nan)
    return [f"T{i:02d}" for i in range(n)], s


def _pair_scores(tickers, s):
    iu = np.triu_indices(len(tickers), 1)
    return s[iu]


def test_generator_gives_fifty_high_and_fifty_low_pairs_in_their_bands():
    tickers, s = _matrix()
    pairs = pair_sheet.generate(tickers, s, seed=7)
    assert len(pairs) == 100
    assert sum(p["band"] == "high" for p in pairs) == 50
    assert sum(p["band"] == "low" for p in pairs) == 50
    vals = _pair_scores(tickers, s)
    p50, p90, p99 = np.percentile(vals, [50, 90, 99])
    for p in pairs:
        if p["band"] == "high":
            assert p90 <= p["score"] <= p99
        else:
            assert p["score"] < p50
    assert len({(p["a"], p["b"]) for p in pairs}) == 100
    assert all(p["a"] != p["b"] for p in pairs)
    assert [p["pair_no"] for p in pairs] == list(range(1, 101))


def test_generator_shuffles_the_bands():
    tickers, s = _matrix()
    bands = [p["band"] for p in pair_sheet.generate(tickers, s, seed=7)]
    assert bands != sorted(bands) and bands != sorted(bands, reverse=True)


def test_same_seed_same_pairs_other_seed_other_pairs():
    tickers, s = _matrix()
    a = pair_sheet.generate(tickers, s, seed=11)
    b = pair_sheet.generate(tickers, s, seed=11)
    c = pair_sheet.generate(tickers, s, seed=12)
    assert a == b
    assert a != c


def test_generator_refuses_a_matrix_too_small_for_fifty_high_pairs():
    tickers, s = _matrix(n=12)  # 66 pairs; the 90th-99th band holds ~6
    with pytest.raises(ValueError, match="high"):
        pair_sheet.generate(tickers, s, seed=1)


def test_sheet_hides_the_band_and_score(tmp_path):
    tickers, s = _matrix()
    pairs = pair_sheet.generate(tickers, s, seed=7)
    sheet, key = tmp_path / "sheet.csv", tmp_path / "key.json"
    pair_sheet.write_sheet(pairs, sheet, key, seed=7, method="toy")
    with sheet.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == ["pair_no", "a", "b", "related"]
    assert len(rows) == 100 and all(r["related"] == "" for r in rows)


def _mark(sheet, answers):
    with sheet.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r["related"] = answers.get(int(r["pair_no"]), "")
    with sheet.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["pair_no", "a", "b", "related"])
        w.writeheader()
        w.writerows(rows)


def test_scorer_returns_the_agreement_on_a_marked_sheet(tmp_path):
    tickers, s = _matrix()
    pairs = pair_sheet.generate(tickers, s, seed=7)
    sheet, key = tmp_path / "sheet.csv", tmp_path / "key.json"
    pair_sheet.write_sheet(pairs, sheet, key, seed=7, method="toy")
    # Agree on every pair, then flip 10 high and 5 low pairs: 85 / 100 agree.
    answers = {p["pair_no"]: ("y" if p["band"] == "high" else "n") for p in pairs}
    highs = [p["pair_no"] for p in pairs if p["band"] == "high"][:10]
    lows = [p["pair_no"] for p in pairs if p["band"] == "low"][:5]
    for n in highs:
        answers[n] = "not"
    for n in lows:
        answers[n] = "related"
    _mark(sheet, answers)
    out = pair_sheet.score(sheet, key)
    assert out["n_marked"] == 100 and out["n_agree"] == 85
    assert out["agreement"] == pytest.approx(0.85)
    assert out["target"] == 0.80 and out["passes"] is True


def test_scorer_reports_unmarked_rows_and_scores_the_rest(tmp_path):
    tickers, s = _matrix()
    pairs = pair_sheet.generate(tickers, s, seed=7)
    sheet, key = tmp_path / "sheet.csv", tmp_path / "key.json"
    pair_sheet.write_sheet(pairs, sheet, key, seed=7, method="toy")
    answers = {p["pair_no"]: ("1" if p["band"] == "high" else "0") for p in pairs[:40]}
    answers[pairs[0]["pair_no"]] = "0" if pairs[0]["band"] == "high" else "1"  # one disagreement
    _mark(sheet, answers)
    out = pair_sheet.score(sheet, key)
    assert out["n_marked"] == 40 and out["n_unmarked"] == 60
    assert out["agreement"] == pytest.approx(39 / 40)


def test_scorer_refuses_an_unreadable_mark(tmp_path):
    tickers, s = _matrix()
    pairs = pair_sheet.generate(tickers, s, seed=7)
    sheet, key = tmp_path / "sheet.csv", tmp_path / "key.json"
    pair_sheet.write_sheet(pairs, sheet, key, seed=7, method="toy")
    _mark(sheet, {1: "maybe"})
    with pytest.raises(ValueError, match="maybe"):
        pair_sheet.score(sheet, key)
