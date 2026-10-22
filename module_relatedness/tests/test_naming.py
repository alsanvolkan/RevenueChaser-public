"""LLM theme naming (Phase 8a) — fake ``complete``, offline.

DeepSeek is never reached: ``naming.name_themes`` takes an injectable ``complete``.
"""
from __future__ import annotations

import json
import logging
import pytest
from datetime import datetime, timezone

from RevenueChaser.core import rawstore
from RevenueChaser.core.providers import deepseek
from RevenueChaser.module_relatedness import naming, themes


def _profiles():
    p = {}
    for i in range(30):
        p[f"G{i}"] = {"products": ["gpu", "ai"], "end_markets": ["data center"],
                      "business_model": ["semis"], "customer_types": ["hyperscaler"],
                      "value_chain_role": ["designer"], "keywords": ["accelerated"]}
    for i in range(30):
        p[f"B{i}"] = {"products": ["loans", "deposits"], "end_markets": ["retail"],
                      "business_model": ["interest"], "customer_types": ["consumer"],
                      "value_chain_role": ["lender"], "keywords": ["branch"]}
    return p


def _seed_run(run_id="nmf"):
    rawstore.replace_all("equity_universe",
                         [{"ticker": f"G{i}", "market_cap": 1e9} for i in range(3)])
    themes.build_shadow(candidates=(2, 3), k_medium=2, k_coarse=2,
                        run_id=run_id, profiles=_profiles())
    themes.cutover_materialize(run_id)
    return run_id


def _run_rows(run_id):
    return [r for r in rawstore.get_all("relatedness_themes")
            if str(r["theme_id"]).startswith(run_id + ":")]


def test_name_themes_writes_label_and_syncs(rawstore_tmp_db):
    run_id = _seed_run()
    calls = []

    def fake(system, user, **kw):
        calls.append(kw)
        return json.dumps({"name": "Semiconductors"})

    out = naming.name_themes(run_id, complete=fake)
    assert out["run_id"] == run_id and out["named"] > 0 and out["pending"] == 0
    med = [r for r in rawstore.get_all("relatedness_themes") if r["level"] == "medium"]
    assert med and all(r["label"].startswith("Semiconductors") for r in med)

    labels = {r["theme_id"]: r["label"] for r in med}
    tt = rawstore.query(
        "SELECT theme_id, theme FROM ticker_themes "
        "WHERE theme_run_id = ? AND level = 'medium'", [run_id])
    assert tt
    for row in tt:
        assert row["theme"] == labels[row["theme_id"]]

    # thinking-low only — never a non-thinking naming call
    assert calls and all(c.get("thinking") is True for c in calls)
    assert all(c.get("reasoning_effort") == "low" for c in calls)
    assert all(c.get("json_mode") is True for c in calls)


def test_name_themes_avoids_collisions(rawstore_tmp_db):
    run_id = _seed_run()

    def fake(system, user, **kw):
        return json.dumps({"name": "Semiconductors"})

    naming.name_themes(run_id, complete=fake)
    labels = [r["label"] for r in _run_rows(run_id) if r["label"]]
    assert len(labels) >= 2
    assert len(labels) == len(set(labels))


def test_name_themes_skips_human_label(rawstore_tmp_db):
    run_id = _seed_run()
    target = next(r for r in rawstore.get_all("relatedness_themes")
                  if r["level"] == "fine")
    target = dict(target)
    target["label"] = "Human Name"
    rawstore.write_rows("relatedness_themes", [target])

    needing = sum(1 for r in rawstore.get_all("relatedness_themes")
                  if naming._needs_name(r["label"]))
    calls = {"n": 0}

    def fake(system, user, **kw):
        calls["n"] += 1
        return json.dumps({"name": f"Theme {calls['n']}"})

    out = naming.name_themes(run_id, complete=fake)
    assert calls["n"] == needing
    assert out["named"] == needing
    assert rawstore.get("relatedness_themes", target["theme_id"])["label"] == "Human Name"


