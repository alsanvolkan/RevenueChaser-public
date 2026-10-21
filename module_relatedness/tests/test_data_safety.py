"""Relatedness data safety (#114): atomic theme cutover, no up-front profile
wipe, empty-profile retry. Offline, synthetic data."""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness import config, engine, profiles as profiles_mod
from RevenueChaser.module_relatedness import store, themes
from RevenueChaser.module_relatedness.tests.test_themes import _profiles


def _prof(**kw):
    out = {f: [] for f in config.PROFILE_FIELDS}
    out.update(kw)
    return out


# ── C1: theme runs ──────────────────────────────────────────────────────────
def _build(run_id):
    return themes.build_shadow(candidates=(2, 3), k_medium=2, k_coarse=2,
                               run_id=run_id, profiles=_profiles())


def _members(run_id):
    return rawstore.query(
        "SELECT count(*) n FROM relatedness_theme_members "
        "WHERE starts_with(theme_id, ?)", [run_id + ":"])[0]["n"]


def _live(run_id):
    return rawstore.query(
        "SELECT count(*) n FROM ticker_themes WHERE theme_run_id = ?",
        [run_id])[0]["n"]


def test_build_keeps_older_run_data(rawstore_tmp_db):
    _build("old")
    themes.cutover_materialize("old")
    before = _members("old")
    _build("new")                      # crash here == build done, no cutover
    assert _members("old") == before and before > 0
    assert themes.current_run_id() == "old"
    assert _live("old") > 0
    tags = rawstore.query(
        "SELECT count(*) n FROM relatedness_theme_tags "
        "WHERE starts_with(theme_id, 'old:')")[0]["n"]
    assert tags > 0


def test_zero_member_run_is_refused(rawstore_tmp_db):
    _build("old")
    themes.cutover_materialize("old")
    rawstore.write_rows("theme_runs", [{"id": "empty", "created_at": "x",
                                        "k_fine": 0, "k_medium": 0, "k_coarse": 0,
                                        "jaccard": 0.0, "is_current": 0}])
    with pytest.raises(ValueError, match="no members"):
        themes.cutover_materialize("empty")
    assert themes.current_run_id() == "old" and _live("old") > 0


def test_cutover_is_one_transaction(rawstore_tmp_db, monkeypatch):
    _build("old")
    themes.cutover_materialize("old")
    old_rows = _live("old")
    _build("new")
    real = rawstore._Tx.execute

    def flaky(self, sql, params=None):
        if "'coarse'" in sql and "INSERT" in sql:
            raise RuntimeError("boom")
        return real(self, sql, params)

    monkeypatch.setattr(rawstore._Tx, "execute", flaky)
    with pytest.raises(RuntimeError):
        themes.cutover_materialize("new")
    monkeypatch.setattr(rawstore._Tx, "execute", real)
    assert themes.current_run_id() == "old"
    assert _live("old") == old_rows and _live("new") == 0


_RUN_TABLES = ("relatedness_themes", "relatedness_theme_tags", "relatedness_theme_members",
               "relatedness_theme_etfs")


def _run_counts(run_id):
    """Row counts of every table that holds ``run_id``'s rows."""
    rawstore.ensure_table("relatedness_theme_etfs")
    out = {t: rawstore.query(
        f"SELECT count(*) n FROM {t} WHERE theme_id = ? OR starts_with(theme_id, ?)",
        [run_id, run_id + ":"])[0]["n"] for t in _RUN_TABLES}
    out["ticker_themes"] = _live(run_id)
    out["theme_runs"] = rawstore.query(
        "SELECT count(*) n FROM theme_runs WHERE id = ?", [run_id])[0]["n"]
    return out


def test_cutover_keeps_older_runs(rawstore_tmp_db):
    """Card 10 / REL-1: a cutover switches the boards and keeps every older run's rows."""
    _build("old")
    themes.cutover_materialize("old")
    rawstore.write_rows("relatedness_theme_etfs", [
        {"theme_id": "old:f0", "etf": "XLK", "rank": 1, "overlap": 0.5}])
    before = _run_counts("old")
    assert all(before.values()), before
    _build("new")
    themes.cutover_materialize("new")
    assert themes.current_run_id() == "new"
    assert _live("new") > 0 and _members("new") > 0
    assert _run_counts("old") == before
    assert rawstore.get("theme_runs", "old")["is_current"] == 0
    # The boards' Theme vocabulary still reads the current run only.
    from RevenueChaser.core import universe
    cat = universe.theme_catalog()
    assert cat and all(tid.startswith("new:") for tid in cat)


