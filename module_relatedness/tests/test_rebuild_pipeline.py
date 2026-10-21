"""#118 A2: ``--rebuild-themes`` and ``--cutover`` share one pipeline, so the boards
never get a run with blank labels, and naming copies every level back."""
from __future__ import annotations

import functools
import json

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness import __main__ as cli
from RevenueChaser.module_relatedness import config, naming, store, themes


def _profiles():
    out = {}
    for i in range(30):
        out[f"G{i}"] = {"products": ["gpu", "ai"], "end_markets": ["data center"],
                        "business_model": ["semis"], "customer_types": ["hyperscaler"],
                        "value_chain_role": ["designer"], "keywords": ["accelerated"]}
        out[f"B{i}"] = {"products": ["loans", "deposits"], "end_markets": ["retail"],
                        "business_model": ["interest"], "customer_types": ["consumer"],
                        "value_chain_role": ["lender"], "keywords": ["branch"]}
    return out


def _seed(monkeypatch):
    profs = _profiles()
    rawstore.write_rows("equity_universe", [
        {"ticker": t, "name": t + " Inc", "market_cap": 1e10,
         "sector": "Technology"} for t in profs])
    for t, p in profs.items():
        store.save_profile(t, "", p, "k", config.PROMPT_VERSION)
    rawstore.write_rows("etf_holdings", [
        {"ticker": "XLK", "entry": json.dumps([{"ticker": t} for t in list(profs)[:20]])}])
    monkeypatch.setattr(themes, "build_shadow", functools.partial(
        themes.build_shadow, candidates=(2, 3), k_medium=2, k_coarse=2))


def _synced(run_id):
    """Every ticker_themes copy equals the label of its theme row."""
    labels = {r["theme_id"]: r["label"] for r in rawstore.get_all("relatedness_themes")}
    rows = rawstore.query(
        "SELECT theme_id, theme, level FROM ticker_themes WHERE theme_run_id = ?",
        [run_id])
    assert rows
    return rows, labels


def test_cli_rebuild_materialises_labels_and_etfs(rawstore_tmp_db, monkeypatch, capsys):
    _seed(monkeypatch)
    assert cli.main(["--rebuild-themes", "--run-id", "rb1"]) == 0
    capsys.readouterr()
    rows, labels = _synced("rb1")
    assert {r["level"] for r in rows} == {"fine", "medium", "coarse"}
    assert all(r["theme"] for r in rows)                      # no blank copies
    assert all(r["theme"] == labels[r["theme_id"]] for r in rows)
    assert rawstore.count("relatedness_theme_etfs") > 0
    assert themes.current_run_id() == "rb1"


def test_cli_rebuild_makes_every_level_unique(rawstore_tmp_db, monkeypatch, capsys):
    _seed(monkeypatch)
    assert cli.main(["--rebuild-themes", "--run-id", "rb2"]) == 0
    capsys.readouterr()
    for level in ("fine", "medium", "coarse"):
        labs = [r["label"] for r in rawstore.get_all("relatedness_themes")
                if r["level"] == level and r["theme_id"].startswith("rb2:")]
        assert labs and len(labs) == len(set(labs)), level


def test_name_themes_copies_every_level(rawstore_tmp_db, monkeypatch, capsys):
    _seed(monkeypatch)
    cli.main(["--rebuild-themes", "--run-id", "rb3"])
    capsys.readouterr()
    n = {"i": 0}

    def fake(system, user, **kw):
        n["i"] += 1
        return json.dumps({"name": f"Name {n['i']}"})

    naming.name_themes("rb3", complete=fake, force=True)
    rows, labels = _synced("rb3")
    assert all(r["theme"] == labels[r["theme_id"]] for r in rows)
    assert all(r["theme"].startswith("Name") for r in rows)


def test_cutover_of_the_current_run_is_refused(rawstore_tmp_db, monkeypatch, capsys):
    _seed(monkeypatch)
    cli.main(["--rebuild-themes", "--run-id", "rb4"])
    capsys.readouterr()
    before = rawstore.get_all("relatedness_themes")
    assert cli.main(["--cutover"]) == 1                       # defaults to current
    assert cli.main(["--cutover", "--run-id", "rb4"]) == 1
    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert "current" in json.loads(out)["error"]
    assert rawstore.get_all("relatedness_themes") == before


