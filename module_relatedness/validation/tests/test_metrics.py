"""Step 0 metrics on toy inputs with known answers (Relatedness v2, #341). Offline, no store."""
from __future__ import annotations

import math

import numpy as np
import pytest

from RevenueChaser.module_relatedness.validation import metrics
from RevenueChaser.module_relatedness.validation.peer_sets import PeerSet


def _ps(name, tickers, kind="hand"):
    return PeerSet(id=name, name=name, tickers=tuple(tickers), borderline=False, kind=kind)


# ── a toy score matrix ────────────────────────────────────────────────────────
# Six names: A B C are one group, X Y Z another. Scores are built so each name's ranking is known.
TICKERS = ["A", "B", "C", "X", "Y", "Z"]


def _toy_scores() -> np.ndarray:
    s = np.full((6, 6), np.nan)
    rank = {  # row -> peers best first (scores 0.9, 0.8, ...)
        "A": ["B", "X", "C", "Y", "Z"],
        "B": ["A", "C", "X", "Y", "Z"],
        "C": ["X", "Y", "Z", "A", "B"],
        "X": ["Y", "Z", "A", "B", "C"],
        "Y": ["X", "Z", "A", "B", "C"],
        "Z": ["A", "B", "C", "X", "Y"],
    }
    idx = {t: i for i, t in enumerate(TICKERS)}
    for t, peers in rank.items():
        for r, p in enumerate(peers):
            s[idx[t], idx[p]] = 0.9 - 0.1 * r
    return s


def test_top_k_excludes_self_and_orders_by_score():
    s = _toy_scores()
    assert metrics.top_k(TICKERS, s, "A", 2) == ["B", "X"]
    assert metrics.top_k(TICKERS, s, "C", 3) == ["X", "Y", "Z"]
    s[0, 0] = 99.0  # a self-score never counts
    assert metrics.top_k(TICKERS, s, "A", 1) == ["B"]


def test_top_k_skips_missing_scores():
    s = _toy_scores()
    s[0, 1] = np.nan  # A has no score for B
    assert metrics.top_k(TICKERS, s, "A", 2) == ["X", "C"]


def test_peer_recall_at_k_known_answer():
    s = _toy_scores()
    sets = [_ps("abc", ["A", "B", "C"]), _ps("xyz", ["X", "Y", "Z"])]
    out = metrics.peer_recall_at_k(TICKERS, s, sets, k=2)
    # abc: A top2 = B,X -> 1/2; B top2 = A,C -> 2/2; C top2 = X,Y -> 0/2  => mean 0.5
    # xyz: X top2 = Y,Z -> 1; Y top2 = X,Z -> 1; Z top2 = A,B -> 0       => mean 2/3
    assert out["per_set"]["abc"] == pytest.approx(0.5)
    assert out["per_set"]["xyz"] == pytest.approx(2 / 3)
    assert out["mean"] == pytest.approx((0.5 + 2 / 3) / 2)
    assert out["k"] == 2


def test_peer_recall_default_k_is_25():
    assert metrics.RECALL_K == 25
    s = _toy_scores()
    out = metrics.peer_recall_at_k(TICKERS, s, [_ps("abc", ["A", "B", "C"])])
    assert out["k"] == 25 and out["per_set"]["abc"] == pytest.approx(1.0)  # every peer fits in a top 25


def test_peer_recall_ignores_a_member_missing_from_the_matrix():
    s = _toy_scores()
    out = metrics.peer_recall_at_k(TICKERS, s, [_ps("abq", ["A", "B", "Q"])], k=1)
    # Q is not scored: A top1 = B -> 1/1, B top1 = A -> 1/1
    assert out["per_set"]["abq"] == pytest.approx(1.0)


