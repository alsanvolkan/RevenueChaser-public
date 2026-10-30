"""``--k-medium``: the medium cut as a CLI setting, so #57's sweep needs no code at run time.

The engine has taken ``k_medium`` since #433, but no caller passed one and the CLI had no flag, so the
live default -- ``max(2, n_fine // 2)`` -- could not be compared against anything without editing the
source. These tests cover the flag, the clamp it reports, and the refusals.
"""
from __future__ import annotations

import functools
import json

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness import __main__ as cli
from RevenueChaser.module_relatedness import themes


def _profiles():
    """Four well-separated 25-name clusters, so the fit realises four fine themes."""
    specs = (
        ("G", ["gpu", "ai"], ["data center"], ["semis"], ["hyperscaler"], ["designer"], ["accelerated"]),
        ("B", ["loans", "deposits"], ["retail"], ["interest"], ["consumer"], ["lender"], ["branch"]),
        ("P", ["crude", "refining"], ["transport"], ["integrated"], ["utility"], ["producer"], ["upstream"]),
        ("H", ["drugs", "biologics"], ["patients"], ["royalty"], ["hospital"], ["manufacturer"], ["clinical"]),
    )
    return {f"{pre}{i}": {"products": prod, "end_markets": mkt, "business_model": bm,
                          "customer_types": cust, "value_chain_role": vcr, "keywords": kw}
            for pre, prod, mkt, bm, cust, vcr, kw in specs for i in range(25)}


@pytest.fixture
def rebuild_spy(rawstore_tmp_db, monkeypatch):
    """``build_shadow`` as the CLI calls it, over fixed profiles; the spy keeps the kwargs."""
    calls = []
    real = functools.partial(themes.build_shadow, candidates=(2, 3, 4), k_coarse=2, profiles=_profiles())

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(themes, "build_shadow", spy)
    return calls


def _last_json(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_without_the_flag_the_medium_cut_is_still_the_derived_half(rebuild_spy, capsys):
    """The live default is unchanged: no ``k_medium``, so the engine derives ``max(2, n_fine // 2)``."""
    assert cli.main(["--rebuild-themes", "--run-id", "km-default", "--build-only"]) == 0
    assert rebuild_spy[-1]["k_medium"] is None
    out = _last_json(capsys)
    assert out["k_fine"] == 4 and out["k_medium"] == 2
    assert out["k_medium_requested"] == "default"


def test_the_flag_reaches_the_engine_and_the_summary_names_it(rebuild_spy, capsys):
    assert cli.main(["--rebuild-themes", "--run-id", "km3", "--build-only", "--k-medium", "3"]) == 0
    assert rebuild_spy[-1]["k_medium"] == 3
    out = _last_json(capsys)
    assert out["k_fine"] == 4 and out["k_medium"] == 3 and out["k_medium_requested"] == 3


def test_a_value_at_or_above_the_fine_count_is_clamped_and_the_summary_shows_both(rebuild_spy, capsys):
    """``n_fine`` itself is one of #57's three settings, so the clamp has to be visible, not silent."""
    assert cli.main(["--rebuild-themes", "--run-id", "km9", "--build-only", "--k-medium", "9"]) == 0
    out = _last_json(capsys)
    assert out["k_medium_requested"] == 9 and out["k_medium"] == out["k_fine"] - 1 == 3


def test_the_run_row_keeps_the_realised_cut(rebuild_spy):
    assert cli.main(["--rebuild-themes", "--run-id", "km-row", "--build-only", "--k-medium", "3"]) == 0
    assert rawstore.get("theme_runs", "km-row")["k_medium"] == 3


@pytest.mark.parametrize("argv", [
    ["--rebuild-themes", "--run-id", "x", "--k-medium", "0"],
    ["--rebuild-themes", "--run-id", "x", "--k-medium", "-1"],
    ["--rebuild-themes", "--run-id", "x", "--k-medium", "2.5"],
    ["--run", "--k-medium", "3"],                       # only a rebuild fits themes
    ["--cutover", "--run-id", "x", "--k-medium", "3"],
    ["--name-themes", "--k-medium", "3"],
    ["--status", "--k-medium", "3"],
])
def test_out_of_range_or_outside_a_rebuild_is_refused(rebuild_spy, monkeypatch, argv):
    from RevenueChaser.module_relatedness import engine
    runs = []
    monkeypatch.setattr(engine, "run", lambda **k: runs.append(k) or {})
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2 and runs == [] and rebuild_spy == []
    assert rawstore.get("theme_runs", "x") is None
