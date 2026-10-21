"""The shared rule (C22, Common K2 #383 R1): no Refresh path (``no_fetch``, the store-only renderers) reaches
``core/history.record_run``.

Only a Fetch records a run (06 rule 9). Every store-only renderer the dashboard serves on a Refresh or a missing
artifact (``dashboard._MODEL_ONLY_RENDERERS``, the 13F, SPX Gamma and Rotation self-heals) runs here with a
``record_run`` spy. The spy only counts: a runner turns a history failure into a job note, so a raising spy could
be swallowed. Offline, temp store.
"""
from __future__ import annotations

import pytest

from RevenueChaser.app import dashboard, runners
from RevenueChaser.core import history, rawstore


@pytest.fixture
def recorded(rawstore_tmp_db, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(history, "record_run", lambda board, **k: calls.append(board) or {})
    return calls


def _refresh_paths():
    paths = dict(dashboard._MODEL_ONLY_RENDERERS)
    paths["module_sec_13f"] = lambda: runners.run_sec_13f_latest(None)
    paths["module_spx_gamma"] = runners.run_spxgamma_cached
    paths["module_sector_rotation"] = dashboard._render_rotation_dashboard
    paths["module_comovement:horizon"] = lambda: runners.run_comovement(None, no_fetch=True, horizon=5)
    return paths


@pytest.mark.parametrize("name", sorted(_refresh_paths()))
def test_a_refresh_path_never_records_a_run(recorded, name):
    try:
        _refresh_paths()[name]()
    except Exception:  # noqa: BLE001 - an empty store may end a renderer early; only a recorded run fails here
        pass
    assert recorded == [], f"{name} recorded a run on a Refresh"
    assert not rawstore.history_db_path().exists()


def test_every_board_that_records_has_a_refresh_path_here():
    """A board that writes history and has a store-only renderer is covered above."""
    covered = {k.split(":")[0] for k in _refresh_paths()}
    writers = {s["board"] for s in rawstore.MANIFEST.values() if s.get("db") == "history" and "board" in s}
    live_only = {"module_earnings_radar", "module_money_flow"}      # no store-only renderer: Fetch only
    assert writers - live_only <= covered
