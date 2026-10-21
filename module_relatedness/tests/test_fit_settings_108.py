"""#108 S1 pre-slice (#388): the NMF fit of a theme run is a setting, no longer hardcoded nndsvd / 60 iterations.

- The default is the best S4 tuning variant (nndsvda init, 500 iterations); avo chose to run S1 with it although it
  stays below the pre-registered S4 bars (card "Kuralı gevşet", 2026-10-15).
- Every fit of one run uses the same setting: the k candidates, the seed-stability runs and the final fine fit.
- ``build_shadow`` and the ``--rebuild-themes`` summary name the fit, so a run's settings are on record.
Offline; the store is a temp DuckDB, no LLM call.
"""
from __future__ import annotations

import functools
import json

import numpy as np
import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness import __main__ as cli
from RevenueChaser.module_relatedness import themes
from RevenueChaser.module_relatedness.tests.test_themes import _half


@pytest.fixture
def nmf_spy(monkeypatch):
    """Record the keyword arguments of every MiniBatchNMF that a fit constructs; the real class still fits."""
    import sklearn.decomposition as decomposition
    real = decomposition.MiniBatchNMF
    made = []

    def spy(**kwargs):
        made.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(decomposition, "MiniBatchNMF", spy)
    return made


def _fits(made):
    return {(m["init"], m["max_iter"]) for m in made}


# ── the setting ─────────────────────────────────────────────────────────────
def test_the_default_fit_is_the_best_s4_variant():
    assert themes.DEFAULT_FIT == themes.NmfFit(init="nndsvda", max_iter=500)


@pytest.mark.parametrize("init, max_iter", [("custom", 500), ("NNDSVDA", 500), ("", 500),
                                            ("nndsvda", 0), ("nndsvda", -1), ("nndsvda", 2.5),
                                            ("nndsvda", True)])
def test_a_fit_sklearn_cannot_run_is_refused(init, max_iter):
    with pytest.raises(ValueError):
        themes.NmfFit(init=init, max_iter=max_iter)


def test_nmf_assign_fits_with_the_given_setting(nmf_spy):
    X = np.abs(np.random.RandomState(0).rand(40, 6))
    themes.nmf_assign(X, 3, seed=4)
    themes.nmf_assign(X, 3, seed=4, fit=themes.NmfFit(init="nndsvd", max_iter=60))
    assert [(m["init"], m["max_iter"], m["random_state"], m["n_components"], m["batch_size"]) for m in nmf_spy] == [
        ("nndsvda", 500, 4, 3, 512), ("nndsvd", 60, 4, 3, 512)]


# ── one run, one fit ────────────────────────────────────────────────────────
def test_every_fit_of_a_run_uses_the_default(rawstore_tmp_db, nmf_spy):
    out = themes.build_shadow(run_id="d", profiles=_half(30), candidates=(2, 3), k_medium=2, k_coarse=2)
    assert "error" not in out
    # 2 candidates x (1 size check + SEEDS stability fits) + the final fine fit
    assert len(nmf_spy) == 2 * (1 + themes.SEEDS) + 1
    assert _fits(nmf_spy) == {("nndsvda", 500)}
    assert out["fit"] == {"init": "nndsvda", "max_iter": 500}


def test_every_fit_of_a_run_uses_the_chosen_setting(rawstore_tmp_db, nmf_spy):
    out = themes.build_shadow(run_id="old", profiles=_half(30), candidates=(2, 3), k_medium=2, k_coarse=2,
                              fit=themes.NmfFit(init="nndsvd", max_iter=60))
    assert len(nmf_spy) == 2 * (1 + themes.SEEDS) + 1
    assert _fits(nmf_spy) == {("nndsvd", 60)}
    assert out["fit"] == {"init": "nndsvd", "max_iter": 60}
    assert rawstore.get("theme_runs", "old")["k_fine"] == out["k_fine"]


def test_choose_k_and_stability_take_the_setting(nmf_spy):
    X = np.abs(np.random.RandomState(2).rand(60, 8))
    fit = themes.NmfFit(init="random", max_iter=7)
    themes.stability(X, 2, seeds=2, fit=fit)
    themes.choose_k(X, [2, 3], seeds=2, fit=fit)
    assert len(nmf_spy) == 2 + 2 * (1 + 2) and _fits(nmf_spy) == {("random", 7)}


def test_the_fallback_k_is_fitted_with_the_setting_too(nmf_spy):
    """No usable candidate: the size-based k's stability still uses the chosen fit."""
    X = np.abs(np.random.RandomState(1).rand(50, 8))
    themes.choose_k(X, [60], seeds=2, fit=themes.NmfFit(init="nndsvdar", max_iter=9))
    assert nmf_spy and _fits(nmf_spy) == {("nndsvdar", 9)}


# ── CLI ─────────────────────────────────────────────────────────────────────
@pytest.fixture
def rebuild_spy(rawstore_tmp_db, monkeypatch):
    calls = []
    real = functools.partial(themes.build_shadow, candidates=(2, 3), k_medium=2, k_coarse=2, profiles=_half(30))

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(themes, "build_shadow", spy)
    return calls


def _last_json(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_rebuild_without_fit_flags_uses_the_default(rebuild_spy, capsys):
    assert cli.main(["--rebuild-themes", "--run-id", "s1", "--build-only"]) == 0
    assert rebuild_spy[-1]["fit"] == themes.DEFAULT_FIT
    assert _last_json(capsys)["fit"] == {"init": "nndsvda", "max_iter": 500}


def test_rebuild_fit_flags_choose_the_fit(rebuild_spy, capsys):
    assert cli.main(["--rebuild-themes", "--run-id", "s1old", "--build-only",
                     "--nmf-init", "nndsvd", "--nmf-max-iter", "60"]) == 0
    assert rebuild_spy[-1]["fit"] == themes.NmfFit(init="nndsvd", max_iter=60)
    assert _last_json(capsys)["fit"] == {"init": "nndsvd", "max_iter": 60}


def test_a_full_rebuild_names_the_fit_too(rebuild_spy, capsys):
    assert cli.main(["--rebuild-themes", "--run-id", "full", "--nmf-max-iter", "80"]) == 0
    assert _last_json(capsys)["fit"] == {"init": "nndsvda", "max_iter": 80}
    assert themes.current_run_id() == "full"


@pytest.mark.parametrize("argv", [
    ["--rebuild-themes", "--run-id", "x", "--nmf-init", "custom"],
    ["--rebuild-themes", "--run-id", "x", "--nmf-max-iter", "0"],
    ["--rebuild-themes", "--run-id", "x", "--nmf-max-iter", "1.5"],
    ["--run", "--nmf-init", "nndsvd"],                     # only a rebuild fits themes
    ["--cutover", "--run-id", "x", "--nmf-max-iter", "60"],
    ["--name-themes", "--nmf-init", "nndsvd"],
    ["--status", "--nmf-max-iter", "60"],
])
def test_fit_flags_outside_a_rebuild_or_out_of_range_are_refused(rebuild_spy, monkeypatch, argv):
    from RevenueChaser.module_relatedness import engine
    runs = []
    monkeypatch.setattr(engine, "run", lambda **k: runs.append(k) or {})
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2 and runs == [] and rebuild_spy == []
    assert rawstore.get("theme_runs", "x") is None
