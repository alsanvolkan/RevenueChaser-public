"""#108 S0 (#388, CL-A): the theme fit is scoped to a universe, the CLIs take a floor or a ticker list, and
``--rebuild-themes`` has a build-only option.

- ``build_shadow`` fits only the classification universe when it loads the stored profiles (the live run held 180
  members outside it, #108 re-review); a caller can pass another ``universe``.
- ``--floor`` (USD millions) and ``--tickers`` choose the names of ``--run`` and ``--rebuild-themes``. Without
  either, the selection is exactly today's (``engine.select_pilot``, the $300M classification universe).
- ``--build-only`` writes the shadow run and stops: no cutover, the boards keep the current run.
Offline; the store is a temp DuckDB, no LLM call.
"""
from __future__ import annotations

import functools
import json

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness import __main__ as cli
from RevenueChaser.module_relatedness import config, engine, store, themes
from RevenueChaser.module_relatedness.tests.test_themes import _profiles4 as _profiles

SMALL, STRAY = "SMALL", "STRAY"


def _seed(caps: dict[str, float]) -> None:
    rawstore.write_rows("equity_universe", [
        {"ticker": t, "name": t + " Inc", "market_cap": cap, "sector": "Technology"} for t, cap in caps.items()])


@pytest.fixture
def stored(rawstore_tmp_db, monkeypatch):
    """100 names at $10B, SMALL at $100M and STRAY with no universe row at all, all profiled.

    There is no market-cap floor any more (avo, 29 Oct 2026), so SMALL is in the universe like the rest;
    only STRAY is outside it. A floor asked for explicitly still leaves SMALL out, which the --floor case below
    checks.
    """
    profs = _profiles()
    _seed({t: 1e10 for t in profs} | {SMALL: 1e8})
    profs[SMALL] = dict(profs["G0"])
    profs[STRAY] = dict(profs["B0"])
    for t, p in profs.items():
        store.save_profile(t, "", p, "k", config.PROMPT_VERSION)
    monkeypatch.setattr(themes, "build_shadow", functools.partial(
        themes.build_shadow, candidates=(2, 3), k_medium=2, k_coarse=2))
    return profs


def _members(run_id):
    return {r["ticker"] for r in rawstore.query(
        "SELECT ticker FROM relatedness_theme_members WHERE starts_with(theme_id, ?)", [run_id + ":"])}


# ── build_shadow(universe=) ─────────────────────────────────────────────────
def test_the_stored_fit_leaves_out_profiles_outside_the_universe(stored):
    out = themes.build_shadow(run_id="s0")
    got = _members("s0")
    assert got == set(stored) - {STRAY}                   # SMALL is in the universe: no floor
    assert out["n"] == 101 and out["out_of_universe"] == 1


def test_an_explicit_floor_narrows_the_fit(stored):
    """The floor is no longer the default, but a caller that asks for one still gets it."""
    from RevenueChaser.core.universe import classification_universe
    themes.build_shadow(run_id="s1", universe=classification_universe(300e6))
    assert SMALL not in _members("s1") and STRAY not in _members("s1")


def test_explicit_profiles_are_narrowed_only_by_an_explicit_universe(stored):
    profs = _profiles() | {STRAY: dict(_profiles()["B0"])}
    themes.build_shadow(run_id="s2", profiles=profs)
    assert STRAY in _members("s2")                        # the caller chose its names
    themes.build_shadow(run_id="s3", profiles=profs, universe=list(_profiles()))
    assert STRAY not in _members("s3")


# ── CLI selection ───────────────────────────────────────────────────────────
@pytest.fixture
def run_spy(rawstore_tmp_db, monkeypatch):
    _seed({"BIG": 1e10, "MID": 1e8, "TINY": 1e7})
    calls = []
    monkeypatch.setattr(engine, "run", lambda **k: calls.append(k) or {"n": 0, "pending": 0})
    return calls


