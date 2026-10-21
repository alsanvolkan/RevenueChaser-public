"""The CFTC TFF read follows the redirect httpx does not follow by default.

No request leaves the process: ``httpx.get`` is a fake whose ``follow_redirects`` default is the library's ``False``,
which is what makes the test red until the fetch asks for the follow (#447's class of bug: the parser saw the
redirect body and the panel went quiet).
"""
from __future__ import annotations

from RevenueChaser.module_spx_gamma import cftc

ROW = {
    "report_date_as_yyyy_mm_dd": "2026-10-06T00:00:00.000",
    "open_interest_all": "2000000",
    "asset_mgr_positions_long": "500000", "asset_mgr_positions_short": "120000",
    "change_in_asset_mgr_long": "1500",
    "lev_money_positions_long": "300000", "lev_money_positions_short": "260000",
    "change_in_lev_money_long": "-2200",
    "dealer_positions_long_all": "90000", "dealer_positions_short_all": "420000",
    "change_in_dealer_long_all": "300",
}


class _Resp:
    status_code, text = 200, ""

    def json(self):
        return [ROW]


def test_the_tff_read_follows_the_redirect(monkeypatch):
    seen = []

    def get(url, params=None, timeout=None, follow_redirects=False, **kwargs):
        seen.append(follow_redirects)
        return _Resp()

    monkeypatch.setattr("httpx.get", get)
    row = cftc.fetch_cftc_tff()
    assert row["report_date"] == "2026-10-06T00:00:00.000" and row["open_interest"] == 2000000.0
    assert seen == [True]                              # one read, one follow
