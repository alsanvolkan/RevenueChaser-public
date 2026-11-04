"""IDF term weights and the peer floor (#395; review findings RL-1 and RL-3). Offline: no LLM, temp store only.

RL-1: without document frequency, two generic tags (``subscription``, ``enterprises``) outscored one specific
shared tag (``euv lithography``), 0.561 against 0.439. RL-3: the top-25 list carried peers that shared one weak
tag, and the percentile counted the zero pairs, so a 0.06 peer printed as p90.
"""
from __future__ import annotations

import pytest

from RevenueChaser.module_relatedness import config, engine, report, similarity, store
from RevenueChaser.module_relatedness.tests.test_pr_c_118 import _prof, _seed_universe


def _corpus() -> dict[str, dict]:
    """Eight SaaS names sharing the generic tags; two lithography names sharing one specific tag."""
    profs = {f"S{i}": _prof(products=[f"app{i}"], business_model=["subscription"],
                            customer_types=["enterprises"]) for i in range(8)}
    profs["A"] = _prof(products=["euv lithography"], business_model=["subscription"],
                       customer_types=["enterprises"])
    profs["C"] = _prof(products=["euv lithography"], business_model=["hardware sales"],
                       customer_types=["foundries"])
    return profs


def test_idf_weighs_a_tag_shared_by_many_below_a_rare_one():
    idf = similarity.idf(_corpus())
    assert idf["subscription"] < idf["euv lithography"]
    assert min(idf.values()) > 0          # a shared tag never counts for nothing


def test_a_specific_shared_tag_outscores_two_generic_ones_rl1():
    profs = _corpus()
    idf = similarity.idf(profs)
    generic = similarity.score_pair(profs["A"], profs["S0"], idf)[0]
    specific = similarity.score_pair(profs["A"], profs["C"], idf)[0]
    assert specific > generic


def test_term_weights_without_idf_stay_the_field_weights():
    w = similarity.term_weights(_prof(products=["gpu"], keywords=["gpu", "ai"]))
    assert w == {"gpu": pytest.approx(1.4), "ai": pytest.approx(0.4)}


def test_all_scores_use_the_same_idf_as_the_peer_lists():
    profs = _corpus()
    peers, _ = similarity.peer_lists(profs)
    oracle = similarity.all_scores(profs)
    for focal, entries in peers.items():
        for p in entries:
            pair = (focal, p["peer"]) if (focal, p["peer"]) in oracle else (p["peer"], focal)
            assert p["score"] == pytest.approx(oracle[pair], abs=1e-4)


def test_peer_lists_hide_a_peer_with_one_shared_tag_rl3():
    profs = {
        "RICH1": _prof(products=["gpu", "cpu"], end_markets=["data center", "gaming"]),
        "RICH2": _prof(products=["gpu", "cpu"], end_markets=["data center", "pc"]),
        "ONE": _prof(products=["gpu"], end_markets=["crypto"], keywords=["mining"]),
    }
    peers, scores = similarity.peer_lists(profs, 25)
    assert [p["peer"] for p in peers["RICH1"]] == ["RICH2"]
    assert peers["RICH1"][0]["n_shared"] == 3
    assert peers["ONE"] == []
    assert len(scores) == 3               # the distribution keeps every non-zero pair, hidden or not


def test_peer_lists_hide_a_peer_under_the_score_floor():
    many = [f"p{i}" for i in range(30)]
    profs = {"X": _prof(products=many),
             "Y": _prof(products=many[:2] + [f"q{i}" for i in range(28)]),   # two shared tags of thirty
             "Z": _prof(products=many)}
    peers, _ = similarity.peer_lists(profs, 25)
    xy = similarity.score_pair(profs["X"], profs["Y"], similarity.idf(profs))[0]
    assert 0 < xy < config.PEER_MIN_SCORE
    assert [p["peer"] for p in peers["X"]] == ["Z"]


def test_percentile_is_over_the_nonzero_pairs(rawstore_tmp_db):
    profs = {"AAA": _prof(products=["alpha", "beta"]), "BBB": _prof(products=["alpha", "beta"]),
             "CCC": _prof(products=["alpha", "beta", "gamma"]),
             "DDD": _prof(products=["delta"]), "EEE": _prof(products=["eps"])}
    _seed_universe(list(profs))
    for t, p in profs.items():
        store.save_profile(t, "cik-" + t, p, "k", "m")
    payload = engine._payload(profs, {})
    aaa = {p["peer"]: p for p in payload["peers"]["AAA"]}
    # Three non-zero pairs of ten: AAA-BBB (1.0) above the two equal AAA-CCC / BBB-CCC scores.
    assert aaa["BBB"]["percentile"] == pytest.approx(66.7, abs=0.1)
    assert aaa["CCC"]["percentile"] == 0.0
    assert payload["n_pairs"] == 10
    assert payload["n_scored"] == 3


def test_the_peer_row_prints_the_shared_tag_count():
    payload = {"as_of": "2026-11-04T00:00:00+00:00", "model": "m", "n": 2, "fields": list(config.PROFILE_FIELDS),
               "companies": [{"ticker": "A", "name": "A Inc", "sector": "Technology", "industry": "x",
                              "market_cap": 1e9, "weights": {}, "profile": {}}],
               "peers": {"A": [{"peer": "B", "name": "B Inc", "score": 0.5, "percentile": 80.0,
                                "shared": ["gpu", "cpu"], "n_shared": 2}]},
               "thinking_compare": {}}
    html = report.render_relatedness_dashboard(payload)
    assert "n_shared" in html
    assert "shared tags" in html           # the help text names the floor
