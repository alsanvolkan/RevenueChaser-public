"""Screenshots of the Data/Health and Runs tabs at the widths the owner uses, for a reviewer to look at.

Runs only when ``RC_SHOTS_DIR`` names a folder (the e2e job on CI sets it and uploads the folder as the
``screenshots`` artifact, kept 14 days). The dashboard is the e2e throwaway one on mock providers, so the
pictures show the layout, not live data. 2 tabs x 5 widths x 2 schemes = 20 PNGs.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from RevenueChaser.core.reporting_common import THEME_STORAGE_KEY

from .harness.cdp_browser import Page, find_chrome
from .harness.devices import REALME_GT2_PRO, Device
from .test_phone_shell import FRAME_READY

SHOTS_DIR = os.environ.get("RC_SHOTS_DIR", "")

# 390 the generic phone, the realme upright, 969 the owner's laptop pane beside the sidebar, two desktops.
SIZES = [(390, 844), REALME_GT2_PRO, (969, 900), (1280, 800), (1920, 1080)]
TABS = ["module_settings", "runs"]

pytestmark = pytest.mark.skipif(not SHOTS_DIR, reason="RC_SHOTS_DIR is not set: no screenshots asked for")


def _label(size) -> str:
    return size.name if isinstance(size, Device) else f"{size[0]}"


@pytest.mark.parametrize("tab", TABS)
def test_screenshots_of_a_tab(dashboard, tab):
    chrome = find_chrome()
    if chrome is None:
        pytest.fail("RC_SHOTS_DIR is set but there is no Chrome/Chromium to take the screenshots")
    out = Path(SHOTS_DIR)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for scheme in ("dark", "light"):
        with Page(chrome) as page:           # a fresh profile: the stored theme is this scheme's alone
            for size in SIZES:
                if isinstance(size, Device):
                    page.emulate_device(size, scheme=scheme)
                else:
                    page.emulate(*size, scheme=scheme)
                page.open(f"{dashboard.url}/#{tab}", ready=FRAME_READY)
                # The shell is dark on a first visit whatever the device says; the toggle's stored choice wins.
                page.eval(f"localStorage.setItem({THEME_STORAGE_KEY!r}, {scheme!r})")
                page.open(f"{dashboard.url}/#{tab}", ready=FRAME_READY)
                path = page.screenshot(out / f"{tab}-{_label(size)}-{scheme}.png")
                assert path.stat().st_size > 1000, f"{path.name} is empty"
                written.append(path.name)
    assert len(written) == len(SIZES) * 2