def test_cutover_does_not_copy_legacy_keyword(rawstore_tmp_db):
    rawstore.replace_all("equity_universe", [{"ticker": "ZZZ", "market_cap": 1e9}])
    rawstore.write_rows("ticker_themes", [
        {"ticker": "ZZZ", "theme_id": "kw-banks", "theme": "Banks",
         "level": "legacy", "theme_run_id": "legacy-keyword"}])
    _build("r")
    themes.cutover_materialize("r")
    assert rawstore.query(
        "SELECT * FROM ticker_themes WHERE theme_run_id='r' AND ticker='ZZZ'") == []
    assert _live("legacy-keyword") == 1      # the legacy rows themselves stay


# ── C2: no up-front wipe, stale badge ───────────────────────────────────────
@pytest.fixture
def _quiet(monkeypatch):
    monkeypatch.setattr(engine, "_cik_for", lambda ts: {t: "" for t in ts})
    monkeypatch.setattr(engine, "_filing_text", lambda t: (t, "text " * 50))


def _old_mode_profile(ticker, **kw):
    ck = store.cache_key("old-model", False, "", "v0", ticker, "h")
    store.save_profile(ticker, "", _prof(**kw), ck, "old-model", prompt_version="v0")


def test_mode_change_with_limit_deletes_nothing(rawstore_tmp_db, monkeypatch, _quiet):
    for t in ("A", "B", "C"):
        _old_mode_profile(t, products=["x"])
    store.set_mode({"model": "old-model", "thinking": False, "effort": "",
                    "prompt_version": "v0"})
    monkeypatch.setattr(engine.profiles_mod, "profile_for",
                        lambda *a, **k: _prof(products=["new"]))
    got = engine.build_profiles(["A"], use_llm=True)       # a --limit 1 style run
    assert got["A"]["products"] == ["new"]
    assert set(store.load_profiles()) == {"A", "B", "C"}
    assert store.load_profiles(["B"])["B"]["products"] == ["x"]


def test_stale_profiles_are_flagged_until_reprofiled(rawstore_tmp_db):
    _old_mode_profile("A", products=["x"])
    fresh = store.mode_stamp()
    ck = store.cache_key(fresh["model"], fresh["thinking"], fresh["effort"],
                         fresh["prompt_version"], "B", "h")
    store.save_profile("B", "", _prof(products=["y"]), ck, fresh["model"])
    assert store.stale_tickers(["A", "B"]) == {"A"}


def test_report_shows_stale_badge():
    from RevenueChaser.module_relatedness import report
    payload = {"as_of": "2026-10-01T00:00:00+00:00", "model": "m", "n": 2,
               "companies": [
                   {"ticker": "AAA", "name": "A", "sector": "Technology", "market_cap": 1e9,
                    "weights": {}, "profile": {}, "stale": True},
                   {"ticker": "BBB", "name": "B", "sector": "Technology", "market_cap": 1e9,
                    "weights": {}, "profile": {}, "stale": False}],
               "peers": {}, "scores": [], "thinking_compare": {},
               "fields": list(config.PROFILE_FIELDS)}
    html = report.render_relatedness_dashboard(payload)
    assert "staleBadge(FOCAL)" in html and "staleBadge(p.peer)" in html
    assert '"stale": true' in html and '"stale": false' in html


def test_payload_marks_stale(rawstore_tmp_db):
    _old_mode_profile("A", products=["x"])
    store.save_scores({})
    payload = engine._payload({"A": store.load_profiles(["A"])["A"]}, {},
                              stale={"A"})
    assert payload["companies"][0]["stale"] is True


