"""Asymmetric Growth Hunter — ABD hisseleri, 3 sinyal taraması.

Universe (#264): the Nasdaq / NYSE / CBOE rows of the SEC ``company_tickers_exchange.json`` are the
member list; the Yahoo screener only adds market data (cap, price, quote) to that list, by ticker.
Üç sinyal (her biri 3 alt kriter, X/3):
  1. Accelerated Growth — rev >= 20%, hızlanan YoY, gross margin >= 40%
  2. Survival          — FCF > 0, net debt/assets <= 10%, runway >= 2 yıl
  3. Momentum/Volume   — 52w zirveye <= 10%, 10g hacim >= 1.4x 3ay, fiyat > 50g MA

Toplam puan = geçen alt kriter sayısı (0-9). Kovalar: 9 · 8-7 · 6-5 · <5.
Tier (#380 S2): a signal is strong with at least 2 of its 3 sub-criteria passed; 3 strong = Balanced (3),
2 strong = Strong (2), otherwise no tier. Server-side, from the default thresholds.

Etiketler: Disruptive Themed (rotation-map teması), Technological
Breakthrough (R&D/Revenue >= 15%). Uyarı: High dilution (YoY hisse artışı > %20).
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

from RevenueChaser.core import earnings_dates, fiscal_year
from RevenueChaser.core import funnel as funnel_mod
from RevenueChaser.core import job_control
from RevenueChaser.core import ledger, prices, rawstore
from RevenueChaser.core import tristate as tri
from RevenueChaser.core import vendor_data
from RevenueChaser.core.config import to_float
from RevenueChaser.core.info_fallback import has_fields, refetch_or_keep
from RevenueChaser.core.liquidity import liquid_assets
from RevenueChaser.core.providers import cache as cache_manager
from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.core.providers import yfinance as yfinance_client
from RevenueChaser.core import universe as _spine
from RevenueChaser.core.universe import (  # noqa: F401
    load_cached_universe as _load_cached_universe,
    load_universe_meta as _load_universe_meta,
)
# The universe writer moved to core/universe_fetch.py (#146, from #217). These names stay importable from here.
from RevenueChaser.core.universe_fetch import (  # noqa: F401
    US_EXCHANGES,
    _UNIVERSE_META_KEY,
    _load_stats_meta,
    _save_universe,
    _save_universe_meta,
    backfill_universe_caps,
    fetch_universe,
    fetch_universe_stats,
)
from RevenueChaser.core.taxonomy import themes_for_many
from RevenueChaser.module_asymmetric_growth import sector_rules          # #18: AG's own rule, beside it now

logger = logging.getLogger(__name__)

REV_GROWTH_MIN = 0.20
GROSS_MARGIN_MIN = 0.40
NET_DEBT_ASSETS_MAX = 0.10   # "low debt" default (slider-ayarlanabilir)
RUNWAY_YEARS_MIN = 2.0
DIST_52W_MAX = 0.10   # "near 52w high" = within 10% of the 52-week high (not the 1% quick-remarks rule)
VOL_RATIO_MIN = 1.4   # volume-ratio floor; the band we accept is ~1.4-1.6x
RND_INTENSITY_MIN = 0.15
DILUTION_WARN = 0.20
TIER_MIN_PASSES = 2   # a signal counts toward the tier with at least 2 of its 3 sub-criteria passed (#380 S2)
# #41 (#66 checkbox 5): reporting only — a scored name under this average daily dollar volume (price * vol_90d) is
# counted in the run summary. A reporting-only default chosen by Claude, not the owner's number; it gates nothing.
THIN_DOLLAR_VOLUME = 250_000
EDGAR_SOURCE = "sec_edgar"   # ``financials["source"]`` when EDGAR supplied the annual figures (the card keeps it)

# Market-cap filtre kovaları (varsayılan aralık + 5 kova)
DEFAULT_MC_RANGE = (50_000_000, 5_000_000_000)

US_COUNTRY = "United States"

PRELOAD_BATCH = 500   # tickers whose stored rows one bulk read loads (bounds memory)

# Momentum from D01 (#380 S4, decision 2): the sessions each input needs, and how far back the one windowed read
# looks (380 calendar days: 252 sessions plus the market's closed days, with room for a gap).
BARS_52W = 252        # the 52-week high is the highest HIGH of these sessions
BARS_SMA = 50         # the mean close of these sessions
BARS_3M = 63          # Yahoo's "3-month" average volume: the mean volume of these sessions
BARS_10D = 10         # the short volume mean
BARS_WINDOW_DAYS = 380

SIGNAL_NAMES = {
    "growth": "Accelerated Growth",
    "survival": "Survival / Runway",
    "momentum": "Momentum / Volume",
}


def _pct_div(num, den):
    if num is None or den is None or den == 0:
        return None
    return num / den


_fy, _anchor, _prior = fiscal_year.fy, fiscal_year.anchor, fiscal_year.prior  # shared with Fundamentals


def _card_anchor(annual: list[dict], as_of: date | None) -> tuple[int | None, list[str]]:
    """``(index of the record the card reads, flags)`` (#380, card O-2 fix).

    Normally ``fiscal_year.anchor``: the newest record with a revenue figure. When that record is stale on
    ``as_of`` but the newest record is not (the newer years carry no revenue figure, e.g. a revenue concept the
    extractor misses), the card reads the newest record instead: its revenue is unknown, and it is flagged
    ``no_recent_revenue`` (no tier). Without ``as_of`` nothing is judged (tests, research scripts).
    """
    ai = _anchor(annual)
    last = len(annual) - 1
    if (as_of is None or ai is None or ai == last or not fiscal_year.is_stale(annual[ai], as_of)
            or fiscal_year.is_stale(annual[last], as_of)):
        return ai, []
    return last, ["no_recent_revenue"]


def _growth(annual: list[dict], i: int | None, field: str) -> tuple[float | None, list[str]]:
    """``(YoY change, flags)`` of ``field`` between ``annual[i]`` and its adjacent prior year, by the C18 rule
    (``fiscal_year.growth``, #380 S1): a prior that is not positive is unknown and flagged ``prior_nonpositive``,
    a change above +500 % keeps its value and is flagged ``suspicious_growth``."""
    j = _prior(annual, i)
    if i is None or j is None:
        return None, []
    return fiscal_year.growth(annual[i].get(field), annual[j].get(field))


def _share_change(cur: dict | None, prev: dict | None) -> tuple[float | None, list[str]]:
    """``(YoY share change, flags)`` on ONE share basis in both years (#380 S1): diluted when both years have it,
    else basic when both have it. No common basis is unknown and flagged ``mixed_share_basis``; a prior count that
    is not positive is unknown."""
    def basis(rec, key):
        return to_float((rec or {}).get(key))
    for key in ("diluted_shares", "basic_shares"):
        now, before = basis(cur, key), basis(prev, key)
        if now is not None and before is not None:
            return ((now - before) / before if before > 0 else None), []
    has = [any(basis(r, k) is not None for k in ("diluted_shares", "basic_shares")) for r in (cur, prev)]
    return None, (["mixed_share_basis"] if all(has) else [])


def tier_of(signals: list[dict]) -> int | None:
    """3 (Balanced) / 2 (Strong) / None: how many signals have at least ``TIER_MIN_PASSES`` known passes (#380 S2).

    Read from the signals as scored with the default thresholds; the board's sliders move points, never the tier.
    """
    strong = sum(1 for s in signals or [] if (s.get("passed") or 0) >= TIER_MIN_PASSES)
    return strong if strong >= 2 else None


def _end(rec: dict | None, *fields: str) -> str | None:
    """The period end (ISO date) of the first of ``fields`` that ``rec`` carries one for; None without
    ``period_ends`` (records stored before #239, yfinance shapes)."""
    ends = (rec or {}).get("period_ends") or {}
    if not isinstance(ends, dict):
        return None
    return next((str(ends[f])[:10] for f in fields if ends.get(f)), None)


def _provenance(annual: list[dict], ai: int | None, pi: int | None) -> dict:
    """What the card read (#380 S3, #66 provenance line): the anchor and prior fiscal years, the three revenue
    period ends (anchor, prior, the year before) and the anchor's balance-sheet and cash-flow period ends."""
    ppi = _prior(annual, pi)
    cur = annual[ai] if ai is not None else None
    return {
        "anchor_fy": _fy(cur),
        "prior_fy": _fy(annual[pi]) if pi is not None else None,
        "period_ends": {
            "revenue": [_end(annual[k], "revenue") if k is not None else None for k in (ai, pi, ppi)],
            "balance_sheet": _end(cur, "total_assets", "cash", "total_debt"),
            "cash_flow": _end(cur, "operating_cash_flow", "capex", "capital_expenditure"),
        },
    }


def _run_day() -> date:
    """The UTC day a Fetch judges anchor age against (card O-2)."""
    return datetime.now(timezone.utc).date()


def _is_us(info: dict) -> bool:
    country = (info.get("country") or "").strip()
    exchange = (info.get("exchange") or "").strip().upper()
    if country and country != US_COUNTRY:
        return False
    if exchange and exchange not in US_EXCHANGES:
        return False
    return True


def technical_from_quote(q: dict) -> dict:
    """Yahoo screener quote'undan teknik alt kriter değerlerini türetir (bulk, per-ticker çağrısız)."""
    price = to_float(q.get("regularMarketPrice"))
    high = to_float(q.get("fiftyTwoWeekHigh"))
    sma50 = to_float(q.get("fiftyDayAverage"))
    v10 = to_float(q.get("averageDailyVolume10Day"))
    v3m = to_float(q.get("averageDailyVolume3Month"))
    return {
        "price": price,
        "high_52w": high,
        "dist_52w_pct": (high - price) / high if (price and high) else None,
        "sma50": sma50,
        "above_sma50": (price > sma50) if (price is not None and sma50 is not None) else None,
        "vol_10d": v10,
        "vol_90d": v3m,
        "vol_ratio": v10 / v3m if (v10 and v3m) else None,
    }


def _bar_day(row) -> date | None:
    """A bar row's session date as a ``date`` (the frame gives a timestamp, a test may give a date or ISO text)."""
    day = row.get("session_date")
    if isinstance(day, datetime):
        return day.date()
    if isinstance(day, date):
        return day
    try:
        return date.fromisoformat(str(day)[:10])
    except (TypeError, ValueError):
        return None


def _max_last(values: list, n: int):
    """The largest of the last ``n`` values, or None when fewer than ``n`` of them are known."""
    known = [v for v in values[-n:] if v is not None]
    return max(known) if len(known) >= n else None


def _mean_last(values: list, n: int):
    """The mean of the last ``n`` values, or None when fewer than ``n`` of them are known."""
    known = [v for v in values[-n:] if v is not None]
    return sum(known) / len(known) if len(known) >= n else None


def technical_from_bars(rows, session: date | None = None) -> dict:
    """Momentum inputs from stored D01 bars (#380 S4, decision 2), the same keys as ``technical_from_quote``.

    ``rows`` are ``price_bars_daily`` rows of ONE ticker (nominal, ``core.prices.daily_bars``), up to and including
    ``session``; a later bar is ignored. price = the last close; ``high_52w`` = the highest **High** of the last
    ``BARS_52W`` sessions; ``dist_52w_pct`` keeps the quote path's sign, ``(high - price) / high`` (>= 0), because
    ``DIST_52W_MAX`` compares it with ``<=``; ``sma50`` = the mean of the last ``BARS_SMA`` closes; ``vol_10d`` /
    ``vol_90d`` = the mean volume of the last ``BARS_10D`` / ``BARS_3M`` sessions (Yahoo's "3-month" average, the
    "90d" label) and their ratio. Fewer bars than a window make that input unknown (None): 252 for the high, 63 for
    the volume, 50 for the mean, 10 for the short volume.
    """
    bars = []
    for row in rows or []:
        day = _bar_day(row)
        if day is None or (session is not None and day > session):
            continue
        bars.append((day, to_float(row.get("high")), to_float(row.get("close")), to_float(row.get("volume"))))
    bars.sort(key=lambda b: b[0])

    closes = [b[2] for b in bars]
    price = closes[-1] if closes else None
    high = _max_last([b[1] for b in bars], BARS_52W)
    sma50 = _mean_last(closes, BARS_SMA)
    vol_10d = _mean_last([b[3] for b in bars], BARS_10D)
    vol_90d = _mean_last([b[3] for b in bars], BARS_3M)
    return {
        "price": price,
        "high_52w": high,
        "dist_52w_pct": (high - price) / high if (price is not None and high) else None,
        "sma50": sma50,
        "above_sma50": (price > sma50) if (price is not None and sma50 is not None) else None,
        "vol_10d": vol_10d,
        "vol_90d": vol_90d,
        "vol_ratio": vol_10d / vol_90d if (vol_10d is not None and vol_90d) else None,
    }


def score_ticker(ticker: str, financials: dict, technical: dict, meta: dict | None = None,
                 spine: dict | None = None, pinned_themes: dict | None = None,
                 as_of: date | None = None) -> dict | None:
    """Build the Small/Micro Cap card from already-fetched data; non-US or missing data -> None.

    ``pinned_themes`` is ``taxonomy.themes_for_many`` of the whole batch; without it the ticker is looked up alone.
    ``as_of`` is the run day the record choice is judged on (``_card_anchor``); a Fetch always passes it.
    """
    meta = meta or {}
    info = financials.get("info", {})
    annual = financials.get("annual", [])

    if not _is_us(info):
        return None

    ai, anchor_flags = _card_anchor(annual, as_of)
    cur = annual[ai] if ai is not None else {}
    pi = _prior(annual, ai)
    # Everything below reads ONE fiscal year (the anchor) and its adjacent prior year (#150 U8), through the C18
    # rules (#380 S1): ``flags`` names every figure a rule made unknown or marked; the anchor year's only.
    rev_growth, flags = _growth(annual, ai, "revenue")
    rev_growth_last, _ = _growth(annual, pi, "revenue")
    gross_margin, margin_flags = fiscal_year.gross_margin(cur)

    ocf = to_float(cur.get("operating_cash_flow"))
    capex = to_float(cur.get("capex"))
    if capex is None:
        capex = to_float(cur.get("capital_expenditure"))
    fcf = (ocf - capex) if (ocf is not None and capex is not None) else None

    total_debt = to_float(cur.get("total_debt"))
    # Liquidity = cash + short-term investments, both from the anchor record (#187).
    liquid, _liquidity_basis = liquid_assets(cur)
    total_assets = to_float(cur.get("total_assets"))
    net_debt = (total_debt - liquid) if (total_debt is not None and liquid is not None) else None
    net_debt_assets = _pct_div(net_debt, total_assets)

    # Runway: FCF>=0 => kendi kendini finanse eder (999.0 sentinel, JSON-safe); FCF<0 => liquidity/|FCF| yıl
    runway = None
    if fcf is not None:
        if fcf >= 0:
            runway = 999.0
        elif liquid is not None and fcf < 0:
            runway = liquid / abs(fcf)

    rd_intensity = _pct_div(to_float(cur.get("rd_expense")), to_float(cur.get("revenue")))

    dilution_yoy, share_flags = _share_change(cur, annual[pi] if pi is not None else None)
    flags = [*anchor_flags, *flags, *margin_flags, *share_flags]

    market_cap = financials.get("market_cap") or meta.get("market_cap")
    price = technical.get("price") or financials.get("current_price") or meta.get("price")
    revenue = to_float(cur.get("revenue"))
    ps_ratio = _pct_div(market_cap, revenue)

    # R5 (#88), #266: the sector and industry are the spine's (UW's two levels); the vendor sector and industry in
    # ``financials`` / ``meta`` never fill the card's own fields.
    gics = _spine.gics_of(ticker, _spine.spine_gics([ticker]) if spine is None else spine)
    sector = gics.sector
    # C17 (card 4, #380 S6): a Financial Services or Real Estate name (the spine sector) cannot be compared on revenue,
    # margin and FCF: it keeps its card and points, is flagged, and has no tier.
    not_comparable = sector_rules.sector_status(sector) == sector_rules.NOT_COMPARABLE
    if not_comparable:
        flags.append("sector_not_comparable")

    # ── Sinyal 1: Accelerated Growth ──
    # A sub-criterion whose input is missing is UNKNOWN (``passed`` None), never a fail (#151, decision 8-A).
    s1 = [
        {"label": "Revenue growth ≥ 20%", "value": rev_growth, "unit": "pct", "threshold": "≥ 20%",
         "op": "gte", "param": "rev_growth",
         "passed": tri.rule((rev_growth,), lambda g: g >= REV_GROWTH_MIN)},
        {"label": "Growth accelerating YoY", "value": (rev_growth, rev_growth_last), "unit": "pair_pct", "threshold": "this > last",
         "op": "pair", "param": "accelerating",
         "passed": tri.rule((rev_growth, rev_growth_last), lambda a, b: a > b)},
        {"label": "Gross margin ≥ 40%", "value": gross_margin, "unit": "pct", "threshold": "≥ 40%",
         "op": "gte", "param": "gross_margin",
         "passed": tri.rule((gross_margin,), lambda g: g >= GROSS_MARGIN_MIN)},
    ]

    # ── Sinyal 2: Survival ──
    s2 = [
        {"label": "Free cash flow positive", "value": fcf, "unit": "usd", "threshold": "> 0",
         "op": "gt", "param": "fcf",
         "passed": tri.rule((fcf,), lambda f: f > 0)},
        {"label": "Net debt / assets ≤ 10%", "value": net_debt_assets, "unit": "pct", "threshold": "≤ 10%",
         "op": "lte", "param": "net_debt_assets",
         "passed": tri.rule((net_debt_assets,), lambda n: n <= NET_DEBT_ASSETS_MAX)},
        {"label": "Cash runway ≥ 2 years", "value": runway, "unit": "runway", "threshold": "≥ 2y",
         "op": "gte", "param": "runway",
         "passed": tri.rule((runway,), lambda r: r >= RUNWAY_YEARS_MIN)},
    ]

    # ── Sinyal 3: Momentum/Volume ──
    dist, vol_ratio, above = technical.get("dist_52w_pct"), technical.get("vol_ratio"), technical.get("above_sma50")
    s3 = [
        {"label": "Within 10% of 52-week high", "value": dist, "unit": "pct", "threshold": "≤ 10%",
         "op": "lte", "param": "dist_52w",
         "passed": tri.rule((dist,), lambda d: d <= DIST_52W_MAX)},
        {"label": "10d volume ≥ 1.4× 3m", "value": vol_ratio, "unit": "ratio", "threshold": "≥ 1.4×",
         "op": "gte", "param": "vol_ratio",
         "passed": tri.rule((vol_ratio,), lambda v: v >= VOL_RATIO_MIN)},
        {"label": "Price above 50-day SMA", "value": above, "unit": "bool", "threshold": "> SMA50",
         "op": "bool", "param": "above_sma50",
         "passed": tri.rule((above,), lambda a: a is True)},
    ]

    def _signal(key, subs):
        passed, n_known, total = tri.tally(subs)
        return {"key": key, "name": SIGNAL_NAMES[key], "passed": passed, "known": n_known, "total": total,
                "subs": subs}

    signals = [_signal("growth", s1), _signal("survival", s2), _signal("momentum", s3)]
    total_points = sum(s["passed"] for s in signals)
    # No tier without a comparable sector (C17) or without a recent revenue figure (O-2 fix): the growth signal
    # cannot be read, so two other strong signals would rank a name nobody can judge on growth.
    tier = None if (not_comparable or anchor_flags) else tier_of(signals)

    from RevenueChaser.core.taxonomy import themes_for
    # C21 (#383): a theme label is the pinned NMF medium run or nothing — no keyword matcher, no sector guess.
    themes = pinned_themes.get(ticker, []) if pinned_themes is not None else themes_for(ticker)
    tech_breakthrough = rd_intensity is not None and rd_intensity >= RND_INTENSITY_MIN
    dilution_warning = dilution_yoy is not None and dilution_yoy > DILUTION_WARN
    earnings_date = to_float((meta.get("quote") or {}).get("earningsTimestampStart"))

    return {
        "ticker": ticker,
        "fiscal_year": _fy(cur),
        "name": info.get("longName") or info.get("shortName") or meta.get("name") or ticker,
        "sector": sector,
        "industry": gics.industry,
        "inst_ownership_pct": info.get("heldPercentInstitutions"),
        "price": price,
        "market_cap": market_cap,
        "ps_ratio": ps_ratio,
        "rev_growth": rev_growth,
        "gross_margin": gross_margin,
        "fcf": fcf,
        "runway_years": runway,
        "rd_intensity": rd_intensity,
        "dilution_yoy": dilution_yoy,
        "earnings_date": earnings_date,
        "total_points": total_points,
        "tier": tier,
        "signals": signals,
        "flags": flags,
        **_provenance(annual, ai, pi),
        "themes": themes,
        "tech_breakthrough": tech_breakthrough,
        "dilution_warning": dilution_warning,
        "us_listed": True,
        "source": financials.get("source") or "yfinance",
    }


def _has_answer(fin: dict | None) -> bool:
    """True when a refetched payload is an answer: real ``info`` fields OR non-empty ``annual`` (#260 C15)."""
    return bool(fin) and (has_fields(fin.get("info")) or bool(fin.get("annual")))


def _note_info_refetch(preload: dict | None, outcome: str) -> None:
    """Count a ``stale`` / ``none`` refetch outcome so the run can say so (the batch's ``_Drops`` carries it)."""
    drops = preload.get("drops") if isinstance(preload, dict) else None
    if drops is not None:
        drops.note_info_refetch(outcome)


def _cached_financials(ticker: str, no_fetch: bool = False, preload: dict | None = None) -> dict | None:
    """SEC EDGAR (tam kapsam) + yfinance info (sektör/endüstri/ülke) birleşimi.

    EDGAR companyfacts yıllık finansalları birincil; yfinance yalnızca info
    (sector/industry/country/name/themes) ve EDGAR boşsa fallback sağlar.
    ``no_fetch=True`` (model-only) yalnızca cache'den okur; eksikse None döner.

    A name with EDGAR annual figures costs Yahoo one ``.info`` request, not four (#150 U13); the
    info-only payload is stored flagged ``info_only`` so no other board reads it as a full one.
    ``preload`` is ``_preload`` of the batch: the stored EDGAR rows and payloads read in bulk.
    """
    if preload is not None and ticker in preload["edgar"]:
        edgar_annual, edgar_meta = preload["edgar"][ticker]
    elif preload is not None and no_fetch:
        edgar_annual, edgar_meta = [], {}   # no stored row, and a store-only run never goes online
    else:
        edgar_annual, edgar_meta = sec_edgar.get_annual(ticker, no_fetch=no_fetch)

    if preload is not None:
        financials = preload["payload"].get(ticker)
    else:
        financials = cache_manager.load(ticker, ignore_ttl=no_fetch, allow_info_only=True)
    if financials is not None and financials.get("info_only") and not edgar_annual:
        financials = None  # an info-only row cannot serve a name that has no EDGAR years
    if financials is None:
        if no_fetch:
            if not edgar_annual:
                return None
            financials = {"info": {}}  # EDGAR alone is enough to score a store-only run (#150 U7)
        else:
            # #260 (C15): a refetch that answers nothing must not lose the expired row. Only a fresh answer is
            # saved (and handed to the run's vendor capture); a stale one serves the scorer marked ``_stale``;
            # nothing stored keeps today's scoring path and is never saved as a skeleton.
            stored = cache_manager.load(ticker, ignore_ttl=True, allow_info_only=True)
            if stored is not None and stored.get("info_only") and not edgar_annual:
                stored = None   # an info-only row cannot serve a name with no EDGAR years (as above)
            replies: list[dict] = []

            def fetch():
                reply = (yfinance_client.get_info if edgar_annual else yfinance_client.get_financials)(ticker)
                replies.append(reply)
                return reply
            # With no EDGAR years only Yahoo's ``annual`` can score the name, so an info-only reply is no answer
            # there (R2B-4). The board catches what main caught; anything else reaches score_errors (R2B-3, #202).
            is_answer = _has_answer if edgar_annual else (lambda fin: bool(fin) and bool(fin.get("annual")))
            financials, status = refetch_or_keep(stored, fetch, is_answer, catch=(OSError, ValueError, KeyError),
                                                 label=ticker, log_level=logging.WARNING)
            if status == "fresh":
                cache_manager.save(ticker, financials)  # info her zaman cache'lenir (yeniden taramalar hızlı)
                if preload is not None and preload.get("vendor") is not None:
                    preload["vendor"].add_yfinance(ticker, financials)   # D12 (#297): stored by the run, per batch
            elif status == "stale":
                _note_info_refetch(preload, status)     # the expired row serves the scorer, marked ``_stale``
            else:
                _note_info_refetch(preload, status)
                financials = {"info": {}} if edgar_annual else None
            if (status != "fresh" and not edgar_annual and replies and _has_answer(replies[0])
                    and preload is not None and preload.get("vendor") is not None):
                # A no-EDGAR name's reply with info but no statements is not saved over the stored row, but its
                # valuation and ownership rows still reach the run's vendor capture (R2B-4, R4 delta review).
                preload["vendor"].add_yfinance(ticker, replies[0])

    if edgar_annual:
        financials["annual"] = edgar_annual
        financials["source"] = EDGAR_SOURCE
        info = financials.get("info") or {}
        if edgar_meta.get("is_us") is False:
            info["country"] = "Foreign"
        elif not info.get("country"):
            info["country"] = "United States"
        if edgar_meta.get("entity_name") and not info.get("longName"):
            info["longName"] = edgar_meta["entity_name"]
        financials["info"] = info

    return financials


def _d01_session() -> date | None:
    """The newest D01 session SPY has a bar for: the session an AG card's momentum is read up to (#380 S4).

    Read through ``ledger.d01_last_bars``, the helper the freeze and the history snapshot use (``d01_has_session``
    is the same read): no price file, no table or no SPY bar reads as no session, and the scan then scores every
    card with unknown momentum instead of failing.
    """
    last = ledger.d01_last_bars(["SPY"]).get("SPY")
    if not last:
        return None
    try:
        return date.fromisoformat(str(last[0])[:10])
    except ValueError:
        return None


def _bars_by_ticker(tickers, session: date | None) -> dict[str, list[dict]]:
    """One windowed nominal D01 read for ``tickers`` up to ``session``: ``{ticker: rows}``.

    ``prices.daily_bars``'s nominal basis (K8a window: ``session - BARS_WINDOW_DAYS``), so a split is applied and a
    dividend is not: the momentum inputs are the price levels of the scored session. A store without D01 bars reads
    as ``{}`` (the 247 research script runs on a snapshot with no price file).
    """
    if not tickers or session is None:
        return {}
    frame = prices.daily_bars(list(tickers), basis="nominal", start=session - timedelta(days=BARS_WINDOW_DAYS),
                              end=session)
    if frame is None or frame.empty:
        return {}
    return {str(ticker): rows.to_dict("records") for ticker, rows in frame.groupby("ticker", sort=False)}


def _newest_bar_sessions(bars: dict) -> dict[str, date]:
    """Each ticker's newest bar day in a batch's ``bars`` (``daily_bars`` sorts by ticker then date)."""
    out = {}
    for ticker, rows in bars.items():
        for row in reversed(rows or []):
            day = _bar_day(row)
            if day is not None:
                out[ticker] = day
                break
    return out


def _preload(tickers: list[str], no_fetch: bool, session: date | None = None) -> dict:
    """The stored EDGAR rows, cached payloads, pinned themes and nominal D01 bars of ``tickers``.

    The boards read the store in a fixed number of queries per batch, and the price bars are no exception: ONE
    windowed ``price_bars_daily`` read for the batch (``prices.daily_bars``, K8a), up to ``session``, the newest D01
    session SPY has. ``{"bars": {}, "session": None}`` when the store holds no D01 bars at all.
    """
    session = session or _d01_session()
    return {
        "edgar": sec_edgar.stored_annual_many(tickers),
        "payload": cache_manager.load_many(tickers, ignore_ttl=no_fetch, allow_info_only=True),
        "themes": themes_for_many(tickers),
        "bars": _bars_by_ticker(tickers, session),
        "session": session,
    }


class _Drops:
    """Why names got no card, by reason (#150 / #232): the funnel's drop steps. Thread-safe.

    It also carries the run's reporting notes (#41, #260 C15) — ``info_refetch_failed`` and the scored names under
    ``THIN_DOLLAR_VOLUME`` — which are counted in the summary and never enter the drop arithmetic.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.by_reason: dict[str, list[str]] = {}
        self.info_refetch_failed = 0   # a refetch that answered nothing (stale or none, #260 C15)
        self.thin: list[str] = []      # scored names under THIN_DOLLAR_VOLUME (#41): a note, never a drop

    def add(self, reason: str, ticker: str) -> None:
        with self._lock:
            self.by_reason.setdefault(reason, []).append(ticker)

    def note_info_refetch(self, outcome: str) -> None:
        """A ``stale`` or ``none`` info refetch, counted once per name so the run summary can report it."""
        with self._lock:
            self.info_refetch_failed += 1

    def note_thin(self, ticker: str) -> None:
        """A scored name whose ``price * vol_90d`` is under ``THIN_DOLLAR_VOLUME``: reported, never dropped."""
        with self._lock:
            self.thin.append(ticker)


def _technical_for(ticker: str, financials: dict, meta: dict, preload: dict | None) -> dict:
    """The momentum inputs of one name: its D01 bars, the price falling back to financials / the quote (#380 S4).

    In a Fetch the batch's ``preload`` carries the bars (``_preload``); the per-ticker path (a research script)
    reads the ticker's own window. A name with no bars keeps a card with unknown momentum (decision 3): only the
    PRICE falls back, to ``financials.current_price`` and then the quote's ``regularMarketPrice``, so a card still
    shows what it costs. Nothing here calls a provider.
    """
    if preload is not None and "bars" in preload:
        rows, session = preload["bars"].get(ticker), preload.get("session")
    else:
        session = _d01_session()
        rows = _bars_by_ticker([ticker], session).get(ticker)
    technical = technical_from_bars(rows or [], session)
    if technical.get("price") is None:
        quote = meta.get("quote") or {}
        technical["price"] = (to_float(financials.get("current_price")) or to_float(meta.get("price"))
                              or to_float(quote.get("regularMarketPrice")))
    return technical


def _is_thin(price, vol_90d) -> bool:
    """True when ``price * vol_90d`` is under ``THIN_DOLLAR_VOLUME`` (#41).

    Unknown inputs (no price, no 90-day volume) are UNTESTED, never thin: the name is counted nowhere, because
    calling a name whose volume nobody read "thin" would be a lie.
    """
    return price is not None and vol_90d is not None and price * vol_90d < THIN_DOLLAR_VOLUME


def enrich_and_score(ticker: str, meta: dict, no_fetch: bool = False,
                     spine: dict | None = None, preload: dict | None = None) -> dict | None:
    """Ticker için yfinance finansal (cache'li) çeker; momentum D01 barlarından (#380 S4).

    A name that gets no card is recorded in ``preload["drops"]`` (when the batch has one) with the reason.
    A US name whose newest annual record ended more than ``fiscal_year.STALE_DAYS`` before the run day
    (``preload["as_of"]``, else today in UTC) gets no card (card O-2, #380 S1; ``fiscal_year.latest_is_stale``: a fresh
    filer whose last revenue figure is older keeps its card, see ``_card_anchor``).
    ``no_price`` is decided on every price source (the D01 close, ``financials.current_price``, the quote), never on
    the momentum inputs: a name with a price but no bars is a card with unknown momentum (decision 3), so an expired
    or missing quote costs no request.
    """
    drops = preload.get("drops") if preload is not None else None

    def dropped(reason: str) -> None:
        if drops is not None:
            drops.add(reason, ticker)
        return None

    try:
        financials = _cached_financials(ticker, no_fetch=no_fetch, preload=preload)
    except (OSError, ValueError, KeyError) as exc:  # pragma: no cover - network / bad payload
        # Anything else (AttributeError, TypeError, ...) is a programming error: it
        # propagates so the runner counts it in ``score_errors`` (#202).
        logger.debug("fin %s: %s", ticker, exc)
        return dropped("fetch_failed")
    if not financials or not financials.get("annual"):
        return dropped("no_annual")
    annual = financials["annual"]
    as_of = (preload or {}).get("as_of") or _run_day()
    if fiscal_year.latest_is_stale(annual, as_of) and _is_us(financials.get("info") or {}):  # foreign stays "foreign"
        return dropped("stale_annual")
    technical = _technical_for(ticker, financials, meta, preload)
    if technical.get("price") is None:
        return dropped("no_price")            # no price in any source, momentum unknown is still a card
    stored = preload["earnings"] if preload is not None and "earnings" in preload else earnings_dates.stored_next_dates()
    scored_meta = earnings_dates.with_stored_date({**meta, "ticker": ticker}, stored)
    card = score_ticker(ticker, financials, technical, scored_meta, spine,
                        pinned_themes=preload["themes"] if preload is not None else None, as_of=as_of)
    if card is None:
        return dropped("foreign")
    # #41: the one place the technical inputs are in hand for a scored name (the card does not carry vol_90d).
    if drops is not None and _is_thin(technical.get("price"), technical.get("vol_90d")):
        drops.note_thin(ticker)
    return card


# Funnel steps in the order a name can leave: (key, label, by design). ``excluded_types`` and the scorer's own
# reasons come first; ``score_errors`` and ``unrecorded`` are added by ``_funnel_steps``.
_DROP_STEPS = (
    ("foreign", "Non-US filers (country or exchange, or a 20-F / 40-F filer)", True),
    ("stale_annual", f"Latest annual report ended more than {fiscal_year.STALE_DAYS} days ago", True),
    ("no_annual", "No annual financials (no EDGAR record and no Yahoo statements)", False),
    ("no_price", "No price in any source (the D01 close, the financials, the stored quote)", False),
    ("fetch_failed", "Provider error while reading the financials", False),
)


def _funnel_steps(universe_total: int, excluded_types: int, drops: _Drops, score_errors: dict[str, list[str]],
                  scored: int, low_liquidity: int = 0, unreadable_sec: int = 0) -> list[dict]:
    """The shared-format funnel (``core.funnel``): universe, then every reason a name has no card, then scored.

    The drops sum to ``universe_total - scored`` by construction: whatever no reason accounts for is shown as
    ``unrecorded`` instead of vanishing. A reason with no names has no row.

    ``low_liquidity`` and ``unreadable_sec`` (#41) are ``note`` steps, never drops: those names HAVE a card, so
    counting them as drops would break the arithmetic above and misstate the board.
    """
    step = funnel_mod.step
    steps = [step("universe", "Universe, all market caps", universe_total, "start")]
    accounted = excluded_types
    if excluded_types:
        steps.append(step("excluded_types", "Excluded security types (ETF, CEF, SPAC, preferred, unit, warrant)",
                          excluded_types, "drop", by_design=True))
    for key, label, by_design in _DROP_STEPS:
        names = sorted(drops.by_reason.get(key, []))
        if names:
            accounted += len(names)
            steps.append(step(key, label, len(names), "drop", by_design=by_design,
                              tickers=names[:funnel_mod.MAX_TICKERS]))
    errored = sorted(t for names in score_errors.values() for t in names)
    if errored:
        accounted += len(errored)
        steps.append(step("score_errors", "Scoring errors (see the log)", len(errored), "drop", by_design=False,
                          tickers=errored[:funnel_mod.MAX_TICKERS]))
    rest = universe_total - accounted - scored
    if rest > 0:
        steps.append(step("unrecorded", "Not scored, reason not recorded", rest, "drop", by_design=False))
    if low_liquidity:
        steps.append(step("low_liquidity", f"Scored, under ${THIN_DOLLAR_VOLUME:,} average daily dollar volume",
                          low_liquidity, "note"))
    if unreadable_sec:
        steps.append(step("unreadable_sec", "Scored, but the financials are Yahoo statements, not EDGAR filings",
                          unreadable_sec, "note"))
    steps.append(step("scored", "Scored (a card on the board)", scored, "result"))
    return steps


def _institutional(ticker: str, totals: dict) -> dict:
    """13F kurumsal sahiplik (en son dönem + QoQ hisse değişimi)."""
    out = {"inst_holders": None, "inst_new": None, "inst_exited": None,
           "inst_buy_flow": None, "inst_sell_flow": None,
           "inst_shares": None, "inst_shares_delta_pct": None}
    periods = totals.get(ticker)
    if not periods:
        return out
    try:
        sorted_periods = sorted(periods)
    except TypeError:
        return out
    latest = sorted_periods[-1]
    p = periods[latest]
    out["inst_holders"] = p.get("holders")
    out["inst_new"] = p.get("new_holders")
    out["inst_exited"] = p.get("exited_holders")
    out["inst_buy_flow"] = p.get("buy_flow")
    out["inst_sell_flow"] = p.get("sell_flow")
    out["inst_shares"] = p.get("shares")
    if len(sorted_periods) >= 2:
        prev = periods[sorted_periods[-2]]
        prev_shares = prev.get("shares")
        cur = p.get("shares")
        if prev_shares is not None and cur is not None and prev_shares:
            out["inst_shares_delta_pct"] = (cur - prev_shares) / prev_shares
    return out


def _points_bucket(p: int) -> str:
    if p >= 9:
        return "pts_9"
    if p >= 7:
        return "pts_8_7"
    if p >= 5:
        return "pts_6_5"
    return "pts_lt5"


GROWTH_SORT_CAP = 1.0   # growth sorts capped at +100 %: a bigger base effect does not outrank a steadier name


def _sort_growth(c: dict) -> tuple:
    """The growth part of the sort key: capped at ``GROWTH_SORT_CAP``; unknown, or flagged ``suspicious_growth``
    (> +500 %, #380 S1), after every known growth."""
    g = c.get("rev_growth")
    if g is None or "suspicious_growth" in (c.get("flags") or []):
        return (1, 0.0)
    return (0, -min(g, GROWTH_SORT_CAP))


def _sort_cards(cards: list[dict]) -> None:
    """Tier (3, 2, none), then points, then capped growth, then ticker (#380 S2)."""
    cards.sort(key=lambda c: (-(c.get("tier") or 0), -(c["total_points"] or 0), _sort_growth(c), c["ticker"]))


def scored_before(cards: list[dict]) -> bool:
    """True when a card was scored by an older model version than this code's (rows a Fetch stored before an
    upgrade: no ``model_version``, or another one). The board then shows every card under a banner (#380 S2)."""
    from RevenueChaser.module_asymmetric_growth import freeze
    current = freeze.model_version()
    return any(c.get("model_version") != current for c in cards)


def _summary(cards: list[dict], excluded_types: int | None, score_errors: dict[str, int],
             info_refetch_failed: int = 0, thin: list[str] | None = None) -> dict:
    """The run's counts. ``thin`` is the batch's low-liquidity note (``_Drops.thin``, #41).

    ``low_liquidity`` and ``unreadable_sec`` are reporting only and never overlap: a scored name is counted once,
    low liquidity first. A summary counted from cards alone (a Refresh with no stored header) has no liquidity
    basis, so its ``low_liquidity`` is ``None`` (unknown) and never 0: the cards do not carry ``vol_90d``, and a
    board that prints 0 for a figure nobody measured is making a claim. ``unreadable_sec`` is a card property, so
    it is counted on that path too.
    """
    thin_names = set(thin or ())
    return {
        "scanned": len(cards),
        "excluded_types": excluded_types,
        "no_spine_gics": sum(1 for c in cards if c["sector"] == "Unclassified"),
        "pts_9": sum(1 for c in cards if (c["total_points"] or 0) >= 9),
        "pts_8_7": sum(1 for c in cards if 7 <= (c["total_points"] or 0) <= 8),
        "pts_6_5": sum(1 for c in cards if 5 <= (c["total_points"] or 0) <= 6),
        "pts_lt5": sum(1 for c in cards if (c["total_points"] or 0) < 5),
        "tier_3": sum(1 for c in cards if c.get("tier") == 3),
        "tier_2": sum(1 for c in cards if c.get("tier") == 2),
        "tier_none": sum(1 for c in cards if c.get("tier") not in (2, 3)),
        "sector_not_comparable": sum(1 for c in cards if "sector_not_comparable" in (c.get("flags") or [])),
        "no_recent_revenue": sum(1 for c in cards if "no_recent_revenue" in (c.get("flags") or [])),
        "disruptive_themed": sum(1 for c in cards if c["themes"]),
        "tech_breakthrough": sum(1 for c in cards if c["tech_breakthrough"]),
        "score_errors": sum(score_errors.values()),
        "score_error_types": score_errors,
        "info_refetch_failed": info_refetch_failed,   # #260 C15: a stale or empty Yahoo refetch
        # #41: reporting only, each scored name counted once (low liquidity first, then unreadable SEC). The names
        # that got no card for a filings reason (``no_annual`` / ``fetch_failed``) are drops and are not counted here.
        "low_liquidity": None if thin is None else sum(1 for c in cards if c["ticker"] in thin_names),
        "unreadable_sec": sum(1 for c in cards if c["ticker"] not in thin_names
                              and c.get("source") != EDGAR_SOURCE),
    }


def stored_result() -> dict:
    """What a Refresh shows (S5, Common K4 #383 R1): the last Fetch, read back, never re-scored.

    The cards are the ``ticker_scores`` rows (``ticker_store``), sorted as a Fetch sorts them, with their index
    labels. Universe, summary and funnel come from the last Fetch's history header (``core/history.last_run``) when
    it is this scan's (its ``asof`` is the table's); otherwise the summary is counted from the cards and there is no
    funnel. ``data_as_of`` (the D01 session the Fetch's prices came from, #380 S4) is restored from that header too.
    No stored card: the no-data message (no stored universe, or no stored scan). No scorer, no network, no write.
    """
    from RevenueChaser.core import history, ticker_store
    from RevenueChaser.core.index_registry import annotate_cards
    from RevenueChaser.core.scan_quality import no_data_message
    from RevenueChaser.module_asymmetric_growth import history as ag_history

    cards = ticker_store.all_cards()
    asof = ticker_store.asof()
    result = {"asof": asof or "", "data_as_of": None, "universe_total": None, "universe_generated_at": None,
              "universe_refreshed_by": None, "universe_partial": False, "funnel_steps": None, "summary": {},
              "cards": cards, "thresholds": THRESHOLDS}
    if not cards:
        stored = bool(rawstore.query("SELECT 1 FROM equity_universe LIMIT 1"))
        reason = "no_scan_stored" if stored else "no_universe_stored"
        result.update(universe_total=0, no_data_reason=reason, no_data_message=no_data_message(reason))
        return result
    _sort_cards(cards)
    annotate_cards(cards)
    try:
        head = history.last_run(ag_history.BOARD)
    except Exception as exc:  # noqa: BLE001 - a locked history file: the cards still show
        logger.warning("AG Refresh: the last Fetch's header could not be read: %s", exc)
        head = None
    ctx = (head or {}).get("context") or {}
    if asof and ctx.get("asof") == asof:
        result.update(universe_total=ctx.get("universe_total"), universe_generated_at=ctx.get("universe_generated_at"),
                      universe_refreshed_by=ctx.get("universe_refreshed_by"),
                      universe_partial=bool(ctx.get("universe_partial")), funnel_steps=head.get("funnel"),
                      data_as_of=ctx.get("data_as_of"),
                      summary=ctx.get("summary") or _summary(cards, None, {}))
    else:
        result["summary"] = _summary(cards, None, {})
    return result


def run_asymmetric_growth_hunter(uw=None, max_workers: int = 10,
                                 refresh_universe: bool = False,
                                 no_fetch: bool = False, run_id: str | None = None) -> dict:
    """Evreni çeker, paralel skorlar, 13F ile zenginleştirir, özet + kart listesi döndürür.

    ``refresh_universe=True`` forces a fresh screener fetch (``refresh=True``);
    otherwise the shared cached universe is reused. ``no_fetch=True`` (model-only)
    runs purely from cached per-ticker data — no network.

    The yfinance answers a Fetch gets are stored in ``vendor_financials`` / ``vendor_valuation`` (#297) after each
    batch, stamped ``run_id`` (the job's id; a fresh one without it). A store failure is a job note, not an error.
    ``data_as_of`` is the newest D01 session among the cards this Fetch scored (#380 S4): the price session their
    momentum came from, None when no card has a bar.
    """
    from RevenueChaser.module_sec_13f import engine as sec_13f

    universe, u_stats = fetch_universe_stats(uw, refreshed_by="Asymmetric Growth",
                                             refresh=refresh_universe, no_fetch=no_fetch)
    # R5 D-2 (#88): the identity master's excluded types (CEF, ETF/ETP, SPAC, preferred, unit, warrant) never screen.
    excluded = _spine.excluded_tickers()
    to_score = [r for r in universe if r["ticker"] not in excluded]
    spine = _spine.spine_gics()
    cards = []
    data_as_of: date | None = None           # the newest D01 session of a card's bars (#380 S4)
    score_errors: dict[str, int] = {}  # exception type -> tickers dropped by it (#202)
    error_tickers: dict[str, list[str]] = {}  # the same, by name (the funnel lists them)
    drops = _Drops()
    # A Refresh fetches nothing, so stores nothing (#313). The universe job is D20's yfinance writer (#300).
    vendor = None if no_fetch else vendor_data.Capture(ownership=True)
    vendor_run = run_id or ledger.new_run_id()
    run_day = _run_day()                    # anchor age is judged against the day the scan runs (card O-2)
    scan_session = _d01_session()           # one D01 cutoff for the whole Fetch, so no batch reads a newer SPY bar
    stored_earnings = earnings_dates.stored_next_dates()      # #304: one read per run, not per batch
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        # The store is read in bulk per batch (a fixed number of queries each), not once per ticker (#150 U13);
        # a batch bounds how many EDGAR and payload rows are in memory at once.
        for start in range(0, len(to_score), PRELOAD_BATCH):
            if vendor is not None:
                vendor.flush(run_id=vendor_run, label="Asymmetric Growth")   # the batch before this one
            job_control.checkpoint()            # a cancel or a passed deadline (#243) loads no further batch
            batch = to_score[start:start + PRELOAD_BATCH]
            preload = _preload([r["ticker"] for r in batch], no_fetch, scan_session)
            preload["earnings"] = stored_earnings   # #304: read once per run, not per batch
            preload["drops"] = drops
            preload["vendor"] = vendor
            preload["as_of"] = run_day
            bar_sessions = _newest_bar_sessions(preload.get("bars") or {})   # where each card's price date comes from
            futs = {ex.submit(enrich_and_score, r["ticker"], r, no_fetch, spine, preload): r["ticker"]
                    for r in batch}
            for fut in as_completed(futs):
                if job_control.stop_requested():  # stop scoring: nothing is returned to write or freeze
                    job_control.stop_pool(futs)
                try:
                    card = fut.result()
                except Exception as exc:  # per-ticker isolation; counted and shown, never silent
                    score_errors[type(exc).__name__] = score_errors.get(type(exc).__name__, 0) + 1
                    error_tickers.setdefault(type(exc).__name__, []).append(futs[fut])
                    logger.warning("AG scoring failed for %s: %s: %s", futs[fut], type(exc).__name__, exc)
                    continue
                if card:
                    cards.append(card)
                    day = bar_sessions.get(card["ticker"])
                    if day is not None and (data_as_of is None or day > data_as_of):
                        data_as_of = day
    if vendor is not None:
        vendor.flush(run_id=vendor_run, label="Asymmetric Growth")

    totals = sec_13f.load_totals()
    from RevenueChaser.module_asymmetric_growth import freeze
    version = freeze.model_version()        # the rows say which rules scored them (old-rows view, #380 S2)
    for c in cards:
        c.update(_institutional(c["ticker"], totals))
        c["model_version"] = version

    _sort_cards(cards)

    from RevenueChaser.core.index_registry import annotate_cards
    annotate_cards(cards)

    summary = _summary(cards, len(universe) - len(to_score), score_errors, drops.info_refetch_failed, drops.thin)

    result = {
        "asof": datetime.now(timezone.utc).isoformat(),
        "data_as_of": data_as_of.isoformat() if data_as_of else None,
        "universe_total": len(universe),
        "universe_generated_at": u_stats.get("universe_generated_at"),
        "universe_refreshed_by": u_stats.get("universe_refreshed_by"),
        "universe_partial": bool(u_stats.get("partial")),
        "funnel_steps": _funnel_steps(len(universe), summary["excluded_types"], drops, error_tickers, len(cards),
                                      summary["low_liquidity"], summary["unreadable_sec"]),
        "summary": summary,
        "cards": cards,
        "thresholds": THRESHOLDS,
    }
    if not universe:
        # The board says why it is empty (#145): a Refresh never loads the universe, a Fetch may find none.
        from RevenueChaser.core.scan_quality import no_data_message
        reason = "no_universe_stored" if no_fetch else "no_universe"
        result["no_data_reason"] = reason
        result["no_data_message"] = no_data_message(reason)
    return result


# Varsayılan eşikler — JS slider'ları bununla başlatılır (client-side re-score).
THRESHOLDS = {
    "rev_growth": REV_GROWTH_MIN,
    "accelerating": True,
    "gross_margin": GROSS_MARGIN_MIN,
    "fcf": True,
    "net_debt_assets": NET_DEBT_ASSETS_MAX,
    "runway": RUNWAY_YEARS_MIN,
    "dist_52w": DIST_52W_MAX,
    "vol_ratio": VOL_RATIO_MIN,
    "above_sma50": True,
    "rd_intensity": RND_INTENSITY_MIN,
    "dilution": DILUTION_WARN,
}

# The C18 rules the scan applies (#380 S1): part of the model version, so a change in ``core/fiscal_year.py``
# moves the ledger's hash too.
RULES = {
    "stale_days": fiscal_year.STALE_DAYS,
    "suspicious_growth": fiscal_year.SUSPICIOUS_GROWTH,
    "tier_min_passes": TIER_MIN_PASSES,
    "not_comparable_sectors": sorted(sector_rules.NOT_COMPARABLE_SECTORS),   # #380 S6 (C17)
    # #380 S4: the momentum windows, read from D01 (nominal) up to the scan's session. In RULES so a change moves
    # the ledger's hash like every other rule the board scores with.
    "momentum_windows": {"high_52w": BARS_52W, "sma50": BARS_SMA, "vol_3m": BARS_3M, "vol_10d": BARS_10D},
}

# ── Forecast ledger (#128) ──
# Bump by hand when the scoring logic changes in a way THRESHOLDS and RULES do not show. The ledger stamps
# every frozen row with "<SELECTION_VERSION>+<hash of THRESHOLDS, DEFAULT_MC_RANGE and RULES>"; a golden
# test (tests/test_ag_freeze_128.py) fails when a threshold changes without this bump.
# 4: #380 S1 honest signals (C18 growth / margin / share basis, stale anchors excluded).
# 5: #380 S2 tiers (sort by tier, ledger bucket tier + points).
# 6: #380 S4 momentum from D01 bars of the scored session (no per-name Yahoo technical fallback).
SELECTION_VERSION = "6"
# A run where more than this share of the tickers it attempted failed to score is not frozen:
# the board is missing too much to stand for the day (#202 counts the errors).
FREEZE_MAX_SCORE_ERROR_RATE = 0.01