def test_precision_at_k_known_answer():
    s = _toy_scores()
    sets = [_ps("abc", ["A", "B", "C"]), _ps("xyz", ["X", "Y", "Z"])]
    out = metrics.precision_at_k(TICKERS, s, sets, k=2)
    # abc: A hits 1/2, B 2/2, C 0/2 -> 0.5 ; xyz: 2/2, 2/2, 0/2 -> 2/3
    assert out["per_set"]["abc"] == pytest.approx(0.5)
    assert out["per_set"]["xyz"] == pytest.approx(2 / 3)
    assert out["mean"] == pytest.approx((0.5 + 2 / 3) / 2)


def _set_first_scores(members, outsiders, first_outsider=False):
    """Every name scores its set's members above every outsider (optionally one outsider ranks first)."""
    tickers = list(members) + list(outsiders)
    s = np.full((len(tickers), len(tickers)), 0.1)
    for i, a in enumerate(tickers):
        for j, b in enumerate(tickers):
            if a in members and b in members:
                s[i, j] = 0.8
    if first_outsider:
        for i, a in enumerate(tickers):
            if a in members:
                s[i, tickers.index(outsiders[0])] = 0.9
    np.fill_diagonal(s, np.nan)
    return tickers, s


def test_precision_at_10_scores_a_perfect_small_set_one():
    # avo, 1 Nov 2026: the denominator is min(k, other scored members), so a perfect 5-name set is 1.0, not 0.4.
    assert metrics.PRECISION_K == 10
    members = ["R1", "R2", "R3", "R4", "R5"]
    tickers, s = _set_first_scores(members, [f"O{i}" for i in range(12)])
    out = metrics.precision_at_k(tickers, s, [_ps("rail", members)])
    assert out["per_set"]["rail"] == pytest.approx(1.0)
    assert "ceiling" not in out


def test_precision_at_10_still_divides_a_large_set_by_ten():
    members = [f"M{i:02d}" for i in range(12)]        # 11 other members each: the denominator is min(10, 11) = 10
    outsiders = ["O1", "O2", "O3"]
    tickers, s = _set_first_scores(members, outsiders, first_outsider=True)
    out = metrics.precision_at_k(tickers, s, [_ps("big", members)])
    assert out["per_set"]["big"] == pytest.approx(9 / 10)   # top 10 = O1 + 9 members; 9/11 would be wrong


def test_precision_counts_only_scored_members_in_the_denominator():
    s = _toy_scores()
    out = metrics.precision_at_k(TICKERS, s, [_ps("abq", ["A", "B", "Q"])])   # Q unscored: 1 other member each
    assert out["per_set"]["abq"] == pytest.approx(1.0)


def test_purity_is_size_weighted_and_lists_low_themes_by_name():
    industry = {"A": "Banks", "B": "Banks", "C": "Banks", "D": "Banks",
                "E": "Insurance", "F": "Semis", "G": "Autos", "H": "Rail", "I": "Air"}
    themes = {
        "f1": {"label": "Big banks", "members": ["A", "B", "C", "D", "E"]},   # 4/5 = 0.8, size 5
        "f2": {"label": "Grab bag", "members": ["F", "G", "H", "I"]},          # 1/4 = 0.25, size 4
    }
    out = metrics.theme_purity(themes, industry)
    assert out["purity"] == pytest.approx((4 + 1) / 9)            # weighted, not (0.8 + 0.25) / 2
    assert out["purity"] != pytest.approx((0.8 + 0.25) / 2)
    assert out["per_theme"]["f1"] == pytest.approx(0.8)
    assert out["below_floor"] == ["Grab bag"]
    assert out["target"] == 0.70 and out["floor"] == 0.4
    assert out["passes"] is False


def test_purity_skips_members_with_no_industry():
    out = metrics.theme_purity({"f1": {"label": "x", "members": ["A", "B", "Q"]}}, {"A": "Banks", "B": "Banks"})
    assert out["purity"] == pytest.approx(1.0)


