"""Step 0 pair check sheet: generator and scorer (Relatedness v2, #341). Offline, no store."""
from __future__ import annotations

import csv
import json

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


# ── review round 1 (#342): B1 sparse input, N2 low-band edge, S3 scorer ─────────────────────────────

def _store_shaped(n=300, top=25, seed=0):
    """A full random symmetric matrix, and the same cut to each name's top ``top`` peers (NaN elsewhere),
    the way ``relatedness_scores`` keeps it."""
    rng = np.random.default_rng(seed)
    full = rng.random((n, n))
    full = (full + full.T) / 2
    np.fill_diagonal(full, np.nan)
    sparse = np.full_like(full, np.nan)
    for i in range(n):
        row = np.where(np.isnan(full[i]), -np.inf, full[i])
        keep = np.argsort(-row, kind="stable")[:top]
        sparse[i, keep] = full[i, keep]
    return [f"T{i:03d}" for i in range(n)], full, sparse


def test_a_store_shaped_matrix_never_puts_a_stored_pair_in_the_low_band():
    tickers, full, sparse = _store_shaped()
    idx = {t: i for i, t in enumerate(tickers)}
    pairs = pair_sheet.generate(tickers, sparse, seed=1)
    lows = [p for p in pairs if p["band"] == "low"]
    assert len(lows) == 50
    for p in lows:
        i, j = idx[p["a"]], idx[p["b"]]
        assert np.isnan(sparse[i, j]) and np.isnan(sparse[j, i]), f"{p['a']}/{p['b']} is a stored top-25 pair"
        assert p["score"] == 0.0
    # The high band is still drawn from the stored (near-neighbour) pairs.
    highs = [p for p in pairs if p["band"] == "high"]
    assert all(not np.isnan(sparse[idx[p["a"]], idx[p["b"]]]) or not np.isnan(sparse[idx[p["b"]], idx[p["a"]]])
               for p in highs)


def test_the_low_band_is_strictly_below_the_median_when_enough_pairs_are():
    # 40 names, 780 pairs: 290 below 0.5, 200 exactly at 0.5 (the median), 290 above.
    n = 40
    iu, ju = np.triu_indices(n, 1)
    rng = np.random.default_rng(4)
    vals = np.concatenate([rng.uniform(0.0, 0.4, 290), np.full(200, 0.5), rng.uniform(0.6, 1.0, 290)])
    vals = rng.permutation(vals)
    s = np.full((n, n), np.nan)
    s[iu, ju] = vals
    s[ju, iu] = vals
    tickers = [f"T{i:02d}" for i in range(n)]
    lows = [p for p in pair_sheet.generate(tickers, s, seed=3) if p["band"] == "low"]
    assert all(p["score"] < 0.5 for p in lows)


def _keyed_sheet(tmp_path, seed=7):
    tickers, s = _matrix()
    pairs = pair_sheet.generate(tickers, s, seed=seed)
    sheet, key = tmp_path / "sheet.csv", tmp_path / "key.json"
    pair_sheet.write_sheet(pairs, sheet, key, seed=seed, method="toy")
    return pairs, sheet, key


def _write_rows(sheet, rows):
    with sheet.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["pair_no", "a", "b", "related"])
        w.writeheader()
        w.writerows(rows)


def _right(p):
    return "y" if p["band"] == "high" else "n"


def _wrong(p):
    return "n" if p["band"] == "high" else "y"


def test_one_correct_row_of_a_hundred_does_not_pass(tmp_path):
    pairs, sheet, key = _keyed_sheet(tmp_path)
    p = pairs[0]
    _write_rows(sheet, [{"pair_no": p["pair_no"], "a": p["a"], "b": p["b"], "related": _right(p)}])
    out = pair_sheet.score(sheet, key)
    assert out["agreement"] == pytest.approx(1.0)
    assert out["n_marked"] == 1 and out["n_unmarked"] == 99 and out["n_missing"] == 99
    assert out["passes"] is None


def test_a_duplicate_pair_number_is_refused(tmp_path):
    pairs, sheet, key = _keyed_sheet(tmp_path)
    rows = [{"pair_no": p["pair_no"], "a": p["a"], "b": p["b"], "related": _right(p)} for p in pairs]
    _write_rows(sheet, rows + [rows[0]])
    with pytest.raises(ValueError, match="more than once"):
        pair_sheet.score(sheet, key)