# ── C3: empty answers ───────────────────────────────────────────────────────
def test_fenced_json_parses():
    raw = "```json\n{\"products\": [\"gpu\"]}\n```"
    assert profiles_mod.parse_profile(raw)["products"] == ["gpu"]
    assert profiles_mod.parse_profile("```\n{\"products\": [\"a\"]}\n```")["products"] == ["a"]


def test_string_valued_fields_are_accepted():
    p = profiles_mod.parse_profile(json.dumps(
        {"products": "GPU, accelerators", "business_model": "Fabless"}))
    assert p["products"] == ["gpu", "accelerators"] and p["business_model"] == ["fabless"]


def test_empty_profile_is_not_done_and_retried_at_most_3_times(
        rawstore_tmp_db, monkeypatch, _quiet):
    calls = []
    monkeypatch.setattr(engine.profiles_mod, "profile_for",
                        lambda *a, **k: calls.append(1) or _prof())
    for _ in range(5):
        got = engine.build_profiles(["A"], use_llm=True)
    assert len(calls) == 3
    row = rawstore.get("relatedness_profiles", "A")
    assert row["status"] == "empty" and row["attempts"] == 3
    assert not any(got["A"].values())


def test_empty_retry_then_success_is_done(rawstore_tmp_db, monkeypatch, _quiet):
    answers = iter([_prof(), _prof(products=["gpu"])])
    monkeypatch.setattr(engine.profiles_mod, "profile_for",
                        lambda *a, **k: next(answers))
    engine.build_profiles(["A"], use_llm=True)
    engine.build_profiles(["A"], use_llm=True)
    assert rawstore.get("relatedness_profiles", "A")["status"] == "ok"
    calls = []
    monkeypatch.setattr(engine.profiles_mod, "profile_for",
                        lambda *a, **k: calls.append(1) or _prof(products=["z"]))
    engine.build_profiles(["A"], use_llm=True)
    assert calls == []                                    # done: not re-called


def test_empty_answer_never_overwrites_a_stale_good_profile(
        rawstore_tmp_db, monkeypatch, _quiet):
    _old_mode_profile("A", products=["x"])
    monkeypatch.setattr(engine.profiles_mod, "profile_for", lambda *a, **k: _prof())
    engine.build_profiles(["A"], use_llm=True)
    assert store.load_profiles(["A"])["A"]["products"] == ["x"]
    assert store.stale_tickers(["A"]) == {"A"}


def test_profiled_count_excludes_empty(rawstore_tmp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(engine.config, "RELATEDNESS_JSON", tmp_path / "r.json")
    monkeypatch.setattr(engine.vc_engine, "fetch_corpus", lambda *a, **k: {})
    store.save_profile("A", "", _prof(products=["gpu"]), "k", "m")
    store.save_profile("B", "", _prof(), "k", "m", status="empty")
    payload = engine.run(no_fetch=True, tickers=["A", "B"])
    assert payload["n"] == 1 and payload["pending"] == 1


# ── REL-11: the payload write is atomic ─────────────────────────────────────
class _HalfWrite:
    """A text file whose ``write`` stores half the text and then fails (a crash or a full disk)."""

    def __init__(self, f):
        self._f = f

    def write(self, text):
        self._f.write(text[: len(text) // 2])
        self._f.flush()
        raise OSError("disk full")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()
        return False


def test_an_interrupted_payload_write_leaves_the_old_file_readable(tmp_path, monkeypatch):
    import os
    import pathlib

    path = tmp_path / "relatedness_latest.json"
    old = {"as_of": "old", "companies": [{"ticker": "A"}]}
    engine._save(path, old)

    real_open, real_fdopen = pathlib.Path.open, os.fdopen
    monkeypatch.setattr(pathlib.Path, "open",
                        lambda self, *a, **k: _HalfWrite(real_open(self, *a, **k)))
    monkeypatch.setattr(os, "fdopen", lambda *a, **k: _HalfWrite(real_fdopen(*a, **k)))
    with pytest.raises(OSError):
        engine._save(path, {"as_of": "new", "companies": [{"ticker": "B"}] * 50})
    monkeypatch.undo()

    assert json.loads(path.read_text(encoding="utf-8")) == old
    assert [p.name for p in tmp_path.iterdir()] == [path.name]      # no temp file left behind
