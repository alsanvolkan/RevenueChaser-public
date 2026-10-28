"""Fable review S6 (SPX Gamma, iron butterfly). Offline.

1. When the report expiry's chain is empty the Fetch retries without an expiry (snapshot.py), and that live chain
   holds every expiry; the legs were picked from whichever expiry came first, so one structure could mix expiries.
   The structure now prices one expiry: the first on or after the day shown, else the nearest.
2. A missing wing strike falls back to the nearest one, but max risk stayed "target width - credit"; it is now the
   wider of the two wings the picked legs really span, minus the credit.
"""
from __future__ import annotations

from RevenueChaser.module_spx_gamma.chains import normalize_option_chain
from RevenueChaser.module_spx_gamma.engines import iron_butterfly as ib

PRED = {"active_pin": 7800.0, "downside_floor": 7650.0, "upside_trigger": 7800.0}
EM = {"points": 36.87, "iv_used": 0.076}     # wing width 35


def _rows(expiry: str, strikes: list[tuple[float, float]]) -> list[dict]:
    yymmdd = expiry[2:4] + expiry[5:7] + expiry[8:10]
    return [{"option_symbol": f"SPXW{yymmdd}{cp}{int(k * 1000):08d}", "nbbo_bid": f"{mid - 0.5:.2f}",
             "nbbo_ask": f"{mid + 0.5:.2f}", "implied_volatility": "0.12", "theta": "-1.0"}
            for k, mid in strikes for cp in ("C", "P")]


def _ib(rows, asof="2026-08-14"):
    return ib.build_iron_butterfly({"current_level": 7790.0, "asof_date": asof, "expected_move": EM,
                                    "prediction": PRED, "option_chain": normalize_option_chain({"data": rows})})


def test_a_live_chain_of_several_expiries_prices_one():
    later = _rows("2026-08-21", [(7765.0, 30.0), (7800.0, 45.0), (7835.0, 28.0)])
    today = _rows("2026-08-14", [(7765.0, 6.0), (7800.0, 15.0), (7835.0, 5.0)])
    p = _ib(later + today)
    assert p["expiry"] == "2026-08-14"
    assert [leg["mid"] for leg in p["legs"]] == [6.0, 15.0, 15.0, 5.0]
    assert p["net_credit"] == 19.0


def test_a_past_expiry_is_never_picked_while_a_later_one_exists():
    rows = _rows("2026-08-13", [(7765.0, 1.0), (7800.0, 2.0), (7835.0, 1.0)]) + \
        _rows("2026-08-17", [(7765.0, 7.0), (7800.0, 16.0), (7835.0, 6.0)])
    assert _ib(rows)["expiry"] == "2026-08-17"


def test_max_risk_uses_the_wings_the_picked_legs_span():
    # the target wings 7765 / 7835 are missing; the nearest strikes are 50 points out
    p = _ib(_rows("2026-08-14", [(7750.0, 6.0), (7800.0, 20.0), (7850.0, 5.0)]))
    legs = {(leg["action"], leg["type"]): leg["strike"] for leg in p["legs"]}
    assert legs[("BUY", "PUT")] == 7750.0 and legs[("BUY", "CALL")] == 7850.0
    assert p["net_credit"] == 29.0
    assert p["max_risk"] == 21.0, "50-point wings, not the 35-point target"
    assert p["return_on_risk"] == round(29.0 / 21.0, 3)