@pytest.mark.parametrize("n_wrong, passes", [(20, True), (21, False)])
def test_a_full_sheet_passes_at_eighty_percent(tmp_path, n_wrong, passes):
    pairs, sheet, key = _keyed_sheet(tmp_path)
    rows = [{"pair_no": p["pair_no"], "a": p["a"], "b": p["b"],
             "related": _wrong(p) if k < n_wrong else _right(p)} for k, p in enumerate(pairs)]
    _write_rows(sheet, rows)
    out = pair_sheet.score(sheet, key)
    assert out["n_marked"] == 100 and out["n_missing"] == 0
    assert out["n_agree"] == 100 - n_wrong and out["passes"] is passes


# ── review round 2 (#342): R1 sparse share, R3 Excel-saved sheets, R4 no silent overwrite ──────────

def _top_k_sparse(n, top=25, seed=0):
    """A store-shaped matrix of ``n`` names: a random symmetric score cut to each name's top ``top`` peers, NaN
    elsewhere (``relatedness_scores`` keeps only those). float32 for the dense step, to keep 3,500 names light."""
    rng = np.random.default_rng(seed)
    full = rng.random((n, n), dtype=np.float32)
    full += full.T
    np.fill_diagonal(full, -1.0)
    keep = np.argpartition(-full, top, axis=1)[:, :top]
    s = np.full((n, n), np.nan)
    rows = np.arange(n)[:, None]
    s[rows, keep] = full[rows, keep]
    del full
    return [f"T{i:04d}" for i in range(n)], s


def test_a_matrix_with_under_one_percent_scored_pairs_is_refused():
    tickers, s = _top_k_sparse(3500)
    with pytest.raises(ValueError, match=r"0\.\d+% of pairs are scored.*dense"):
        pair_sheet.generate(tickers, s, seed=1)


def test_the_pilot_universe_size_still_gives_fifty_scored_high_pairs():
    tickers, s = _top_k_sparse(1503)
    idx = {t: i for i, t in enumerate(tickers)}
    highs = [p for p in pair_sheet.generate(tickers, s, seed=1) if p["band"] == "high"]
    assert len(highs) == 50
    for p in highs:
        i, j = idx[p["a"]], idx[p["b"]]
        assert not (np.isnan(s[i, j]) and np.isnan(s[j, i]))


def _plain_marked(tmp_path):
    pairs, sheet, key = _keyed_sheet(tmp_path)
    rows = [{"pair_no": p["pair_no"], "a": p["a"], "b": p["b"], "related": _right(p) if k % 7 else _wrong(p)}
            for k, p in enumerate(pairs)]
    _write_rows(sheet, rows)
    return rows, sheet, key


def test_a_sheet_saved_with_a_bom_scores_like_the_plain_one(tmp_path):
    rows, sheet, key = _plain_marked(tmp_path)
    plain = pair_sheet.score(sheet, key)
    sheet.write_bytes(b"\xef\xbb\xbf" + sheet.read_bytes())
    assert pair_sheet.score(sheet, key) == plain


def test_a_semicolon_sheet_scores_like_the_plain_one(tmp_path):
    rows, sheet, key = _plain_marked(tmp_path)
    plain = pair_sheet.score(sheet, key)
    with sheet.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["pair_no", "a", "b", "related"], delimiter=";")
        w.writeheader()
        w.writerows(rows)
    assert pair_sheet.score(sheet, key) == plain


def test_a_sheet_without_the_header_names_the_expected_one(tmp_path):
    rows, sheet, key = _plain_marked(tmp_path)
    sheet.write_text("1,A,B,y\n2,C,D,n\n", encoding="utf-8")
    with pytest.raises(ValueError, match="pair_no, a, b, related"):
        pair_sheet.score(sheet, key)


def test_writing_over_a_marked_sheet_is_refused_without_force(tmp_path):
    rows, sheet, key = _plain_marked(tmp_path)
    before_sheet, before_key = sheet.read_bytes(), key.read_bytes()
    tickers, s = _matrix()
    pairs = pair_sheet.generate(tickers, s, seed=8)
    with pytest.raises(FileExistsError, match="sheet.csv"):
        pair_sheet.write_sheet(pairs, sheet, key, seed=8)
    assert sheet.read_bytes() == before_sheet and key.read_bytes() == before_key
    pair_sheet.write_sheet(pairs, sheet, key, seed=8, force=True)
    assert sheet.read_bytes() != before_sheet
    assert json.loads(key.read_text(encoding="utf-8"))["seed"] == 8


def test_an_existing_key_alone_also_blocks_the_write(tmp_path):
    tickers, s = _matrix()
    pairs = pair_sheet.generate(tickers, s, seed=8)
    sheet, key = tmp_path / "sheet.csv", tmp_path / "key.json"
    key.write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError, match="key.json"):
        pair_sheet.write_sheet(pairs, sheet, key, seed=8)
    assert not sheet.exists()