def test_name_themes_retries_without_thinking_on_empty_content(rawstore_tmp_db):
    run_id = _seed_run()
    calls = []

    def fake(system, user, **kw):
        calls.append(kw)
        if kw.get("thinking"):
            raise deepseek.DeepSeekError("DeepSeek returned no content")
        return json.dumps({"name": "Regional Banks"})

    out = naming.name_themes(run_id, complete=fake)
    assert out["named"] > 0
    assert any(c.get("thinking") is True for c in calls)
    assert any(c.get("thinking") is False for c in calls)
    med = [r for r in rawstore.get_all("relatedness_themes") if r["level"] == "medium"]
    assert med and all("Bank" in r["label"] or r["label"].startswith("Regional")
                       for r in med)


def test_name_themes_peak_keeps_written(rawstore_tmp_db):
    run_id = _seed_run()
    calls = {"n": 0}

    def fake(system, user, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise deepseek.PeakHoursError(
                datetime(2026, 9, 21, 1, 0, tzinfo=timezone.utc))
        return json.dumps({"name": f"Theme {calls['n']}"})

    out = naming.name_themes(run_id, complete=fake)
    assert calls["n"] == 2
    assert out["pending"] >= 1
    assert out["next_off_peak"] == "2026-09-21 01:00"
    labels = [r["label"] for r in rawstore.get_all("relatedness_themes") if r["label"]]
    assert any(label.startswith("Theme 1") for label in labels)


def test_needs_name_rule():
    assert naming._needs_name("")
    assert naming._needs_name("gpu, ai, data center")   # tag-dump fallback
    assert not naming._needs_name("Semiconductors")


def test_name_themes_parents_see_child_labels(rawstore_tmp_db):
    """Bottom-up: a medium/coarse prompt carries its children's written names."""
    run_id = _seed_run()
    by_level: dict[str, list[str]] = {}

    def fake(system, user, **kw):
        level = user.splitlines()[0].split(": ", 1)[1]
        by_level.setdefault(level, []).append(user)
        return json.dumps({"name": f"Name {len(by_level[level])}"})

    naming.name_themes(run_id, complete=fake)
    assert "Already-named child clusters" in by_level["medium"][0]
    assert "Already-named child clusters" in by_level["coarse"][0]


def test_cli_name_themes_prints_summary(rawstore_tmp_db, monkeypatch, capsys):
    from RevenueChaser.module_relatedness import __main__ as cli

    monkeypatch.setattr(naming, "name_themes",
                        lambda rid=None, **k: {"run_id": rid or "cur", "named": 4,
                                               "pending": 0, "next_off_peak": None})
    assert cli.main(["--name-themes", "--run-id", "r7"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out == {"run_id": "r7", "named": 4, "pending": 0, "next_off_peak": None}


def test_cli_name_themes_peak_exits_zero(rawstore_tmp_db, monkeypatch, capsys):
    from RevenueChaser.module_relatedness import __main__ as cli

    monkeypatch.setattr(naming, "name_themes",
                        lambda rid=None, **k: {"run_id": rid, "named": 1, "pending": 5,
                                               "next_off_peak": "2026-09-21 01:00"})
    assert cli.main(["--name-themes"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["next_off_peak"] == "2026-09-21 01:00"


def test_cli_rebuild_themes_cuts_over_and_names(rawstore_tmp_db, monkeypatch, capsys):
    from RevenueChaser.module_relatedness import __main__ as cli

    seen = {}
    monkeypatch.setattr(themes, "build_shadow",
                        lambda **k: {"run_id": "r9", "themes": 6, "k_fine": 2})
    monkeypatch.setattr(themes, "cutover_materialize",
                        lambda rid: seen.__setitem__("cut", rid) or {"rows": 3})
    monkeypatch.setattr(naming, "name_themes",
                        lambda rid=None, **k: {"run_id": rid, "named": 6,
                                               "pending": 0, "next_off_peak": None})

    assert cli.main(["--rebuild-themes", "--name"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["run_id"] == "r9" and out["named"] == 6 and out["rows"] == 3
    assert seen["cut"] == "r9"        # cutover switches the run atomically


# --- #118 A4 (C10 + #23 P3): auth and transport failures ---------------------------

def _labels(run_id):
    return {r["theme_id"]: r["label"] for r in _run_rows(run_id)}


def test_auth_failure_stops_at_once_and_writes_nothing(rawstore_tmp_db):
    run_id = _seed_run()
    themes.relabel_from_tags(run_id)
    before = _labels(run_id)
    calls = []

    def fake(system, user, **kw):
        calls.append(kw)
        raise deepseek.DeepSeekAuthError("rejected (HTTP 401)")

    with pytest.raises(deepseek.DeepSeekAuthError):
        naming.name_themes(run_id, complete=fake)
    assert len(calls) == 1                                   # no thinking-off retry
    assert _labels(run_id) == before                         # no "Theme fN" names

    def good(system, user, **kw):
        return json.dumps({"name": "Semiconductors"})

    out = naming.name_themes(run_id, complete=good)          # a later good run names all
    assert out["named"] == len(before) and out["pending"] == 0


def test_transport_failure_keeps_written_names_and_syncs(rawstore_tmp_db):
    run_id = _seed_run()
    calls = {"n": 0}

    def fake(system, user, **kw):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise deepseek.DeepSeekTransientError("HTTP 503")
        return json.dumps({"name": f"Name {calls['n']}"})

    out = naming.name_themes(run_id, complete=fake)
    assert out["named"] == 2 and out["pending"] >= 1 and "503" in out["error"]
    labels = _labels(run_id)
    assert sum(1 for v in labels.values() if v.startswith("Name ")) == 2
    synced = rawstore.query(
        "SELECT theme_id, theme FROM ticker_themes WHERE theme_run_id = ?", [run_id])
    assert all(r["theme"] == labels[r["theme_id"]] for r in synced)
    assert calls["n"] == 3                                   # 2 ok + the 503: stop, no retry


def test_unusable_reply_leaves_the_tag_label_pending(rawstore_tmp_db):
    run_id = _seed_run()
    themes.relabel_from_tags(run_id)
    before = _labels(run_id)
    out = naming.name_themes(run_id, complete=lambda s, u, **kw: "not json")
    assert out["named"] == 0 and out["pending"] == len(before)
    assert _labels(run_id) == before                         # still needs a name
    assert all(naming._needs_name(v) for v in before.values())


def test_cli_name_themes_reports_an_auth_failure(rawstore_tmp_db, monkeypatch, capsys):
    from RevenueChaser.module_relatedness import __main__ as cli

    def boom(rid=None, **k):
        raise deepseek.DeepSeekAuthError("DeepSeek rejected the API key")

    monkeypatch.setattr(naming, "name_themes", boom)
    assert cli.main(["--name-themes"]) == 1
    assert "rejected" in json.loads(capsys.readouterr().out.strip().splitlines()[-1])["error"]


# --- #118 A5 (C11 + C4 leftover) ---------------------------------------------------

def test_disambiguation_tag_is_title_cased_and_survives_the_scrub():
    out = naming._disambiguate("Chips", ["5g", "gpu"], {"chips"})
    assert out == "Chips (Gpu)"                       # "5g" would be scrubbed as junk
    assert themes.clean_theme_label(out) == out
    assert naming._disambiguate("Chips", [], {"chips", "chips (2)"}) == "Chips (3)"


def test_names_stay_unique_after_scrub_and_unique_pass(rawstore_tmp_db):
    run_id = _seed_run()
    naming.name_themes(run_id, complete=lambda s, u, **kw: json.dumps({"name": "Chips"}))
    themes.scrub_run_labels(run_id)
    for level in themes.LEVELS:
        themes.unique_theme_labels(run_id, level)
    for level in themes.LEVELS:
        labs = [r["label"] for r in _run_rows(run_id) if r["level"] == level]
        assert labs and len(labs) == len(set(labs)), level


def test_markup_in_a_model_name_is_rejected(rawstore_tmp_db):
    assert naming._parse_name(json.dumps({"name": "<img src=x onerror=alert(1)>"})) == ""
    assert naming._parse_name(json.dumps({"name": "Chips </script>"})) == ""
    run_id = _seed_run()
    themes.relabel_from_tags(run_id)
    out = naming.name_themes(
        run_id, complete=lambda s, u, **kw: json.dumps({"name": "<b>Chips</b>"}))
    assert out["named"] == 0
    assert not any("<" in r["label"] for r in _run_rows(run_id))


# --- alsanvolkan/RevenueChaser#433: collision suffix + the medium level -------------

def _seed_plain(theme_rows, tag_rows=()):
    """Seed the naming tables directly: full control over tags, parents and labels."""
    rawstore.ensure_table("ticker_themes")
    rawstore.write_rows("relatedness_themes", theme_rows)
    if tag_rows:
        rawstore.write_rows("relatedness_theme_tags", tag_rows)


def _seed_collision(run_id):
    """Three fine themes where the alphabetical tag and the rarest tag differ.

    ``common`` sits in all three themes, ``zzz`` only in ``f1``: for ``f1`` the
    doc-frequency order is ``zzz`` first, the alphabetical one ``common``.
    """
    _seed_plain([
        {"theme_id": f"{run_id}:f{i}", "level": "fine", "parent_id": "",
         "label": "", "size": 3} for i in range(3)
    ], [
        {"theme_id": f"{run_id}:f0", "tag": "alpha", "rank": 0},
        {"theme_id": f"{run_id}:f0", "tag": "common", "rank": 1},
        {"theme_id": f"{run_id}:f1", "tag": "common", "rank": 0},
        {"theme_id": f"{run_id}:f1", "tag": "zzz", "rank": 1},
        {"theme_id": f"{run_id}:f2", "tag": "beta", "rank": 0},
        {"theme_id": f"{run_id}:f2", "tag": "common", "rank": 1},
    ])


def _seed_medium(run_id, fine_labels):
    """One medium theme over ``fine_labels``' fine themes, which carry human names."""
    _seed_plain([
        {"theme_id": f"{run_id}:f{i}", "level": "fine",
         "parent_id": f"{run_id}:m0", "label": label, "size": 2}
        for i, label in enumerate(fine_labels)
    ] + [
        {"theme_id": f"{run_id}:m0", "level": "medium", "parent_id": "",
         "label": "", "size": 2 * len(fine_labels)},
    ])


def test_collision_suffix_follows_the_doc_frequency_order(rawstore_tmp_db):
    """#433: the suffix is the rarest free tag, not the alphabetically first one."""
    _seed_collision("coll")
    naming.name_themes("coll", complete=lambda s, u, **kw: json.dumps({"name": "Chips"}))
    labels = _labels("coll")
    assert labels["coll:f1"] == "Chips (Zzz)"        # in 1 theme
    assert labels["coll:f1"] != "Chips (Common)"     # in all 3


def test_every_taken_tag_falls_back_to_a_number(rawstore_tmp_db):
    used = {"chips", "chips (asic)", "chips (gpu)"}
    assert naming._disambiguate("Chips", ["asic", "gpu"], used) == "Chips (2)"
    used.add("chips (2)")
    assert naming._disambiguate("Chips", ["asic", "gpu"], used) == "Chips (3)"


def test_no_collision_keeps_the_model_name_unchanged(rawstore_tmp_db):
    assert naming._disambiguate("Chips", ["gpu"], set()) == "Chips"
    _seed_plain([{"theme_id": "solo-name:f0", "level": "fine",
                  "parent_id": "", "label": "", "size": 2}])
    naming.name_themes("solo-name",
                       complete=lambda s, u, **kw: json.dumps({"name": "Chips"}))
    assert _labels("solo-name")["solo-name:f0"] == "Chips"


def test_naming_is_deterministic_across_identical_runs(rawstore_tmp_db):
    seen = []
    for run_id in ("det-a", "det-b"):
        _seed_collision(run_id)
        naming.name_themes(run_id,
                           complete=lambda s, u, **kw: json.dumps({"name": "Chips"}))
        seen.append({tid.split(":", 1)[1]: label
                     for tid, label in _labels(run_id).items()})
    assert seen[0] == seen[1]


def test_k_medium_is_clamped_below_the_fine_count(rawstore_tmp_db, caplog):
    with caplog.at_level(logging.INFO):
        out = themes.build_shadow(candidates=(2, 3), k_medium=5, k_coarse=2,
                                  run_id="clamp", profiles=_profiles())
    assert out["k_fine"] >= 2
    # not 5: the clamp keeps the cut below the fine count, which merges one pair of fines and
    # leaves the rest with a single fine child (#433)
    assert out["k_medium"] == out["k_fine"] - 1
    assert rawstore.get("theme_runs", "clamp")["k_medium"] == out["k_medium"]
    msgs = [r.getMessage() for r in caplog.records if "k_medium" in r.getMessage()]
    assert len(msgs) == 1                            # logged once, not per medium theme
    assert "5" in msgs[0] and str(out["k_medium"]) in msgs[0]
    assert "realised" in msgs[0]                     # the log names the fine count the clamp read


def test_single_child_medium_reuses_the_fine_name_without_a_call(rawstore_tmp_db):
    _seed_medium("solo", ["Semiconductors"])
    calls = []

    def fake(system, user, **kw):
        calls.append(user)
        return json.dumps({"name": "Chips"})

    out = naming.name_themes("solo", complete=fake)
    assert calls == []                               # never a model call for a copy
    assert _labels("solo")["solo:m0"] == "Semiconductors"
    assert out["named"] == 1 and out["pending"] == 0


def test_medium_with_two_children_still_asks_the_model_once(rawstore_tmp_db):
    _seed_medium("pair", ["Semiconductors", "Regional Banks"])
    calls = []

    def fake(system, user, **kw):
        calls.append(user)
        return json.dumps({"name": "Merged Business"})

    naming.name_themes("pair", complete=fake)
    assert len(calls) == 1
    assert _labels("pair")["pair:m0"] == "Merged Business"


def test_single_child_medium_copies_a_name_written_in_the_same_pass(rawstore_tmp_db):
    """#433: the fine child is named first in the pass (``LEVEL_ORDER``), and the medium
    theme then reuses that name without a second call."""
    _seed_plain([
        {"theme_id": "same:f0", "level": "fine", "parent_id": "same:m0",
         "label": "", "size": 2},
        {"theme_id": "same:m0", "level": "medium", "parent_id": "",
         "label": "", "size": 2},
    ])
    calls = []

    def fake(system, user, **kw):
        calls.append(user)
        return json.dumps({"name": "Chips"})

    out = naming.name_themes("same", complete=fake)
    assert len(calls) == 1                           # the fine child only
    assert _labels("same") == {"same:f0": "Chips", "same:m0": "Chips"}
    assert out["named"] == 2 and out["pending"] == 0
    assert out["copied"] == 1                        # the copy is counted apart from model names


def test_an_unnamed_child_falls_through_to_the_model(rawstore_tmp_db):
    """#433: the copy rule needs a name to copy. A fine child whose call returned nothing is
    still unnamed when the medium theme is reached, so the medium theme is asked."""
    _seed_plain([
        {"theme_id": "fall:f0", "level": "fine", "parent_id": "fall:m0",
         "label": "", "size": 2},
        {"theme_id": "fall:m0", "level": "medium", "parent_id": "",
         "label": "", "size": 2},
    ])
    asked = []

    def fake(system, user, **kw):
        asked.append(user)
        if "Level: fine" in user:
            return "not json"                        # the fine child stays unnamed
        return json.dumps({"name": "Merged Business"})

    out = naming.name_themes("fall", complete=fake)
    assert len(asked) == 2                           # the child and then the medium theme
    assert _labels("fall") == {"fall:f0": "", "fall:m0": "Merged Business"}
    assert out["named"] == 1 and out["copied"] == 0
