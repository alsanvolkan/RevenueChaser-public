"""Tests for money-flow leaderboard calculation."""
import pytest

from RevenueChaser.module_sector_rotation.money_flow import get_money_flow_leaderboards


def _df():
    import pandas as pd
    # two tickers, 22 days each, deterministic upward/downward price moves
    rows = []
    for i in range(25):
        # UP: rising closes -> positive directional flow
        rows.append({"ticker": "UP", "date": f"2026-07-{i+1:02d}", "close": 100.0 + i, "volume": 1000.0})
        # DOWN: falling closes -> negative directional flow
        rows.append({"ticker": "DOWN", "date": f"2026-07-{i+1:02d}", "close": 200.0 - i, "volume": 1000.0})
    return pd.DataFrame(rows)


class TestMoneyFlow:
    def test_returns_inflow_outflow_ranking(self):
        result = get_money_flow_leaderboards(_df(), window=21, top_n=4)
        assert "inflows" in result and "outflows" in result
        inflows = result["inflows"]
        outflows = result["outflows"]
        # DF70 / #181 R20: a positive score is never an outflow, a negative one never an inflow
        assert [r["ticker"] for r in inflows] == ["UP"]
        assert [r["ticker"] for r in outflows] == ["DOWN"]
        assert inflows[0]["flow_score"] > 0
        assert outflows[0]["flow_score"] < 0

    def test_scores_bounded(self):
        result = get_money_flow_leaderboards(_df(), window=21, top_n=4)
        for row in result["inflows"] + result["outflows"]:
            assert -100.0 <= row["flow_score"] <= 100.0

    def test_empty_df(self):
        import pandas as pd
        assert get_money_flow_leaderboards(pd.DataFrame()) == {"inflows": [], "outflows": []}
        assert get_money_flow_leaderboards(None) == {"inflows": [], "outflows": []}