def test_relabel_only_empty_keeps_names(rawstore_tmp_db, monkeypatch):
    _seed(monkeypatch)
    themes.build_shadow(run_id="rb5")
    fine = next(r for r in rawstore.get_all("relatedness_themes")
                if r["theme_id"].startswith("rb5:") and r["level"] == "fine")
    rawstore.write_rows("relatedness_themes", [{**fine, "label": "Human Name"}])
    themes.relabel_from_tags("rb5", only_empty=True)
    assert rawstore.get("relatedness_themes", fine["theme_id"])["label"] == "Human Name"
    others = [r for r in rawstore.get_all("relatedness_themes")
              if r["theme_id"].startswith("rb5:") and r["theme_id"] != fine["theme_id"]]
    assert all(r["label"] for r in others)


def test_finalize_run_uniques_fine_and_medium_duplicates(rawstore_tmp_db):
    """Both levels end unique. A medium theme now always merges two fine themes
    (#433), so two medium themes need three fine ones — seeded directly, because
    the two-cluster NMF fixture can only produce one medium theme."""
    run = "rb6"
    rawstore.write_rows("equity_universe", [
        {"ticker": f"G{i}", "name": f"G{i} Inc", "market_cap": 1e10,
         "sector": "Technology"} for i in range(6)])
    rawstore.write_rows("relatedness_themes", [
        {"theme_id": f"{run}:f0", "level": "fine", "parent_id": f"{run}:m0",
         "label": "Same Name", "size": 2},
        {"theme_id": f"{run}:f1", "level": "fine", "parent_id": f"{run}:m0",
         "label": "Same Name", "size": 2},
        {"theme_id": f"{run}:f2", "level": "fine", "parent_id": f"{run}:m1",
         "label": "Same Name", "size": 2},
        {"theme_id": f"{run}:m0", "level": "medium", "parent_id": f"{run}:c0",
         "label": "Same Name", "size": 4},
        {"theme_id": f"{run}:m1", "level": "medium", "parent_id": f"{run}:c0",
         "label": "Same Name", "size": 2},
        {"theme_id": f"{run}:c0", "level": "coarse", "parent_id": "",
         "label": "Same Name", "size": 6},
    ])
    rawstore.write_rows("relatedness_theme_members", [
        {"ticker": f"G{i}", "theme_id": f"{run}:f{i // 2}", "weight": 1.0}
        for i in range(6)])
    rawstore.write_rows("theme_runs", [{
        "id": run, "created_at": "2026-01-01", "k_fine": 3, "k_medium": 2,
        "k_coarse": 1, "jaccard": 1.0, "is_current": 0}])
    themes.finalize_run(run)
    for level in ("fine", "medium"):
        labs = [r["label"] for r in rawstore.get_all("relatedness_themes")
                if r["theme_id"].startswith(run + ":") and r["level"] == level]
        assert len(labs) > 1 and len(labs) == len(set(labs)), level


def test_rebuild_of_the_current_run_id_is_refused(rawstore_tmp_db, monkeypatch, capsys):
    _seed(monkeypatch)
    cli.main(["--rebuild-themes", "--run-id", "rb7"])
    capsys.readouterr()
    before = rawstore.get_all("relatedness_themes")
    assert cli.main(["--rebuild-themes", "--run-id", "rb7"]) == 1
    assert "current" in json.loads(capsys.readouterr().out.strip().splitlines()[-1])["error"]
    assert rawstore.get_all("relatedness_themes") == before
    assert themes.current_run_id() == "rb7"


def test_rebuild_summary_reports_unthemed_names(rawstore_tmp_db, monkeypatch, capsys):
    _seed(monkeypatch)
    for t in ("LONE1", "LONE2"):
        store.save_profile(t, "", {"products": [f"only {t}"]}, "k", config.PROMPT_VERSION)
    # In the universe, so the scoped fit (#108 S0) keeps them and they reach the unthemed count.
    rawstore.write_rows("equity_universe", [
        {"ticker": t, "name": t, "market_cap": 1e10, "sector": "Technology"} for t in ("LONE1", "LONE2")])
    cli.main(["--rebuild-themes", "--run-id", "rb8"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["unthemed"] == 2


def test_cutover_keeps_existing_names(rawstore_tmp_db, monkeypatch, capsys):
    _seed(monkeypatch)
    themes.build_shadow(run_id="sh1")
    fine = next(r for r in rawstore.get_all("relatedness_themes")
                if r["theme_id"].startswith("sh1:") and r["level"] == "fine")
    rawstore.write_rows("relatedness_themes", [{**fine, "label": "Human Name"}])
    assert cli.main(["--cutover", "--run-id", "sh1"]) == 0
    capsys.readouterr()
    assert rawstore.get("relatedness_themes", fine["theme_id"])["label"] == "Human Name"