def test_a_run_with_no_new_argument_selects_exactly_today(run_spy):
    assert cli.main(["--run"]) == 0
    (call,) = run_spy
    assert call.get("tickers") is None                    # engine.select_pilot(): the whole universe
    assert engine.select_pilot() == ["BIG", "MID", "TINY"]   # no floor: every name, cap-descending


def test_floor_50_narrows_the_selection_to_the_names_above_it(run_spy):
    """With no default floor the flag takes names away (TINY at $10M), it no longer adds them."""
    assert cli.main(["--run", "--floor", "50"]) == 0
    assert run_spy[-1]["tickers"] == ["BIG", "MID"]


def test_tickers_pick_the_names(run_spy):
    assert cli.main(["--run", "--tickers", "mid, big"]) == 0
    assert run_spy[-1]["tickers"] == ["MID", "BIG"]


@pytest.mark.parametrize("argv", [["--run", "--floor", "0"], ["--run", "--floor", "-5"],
                                  ["--run", "--floor", "50", "--tickers", "BIG"]])
def test_a_bad_selection_is_refused(run_spy, argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2 and run_spy == []


# ── --rebuild-themes --build-only ───────────────────────────────────────────
def _live_state():
    return (themes.current_run_id(),
            rawstore.query("SELECT ticker, theme_id, theme, level, theme_run_id FROM ticker_themes "
                           "ORDER BY ALL"))


def test_build_only_writes_the_shadow_run_and_keeps_the_boards(stored, capsys):
    assert cli.main(["--rebuild-themes", "--run-id", "live"]) == 0
    before = _live_state()
    assert before[0] == "live" and before[1]
    capsys.readouterr()
    assert cli.main(["--rebuild-themes", "--run-id", "shadow", "--build-only"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["run_id"] == "shadow" and out["themes"] > 0 and out.get("rows") is None
    assert _live_state() == before                        # is_current and ticker_themes unchanged
    assert _members("shadow") == set(stored) - {STRAY}
    assert rawstore.get("theme_runs", "shadow")["is_current"] == 0
    # The shadow is a normal --cutover input afterwards.
    assert cli.main(["--cutover", "--run-id", "shadow"]) == 0
    assert themes.current_run_id() == "shadow"


@pytest.mark.parametrize("argv", [
    ["--rebuild-themes", "--run-id", "x", "--build-only", "--name"],
    ["--rebuild-themes", "--run-id", "x", "--build-only", "--adopt-medium-from", "old"],
    ["--run", "--build-only"],                            # without --rebuild-themes it would run the paid job
    ["--cutover", "--run-id", "x", "--build-only"],
])
def test_build_only_misuse_is_refused(stored, monkeypatch, argv):
    runs = []
    monkeypatch.setattr(engine, "run", lambda **k: runs.append(k) or {})
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2 and runs == [] and rawstore.get("theme_runs", "x") is None


@pytest.mark.parametrize("argv", [
    ["--status", "--floor", "50"],
    ["--name-themes", "--tickers", "BIG"],
    ["--cutover", "--run-id", "x", "--floor", "50"],
    ["--floor", "50"],                                     # no command at all
])
def test_a_selection_without_run_or_rebuild_is_refused(run_spy, argv):
    """F3 (#406 review): a selection that the command would silently ignore is refused."""
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2 and run_spy == []


def test_the_help_warns_what_a_selected_run_does(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "full Fetch of exactly these names" in text and "replaces every peer score" in text


def test_rebuild_with_a_floor_fits_the_narrower_universe(stored):
    """$1B leaves SMALL ($100M) out; without the flag it would be in, because there is no default floor."""
    assert cli.main(["--rebuild-themes", "--run-id", "wide", "--build-only", "--floor", "1000"]) == 0
    assert SMALL not in _members("wide") and STRAY not in _members("wide")


@pytest.mark.parametrize("argv", [["--run", "--tickers", ","], ["--run", "--floor", "1e9"],
                                  ["--rebuild-themes", "--run-id", "e", "--floor", "1e9"]])
def test_an_empty_selection_is_refused(run_spy, argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2 and run_spy == [] and rawstore.get("theme_runs", "e") is None
