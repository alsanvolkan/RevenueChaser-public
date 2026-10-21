"""LLM theme naming (Phase 8a) — fake ``complete``, offline.

DeepSeek is never reached: ``naming.name_themes`` takes an injectable ``complete``.
"""
from __future__ import annotations

import json
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
