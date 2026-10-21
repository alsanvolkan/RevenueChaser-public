"""A cross-site page cannot trigger a Refresh (#175 review).

``GET /view/<module>?refresh=1`` drops the cached board and rebuilds it from the store, and a
GET carries no Origin and needs no ``X-RC-Panel`` header: an ``<img src=".../view/x?refresh=1">`` on
any page the user visits would run it. The browser says where the request came from in
``Sec-Fetch-Site``; ``/discord/media`` already refuses ``cross-site``. Refresh now refuses
``cross-site`` and ``same-site`` (another localhost port is same-site, #231). ``same-origin`` (the
shell's iframes) and ``none`` (the address bar) still work, and so does a client that sends no
header at all (curl, the test client).
"""
from __future__ import annotations

import pytest

from RevenueChaser.app import dashboard

# (module, the runner or hook its refresh branch calls)
BRANCHES = [
    ("module_relatedness", "model"),          # generic: drop the artifact, rebuild from the store
    ("module_financials", "run_quick_cached"),
    ("module_oipd", "run_oipd_cached"),
    ("module_comovement", "run_comovement"),
    ("module_spx_gamma", "run_spxgamma_cached"),
]


@pytest.fixture
def board(tmp_path, monkeypatch, rawstore_tmp_db):
    """One cached artifact for every module, and spies where a refresh would rebuild."""
    artifact = tmp_path / "board.html"
    artifact.write_text("<html>cached</html>", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(dashboard, "_module_file", lambda mod: artifact)
    monkeypatch.setattr(dashboard, "_STORE_REFRESHABLE", frozenset({"module_relatedness"}))
    monkeypatch.setattr(dashboard, "_MODEL_ONLY_RENDERERS",
                        {"module_relatedness": lambda: calls.append("model")})
    for name in ("run_quick_cached", "run_oipd_cached", "run_comovement", "run_spxgamma_cached"):
        monkeypatch.setattr(dashboard.runners, name, lambda *a, _n=name, **k: calls.append(_n))
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client(), artifact, calls


@pytest.mark.parametrize("site", ["cross-site", "same-site", "Cross-Site"])
@pytest.mark.parametrize("mod,_runner", BRANCHES)
def test_a_cross_origin_refresh_rebuilds_nothing(board, mod, _runner, site):
    client, artifact, calls = board
    r = client.get(f"/view/{mod}?refresh=1", headers={"Sec-Fetch-Site": site})
    assert r.status_code == 403
    assert calls == [] and artifact.exists(), "the refresh ran anyway"


@pytest.mark.parametrize("headers", [{"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"}, {}])
@pytest.mark.parametrize("mod,runner", BRANCHES)
def test_a_refresh_from_the_shell_the_address_bar_or_curl_still_runs(board, mod, runner, headers):
    client, artifact, calls = board
    assert client.get(f"/view/{mod}?refresh=1", headers=headers).status_code != 403
    assert calls == [runner]


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_a_plain_cross_site_load_of_the_cached_board_is_not_touched(board, site):
    """Only the rebuild is refused; showing the cached file is what a plain load always did."""
    client, artifact, calls = board
    r = client.get("/view/module_relatedness", headers={"Sec-Fetch-Site": site})
    assert r.status_code == 200 and b"cached" in r.data
    assert calls == [] and artifact.exists()
