"""#108 sector coarse layer: the coarse level is the 11 Morningstar sectors, not an NMF/agglomerative cut.

avo's card 2026-10-16 ("Sektörden coarse"): the nmf-d5-2026-10-16 shadow run put 56% of its names in one NMF
coarse, and ``rebalance_coarses`` still left a 44% bucket. Each medium is now parented to the modal sector of its
members, so the coarse level is the sector spine and its largest bucket is the largest sector.
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness import themes


def _seed(run, mediums, sectors, coarse_parent="c0", current=0):
    """``mediums``: ``{mi: [tickers]}``, one fine per medium, all under one NMF coarse ``c0``."""
    rows = [{"theme_id": f"{run}:c0", "level": "coarse", "parent_id": "", "label": "Giant",
             "size": sum(len(v) for v in mediums.values())},
            {"theme_id": f"{run}:c1", "level": "coarse", "parent_id": "", "label": "Empty", "size": 0}]
    members = []
    for mi, tickers in mediums.items():
        rows.append({"theme_id": f"{run}:m{mi}", "level": "medium", "parent_id": f"{run}:{coarse_parent}",
                     "label": f"Med{mi}", "size": len(tickers)})
        rows.append({"theme_id": f"{run}:f{mi}", "level": "fine", "parent_id": f"{run}:m{mi}",
                     "label": f"Fine{mi}", "size": len(tickers)})
        members += [{"ticker": t, "theme_id": f"{run}:f{mi}", "weight": 1.0} for t in tickers]
    rawstore.write_rows("relatedness_themes", rows)
    rawstore.write_rows("relatedness_theme_members", members)
    rawstore.write_rows("equity_universe", [{"ticker": t, "market_cap": 1e9, "sector": s}
                                            for t, s in sectors.items()])
    rawstore.write_rows("theme_runs", [{"id": run, "created_at": "2026-10-16T00:00:00+00:00",
                                        "k_fine": len(mediums), "k_medium": len(mediums), "k_coarse": 2,
                                        "jaccard": 0.74, "is_current": current}])


def _rows(run, level):
    return {r["theme_id"]: r for r in themes.run_rows("relatedness_themes", run, level)}


# --- the plan: one sector per medium ------------------------------------------------------------------------------

def test_a_medium_goes_to_the_modal_sector_of_its_members():
    plan = themes.sector_coarse_plan(
        {"m0": {"A", "B", "C"}, "m1": {"D", "E"}},
        {"A": "Technology", "B": "Technology", "C": "Healthcare", "D": "Energy", "E": "Energy"})
    assert plan == {"m0": "Technology", "m1": "Energy"}


def test_names_without_a_sector_do_not_vote():
    """Three unknown names and one bank: the medium is Financial Services, not Unclassified."""
    plan = themes.sector_coarse_plan(
        {"m0": {"A", "B", "C", "D"}},
        {"A": "Unclassified", "B": "", "D": "Financial Services"})
    assert plan == {"m0": "Financial Services"}


def test_a_medium_with_no_sector_value_is_unclassified():
    plan = themes.sector_coarse_plan({"m0": {"A", "B"}, "m1": set()}, {"A": "Unclassified"})
    assert plan == {"m0": "Unclassified", "m1": "Unclassified"}


def test_a_tie_goes_to_the_first_sector_in_morningstar_order():
    """Deterministic: Basic Materials comes before Utilities in ``MORNINGSTAR_SECTORS``, whatever the set order."""
    for order in (["A", "B"], ["B", "A"]):
        plan = themes.sector_coarse_plan({"m0": set(order)}, {"A": "Utilities", "B": "Basic Materials"})
        assert plan == {"m0": "Basic Materials"}


def test_a_stored_gics_spelling_votes_for_its_morningstar_sector():
    plan = themes.sector_coarse_plan({"m0": {"A", "B", "C"}},
                                     {"A": "Information Technology", "B": "Information Technology", "C": "Healthcare"})
    assert plan == {"m0": "Technology"}


# --- the store writer ---------------------------------------------------------------------------------------------

def _two_sector_run(run="sec-run", current=0):
    mediums = {0: ["T0", "T1", "T2"], 1: ["T3", "T4"], 2: ["B0", "B1", "B2", "B3"], 3: ["U0"]}
    sectors = {"T0": "Technology", "T1": "Technology", "T2": "Healthcare", "T3": "Technology",
               "T4": "Technology", "B0": "Financial Services", "B1": "Financial Services",
               "B2": "Financial Services", "B3": "Technology"}
    _seed(run, mediums, sectors, current=current)
    return run


def test_sector_coarses_reparents_every_medium_under_its_sector(rawstore_tmp_db):
    run = _two_sector_run()
    out = themes.sector_coarses(run)

    coarses = _rows(run, "coarse")
    assert {r["label"] for r in coarses.values()} == {"Technology", "Financial Services", "Unclassified"}
    assert f"{run}:c0" not in coarses and f"{run}:c1" not in coarses        # the NMF coarses are gone
    by_label = {r["label"]: r for r in coarses.values()}
    assert by_label["Technology"]["theme_id"] == f"{run}:sec-technology"
    assert by_label["Financial Services"]["theme_id"] == f"{run}:sec-financial-services"
    assert {k: r["size"] for k, r in by_label.items()} == {"Technology": 5, "Financial Services": 4,
                                                           "Unclassified": 1}
    assert all(r["parent_id"] == "" for r in coarses.values())

    meds = _rows(run, "medium")
    assert meds[f"{run}:m0"]["parent_id"] == f"{run}:sec-technology"
    assert meds[f"{run}:m1"]["parent_id"] == f"{run}:sec-technology"
    assert meds[f"{run}:m2"]["parent_id"] == f"{run}:sec-financial-services"
    assert meds[f"{run}:m3"]["parent_id"] == f"{run}:sec-unclassified"
    assert {r["label"] for r in meds.values()} == {"Med0", "Med1", "Med2", "Med3"}   # labels untouched

    assert rawstore.get("theme_runs", run)["k_coarse"] == 3
    assert out["k_coarse"] == 3
    assert out["profiled"] == 10
    assert out["max_share"] == pytest.approx(0.5)
    assert out["sizes"] == {"Technology": 5, "Financial Services": 4, "Unclassified": 1}
    # 8 of 10 names sit under their own sector: T2 (Healthcare) and B3 (Technology) do not; U0 has no sector
    assert out["purity"] == pytest.approx(0.8)
    assert out["unclassified_mediums"] == 1
    assert out["mixed_mediums"] == 0          # no medium's modal sector holds under half of its sector-known names


def test_sector_coarses_is_idempotent(rawstore_tmp_db):
    run = _two_sector_run()
    first = themes.sector_coarses(run)
    snap = sorted((r["theme_id"], r["parent_id"], r["label"], r["size"])
                  for r in themes.run_rows("relatedness_themes", run))
    second = themes.sector_coarses(run)
    assert first == second
    assert snap == sorted((r["theme_id"], r["parent_id"], r["label"], r["size"])
                          for r in themes.run_rows("relatedness_themes", run))


def test_sector_coarses_stays_inside_its_run(rawstore_tmp_db):
    run = _two_sector_run("sec-a")
    _two_sector_run("sec-b")
    before = sorted((r["theme_id"], r["parent_id"], r["label"]) for r in themes.run_rows("relatedness_themes",
                                                                                        "sec-b"))
    themes.sector_coarses(run)
    after = sorted((r["theme_id"], r["parent_id"], r["label"]) for r in themes.run_rows("relatedness_themes",
                                                                                       "sec-b"))
    assert before == after
    assert rawstore.get("theme_runs", "sec-b")["k_coarse"] == 2


def test_a_medium_split_exactly_in_half_is_not_mixed():
    """The modal sector must hold under half of the sector-known names to count as mixed; a 2-2 tie holds half."""
    med = {"m0": {"A", "B", "C", "D"}}
    sec = {"A": "Technology", "B": "Technology", "C": "Energy", "D": "Energy"}
    plan = themes.sector_coarse_plan(med, sec)
    assert plan == {"m0": "Energy"}
    assert themes.sector_coarse_summary(med, sec, plan)["mixed_mediums"] == 0


def test_a_mixed_medium_is_counted(rawstore_tmp_db):
    run = "sec-mixed"
    _seed(run, {0: ["A", "B", "C", "D", "E"]},
          {"A": "Technology", "B": "Technology", "C": "Energy", "D": "Utilities", "E": "Healthcare"})
    out = themes.sector_coarses(run)
    assert out["mixed_mediums"] == 1
    assert out["purity"] == pytest.approx(0.4)


# --- the pre-switch pipeline and the CLI --------------------------------------------------------------------------

def test_finalize_run_with_sector_coarse_materializes_sector_labels(rawstore_tmp_db):
    run = _two_sector_run()
    themes.finalize_run(run, coarse="sector")
    rows = rawstore.query("SELECT ticker, theme FROM ticker_themes WHERE theme_run_id = ? AND level = 'coarse'",
                          [run])
    coarse_of = {r["ticker"]: r["theme"] for r in rows}
    assert coarse_of["T2"] == "Technology" and coarse_of["B0"] == "Financial Services"
    assert coarse_of["U0"] == "Unclassified"
    assert len(coarse_of) == 10
    assert rawstore.get("theme_runs", run)["is_current"] == 1


def test_finalize_run_refuses_an_unknown_coarse_mode(rawstore_tmp_db):
    run = _two_sector_run()
    with pytest.raises(ValueError, match="coarse"):
        themes.finalize_run(run, coarse="gics")
    assert rawstore.get("theme_runs", run)["is_current"] == 0


def _record_pipeline(monkeypatch, order):
    monkeypatch.setattr(themes, "current_run_id", lambda: "cur")
    monkeypatch.setattr(themes, "relabel_from_tags", lambda rid, **k: order.append("relabel") or 0)
    monkeypatch.setattr(themes, "unique_theme_labels", lambda rid, level="coarse": order.append("unique") or 0)
    monkeypatch.setattr(themes, "scrub_run_labels", lambda rid: order.append("scrub") or 0)
    monkeypatch.setattr(themes, "drop_empty_coarses", lambda rid: order.append("drop") or {"dropped": 0})
    monkeypatch.setattr(themes, "rebalance_coarses", lambda rid: order.append("rebalance") or {})
    monkeypatch.setattr(themes, "sector_coarses", lambda rid: order.append("sector") or {"k_coarse": 11})
    monkeypatch.setattr(themes, "match_theme_etfs", lambda rid: order.append("etfs") or {"themes": 1})
    monkeypatch.setattr(themes, "cutover_materialize",
                        lambda rid: order.append("cut") or {"run_id": rid, "rows": 9, "profiled": 3})


def test_cli_cutover_coarse_sector_replaces_drop_and_rebalance(rawstore_tmp_db, monkeypatch, capsys):
    from RevenueChaser.module_relatedness import __main__ as cli
    order: list[str] = []
    _record_pipeline(monkeypatch, order)
    assert cli.main(["--cutover", "--run-id", "tgt", "--coarse", "sector"]) == 0
    assert order == ["relabel", "scrub", "sector", "unique", "unique", "unique", "etfs", "cut"]
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["coarse"] == "sector" and out["k_coarse"] == 11


def test_cli_cutover_default_keeps_the_nmf_coarse(rawstore_tmp_db, monkeypatch, capsys):
    from RevenueChaser.module_relatedness import __main__ as cli
    order: list[str] = []
    _record_pipeline(monkeypatch, order)
    assert cli.main(["--cutover", "--run-id", "tgt"]) == 0
    assert order == ["relabel", "scrub", "drop", "rebalance", "unique", "unique", "unique", "etfs", "cut"]
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["coarse"] == "nmf"


def test_finalize_run_nmf_reports_the_rebalanced_k_coarse(rawstore_tmp_db, monkeypatch):
    order: list[str] = []
    _record_pipeline(monkeypatch, order)
    monkeypatch.setattr(themes, "rebalance_coarses", lambda rid: {"k_coarse": 7})
    assert themes.finalize_run("tgt")["k_coarse"] == 7


def test_cli_rebuild_themes_passes_the_coarse_mode(rawstore_tmp_db, monkeypatch, capsys):
    from RevenueChaser.module_relatedness import __main__ as cli
    seen = {}
    monkeypatch.setattr(themes, "current_run_id", lambda: "cur")
    monkeypatch.setattr(themes, "build_shadow", lambda **k: {"run_id": "new", "themes": 5})
    monkeypatch.setattr(themes, "finalize_run",
                        lambda rid, adopt=None, coarse="nmf": seen.update(rid=rid, coarse=coarse)
                        or {"rows": 1, "coarse": coarse})
    assert cli.main(["--rebuild-themes", "--run-id", "new", "--coarse", "sector"]) == 0
    assert seen == {"rid": "new", "coarse": "sector"}


def test_cli_rejects_an_unknown_coarse_mode(capsys):
    from RevenueChaser.module_relatedness import __main__ as cli
    with pytest.raises(SystemExit):
        cli.main(["--cutover", "--run-id", "tgt", "--coarse", "gics"])


@pytest.mark.parametrize("argv", [
    ["--name-themes", "--coarse", "sector"],
    ["--rebuild-themes", "--build-only", "--coarse", "sector"],
    ["--status", "--coarse", "sector"],
])
def test_cli_coarse_sector_goes_with_a_switch_only(argv, monkeypatch):
    """``--coarse sector`` only changes ``finalize_run``; on any other action it would be silently ignored."""
    from RevenueChaser.module_relatedness import __main__ as cli
    monkeypatch.setattr(themes, "build_shadow", lambda **k: pytest.fail("must not build"))
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
