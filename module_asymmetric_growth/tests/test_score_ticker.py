"""Asymmetric Growth Hunter — score_ticker in isolation (no network).

Pin: 3 signals x 3 sub-criteria, overlays (R&D>=15%, dilution>20%), non-US
excluded, US-listed flag. Buckets are client-side (report JS); here we pin
total_points.
"""
from RevenueChaser.module_asymmetric_growth.engine import score_ticker


def _fin(shares=(100, 101, 102), country="United States", exchange="NMS",
         rd=(100, 130, 200)) -> dict:
    """3-year annual set (oldest -> newest) with an accelerating growth path."""
    revs = (800, 850, 1080)
    return {
        "info": {"country": country, "exchange": exchange, "longName": "Test Corp",
                 "industry": "Software"},
        "annual": [
            {"revenue": revs[0], "gross_profit": 400, "operating_cash_flow": 120,
             "capex": 20, "total_debt": 50, "cash": 200, "total_assets": 1000,
             "rd_expense": rd[0], "diluted_shares": shares[0]},
            {"revenue": revs[1], "gross_profit": 500, "operating_cash_flow": 150,
             "capex": 25, "total_debt": 50, "cash": 250, "total_assets": 1050,
             "rd_expense": rd[1], "diluted_shares": shares[1]},
            {"revenue": revs[2], "gross_profit": 700, "operating_cash_flow": 250,
             "capex": 40, "total_debt": 60, "cash": 400, "total_assets": 1200,
             "rd_expense": rd[2], "diluted_shares": shares[2]},
        ],
        "sector": "Technology",
        "market_cap": 5e9,
        "source": "test",
    }


def _tech(dist=0.05, vol=1.6, above=True) -> dict:
    return {"price": 100.0, "dist_52w_pct": dist, "vol_ratio": vol, "above_sma50": above}


def test_full_pass_nine_points():
    card = score_ticker("TEST", _fin(), _tech())
    assert card is not None
    assert card["total_points"] == 9
    assert card["us_listed"] is True
    assert card["rev_growth"] is not None and card["rev_growth"] >= 0.20
    assert card["gross_margin"] >= 0.40
    assert card["fcf"] > 0
    assert card["runway_years"] == 999.0  # self-funded sentinel


def test_partial_points_when_off_52w_high():
    card = score_ticker("TEST", _fin(), _tech(dist=0.30))
    assert card["total_points"] == 8  # only momentum/dist fails


def test_non_us_excluded():
    card = score_ticker("TEST", _fin(country="Japan"), _tech())
    assert card is None


def test_foreign_exchange_excluded():
    card = score_ticker("TEST", _fin(exchange="TSE"), _tech())
    assert card is None


def test_tech_breakthrough_overlay():
    card = score_ticker("TEST", _fin(), _tech())
    assert card["tech_breakthrough"] is True  # R&D/revenue ~18.5% >= 15%


def test_dilution_warning_overlay():
    card = score_ticker("TEST", _fin(shares=(100, 100, 130)), _tech())
    assert card["dilution_warning"] is True  # +30% YoY shares > 20%


def test_survival_fails_with_negative_fcf():
    fin = _fin()
    fin["annual"][2]["operating_cash_flow"] = 10  # fcf = 10 - 40 < 0
    card = score_ticker("TEST", fin, _tech())
    assert card["fcf"] < 0
    assert card["runway_years"] < 999.0
    survival = next(s for s in card["signals"] if s["key"] == "survival")
    assert survival["passed"] < 3
