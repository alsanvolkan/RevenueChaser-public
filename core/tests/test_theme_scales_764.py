"""The scale half of the shared theme (#764): space, type, radius, elevation, focus, motion, touch target and
the categorical chart ramp. Colour is per scheme (``THEME_TOKENS``); these are not, so they are written once
into the base ``:root`` and the light blocks leave them alone."""
from __future__ import annotations

import re

from RevenueChaser.core import reporting_common as rc

# The ramp as it was validated: lightness band, chroma floor, adjacent-pair colour-vision separation and
# contrast against both board surfaces. The order is part of that result — swapping #0A9E6E and #E8445F
# next to each other drops the deutan separation under the floor.
RAMP = ["#0C87CC", "#C77C09", "#0A9E6E", "#8B5CF6", "#E8445F", "#2F9E8F"]


def _base_root() -> str:
    """The first ``:root{...}`` block of ``THEME_CSS`` — the one that is not scheme-specific."""
    return re.search(r"^:root\{(.*?)\}\n", rc.THEME_CSS, re.S | re.M).group(1)   # after the @font-face (#761)


def test_every_scale_token_is_in_the_base_root():
    base = _base_root()
    for name, value in rc.THEME_SCALE.items():
        assert f"--{name}:{value}" in base, f"--{name} missing from the base :root"


def test_a_scale_token_is_never_redefined_per_scheme():
    for scheme in ("dark", "light"):
        clash = set(rc.THEME_SCALE) & set(rc.THEME_TOKENS[scheme])
        assert not clash, f"{sorted(clash)} is defined as both a colour and a scale token in {scheme}"


def test_space_scale_is_the_documented_ladder():
    got = [rc.THEME_SCALE[f"s{i}"] for i in range(1, 9)]
    assert got == ["4px", "8px", "12px", "16px", "24px", "32px", "48px", "64px"]


def test_chart_ramp_keeps_its_validated_order():
    assert [rc.THEME_SCALE[f"c{i}"] for i in range(1, 7)] == RAMP


def test_focus_and_float_shadow_follow_the_scheme():
    # Theme D (#761) makes --focus a colour token per scheme, the accent; the float shadow points at the dialog
    # elevation, which reads --shadow, so the light blocks need no second copy of it.
    for scheme in ("dark", "light"):
        assert rc.THEME_TOKENS[scheme]["focus"] == rc.THEME_TOKENS[scheme]["accent"]
    assert rc.THEME_SCALE["e-float"] == "var(--elev-2)" and "var(--shadow)" in rc.THEME_SCALE["elev-2"]


def test_reduced_motion_stops_the_transition_duration():
    assert "@media (prefers-reduced-motion: reduce){:root{--motion:0ms}}" in rc.THEME_CSS
