"""Synthetic financials fixture — no network."""
from __future__ import annotations

import pytest


@pytest.fixture
def financials() -> dict:
    return {
        "ticker": "TEST",
        "sector": "Technology",
        "shares_outstanding": 1000.0,
        "market_cap": 100_000.0,
        "beta": 1.2,
        "current_price": 100.0,
        "currency": "USD",
        "source": "test",
        "annual": [
            {
                "fiscal_year": "2021", "revenue": 800.0, "ebitda": 240.0, "ebit": 200.0,
                "depreciation": 40.0, "capex": 40.0, "net_income": 150.0,
                "total_debt": 300.0, "cash": 100.0, "total_assets": 1500.0,
                "total_equity": 800.0, "receivables": 80.0, "inventory": 60.0,
                "payables": 40.0, "current_assets": 250.0, "current_liabilities": 150.0,
                "interest_expense": 20.0, "pretax_income": 190.0, "tax_provision": 40.0,
            },
            {
                "fiscal_year": "2022", "revenue": 900.0, "ebitda": 270.0, "ebit": 225.0,
                "depreciation": 45.0, "capex": 45.0, "net_income": 170.0,
                "total_debt": 280.0, "cash": 110.0, "total_assets": 1650.0,
                "total_equity": 900.0, "receivables": 90.0, "inventory": 67.5,
                "payables": 45.0, "current_assets": 280.0, "current_liabilities": 160.0,
                "interest_expense": 18.0, "pretax_income": 215.0, "tax_provision": 45.0,
            },
            {
                "fiscal_year": "2023", "revenue": 1000.0, "ebitda": 300.0, "ebit": 250.0,
                "depreciation": 50.0, "capex": 50.0, "net_income": 190.0,
                "total_debt": 260.0, "cash": 120.0, "total_assets": 1800.0,
                "total_equity": 1000.0, "receivables": 100.0, "inventory": 75.0,
                "payables": 50.0, "current_assets": 310.0, "current_liabilities": 170.0,
                "interest_expense": 16.0, "pretax_income": 240.0, "tax_provision": 50.0,
            },
        ],
    }
