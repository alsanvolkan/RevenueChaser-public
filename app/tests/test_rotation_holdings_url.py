"""The Rotation page's holdings click must call routes that exist.

The expand-row script tried ``/view/sector_rotation/holdings`` first. The dashboard never registered that
path (the routes are ``/view/module_sector_rotation/holdings`` and the legacy ``/view/rotation/holdings``),
so every click logged a 404 before the fallback answered.
"""
from __future__ import annotations

import re
from pathlib import Path

from RevenueChaser.app import dashboard
from RevenueChaser.module_sector_rotation import report

_URL = re.compile(r"'(/view/[A-Za-z_/]+/holdings)\?ticker='")


def _script_urls() -> list[str]:
    return _URL.findall(Path(report.__file__).read_text(encoding="utf-8"))


def test_the_script_names_the_current_route_first_and_the_legacy_route_second():
    assert _script_urls() == ["/view/module_sector_rotation/holdings", "/view/rotation/holdings"]


def test_every_holdings_url_in_the_script_is_a_registered_route():
    registered = {rule.rule for rule in dashboard.app.url_map.iter_rules()}
    urls = _script_urls()
    assert urls
    assert [u for u in urls if u not in registered] == []
