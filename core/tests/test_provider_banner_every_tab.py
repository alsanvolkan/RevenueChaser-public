"""Every data tab shows the shared provider banner when its run recorded failures (#383 C4b, Common K3).

``reporting_common.provider_health_html`` is the one producer. Six boards had it before R2a (Earnings Radar,
Implied Probability, UW Interaction, Sector Rotation, 13F, LEAPS); C4b puts it on the other ten. Each fixture in
``board_fixtures`` passes a failing health (``board_fixtures.health``) the way its runner hands it over, so the
browser and escaping guards cover the banner too. A clean run shows no banner (``test_reporting_common``).
"""
from __future__ import annotations

import pytest

from RevenueChaser.core.tests.board_fixtures import BOARDS
from RevenueChaser.core.tests.test_help_every_tab import HELP_TABS

BANNER = 'class="rc-provider-health"'
#: Tabs that call no provider, so have no run that can fail: Welcome reads only stored data (its Fetch is a build);
#: LEAPS reads the builder's stored ``option_far_contract`` and has no Fetch at all (#746).
NO_PROVIDER = {"module_welcome", "module_leaps"}


@pytest.mark.parametrize("key", [k for k in HELP_TABS if k not in NO_PROVIDER])
def test_the_tab_shows_the_provider_banner_once(key, tmp_path, monkeypatch, rawstore_tmp_db):
    html = BOARDS[key].render(tmp_path, monkeypatch)
    assert html.count(BANNER) == 1, "provider_health_html missing (or rendered twice)"
    assert "Data incomplete." in html
