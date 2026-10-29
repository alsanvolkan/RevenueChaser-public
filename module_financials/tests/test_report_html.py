"""The Financials report must always carry the date the data refers to.

Whether the page came from a live Fetch or a store-only Refresh, the header
shows when the underlying yfinance payload was fetched into .store — so the
viewer can never mistake a cached re-render for fresh data.
"""
from __future__ import annotations

from RevenueChaser.module_financials.reporting.html_builder import build_report_html


def _results(financials: dict) -> dict:
    return {
        "ticker": financials.get("ticker", "TEST"),
        "financials": financials,
        "modules": {},
        "warnings": [],
    }


def test_report_renders_the_data_date_from_the_store_stamp(financials):
    financials["_fetched_at_iso"] = "2026-09-02T08:00:00+00:00"
    html = build_report_html(_results(financials))
    assert "Last sync:" in html
    assert "Tuesday, September 01, 2026 EOD" in html          # 04:00 ET: before the open (review S4)


def test_report_still_renders_when_the_stamp_is_missing(financials):
    financials.pop("_fetched_at_iso", None)
    html = build_report_html(_results(financials))
    assert "Last sync:" in html
    assert "N/A" in html


_EVIL = "\"'><img src=x onerror=alert(1)>"


def test_provider_strings_and_the_ticker_are_escaped(financials):
    """M8 / FN-1: the base template rendered with autoescape off (ADR-X1, #144)."""
    financials["info"] = {"longName": _EVIL + "name"}
    financials.update(sector=_EVIL + "sector", source=_EVIL + "source")
    res = _results(financials)
    res.update(ticker=_EVIL + "ticker", warnings=[_EVIL + "warning"])
    html = build_report_html(res)
    assert "<img" not in html
    for tag in ("name", "sector", "source", "ticker", "warning"):
        assert "&lt;img src=x onerror=alert(1)&gt;" + tag in html


def test_quick_remarks_fragment_is_not_double_escaped(financials):
    res = _results(financials)
    res["quick_remarks"] = {"ticker": "TEST", "name": _EVIL, "remarks": ["r"], "warnings": []}
    html = build_report_html(res)
    assert "<img" not in html
    assert "&amp;lt;" not in html              # escaped once, by the fragment itself
    assert '<section class="qr"' in html          # the fragment renders as markup


def test_footer_names_only_the_source_the_job_reads(financials):
    """DF46: the job reads yfinance only; the footer must not cite EDGAR or Unusual Whales."""
    financials["source"] = "yfinance"
    html = build_report_html(_results(financials))
    footer = html[html.index("<footer"):]
    assert "Source: yfinance." in footer
    assert "EDGAR" not in footer
    assert "Unusual Whales" not in footer
