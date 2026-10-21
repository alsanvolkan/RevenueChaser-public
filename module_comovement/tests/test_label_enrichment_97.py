"""#97: a cut is labeled by enrichment against its background, not by a bare majority."""
from __future__ import annotations

from scipy.stats import hypergeom

from RevenueChaser.module_comovement import config, label


def _ids(prefix: str, n: int) -> list[str]:
    return [f"{prefix}{i}" for i in range(n)]


def _call(clusters, industry, background, themes=None):
    return label.label_level(clusters, industry, themes, background)


def test_enrichment_thresholds_are_the_approved_gates():
    assert config.LABEL_Q_MAX == 0.01
    assert config.LABEL_MIN_N == 4
    assert config.LABEL_MIN_HITS == 4
    assert config.LABEL_MIN_ENRICH_SHARE == 0.25
    assert config.LABEL_MIN_LIFT == 3.0


def test_a_planted_regional_bank_group_is_named_with_share_and_lift():
    # 10 of 12 are Regional Banks; the background has 20 of 400.
    # share = 10/12 -> 83%, lift = (10/12) / (20/400) = 16.666... -> 17.
    members = _ids("C", 12)
    industry = {t: "Regional Banks" for t in members[:10]}
    industry.update({t: "Software" for t in members[10:]})
    others = _ids("B", 388)
    for t in others[:10]:
        industry[t] = "Regional Banks"
    for t in others[10:]:
        industry[t] = "Software"
    background = members + others
    assert len(background) == 400
    assert sum(v == "Regional Banks" for v in industry.values()) == 20
    assert _call({1: members}, industry, background)[1] == "Regional Banks 83%, ×17"


def test_an_industry_that_fails_only_q_is_not_the_label():
    # x=4, n=6, K=6, N=27: share 67%, lift 3, hits 4, but p ≈ 0.011 so q is not < 0.01.
    members = _ids("C", 6)
    industry = {t: "Thin Banks" for t in members[:4]}
    extra = _ids("E", 2)
    fillers = _ids("F", 19)
    for t in extra:
        industry[t] = "Thin Banks"
    background = members + extra + fillers
    assert len(background) == 27
    assert float(hypergeom.sf(3, 27, 6, 6)) >= 0.01
    assert _call({1: members}, industry, background)[1] is None


def test_an_industry_that_fails_only_hits_is_not_the_label():
    # x=3, n=4, K=3, N=15: share 75%, lift 3.75, q < 0.01, but hits 3 < 4.
    members = _ids("C", 4)
    industry = {t: "Rare Banks" for t in members[:3]}
    background = members + _ids("F", 11)
    assert float(hypergeom.sf(2, 15, 3, 4)) < 0.01
    assert _call({1: members}, industry, background)[1] is None


def test_an_industry_that_fails_only_share_is_not_the_label():
    # x=4, n=17, K=4, N=52: hits 4, lift ≈ 3.06, q < 0.01, share 4/17 < 0.25.
    members = _ids("C", 17)
    industry = {t: "Spread Banks" for t in members[:4]}
    background = members + _ids("F", 35)
    assert float(hypergeom.sf(3, 52, 4, 17)) < 0.01
    assert _call({1: members}, industry, background)[1] is None


def test_an_industry_that_fails_only_lift_is_not_the_label():
    # x=4, n=4, K=4, N=11: share 100%, hits 4, q < 0.01, lift 2.75 < 3.
    members = _ids("C", 4)
    industry = {t: "Common Banks" for t in members}
    background = members + _ids("F", 7)
    assert float(hypergeom.sf(3, 11, 4, 4)) < 0.01
    assert _call({1: members}, industry, background)[1] is None


def test_a_passing_theme_names_the_cluster_when_no_industry_passes():
    members = _ids("C", 12)
    industry = {t: "Broad" for t in members}
    others = _ids("B", 388)
    for t in others:
        industry[t] = "Broad"
    themes = {t: ["Cloud Computing"] for t in members[:10]}
    for t in others[:10]:
        themes[t] = ["Cloud Computing"]
    got = _call({1: members}, industry, members + others, themes)
    assert got[1] == "Cloud Computing 83%, ×17"


def test_nothing_that_passes_stays_none():
    members = _ids("C", 12)
    industry = {t: "Broad" for t in members}
    others = _ids("B", 388)
    for t in others:
        industry[t] = "Broad"
    background = members + others
    assert _call({1: members}, industry, background)[1] is None
    themes = {t: ["Other"] for t in members}
    assert _call({1: members}, industry, background, themes)[1] is None


