"""Run a rendered board's inline scripts under node against a small DOM stub.

Shared by the escaping tests (ADR-X1, #144): a string assertion on the HTML
cannot see what client JS puts into ``innerHTML``, so these run the real script
and read the elements back. Not a test module (no ``test_`` prefix).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT_RE = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S | re.I)

DOM_STUB = r"""
var _els = {};
function _el(id){
  var attrs = {};
  var el = {id: id || "", innerHTML: "", textContent: "", value: "", className: "",
    title: "", style: {}, dataset: {}, options: {length: 0}, hidden: false, checked: false,
    classList: {add: function(){}, remove: function(){}, toggle: function(){},
                contains: function(){ return false; }},
    setAttribute: function(k, v){ attrs[k] = String(v); },
    getAttribute: function(k){ return k in attrs ? attrs[k] : null; },
    removeAttribute: function(k){ delete attrs[k]; },
    appendChild: function(){}, addEventListener: function(){},
    querySelector: function(){ return _el(""); },
    querySelectorAll: function(){ return []; }, focus: function(){},
    closest: function(){ return null; }, contains: function(){ return false; }};
  return el;
}
globalThis.document = {
  getElementById: function(id){ if(!_els[id]) _els[id] = _el(id); return _els[id]; },
  createElement: function(){ return _el(""); },
  querySelector: function(){ return _el(""); },
  querySelectorAll: function(){ return []; },
  addEventListener: function(){},
  documentElement: _el("html"),
  body: _el("body")
};
globalThis.window = globalThis;
globalThis.addEventListener = function(){};
globalThis.location = {hash: "", search: "", pathname: "/", href: "http://127.0.0.1/"};
globalThis.history = {replaceState: function(){}};
globalThis.localStorage = {getItem: function(){ return null; }, setItem: function(){}};
globalThis.fetch = function(){ return new Promise(function(){}); };
"""


def inline_scripts(html: str) -> list[str]:
    """Bodies of the page's inline ``<script>`` blocks (``src=`` blocks skipped)."""
    return [m.group("body") for m in _SCRIPT_RE.finditer(html)
            if "src=" not in m.group("attrs").lower()]


def with_payload(html: str, payload) -> str:
    """``html`` with a first script that answers the page's ``fetch`` with ``payload`` (K10, #383 C6a).

    A board on the payload route loads its rows with ``rcPayload``; a test (node or the browser guard) has no
    dashboard to serve them, so this stub stands in for ``GET /view/<mod>/payload.json``. The payload goes
    through ``script_json`` like any data in a script. Placed before ``</head>`` (else at the top), so it runs
    after a guard hook that blocks ``fetch`` and before the board's own scripts.
    """
    from RevenueChaser.core.html_safe import script_json
    stub = ("<script>window.fetch=function(){var p=" + script_json(payload) + ";"
            "return Promise.resolve({ok:true,status:200,json:function(){return Promise.resolve(p);}});};"
            "</script>")
    i = html.lower().find("</head>")
    return html[:i] + stub + html[i:] if i >= 0 else stub + html


def run_board_js(html: str, tail: str, tmp_path: Path, payload=None):
    """Run every inline script of ``html``, then ``tail``; return the JSON it logs.

    ``tail`` ends with ``console.log(JSON.stringify(...))``; ``_els[id]`` reads an
    element back. With ``payload`` (a board on the K10 route) the page's ``fetch`` answers with it
    (``with_payload``) and ``tail`` runs once the page has loaded it (after the pending promises).
    """
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    if payload is not None:
        html = with_payload(html, payload)
        tail = "setTimeout(function(){\n" + tail + "\n}, 0);"
    path = tmp_path / "board.js"
    path.write_text(DOM_STUB + "\n;\n".join(inline_scripts(html)) + "\n" + tail,
                    encoding="utf-8")
    out = subprocess.run(["node", str(path)], capture_output=True, text=True,
                         encoding="utf-8")
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


# Every placeholder name a board template substitutes. A value that spells one of
# them must come out literally (one-pass ``html_safe.fill``), and a ``<!--<script>``
# in a value must not reach the HTML tokenizer (``html_safe.script_json``).
PLACEHOLDERS = (
    "__DATA__ __CUTS__ __CUT_WORDS__ __BETA__ __HORIZON__ __WINDOW__ __LOCKED__ __MARKET__ "
    "__APPLIED__ __OOS__ __META__ __ETF__ __GATES__ __PERIODS__ "
    "__COMPANIES__ __PEERS__ __GRAPHS__ "
    "__DEFAULT_FOCAL__ __FILINGS__ __HOLDINGS_JSON__ __THEME_ETFS_JSON__ __DEFAULT_SORT__ __BOOT__ __MOD__")
PROBE = "X<!--<script>x " + PLACEHOLDERS


def assert_scripts_intact(html: str, tmp_path: Path) -> None:
    """Each inline script parses and holds no ``<!--`` / ``<script``; the probe's
    placeholder names survive literally."""
    assert html.lower().count("<script") == html.lower().count("</script"), "unbalanced <script>"
    bodies = inline_scripts(html)
    assert bodies, "no inline script"
    for i, body in enumerate(bodies):
        assert "<!--" not in body, f"script {i} holds <!--"
        assert "<script" not in body.lower(), f"script {i} holds <script"
    # Each embedded copy of the probe still carries every placeholder name.
    joined = "\n".join(bodies)
    copies = joined.count("!--")
    assert copies, "the probe never reached an inline script"
    assert joined.count(PLACEHOLDERS) == copies, "a placeholder was substituted inside a value"
    assert_scripts_parse(html, tmp_path)


def assert_scripts_parse(html: str, tmp_path: Path) -> None:
    """``node --check`` passes on every inline script block."""
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    for i, body in enumerate(inline_scripts(html)):
        path = tmp_path / f"script{i}.js"
        path.write_text(body, encoding="utf-8")
        out = subprocess.run(["node", "--check", str(path)], capture_output=True,
                             text=True, encoding="utf-8")
        assert out.returncode == 0, f"script {i}: {out.stderr[-1500:]}"
