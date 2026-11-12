"""The shared theme tokens (#184): one palette per scheme, defined once in ``core/reporting_common``, used by the
shell and offered to the boards. Dark by default on every device (avo, 25 Oct 2026); Light, or Auto (follow
the device's ``prefers-color-scheme``), when the reader chooses it, remembered in ``localStorage``."""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from RevenueChaser.core import reporting_common as rc

# The palette avo picked on 12 Nov 2026 (13F thread, option D "Full design system"). Dark is the default, so
# "dark when dark is chosen" means exactly these values; a change to them is a design decision, not a refactor.
PICKED_DARK = {
    "bg": "#1C1B19", "fg": "#D9D5CC", "bright": "#F3F0E8", "card": "#242320", "card-2": "#2C2A26",
    "sidebar": "#201F1C", "pos": "#8CC5A2", "neg": "#E3988A", "amber": "#DDB066", "muted": "#A39E93",
    "accent": "#D99272", "focus": "#D99272", "data-1": "#5E9AD0", "data-2": "#D9773F",
}
PICKED_LIGHT = {
    "bg": "#F7F5F0", "fg": "#3B3833", "bright": "#1F1D1A", "card": "#FFFEFB", "card-2": "#F0EDE6",
    "pos": "#2F6B4F", "neg": "#A3473B", "amber": "#8F5F14", "muted": "#68645C", "accent": "#A9542F",
    "data-1": "#2F6FA8", "data-2": "#C0602E",
}


@pytest.mark.parametrize("scheme, picked", [("dark", PICKED_DARK), ("light", PICKED_LIGHT)])
def test_the_palette_is_the_one_avo_picked(scheme, picked):
    for name, value in picked.items():
        assert rc.THEME_TOKENS[scheme][name] == value, f"--{name} changed in {scheme}"


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("name", ["fg", "muted", "pos", "neg", "amber", "accent", "sky"])
def test_every_text_colour_reads_on_a_card(scheme, name):
    """WCAG AA, 4.5:1, on both the card and the page background."""
    t = rc.THEME_TOKENS[scheme]
    for ground in ("card", "bg"):
        assert _contrast(_rgba(t[name])[:3], _rgba(t[ground])[:3]) >= 4.5, (scheme, name, ground)


def test_the_scales_are_one_set_for_both_schemes():
    """Type, spacing, radius and elevation are scheme-independent and ride the default :root block."""
    scale = rc.THEME_SCALE
    assert [scale[f"fs-{k}"] for k in ("xs", "sm", "base", "md", "lg", "xl")] == \
        ["11px", "12.5px", "14px", "16px", "20px", "26px"]
    assert [scale[f"sp-{i}"] for i in range(1, 8)] == ["4px", "8px", "12px", "16px", "24px", "32px", "48px"]
    assert scale["touch"] == "44px"
    root = _block(rc.THEME_CSS, ":root")
    for name, value in scale.items():
        assert f"--{name}:{value}" in root, name


def test_inter_is_served_by_the_dashboard_not_a_font_host():
    from pathlib import Path
    assert "url(/static/fonts/InterVariable.woff2)" in rc.THEME_CSS
    assert "fonts.googleapis" not in rc.THEME_CSS
    static = Path(rc.__file__).resolve().parent.parent / "app" / "static" / "fonts"
    assert (static / "InterVariable.woff2").is_file() and (static / "Inter-OFL.txt").is_file()


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


def test_the_css_is_dark_by_default_on_any_device_and_obeys_the_choice():
    """avo, 25 Oct 2026: the default is dark. A light device shows light only when the reader chose Auto."""
    css = rc.THEME_CSS
    dark = _block(css, ":root")
    assert "--bg:" + rc.THEME_TOKENS["dark"]["bg"] in dark and "color-scheme:dark" in dark
    m = re.search(r"@media\s*\(prefers-color-scheme:\s*light\)\s*\{(.*?)\}\s*\}", css, re.S)
    assert m, "no prefers-color-scheme: light block"
    selector = m.group(1).split("{")[0].strip()
    assert selector == ':root[data-theme="auto"]', f"a light device gets light without choosing it: {selector}"
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