def test_bh_blocks_a_cluster_whose_raw_p_is_under_the_cutoff():
    # Cluster 1: 4/4 Signal Bank, K=7 of N=21. Raw p ≈ 0.0058 < 0.01 and every
    # other gate passes, but a second test makes the BH q ≈ 0.0117.
    signal = _ids("S", 4)
    widgets = _ids("W", 4)
    industry = {t: "Signal Bank" for t in signal + _ids("E", 3)}
    industry.update({t: "Widgets" for t in widgets + _ids("U", 6)})
    background = signal + widgets + _ids("E", 3) + _ids("U", 6) + _ids("Z", 4)
    assert len(background) == 21
    raw = float(hypergeom.sf(3, 21, 7, 4))
    assert raw < 0.01
    got = _call({1: signal, 2: widgets}, industry, background)
    assert got[1] is None
    assert got[2] is None


def test_blank_and_unclassified_industries_never_win():
    members = _ids("C", 12)
    background = members + _ids("B", 20)
    for bad in ("", "Unclassified", "Other", " Other "):
        industry = {t: bad for t in background}
        assert _call({1: members}, industry, background)[1] is None, bad
    # A real industry that passes still wins when unclassified is the majority.
    real = _ids("R", 6)
    unknown = _ids("U", 10)
    cluster = real + unknown
    industry = {t: "Regional Banks" for t in real}
    industry.update({t: "Unclassified" for t in unknown})
    extra_real = _ids("E", 14)
    for t in extra_real:
        industry[t] = "Regional Banks"
    fillers = _ids("F", 370)
    bg = cluster + extra_real + fillers
    assert len(bg) == 400
    assert _call({1: cluster}, industry, bg)[1] == "Regional Banks 38%, ×7.5"
    themes = {t: ["Other"] for t in members}
    assert _call({1: members}, {t: "" for t in background}, background, themes)[1] is None


def test_ties_break_by_share_then_by_name():
    # Same lift, higher share wins even when the other name sorts first.
    zinc = _ids("Z", 6)
    alpha = _ids("A", 4)
    rest = _ids("R", 2)
    members = zinc + alpha + rest
    industry = {t: "Zinc" for t in zinc}
    industry.update({t: "Alpha" for t in alpha})
    background = members + _ids("F", 24)
    assert len(background) == 36
    assert _call({1: members}, industry, background)[1] == "Zinc 50%, ×3.0"
    # Same lift and the same share: the name that sorts first wins.
    apple = _ids("P", 4)
    mango = _ids("M", 4)
    tied = apple + mango
    tied_industry = {t: "Mango" for t in mango}
    tied_industry.update({t: "Apple" for t in apple})
    tied_bg = tied + _ids("F", 28)
    assert _call({1: tied}, tied_industry, tied_bg)[1] == "Apple 50%, ×4.5"


def test_bh_step_up_names_both_clusters_with_the_same_p():
    # M3: two clusters, each 4/4 of K = 7, N = 21, p = 0.00585. Step-up gives
    # both q = 0.00585, so both are named. Without it the first gets q = 0.0117.
    alpha = _ids("A", 4)
    beta = _ids("B", 4)
    industry = {t: "AlphaBank" for t in alpha + _ids("E", 3)}
    industry.update({t: "BetaBank" for t in beta + _ids("U", 3)})
    background = alpha + beta + _ids("E", 3) + _ids("U", 3) + _ids("Z", 7)
    assert len(background) == 21
    raw = float(hypergeom.sf(3, 21, 7, 4))
    assert abs(raw - 0.00585) < 5e-6
    assert raw < 0.01 and raw * 2 > 0.01
    got = _call({1: alpha, 2: beta}, industry, background)
    assert got[1] == "AlphaBank 100%, ×3.0"
    assert got[2] == "BetaBank 100%, ×3.0"


def test_the_label_is_the_tag_with_the_higher_lift_including_share_25():
    # M10 / M7: 16 names, 8 Common (K = 40 of 400, lift 5) and 4 Rare
    # (K = 4, lift 25, share 25%). Lift wins: "Rare 25%, ×25".
    members = _ids("C", 16)
    industry = {t: "Common" for t in members[:8]}
    industry.update({t: "Rare" for t in members[8:12]})
    extra = _ids("E", 32)
    for t in extra:
        industry[t] = "Common"
    background = members + extra + _ids("F", 352)
    assert len(background) == 400
    assert sum(v == "Common" for v in industry.values()) == 40
    assert sum(v == "Rare" for v in industry.values()) == 4
    assert _call({1: members}, industry, background)[1] == "Rare 25%, ×25"


def test_industry_wins_when_a_theme_also_passes():
    # M11: both an industry and a theme pass; the industry is the label.
    members = _ids("C", 12)
    industry = {t: "Regional Banks" for t in members[:10]}
    industry.update({t: "Software" for t in members[10:]})
    others = _ids("B", 388)
    for t in others[:10]:
        industry[t] = "Regional Banks"
    for t in others[10:]:
        industry[t] = "Software"
    themes = {t: ["Cloud Computing"] for t in members[:10]}
    for t in others[:10]:
        themes[t] = ["Cloud Computing"]
    got = _call({1: members}, industry, members + others, themes)
    assert got[1] == "Regional Banks 83%, ×17"


