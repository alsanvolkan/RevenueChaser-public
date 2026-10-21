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


def test_finalize_run_uniques_fine_and_medium_duplicates(rawstore_tmp_db, monkeypatch):
    _seed(monkeypatch)
    themes.build_shadow(run_id="rb6")
    rows = [r for r in rawstore.get_all("relatedness_themes")
            if r["theme_id"].startswith("rb6:") and r["level"] in ("fine", "medium")]
    rawstore.write_rows("relatedness_themes", [{**r, "label": "Same Name"} for r in rows])
    themes.finalize_run("rb6")
    for level in ("fine", "medium"):
        labs = [r["label"] for r in rawstore.get_all("relatedness_themes")
                if r["theme_id"].startswith("rb6:") and r["level"] == level]
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
