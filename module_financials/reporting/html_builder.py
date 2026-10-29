"""HTML rapor üretici — engine çıktılarını Jinja2 dark-theme template'e enjekte eder."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

from RevenueChaser.core import calendar as market_calendar
from RevenueChaser.core.reporting_common import (help_html, provider_health_html, recorded_sessions_html,
                                                 watch_buttons_html, watchlist_html)

_TEMPLATES = Path(__file__).resolve().parent / "templates"
# Autoescape on: ticker, company, sector, source and warnings come from the
# request or the provider (M8 / ADR-X1, #144). Pre-rendered fragments go in as Markup.
_ENV = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)

# The shared "What am I looking at?" box (#383 C3); fixed text, so it goes in as Markup.
HELP = help_html(
    "One company at a time: <b>Quick Remarks</b> (the stored scores for the ticker) on top, then the "
    "<b>financial model</b>: DCF, three-statement view, comparables, credit and sensitivity. "
    "<b>Fetch</b> downloads the ticker's price, shares, beta and statements from <b>Yahoo</b> into the store and "
    "rebuilds the report. The models read the annual statements the company filed with the <b>SEC</b> (the "
    "fundamentals layer, re-read on each filing day) once that is built, and Yahoo's before, or when Yahoo's "
    "year is newer or gives an input the SEC's lacks (depreciation, working capital); the "
    "<b>Source</b> line says which, why, and which inputs the SEC's statements lack. <b>Refresh</b> rebuilds the report for the ticker in the "
    "box from the stored data with no network call; a ticker with nothing stored needs a Fetch first.")


def _fmt_usd(v) -> str:
    if v is None:
        return "N/A"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e12:
        return f"{sign}${a / 1e12:,.2f}T"
    if a >= 1e9:
        return f"{sign}${a / 1e9:,.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.2f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:,.1f}K"
    return f"{sign}${a:,.0f}"


def _fmt_num(v, nd=2) -> str:
    if v is None:
        return "N/A"
    if isinstance(v, str):
        return v          # a word the engine chose ("NM": a non-positive denominator has no multiple)
    return f"{v:,.{nd}f}"


def _fmt_pct(v, nd=1) -> str:
    if v is None:
        return "N/A"
    return f"{v * 100:,.{nd}f}%"


def _fmt_price(v) -> str:
    if v is None:
        return "N/A"
    return f"${v:,.2f}"


def _fmt_day(iso: str | None) -> str:
    if not iso:
        return "N/A"
    text = str(iso)
    return text[:10] if len(text) >= 10 else text


def _fmt_friendly_day(iso: str | None) -> str:
    """``Tuesday, September 01, 2026 EOD`` for the top bar's ``Last sync`` (AGENTS.md §6, FIN-10).

    The day is the New York session the stamp belongs to (``core/calendar.session_for``, rule B), the same day the
    comps' "as of" names (``history._as_of``); the stamp is written in UTC, and read as UTC a Tuesday-evening Fetch
    said Wednesday. A fetch inside the session reads ``intraday``, not ``EOD`` (Financials review S4). A naive stamp
    is New York time; one outside the holiday table shows its date as it reads.
    """
    if not iso:
        return "N/A"
    try:
        moment = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        stamp = market_calendar.session_for(moment if moment.tzinfo else moment.replace(tzinfo=market_calendar.NY))
    except ValueError:
        return _fmt_day(iso)
    word = "intraday" if stamp.quality == market_calendar.QUALITY_INTRADAY else "EOD"
    return f"{stamp.session_date:%A, %B %d, %Y} {word}"


def _fmt_tax_rate(assumptions: dict | None) -> str:
    """The model's tax rate; ``default 21.0% (no sector rate)`` when no statement or user value set it (FIN-9 B).

    The parenthetical is true, not decoration: no sector band carries a ``tax_rate``
    (``module_financials/engines/_inputs.default_tax_rate``), so the fallback is the single
    ``DEFAULT_TAX_RATE`` for every sector and the row must not read as a sector-specific one.
    """
    a = assumptions or {}
    rate = _fmt_pct(a.get("tax_rate"))
    return f"default {rate} (no sector rate)" if a.get("tax_rate_source") == "default" else rate


def _or_default(text: str, is_default=False) -> str:
    """``default 30.0%``: a shown value the model took from a built-in constant, because neither the user nor the
    data gave one (reviews S1 and S2, the tax row's wording)."""
    return f"default {text}" if is_default else text


_ENV.filters["usd"] = _fmt_usd
_ENV.filters["num"] = _fmt_num
_ENV.filters["pct"] = _fmt_pct
_ENV.filters["price"] = _fmt_price
_ENV.filters["day"] = _fmt_day
_ENV.filters["friendly_day"] = _fmt_friendly_day
_ENV.filters["tax_rate"] = _fmt_tax_rate
_ENV.filters["or_default"] = _or_default
# The watch-list pair (PR 4): ``watch_buttons_html`` escapes the ticker and gives nothing for one the dashboard
# refuses, so its output is markup the template takes as it is.
_ENV.globals["watch_pair"] = lambda ticker: Markup(watch_buttons_html(ticker))


def build_report_html(results: dict) -> str:
    """results -> tam HTML string."""
    financials = results.get("financials", {})
    modules = results.get("modules", {})
    ticker = results.get("ticker", financials.get("ticker", ""))

    info = financials.get("info", {})

    # Quick Remarks bölümü (ticker_scores'dan beslenir; her zaman üstte)
    quick_remarks = results.get("quick_remarks")
    if quick_remarks:
        from .quick_remarks_html import render_quick_remarks_section
        # The fragment escapes its own fields (FN-1).
        quick_remarks_section = Markup(render_quick_remarks_section(quick_remarks))
    else:
        quick_remarks_section = ""

    ctx = {
        "ticker": ticker,
        "company": info.get("longName") or info.get("shortName") or ticker,
        "sector": financials.get("sector"),
        "price": financials.get("current_price"),
        # The prices store's session of that price (G6 follow-up); None when the price is Yahoo's.
        "price_session": (financials.get("market_meta") or {}).get("price_session"),
        "currency": financials.get("currency", "USD"),
        "shares_outstanding": financials.get("shares_outstanding"),
        "market_cap": financials.get("market_cap"),
        "enterprise_value": financials.get("enterprise_value"),
        "source": financials.get("source", "yfinance"),
        "data_date": financials.get("_fetched_at_iso") or financials.get("fetched_at_iso"),
        "quick_remarks": quick_remarks,
        "quick_remarks_section": quick_remarks_section,
        "help_block": Markup(HELP),
        # Escapes every provider string itself (Common K3); empty for a clean run.
        "provider_banner": Markup(provider_health_html(results.get("provider_health"))),
        # Every Fetch of any ticker, newest first (#383 C7); escapes its own strings.
        "history_block": Markup(recorded_sessions_html("module_financials")),
        "watch_mount": Markup(watchlist_html("financials")),
        "dcf": modules.get("dcf"),
        "three_statement": modules.get("3stmt") or modules.get("three_statement"),
        "comps": modules.get("comps"),
        "credit": modules.get("credit"),
        "sensitivity": modules.get("sensitivity"),
        "warnings": results.get("warnings", []),
    }
    return _ENV.get_template("report_base.html.j2").render(**ctx)