def test_industry_and_theme_are_separate_bh_families():
    # M13: one industry test, p ≈ 0.006, plus several theme tests at p = 1.
    # Separate families: the industry is named. Pooled, its q would exceed 0.01.
    signal = _ids("S", 4)
    extra = _ids("E", 3)
    fillers = _ids("F", 14)
    background = signal + extra + fillers
    assert len(background) == 21
    industry = {t: "Signal Bank" for t in signal + extra}
    themes = {t: ["NoiseA", "NoiseB", "NoiseC"] for t in background}
    raw = float(hypergeom.sf(3, 21, 7, 4))
    assert abs(raw - 0.006) < 2e-4
    assert float(hypergeom.sf(3, 21, 21, 4)) == 1.0
    assert raw * 4 > 0.01
    assert _call({1: signal}, industry, background, themes)[1] == "Signal Bank 100%, ×3.0"


def test_the_bh_family_includes_the_low_hit_pairs():
    # M12: one strong pair (p ≈ 0.004) plus two 1-hit pairs at the same cut.
    # q = 3p ≈ 0.012, so the strong pair stays neutral.
    strong = _ids("S", 4)
    rest = _ids("R", 4)
    cluster = strong + rest
    hit_a = _ids("A", 4)
    hit_b = _ids("B", 4)
    industry = {t: "Strong" for t in strong + _ids("E", 16)}
    industry[hit_a[0]] = "HitA"
    industry[hit_b[0]] = "HitB"
    background = cluster + hit_a + hit_b + _ids("E", 16) + _ids("F", 168)
    assert len(background) == 200
    raw = float(hypergeom.sf(3, 200, 20, 8))
    assert abs(raw - 0.004) < 1e-5
    assert abs(raw * 3 - 0.012) < 1e-5
    got = _call({1: cluster, 2: hit_a, 3: hit_b}, industry, background)
    assert got[1] is None
    assert got[2] is None and got[3] is None


def test_theme_fallback_uses_the_catalog_medium_label_not_ticker_scores(rawstore_tmp_db):
    from RevenueChaser.core import rawstore
    from RevenueChaser.module_comovement import engine
    from RevenueChaser.module_comovement.tests.test_comovement import _seed_ohlc
    from RevenueChaser.module_comovement.tests.test_precompute import (
        _seed_universe_industry, _tight_returns)

    groups = (("A1", "A2", "A3", "A4"), ("B1", "B2", "B3", "B4"),
              ("C1", "C2", "C3", "C4"), ("D1", "D2", "D3", "D4"))
    ret = _tight_returns(groups)
    _seed_universe_industry({t: "Broad Industry" for t in ret.columns})
    _seed_ohlc(ret)
    named = groups[0]
    rawstore.write_rows("theme_runs", [{
        "id": "r1", "created_at": "x", "k_fine": 1, "k_medium": 1, "k_coarse": 1,
        "jaccard": 1.0, "is_current": 1}])
    rawstore.write_rows("relatedness_themes", [
        {"theme_id": "r1:c1", "level": "coarse", "parent_id": "", "label": "Coarse Only", "size": 4},
        {"theme_id": "r1:m1", "level": "medium", "parent_id": "r1:c1", "label": "Catalog Medium", "size": 4},
        {"theme_id": "r1:f1", "level": "fine", "parent_id": "r1:m1", "label": "Fine Only", "size": 4},
    ])
    rawstore.write_rows("relatedness_theme_members", [
        {"ticker": t, "theme_id": "r1:f1", "weight": 1.0} for t in named])
    rawstore.replace_all("ticker_scores", [
        {"ticker": t, "themes": ["Keyword Theme"]} for t in named])

    stored = engine.run(no_fetch=True, persist=True, cuts=(4,), window=126)
    labels = [c["label"] for c in stored["clusters"]["4"]]
    assert "Catalog Medium 100%, ×4.0" in labels
    assert not any("Keyword" in lab or lab in ("Fine Only", "Coarse Only") for lab in labels)

    rawstore.replace_all("ticker_scores", [
        {"ticker": t, "themes": ["Served Keyword"]} for t in named])
    served = engine.run(no_fetch=True, cuts=(4,), window=126)
    assert served["as_of"] == stored["as_of"]
    served_labels = [c["label"] for c in served["clusters"]["4"]]
    assert "Catalog Medium 100%, ×4.0" in served_labels
    assert not any("Keyword" in lab for lab in served_labels)