def test_etf_theme_recall_at_medium():
    medium = {"m1": {"label": "Semis", "members": ["A", "B", "C", "D"]},
              "m2": {"label": "Banks", "members": ["E", "F", "G"]}}
    etfs = [_ps("SMH", ["A", "B", "C", "E"], kind="etf"),   # best theme m1: 3/4
            _ps("KRE", ["E", "F", "A", "B", "Q"], kind="etf"),  # m2 holds E,F: 2/5 ; m1 holds A,B: 2/5
            _ps("XBI", ["A", "E"], kind="etf")]             # 1/2
    out = metrics.etf_theme_recall(etfs, medium)
    assert out["per_etf"]["SMH"]["recall"] == pytest.approx(0.75)
    assert out["per_etf"]["SMH"]["theme"] == "m1"
    assert out["per_etf"]["KRE"]["recall"] == pytest.approx(0.4)
    assert out["median"] == pytest.approx(0.5)
    assert out["target"] == 0.6 and out["passes"] is False


def test_etf_theme_recall_passes_at_the_target():
    medium = {"m1": {"label": "x", "members": ["A", "B", "C"]}}
    out = metrics.etf_theme_recall([_ps("E1", ["A", "B", "C", "D", "F"], kind="etf")], medium)
    assert out["median"] == pytest.approx(0.6) and out["passes"] is True


def test_seed_ari_is_one_for_identical_seeds():
    a = {"A": 0, "B": 0, "C": 1, "D": 1}
    relabelled = {"A": 7, "B": 7, "C": 3, "D": 3}
    assert metrics.seed_ari([a, dict(a), relabelled]) == pytest.approx(1.0)


def test_seed_ari_drops_below_one_when_seeds_disagree():
    a = {"A": 0, "B": 0, "C": 1, "D": 1}
    b = {"A": 0, "B": 1, "C": 0, "D": 1}
    assert metrics.seed_ari([a, b]) < 1.0


def test_seed_ari_needs_two_runs():
    assert math.isnan(metrics.seed_ari([{"A": 0}]))


def test_theme_runs_values_names_the_five_columns():
    report = {"peer_recall": {"mean": 0.5}, "hand_precision": {"mean": 0.4}, "purity": {"purity": 0.7},
              "etf_recall": {"median": 0.6}, "seed_ari": 0.95}
    vals = metrics.theme_runs_values(report)
    assert tuple(vals) == metrics.THEME_RUNS_COLUMNS and len(vals) == 5
    assert vals == {"val_peer_recall_25": 0.5, "val_hand_precision_10": 0.4, "val_gics_purity": 0.7,
                    "val_etf_recall_medium": 0.6, "val_seed_ari": 0.95}
    empty = metrics.theme_runs_values({})
    assert all(v is None for v in empty.values())


# ── review round 1 (#342): N1 tie order, N5 singleton themes ───────────────────────────────────────

def test_top_k_breaks_ties_by_ticker():
    tickers = ["Q", "D", "M", "B", "Z"]
    s = np.full((5, 5), 0.5)
    np.fill_diagonal(s, np.nan)
    s[0, 4] = 0.9                       # Q's best peer is Z; D, M and B tie behind it
    assert metrics.top_k(tickers, s, "Q", 4) == ["Z", "B", "D", "M"]
    assert metrics.top_k(tickers, s, "Q", 2) == ["Z", "B"]


def test_purity_leaves_out_single_member_themes_and_counts_them():
    industry = {f"S{i}": f"Ind{i}" for i in range(8)}
    industry.update({f"X{i}": f"Mix{i % 4}" for i in range(8)})
    themes = {f"s{i}": {"label": f"solo {i}", "members": [f"S{i}"]} for i in range(8)}
    themes["mixed"] = {"label": "Mixed", "members": [f"X{i}" for i in range(8)]}   # 2 of 8 per industry
    out = metrics.theme_purity(themes, industry)
    assert out["purity"] == pytest.approx(0.25)          # not (8 + 2) / 16 = 0.625
    assert out["n_singletons"] == 8
    assert "solo 0" not in out["below_floor"] and out["below_floor"] == ["Mixed"]