@pytest.mark.parametrize("stored, expected", [("light", "light"), ("dark", "dark"), ("auto", "auto"),
                                              (None, "dark"), ("bogus", "dark")])
def test_the_page_applies_the_stored_choice(tmp_path, stored, expected):
    store = "{}" if stored is None else json.dumps({rc.THEME_STORAGE_KEY: stored})
    script = (_DOC + f"var _s = {store};\n"
              "globalThis.localStorage = {getItem: function(k){ return k in _s ? _s[k] : null; },"
              " setItem: function(k, v){ _s[k] = v; }, removeItem: function(k){ delete _s[k]; }};\n"
              + rc.THEME_JS + "\nconsole.log(JSON.stringify(attrs['data-theme'] || null));")
    assert _node(script, tmp_path) == expected


def test_a_storage_that_throws_leaves_the_page_on_dark(tmp_path):
    """Private windows and blocked site data throw on every localStorage access."""
    script = (_DOC + "Object.defineProperty(globalThis, 'localStorage', {get: function(){ throw new Error('denied'); }});\n"
              + rc.THEME_JS + "\n"
              "var r = rcThemeSet('light');\n"
              "console.log(JSON.stringify([attrs['data-theme'] || null, rcThemeGet()]));")
    # The choice still applies to this page; it just is not remembered.
    assert _node(script, tmp_path) == ["light", "dark"]


def test_choosing_dark_forgets_the_stored_choice(tmp_path):
    """Dark is the default, so it is stored as nothing; Light and Auto are remembered."""
    script = (_DOC + "var _s = {};\n"
              "globalThis.localStorage = {getItem: function(k){ return k in _s ? _s[k] : null; },"
              " setItem: function(k, v){ _s[k] = v; }, removeItem: function(k){ delete _s[k]; }};\n"
              + rc.THEME_JS + "\n"
              "var out = [];\n"
              "rcThemeSet('light'); out.push([attrs['data-theme'] || null, _s['rc-theme'] || null]);\n"
              "rcThemeSet('auto'); out.push([attrs['data-theme'] || null, _s['rc-theme'] || null]);\n"
              "rcThemeSet('dark'); out.push([attrs['data-theme'] || null, _s['rc-theme'] || null]);\n"
              "console.log(JSON.stringify(out));")
    assert _node(script, tmp_path) == [["light", "light"], ["auto", "auto"], ["dark", None]]


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


def _rgba(value: str) -> tuple[float, float, float, float]:
    if value.startswith("#"):
        v = value.lstrip("#")
        return (*(int(v[i:i + 2], 16) for i in (0, 2, 4)), 1.0)
    parts = [p.strip() for p in value[value.index("(") + 1:value.index(")")].split(",")]
    return (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]) if len(parts) > 3 else 1.0)


def _over(top: str, under: str) -> tuple[float, float, float]:
    r, g, b, a = _rgba(top)
    R, G, B, _ = _rgba(under)
    return (r * a + R * (1 - a), g * a + G * (1 - a), b * a + B * (1 - a))


def _contrast(fg, bg) -> float:
    def lum(c):
        def ch(x):
            x /= 255
            return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
        return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])
    hi, lo = sorted((lum(fg), lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("sign", ["pos", "neg"])
def test_the_spx_calendar_days_are_readable(scheme, sign):
    """#190 review: #eafff5 / #ffd9d9 on a 35 % green / red tint over a white card was 1.22:1 and 1.09:1."""
    t = rc.THEME_TOKENS[scheme]
    cell = _over(t[f"gex-{sign}-bg"], t["card"])
    assert _contrast(_rgba(t[f"gex-{sign}-fg"])[:3], cell) >= 4.5, (scheme, sign)


def test_the_touch_sized_summaries_keep_their_arrow():
    """#190 review: display:flex on a <summary> drops its disclosure triangle (it lives on display:list-item)."""
    for css in (rc.HELP_CSS, rc.HISTORY_CSS):
        touch = css[css.index("@media (max-width: 900px), (pointer: coarse)"):]
        assert "display:flex" not in touch
