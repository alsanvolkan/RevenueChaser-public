"""Real phones the e2e tier emulates, measured on the device itself.

A device is what ``Page.emulate_device`` hands Chrome: the CSS viewport, the pixel ratio, mobile with touch,
and the user agent. ``PHONE = (390, 844)`` in the tests stays the generic narrow phone; these are the owner's
own screens.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Device:
    name: str
    width: int
    height: int
    scale: float
    user_agent: str
    mobile: bool = True


_REALME_UA = ("Mozilla/5.0 (Linux; Android 15; RMX3301) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/156.0.8078.47 Mobile Safari/537.36")

# realme GT2 Pro (RMX3301), measured 8 Nov 2026 02:36 Amsterdam on the phone, portrait, in the Claude app:
# viewport 411 x 806 CSS px, screen 412 x 919 CSS px, devicePixelRatio 3.5, 5 touch points, Android 15, Chrome 156.
# The width is the screen's 412 px: the missing 1 px is the app's scrollbar gutter, which Chrome does not have.
REALME_GT2_PRO = Device("realme-gt2-pro", 412, 806, 3.5, _REALME_UA)

# Sideways the page gets the screen's long side (919 px) as its width. The 360 px of height is an estimate
# (412 minus the status bar and a collapsed address bar), not a measurement: check it once on the phone.
REALME_GT2_PRO_LANDSCAPE = Device("realme-gt2-pro-landscape", 919, 360, 3.5, _REALME_UA)

DEVICES = (REALME_GT2_PRO, REALME_GT2_PRO_LANDSCAPE)
