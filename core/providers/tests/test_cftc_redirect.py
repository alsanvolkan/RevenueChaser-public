"""The CFTC TFF provider read against a redirecting host (core/providers/cftc.py).

httpx follows no redirect by default, so a 301/307 from the Socrata host would be read as the payload:
the retry loop would take the redirect body as the row. No request leaves the process: ``httpx.get`` is
a fake that records whether it was asked to follow.
"""
from __future__ import annotations

from RevenueChaser.core.providers import cftc

ROW = {
    "market_and_exchange_names": cftc.E_MINI_MARKET,
    "report_date_as_yyyy_mm_dd": "2026-10-06T00:00:00.000",
    "open_interest_all": "2000000",
    "asset_mgr_positions_long": "500000", "asset_mgr_positions_short": "120000",
    "change_in_asset_mgr_long": "1500",
    "lev_money_positions_long": "300000", "lev_money_positions_short": "260000",
    "change_in_lev_money_long": "-2200",
    "dealer_positions_long_all": "90000", "dealer_positions_short_all": "420000",
    "change_in_dealer_long_all": "300",
}


def test_the_tff_read_follows_a_redirect(monkeypatch):
    seen = []

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return [ROW]

    def get(url, *args, params=None, timeout=None, follow_redirects=False, **kwargs):
        seen.append(follow_redirects)
        return _Resp()

    monkeypatch.setattr("httpx.get", get)
    row = cftc.fetch_cftc_tff()
    assert row is not None and row["asset_manager"]["long"] == 500000.0
    assert seen == [True]
