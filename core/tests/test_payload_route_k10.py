"""K10 (#383 C6a): a board's data can live outside its HTML, served pre-compressed.

- ``reporting_common.write_board`` writes the HTML and, with a payload, ``<stem>.payload.json.gz`` next to it
  (gzip, card F); without one it drops a stale payload file, so a board that stops using it leaves none behind.
- ``GET /view/<mod>/payload.json`` serves that file as it is with ``Content-Encoding: gzip`` (no new package,
  no compression per request); a client that does not accept gzip gets the plain JSON.
- Refresh rewrites or drops both files: every Refresh branch unlinks the payload with the HTML (dashboard.py
  used to unlink the HTML only), so a renderer that writes no payload leaves none.
- ``PAYLOAD_JS`` is the page side: ``rcPayload(mod)`` fetches that route.
No board moves onto it in R2a. Offline, temp folders.
"""
from __future__ import annotations

import gzip
import json
import shutil
import subprocess

import pytest

from RevenueChaser.app import dashboard
from RevenueChaser.core import reporting_common as rc

PAYLOAD = {"rows": [{"ticker": "AAA", "name": "Ä & <b>"}], "n": 1}


def test_write_board_writes_the_html_and_the_gzip_payload_next_to_it(tmp_path):
    html = tmp_path / "x_latest.html"
    rc.write_board(html, "<html>x</html>", PAYLOAD)
    assert html.read_text(encoding="utf-8") == "<html>x</html>"
    pp = tmp_path / "x_latest.payload.json.gz"
    assert rc.payload_path(html) == pp
    assert json.loads(gzip.decompress(pp.read_bytes()).decode("utf-8")) == PAYLOAD


def test_write_board_without_a_payload_drops_a_stale_one(tmp_path):
    html = tmp_path / "x_latest.html"
    rc.write_board(html, "<html>1</html>", PAYLOAD)
    rc.write_board(html, "<html>2</html>")
    assert html.read_text(encoding="utf-8") == "<html>2</html>"
    assert not rc.payload_path(html).exists()


def test_the_payload_file_is_the_same_bytes_for_the_same_data(tmp_path):
    a, b = tmp_path / "a.html", tmp_path / "b.html"
    rc.write_board(a, "", PAYLOAD)
    rc.write_board(b, "", PAYLOAD)
    assert rc.payload_path(a).read_bytes() == rc.payload_path(b).read_bytes()  # gzip mtime fixed


@pytest.fixture
def board(tmp_path, monkeypatch, rawstore_tmp_db):
    html = tmp_path / "relatedness_latest.html"
    monkeypatch.setattr(dashboard, "_module_file",
                        lambda mod: html if mod == "module_relatedness" else None)
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client(), html


def test_the_route_serves_the_payload_precompressed(board):
    client, html = board
    rc.write_board(html, "<html>x</html>", PAYLOAD)
    r = client.get("/view/module_relatedness/payload.json", headers={"Accept-Encoding": "gzip, deflate"})
    assert r.status_code == 200
    assert r.headers["Content-Encoding"] == "gzip"
    assert r.headers["Content-Type"].startswith("application/json")
    assert "Accept-Encoding" in r.headers.get("Vary", "")
    assert r.headers.get("Cache-Control", "").startswith("no-store")
    assert r.data == rc.payload_path(html).read_bytes(), "served as stored, not compressed per request"
    assert json.loads(gzip.decompress(r.data)) == PAYLOAD


def test_a_client_without_gzip_gets_plain_json(board):
    client, html = board
    rc.write_board(html, "<html>x</html>", PAYLOAD)
    r = client.get("/view/module_relatedness/payload.json", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert "Content-Encoding" not in r.headers
    assert json.loads(r.data) == PAYLOAD


def test_no_payload_and_unknown_board_are_404(board):
    client, html = board
    rc.write_board(html, "<html>x</html>")
    assert client.get("/view/module_relatedness/payload.json").status_code == 404
    assert client.get("/view/module_nope/payload.json").status_code == 404


def test_refresh_leaves_no_stale_payload(board, monkeypatch):
    client, html = board
    rc.write_board(html, "<html>old</html>", PAYLOAD)
    # The store rebuild writes the HTML only (no board is on K10 yet): the old payload must go with the old page.
    monkeypatch.setattr(dashboard, "_MODEL_ONLY_RENDERERS",
                        {"module_relatedness": lambda: rc.write_artifact(html, "<html>new</html>")})
    monkeypatch.setattr(dashboard, "_STORE_REFRESHABLE", frozenset({"module_relatedness"}))
    r = client.get("/view/module_relatedness?refresh=1")
    assert r.status_code == 200 and b"new" in r.data
    assert not rc.payload_path(html).exists()
    assert client.get("/view/module_relatedness/payload.json").status_code == 404


def test_refresh_rewrites_both_for_a_board_on_the_route(board, monkeypatch):
    client, html = board
    rc.write_board(html, "<html>old</html>", PAYLOAD)
    monkeypatch.setattr(dashboard, "_MODEL_ONLY_RENDERERS",
                        {"module_relatedness": lambda: rc.write_board(html, "<html>new</html>", {"n": 2})})
    monkeypatch.setattr(dashboard, "_STORE_REFRESHABLE", frozenset({"module_relatedness"}))
    assert client.get("/view/module_relatedness?refresh=1").status_code == 200
    r = client.get("/view/module_relatedness/payload.json", headers={"Accept-Encoding": "identity"})
    assert json.loads(r.data) == {"n": 2}


@pytest.mark.parametrize("mod, runner", [("module_financials", "run_quick_cached"), ("module_oipd", "run_oipd_cached"),
                                         ("module_comovement", "run_comovement")])
def test_the_per_board_refresh_branches_drop_the_payload_too(mod, runner, tmp_path, monkeypatch, rawstore_tmp_db):
    """#383 C6a review (N7): Financials, Implied Probability and Co-movement refresh before the generic branch; their
    rebuild writes the HTML only, so the old payload goes with the old page there too."""
    from RevenueChaser.app import runners
    html = tmp_path / f"{mod}_latest.html"
    rc.write_board(html, "<html>old</html>", PAYLOAD)
    monkeypatch.setattr(dashboard, "_module_file", lambda m: html if m == mod else None)
    monkeypatch.setattr(runners, runner, lambda *a, **k: rc.write_artifact(html, "<html>new</html>") or html)
    dashboard.app.config["TESTING"] = True
    r = dashboard.app.test_client().get(f"/view/{mod}?refresh=1")
    assert r.status_code == 200 and b"new" in r.data
    assert not rc.payload_path(html).exists()


_NODE = r"""
var calls = [];
function fetch(url, opts){
  calls.push([url, opts]);
  var ok = url.indexOf("module_ok") >= 0;
  return Promise.resolve({ok: ok, status: ok ? 200 : 404, json: function(){ return Promise.resolve({n: 1}); }});
}
__JS__
rcPayload("module_ok").then(function(d){
  return rcPayload("module_gone").then(function(){ return "resolved"; }, function(e){ return String(e.message); })
    .then(function(miss){ console.log(JSON.stringify({data: d, miss: miss, calls: calls})); });
});
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_rc_payload_fetches_the_route(tmp_path):
    p = tmp_path / "t.js"
    p.write_text(_NODE.replace("__JS__", rc.PAYLOAD_JS), encoding="utf-8")
    out = subprocess.run(["node", str(p)], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["data"] == {"n": 1}
    assert got["miss"] == "payload 404"
    assert got["calls"][0][0] == "/view/module_ok/payload.json"
    assert got["calls"][0][1]["cache"] == "no-store"
