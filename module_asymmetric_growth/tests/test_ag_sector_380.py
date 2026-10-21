"""#380 AG W1 S6 sector not comparable (card 4, AG-10, C17 ``core.sector_rules``).

Financial Services and Real Estate (the **spine** sector, never the vendor's) cannot be compared on revenue growth,
gross margin and FCF: such a card is flagged ``sector_not_comparable`` and has no tier. It stays a card and a ledger
pick. Utilities is comparable (card 4 (b)). Offline.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import sector_rules
from RevenueChaser.core.gics_map import GicsPath
from RevenueChaser.core.tests.board_js import run_board_js
from RevenueChaser.module_asymmetric_growth import engine, freeze, report
from RevenueChaser.module_asymmetric_growth.engine import score_ticker
from RevenueChaser.module_asymmetric_growth.tests.test_ag_payload_route_380 import _card, _result
from RevenueChaser.module_asymmetric_growth.tests.test_score_ticker import _fin, _tech


def _scored(spine_sector: str, vendor_sector: str = "Technology") -> dict:
    fin = _fin()
    fin["info"]["sector"] = vendor_sector
    fin["sector"] = vendor_sector
    return score_ticker("TEST", fin, _tech(), spine={"TEST": GicsPath(spine_sector, "Some Industry")})


@pytest.mark.parametrize("sector", ["Real Estate", "Financial Services"])
def test_a_not_comparable_spine_sector_is_flagged_and_has_no_tier(sector):
    card = _scored(sector)
    assert card is not None and card["total_points"] == 9      # still a card, its points unchanged
    assert card["sector"] == sector
    assert "sector_not_comparable" in card["flags"]
    assert card["tier"] is None


def test_the_spine_sector_decides_not_the_vendors():
    assert "sector_not_comparable" in _scored("Real Estate", vendor_sector="Technology")["flags"]
    card = _scored("Technology", vendor_sector="Real Estate")
    assert "sector_not_comparable" not in card["flags"] and card["tier"] == 3


def test_utilities_is_comparable():
    card = _scored("Utilities")
    assert card["flags"] == [] and card["tier"] == 3


def test_a_flagged_card_stays_a_ledger_pick():
    card = _scored("Real Estate")
    card.update(price=10.0, market_cap=1e9)
    rows = freeze.picks([card])
    assert len(rows) == 1 and rows[0]["bucket"] == "tier:none|pts_9"


def test_the_summary_counts_the_flagged_cards():
    cards = [_scored("Real Estate"), _scored("Technology")]
    assert engine._summary(cards, 0, {})["sector_not_comparable"] == 1


def test_the_sector_rule_is_in_the_effective_config():
    assert freeze.effective_config()["RULES"]["not_comparable_sectors"] == sorted(sector_rules.NOT_COMPARABLE_SECTORS)


def test_the_board_shows_the_flags(tmp_path, rawstore_tmp_db):
    mv = freeze.model_version()
    cards = [_card("REIT", sector="Real Estate", tier=None, flags=["sector_not_comparable"], model_version=mv),
             _card("ODD", tier=None, flags=["suspicious_growth", "mixed_share_basis"], model_version=mv)]
    result = _result(cards)
    log = run_board_js(report.render_asymmetric_growth_hunter_html(result), "setTier('all');\n"
                       "console.log(JSON.stringify({grid: _els['grid'].innerHTML}));", tmp_path,
                       payload=report.board_payload(result))
    assert "Not comparable" in log["grid"]
    assert "Growth above +500%" in log["grid"] and "Share counts on different bases" in log["grid"]
