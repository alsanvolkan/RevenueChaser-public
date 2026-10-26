"""The e2e tier runs under ``pytest -n 4``: two dashboards in flight at once must never share a port or a folder.

Every xdist worker inherits the controller's data roots (root ``conftest._redirect_data_roots``), so a root the
dashboard fixture does not set itself is ONE folder for all four workers. And a port picked by binding to 0 and
closing is free only until someone else binds it: another worker's dashboard or mock server can take it before
the dashboard subprocess does, and that dashboard then exits with "address in use".
"""
from __future__ import annotations

import socket
from pathlib import Path

from .harness import dashboard_driver
from .harness.dashboard_driver import Dashboard

# Every folder the dashboard subprocess writes to outside .store/.cache.
SIDE_ROOTS = ("RC_EDGAR_DOCS_DIR", "RC_DISCORD_MEDIA_DIR", "RAW_STORE_BACKUP_DIR", "SPX_GAMMA_DATA_ROOT",
              "RC_PATREON_DATA_DIR")


def test_every_side_root_of_the_dashboard_is_the_tests_own(dashboard, tmp_path):
    for var in SIDE_ROOTS:
        assert var in dashboard.env, f"{var} would be inherited: one folder shared by every xdist worker"
        assert Path(dashboard.env[var]).is_relative_to(tmp_path), (var, dashboard.env[var])


def test_a_port_taken_before_the_dashboard_binds_it_cannot_break_startup(e2e_dirs, e2e_env, tmp_path,
                                                                         monkeypatch):
    store, cache = e2e_dirs
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        # The race, made deterministic: whatever port the harness might pick ahead of time is already bound.
        monkeypatch.setattr(dashboard_driver, "_free_port", lambda: taken, raising=False)
        dash = Dashboard(store_dir=store, cache_dir=cache, env=dict(e2e_env), log_path=tmp_path / "dashboard.log")
        try:
            dash.start()
            assert dash.port != taken
            assert dash.get("/").status_code == 200
            dash.restart()
            assert dash.port != taken
            assert dash.get("/").status_code == 200
        finally:
            dash.stop()
