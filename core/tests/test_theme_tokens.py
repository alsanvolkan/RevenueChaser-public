"""The shared theme tokens (#184): one palette per scheme, defined once in ``core/reporting_common``, used by the
shell and offered to the boards. Automatic via ``prefers-color-scheme``, and a manual choice stored in
``localStorage`` that wins over it."""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from RevenueChaser.core import reporting_common as rc

# The shell's :root before #184 (templates/dashboard.html:12-16 on 3d6ad16) plus the colours it hard-coded
# elsewhere. "Dark when dark is chosen" means exactly these values.
SHELL_DARK_BEFORE = {
    "bg": "#05111F", "fg": "#C5C6C8", "bright": "#F0F0F0", "card": "#0B1828",
    "border": "rgba(197,198,200,.15)", "sidebar": "#0B1828", "pos": "#34D399", "neg": "#F87171",
    "amber": "#FBBF24", "sky": "#0EA5E9", "muted": "#9C9DA1",
    "input-bg": "#101F33", "input-border": "#2F434C", "hover": "#18293a", "topbar": "rgba(12,20,24,.8)",
    "nav-fg": "rgba(224,233,235,.75)", "nav-on": "#101F33", "hover-faint": "rgba(255,255,255,.04)",
    "frame-bg": "#12131c", "overlay": "rgba(5,17,31,.7)", "scroll-thumb": "#5b7a8c",
    "scroll-thumb-hover": "#6f93a7", "scroll-track": "#0b1620", "primary-bg": "#E2E3E5",
    "primary-fg": "#0A1420", "oi-bg": "rgba(11,24,40,.55)",
}


def test_dark_is_the_shell_palette_value_for_value():
    for name, value in SHELL_DARK_BEFORE.items():
        assert rc.THEME_TOKENS["dark"][name] == value, f"--{name} changed in dark"


def test_both_schemes_define_the_same_tokens():
    assert set(rc.THEME_TOKENS["dark"]) == set(rc.THEME_TOKENS["light"])


def _hex_luma(value: str) -> float:
    v = value.lstrip("#")
    r, g, b = (int(v[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_light_is_light():
    light = rc.THEME_TOKENS["light"]
    assert _hex_luma(light["bg"]) > 200 and _hex_luma(light["card"]) > 200
    assert _hex_luma(light["fg"]) < 100 and _hex_luma(light["bright"]) < 60


def _block(css: str, selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^{}]*)\}", css)
    assert m, f"no {selector} block"
    return m.group(1)


def test_the_css_defaults_to_dark_follows_the_device_and_obeys_the_manual_choice():
    css = rc.THEME_CSS
    dark = _block(css, ":root")
    assert "--bg:#05111F" in dark and "color-scheme:dark" in dark
    # Automatic: a light device gets light unless the manual choice says dark.
    m = re.search(r"@media\s*\(prefers-color-scheme:\s*light\)\s*\{(.*?)\}\s*\}", css, re.S)
    assert m, "no prefers-color-scheme: light block"
    assert ':root:not([data-theme="dark"])' in m.group(1)
    assert "--bg:" + rc.THEME_TOKENS["light"]["bg"] in m.group(1)
    # Manual: data-theme="light" forces light on a dark device.
    forced = _block(css, ':root[data-theme="light"]')
    assert "--bg:" + rc.THEME_TOKENS["light"]["bg"] in forced and "color-scheme:light" in forced


def test_the_stored_choice_has_one_key():
    assert rc.THEME_STORAGE_KEY == "rc-theme"
    assert rc.THEME_STORAGE_KEY in rc.THEME_JS


def _node(script: str, tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    path = tmp_path / "theme.js"
    path.write_text(script, encoding="utf-8")
    out = subprocess.run(["node", str(path)], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


_DOC = """
var attrs = {};
globalThis.document = {documentElement: {
  setAttribute: function(k, v){ attrs[k] = String(v); }, removeAttribute: function(k){ delete attrs[k]; },
  getAttribute: function(k){ return k in attrs ? attrs[k] : null; }}};
globalThis.window = globalThis;
globalThis.addEventListener = function(){};
"""


@pytest.mark.parametrize("stored, expected", [("light", "light"), ("dark", "dark"), (None, None), ("bogus", None)])
def test_the_page_applies_the_stored_choice(tmp_path, stored, expected):
    store = "{}" if stored is None else json.dumps({rc.THEME_STORAGE_KEY: stored})
    script = (_DOC + f"var _s = {store};\n"
              "globalThis.localStorage = {getItem: function(k){ return k in _s ? _s[k] : null; },"
              " setItem: function(k, v){ _s[k] = v; }, removeItem: function(k){ delete _s[k]; }};\n"
              + rc.THEME_JS + "\nconsole.log(JSON.stringify(attrs['data-theme'] || null));")
    assert _node(script, tmp_path) == expected


def test_a_storage_that_throws_leaves_the_page_on_auto(tmp_path):
    """Private windows and blocked site data throw on every localStorage access."""
    script = (_DOC + "Object.defineProperty(globalThis, 'localStorage', {get: function(){ throw new Error('denied'); }});\n"
              + rc.THEME_JS + "\n"
              "var r = rcThemeSet('light');\n"
              "console.log(JSON.stringify([attrs['data-theme'] || null, rcThemeGet()]));")
    # The choice still applies to this page; it just is not remembered.
    assert _node(script, tmp_path) == ["light", "auto"]


def test_choosing_auto_forgets_the_manual_choice(tmp_path):
    script = (_DOC + "var _s = {};\n"
              "globalThis.localStorage = {getItem: function(k){ return k in _s ? _s[k] : null; },"
              " setItem: function(k, v){ _s[k] = v; }, removeItem: function(k){ delete _s[k]; }};\n"
              + rc.THEME_JS + "\n"
              "var out = [];\n"
              "rcThemeSet('dark'); out.push([attrs['data-theme'] || null, _s['rc-theme'] || null]);\n"
              "rcThemeSet('auto'); out.push([attrs['data-theme'] || null, _s['rc-theme'] || null]);\n"
              "console.log(JSON.stringify(out));")
    assert _node(script, tmp_path) == [["dark", "dark"], [None, None]]


def test_every_storage_access_is_guarded():
    """CLAUDE.md, the owner's rule for this slice: every storage access sits in a try/catch."""
    js = rc.THEME_JS
    for m in re.finditer(r"localStorage\.\w+\(", js):
        before = js[:m.start()]
        assert before.rfind("try") > before.rfind("}"), f"unguarded {m.group(0)} at {m.start()}"


def test_the_shared_summaries_are_touch_sized_on_a_phone():
    """The help and history panels' summaries had 9 px padding: a 34 px target on a phone."""
    for css in (rc.HELP_CSS, rc.HISTORY_CSS):
        m = re.search(r"@media\s*\(max-width:\s*900px\),\s*\(pointer:\s*coarse\)\s*\{(.*?)\}\s*</style>", css, re.S)
        assert m, "no phone / touch rule"
        assert "min-height:44px" in m.group(1)
