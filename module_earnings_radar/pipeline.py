"""UW-first earnings pipeline — Unusual Whales API for all data."""
import json
import logging
from datetime import date, datetime, timedelta, timezone

from RevenueChaser.core.providers.unusual_whales import UWClient
from RevenueChaser.module_earnings_radar.classifier import classify_ticker, TICKER_OVERRIDES, get_cluster_label
from RevenueChaser.module_earnings_radar.rules import (
    rule1_drivers, rule2_guidance, rule3_financial, rule4_volatility,
    rule5_positioning, rule6_macro, rule7_accounting, rule8_sentiment,
)
from RevenueChaser.module_earnings_radar.composite import compute_composite_scores, select_action, classify_bias, classify_conviction, recommend_options_structure
from RevenueChaser.module_earnings_radar import earnings_store, stored_inputs
from RevenueChaser.core import calendar, rawstore, vendor_data
from RevenueChaser.core import universe as _spine
from RevenueChaser.core.gics_map import is_unclassified
from RevenueChaser.core.config import (
    RULE_WEIGHTS, INST_FLOW_MIN_NET, MAX_DEEP_CHECKS_PER_DAY,
    EARNINGS_SCORE_WORKERS as SCORE_WORKERS,
)
from RevenueChaser.core.calendar import now_ny, today_ny
from RevenueChaser.core.reaction_anchor import reaction_anchor, session_closes, timing_kind
from RevenueChaser.module_earnings_radar.models import TradeCard, RuleResult
from RevenueChaser.module_sector_rotation.engine import get_quadrant_for_ticker
from RevenueChaser.module_leaps.engine import load_cache as leaps_load_cache

logger = logging.getLogger(__name__)

# Sessions of price history per ticker, fetched in the one OHLC request (UW over-asks rows, so
# ``days`` means sessions). Four quarterly prints span ~190 sessions; 270 leaves a margin.
REACTION_OHLC_SESSIONS = 270
READER_OHLC_SESSIONS = 60


def yield_beta_tstat(stock_by_date: dict[str, float], yield_by_date: dict[str, float],
                     beta: float | None) -> float | None:
    """The OLS t-stat of R6's yield beta, over the same return pairs as ``rule6_macro.yield_beta``.

    ``rule6_macro.score`` skips the NO_TRADE veto when ``|t| < 2`` (the noise gate, ``docs/rules/redesign.md``): a
    60-return beta of a volatile name can pass 4 from noise alone. None when there is no beta or the t-stat
    cannot be formed (fewer than three pairs, no yield variance, a perfect fit) — the gate then stays open.
    """
    if beta is None:
        return None
    dates = sorted(set(stock_by_date) & set(yield_by_date))
    r, dy = [], []
    for a, b in zip(dates, dates[1:]):
        p0, p1 = stock_by_date[a], stock_by_date[b]
        y0, y1 = yield_by_date[a], yield_by_date[b]
        if p0 in (None, 0) or p1 is None or y0 is None or y1 is None:
            continue
        r.append((p1 - p0) / p0)
        dy.append((y1 - y0) / 100.0)
    n = len(r)
    if n < 3:
        return None
    mean_r, mean_dy = sum(r) / n, sum(dy) / n
    sxx = sum((x - mean_dy) ** 2 for x in dy)
    sse = sum((ri - mean_r - beta * (di - mean_dy)) ** 2 for ri, di in zip(r, dy))
    if sxx == 0 or sse == 0:
        return None
    return beta / ((sse / (n - 2)) / sxx) ** 0.5


def r6_beta(bars: list, yield_by_date: dict[str, float]) -> tuple[float | None, float | None]:
    """``(beta, t)``: R6's yield beta and its t-stat from the reader's bar slice (the last 60 sessions).

    The one place the card's R6 inputs are formed, so the E5 card script counts what the board does.
    """
    stock_by_date = {}
    for c in bars or []:
        d = str(c.get("date") or c.get("datetime") or "")[:10]
        if d and c.get("close"):
            try:
                stock_by_date[d] = float(c["close"])
            except (TypeError, ValueError):
                continue
    beta = rule6_macro.yield_beta(stock_by_date, yield_by_date or {})
    return beta, yield_beta_tstat(stock_by_date, yield_by_date or {}, beta)


def _agh_context(ticker: str) -> dict:
    """Cache-first AGH snapshot from ticker_store. Missing row → empty defaults."""
    out = {"agh_points": None, "agh_accel": None, "agh_owners": "", "agh_dilution": False}
    try:
        from RevenueChaser.core.ticker_store import get as ts_get
        row = ts_get(ticker)
    except Exception:
        row = None
    if not row:
        return out
    pts = row.get("total_points")
    try:
        out["agh_points"] = int(pts) if pts is not None else None
    except (TypeError, ValueError):
        out["agh_points"] = None
    for sig in row.get("signals") or []:
        if sig.get("key") != "growth":
            continue
        for sub in sig.get("subs") or []:
            label = (sub.get("label") or "") + " " + (sub.get("param") or "")
            if "accelerat" in label.lower():
                out["agh_accel"] = bool(sub.get("passed"))
                break
    buy, sell = row.get("inst_buy_flow"), row.get("inst_sell_flow")
    if buy is not None and sell is not None:
        try:
            b, s = float(buy), float(sell)
            if b > s:
                out["agh_owners"] = "Accumulating"
            elif s > b:
                out["agh_owners"] = "Distributing"
            else:
                out["agh_owners"] = "Mixed"
        except (TypeError, ValueError):
            pass
    out["agh_dilution"] = bool(row.get("dilution_warning"))
    return out


def _strike_fields(payload) -> list[dict]:
    """A spot-exposure answer cut to the 13 fields ``normalize_strike_rows`` reads (strike, 3 greeks x 2 books x
    call/put); the rest of each UW strike row is not kept."""
    from RevenueChaser.core.gex_profile import _GREEKS
    keep = ["strike"] + [f"{side}_{g}{book}" for g in _GREEKS for book in ("_oi", "_vol") for side in ("call", "put")]
    data = payload.get("data") if isinstance(payload, dict) else payload
    return [{k: r.get(k) for k in keep if k in r} for r in data or [] if isinstance(r, dict)]


def nrc_acceleration(card) -> float | None:
    """NRC acceleration 0–100 from AGH accel flag. Unknown → None (skip weight)."""
    flag = getattr(card, "agh_accel", None)
    if flag is True:
        return 80.0
    if flag is False:
        return 25.0
    return None


def nrc_quality(card) -> float | None:
    """Map R7 Sloan score (−100..+100) to 0–100. Unavailable → None."""
    r7 = (getattr(card, "rule_audit", None) or {}).get("rule7_accounting") or {}
    if not r7.get("available") or r7.get("score") is None:
        return None
    try:
        return max(0.0, min(100.0, (float(r7["score"]) + 100.0) / 2.0))
    except (TypeError, ValueError):
        return None


class EarningsPipeline:
    def __init__(self, tickers: list[str], uw: UWClient, cal_meta: dict = None, nrc_results: dict = None, etf_quadrant_map: dict = None):
        self.tickers = tickers
        self.uw = uw
        self.cal_meta = cal_meta or {}
        self.nrc = nrc_results or {}
        self.etf_quadrant_map = etf_quadrant_map or {}
        self._screener_cache = None
        # The screener batch's outcome (``{"status", "error"}``), kept for the run's history note and
        # the board notice (E2, #372); None until the cache is read.
        self.screener_result = None
        self._shorts_cache = None
        self._dgs10_cache = None
        self._rate_events = None        # the stored economic calendar, read once per run (E5d, #372)
        self._peers_registered = False
        self._preselected_tickers = None
        # The stored D01 bars for the tickers that will be scored, read once before the workers start (E5e, #372).
        self._ohlc_bars = None
        # The stored estimate snapshots for the tickers that will be scored (E6, #372, D14).
        self._estimates = None
        # Which tickers read their year of bars from the store and which from UW (the input_sources note).
        self._ohlc_stored: set = set()
        self._ohlc_live: set = set()
        self._bypass_tickers = set()
        self._pre_scores = {}
        self._watchlist = set()
        self._filter_outcome = {}
        self._filter_stats = {"total": 0, "passed": 0, "bypassed": 0, "filtered": 0,
                               "by_reason": {}, "excluded": []}
        # The UW statements the scoring workers read; the runner stores them with the job's id (#297).
        self.vendor = vendor_data.Capture()
        # The UW earnings rows the scoring workers read; the runner stores the changed prints (#298, D13).
        self.earnings_capture = earnings_store.Capture()
        # What this run read from the store and what it asked the provider for (E5, #372): the runner fills
        # "calendar" from the calendar read, each warm step fills its own slot, and the run's history records it.
        self.input_sources = {
            "calendar": {"stored": 0, "live": 0, "session": None},
            "short_interest": {"stored": 0, "live": 0, "session": None},
            "dgs10": "none",
            "rate_events": "none",
            "ohlc": {"stored": 0, "live": 0},
            "estimates": {"stored": 0, "missing": 0},
        }

    @property
    def screener_cache(self) -> dict[str, dict]:
        if self._screener_cache is None:
            fn = getattr(self.uw, "screener_batch_result", None)
            if fn is not None:
                # Keep the batch outcome: a partial/failed answer means a name can have no row at all,
                # which is unknown liquidity, not thin liquidity (E2, #372).
                res = fn(self.tickers)
                self.screener_result = {"status": res.status, "error": res.error}
                self._screener_cache = res.data or {}
            else:                                 # fakes and older clients only offer the plain batch
                self._screener_cache = self.uw.screener_batch(self.tickers)
                self.screener_result = {"status": "ok", "error": None}
        return self._screener_cache

    def _warm_short_interest(self) -> dict[str, dict]:
        """Fetch short-interest rows once for the tickers that will be deep-scored.

        The nightly D10 sweep already stored them (E5c, #372), so UW's batch /short_screener (one extra round per
        ~100 tickers) is asked only for the tickers the store does not cover — and not at all when it covers them
        all. Stored rows are parsed with ``UWClient._parse_short_row``, the mapping the live answer would have gone
        through, so R5 cannot tell the two apart.

        Every ticker's squeeze metrics are then registered under its spine sector (the sector the card is scored
        against, #272) so the in-sector peer Z-scores can be built before scoring. A name the spine
        cannot place is nobody's peer. Test fakes that lack
        ``short_screener`` keep working — R5 simply falls back to flow-only.
        """
        if self._shorts_cache is None:
            from RevenueChaser.module_earnings_radar import stored_inputs
            stored, session = stored_inputs.short_rows(self.tickers)
            parsed: dict[str, dict] = {}
            missing: list[str] = []
            for t in self.tickers:
                row = stored.get(t)
                if row is None:
                    missing.append(t)
                    continue
                parsed[t] = UWClient._parse_short_row(row)
            n_stored = len(parsed)
            n_live = 0
            if missing and hasattr(self.uw, "short_screener"):
                rows = self.uw.short_screener(missing) or {}
                for t, row in rows.items():
                    parsed[t] = self.uw._parse_short_row(row)
                n_live = len(rows)
            self._shorts_cache = parsed
            self.input_sources["short_interest"] = {
                "stored": n_stored, "live": n_live,
                "session": session.isoformat() if session else None}
        if not self._peers_registered:
            # the peer group is the spine's sector, the one _score reads: for a name the S&P 1500 list or the SIC
            # table places differently from the UW calendar the two differ (#272)
            if getattr(self, "_spine_gics", None) is None:
                self._spine_gics = _spine.spine_gics()
            for t in self.tickers:
                stats = self._shorts_cache.get(t)
                if not stats:
                    continue
                sector = _spine.gics_of(t, self._spine_gics).sector
                if sector and not is_unclassified(sector):
                    rule5_positioning.register_peer_stats(t, sector, stats)
            self._peers_registered = True
        return self._shorts_cache

    def _warm_dgs10(self) -> dict[str, float]:
        """One FRED DGS10 daily fetch per run for R6 yield beta — unless the nightly D27 sweep already stored it
        (E5d, #372), in which case FRED is not called at all.

        Missing key or a failed fetch leaves an empty map — R6 stays
        unavailable and the earnings run continues. Tests that pre-set
        ``_dgs10_cache`` skip the store and the network.
        """
        if self._dgs10_cache is not None:
            return self._dgs10_cache
        stored = stored_inputs.dgs10_map()
        if stored:
            self._dgs10_cache = stored
            self.input_sources["dgs10"] = "stored"
            return self._dgs10_cache
        from RevenueChaser.module_earnings_radar.macro_watch import fred_daily_map
        self._dgs10_cache = fred_daily_map("DGS10") or {}
        self.input_sources["dgs10"] = "fred" if self._dgs10_cache else "none"
        return self._dgs10_cache

    def _warm_rate_events(self) -> list[dict]:
        """The stored ``economic_calendar`` rows, read once per run (E5d, #372): every card's overlap is a lookup
        in this list, not a table read of its own."""
        if self._rate_events is None:
            self._rate_events = stored_inputs.rate_events()
        return self._rate_events

    def _year_of_bars(self, ticker: str, report_date: date | None = None) -> list:
        """The year of bars the reaction columns read: the stored D01 series when it is current, else UW (E5e).

        "Current" is the reaction columns' own anchor: the newest bar must be the latest complete session, or the
        reactions would be measured against a stale close. One ``etf_ohlc`` request per stale/unknown ticker.

        In the US session (09:30-16:30 New York) the latest complete session is yesterday, and only the live
        answer carries today's bar. A name that reports today, or reported on the previous trading day (before
        or after its open), measures "Current" and "Since ER" against today's bar — with the stored series both read +0.0% — so it reads
        live (review M3). Every other name keeps the stored series.
        """
        stored = (self._ohlc_bars or {}).get(ticker) or []
        if (stored and stored[-1].get("date") == calendar.latest_complete_session().isoformat()
                and not self._reports_around_now(report_date)):
            self._ohlc_stored.add(ticker)
        else:
            stored = self.uw.etf_ohlc(ticker, days=REACTION_OHLC_SESSIONS) or []
            self._ohlc_live.add(ticker)
        self.input_sources["ohlc"] = {"stored": len(self._ohlc_stored), "live": len(self._ohlc_live)}
        return stored

    @staticmethod
    def _reports_around_now(report_date: date | None) -> bool:
        """In the US session, and the print is today or on the previous trading day."""
        if report_date is None or not calendar.in_us_session_window():
            return False
        today = today_ny()
        return report_date in (today, calendar.previous_trading_day(today - timedelta(days=1)))

    def _warm_estimates(self) -> dict[str, dict]:
        """The stored ``estimate_snapshots`` ``0q`` rows for the run's tickers (E6, #372, D14).

        Only a read: the nightly sweep owns the table. R1 is logged only (weight 0), so a missing snapshot is never
        a reason to call a provider — it just leaves R1 unavailable on that card.
        """
        if self._estimates is None:
            rows, _session = stored_inputs.estimates(self.tickers)
            self._estimates = rows
            named = {str(t).upper() for t in self.tickers if t}
            self.input_sources["estimates"] = {"stored": len(rows), "missing": len(named - set(rows))}
        return self._estimates

    def _gex_profile(self, ticker: str, spot, em_pct: float | None, capture: dict | None = None) -> dict:
        """Strike-level GEX profile (Directionalized Volume) for R4.

        One extra UW call per deep-scored ticker when ``R4_GEX_PROFILE_ENABLED``
        and the provider exposes ``spot_exposures_by_strike``. Any failure or a
        payload without usable rows returns ``{}`` — R4 then falls back to the
        ``gex-levels`` flip (never fabricated, never a block).
        """
        from RevenueChaser.core.config import R4_GEX_PROFILE_ENABLED
        from RevenueChaser.core.gex_profile import normalize_strike_rows, build_profile
        if not R4_GEX_PROFILE_ENABLED or not em_pct:
            return {}
        if not hasattr(self.uw, "spot_exposures_by_strike"):
            return {}
        try:
            payload = self.uw.spot_exposures_by_strike(ticker) or []
            if capture is not None:
                capture["spot_exposures"] = _strike_fields(payload)   # the fields R4 reads, kept in the history row
            rows = normalize_strike_rows(payload)
            spot_f = _fmt_price_float(spot)
            if not rows or spot_f is None:
                return {}
            return build_profile(rows, spot_f, em_pct=em_pct)
        except Exception as e:  # noqa: BLE001 — a missing profile must never kill a score
            logger.debug("gex profile %s unavailable: %s", ticker, e)
            return {}

    def _term_slope(self, ticker: str, em_pct: float | None, capture: dict | None = None) -> float | None:
        """Per-day ATM-IV slope (nearest expiry -> 45 DTE) for R4's slope term.

        One extra UW call per deep-scored ticker when
        ``R4_TERM_STRUCTURE_ENABLED`` and the provider exposes ``term_structure``
        (mirrors ``_gex_profile``). Any failure, an empty curve, or a curve that
        cannot span near -> horizon returns ``None`` — R4 then simply has no
        slope term; a slope is never fabricated.
        """
        from RevenueChaser.core.config import R4_TERM_STRUCTURE_ENABLED
        from RevenueChaser.core.vol_metrics import term_structure_curve, term_structure_slope
        if not R4_TERM_STRUCTURE_ENABLED or not em_pct:
            return None
        if not hasattr(self.uw, "term_structure"):
            return None
        try:
            rows = self.uw.term_structure(ticker) or []
            if capture is not None:
                capture["term_structure"] = rows             # the answer, kept in the card's history row
            curve = term_structure_curve(rows)
            return term_structure_slope(curve)
        except Exception as e:  # noqa: BLE001 — a missing slope must never kill a score
            logger.debug("term structure %s unavailable: %s", ticker, e)
            return None

    def _yz_rv30(self, ticker: str, ohlc: list) -> float | None:
        """Yang-Zhang RV30 from the OHLC the pipeline already fetched (offline).

        Drops any bar dated today-or-later so a still-forming bar never feeds
        realized vol (the skill's ``bars_through`` rule). Returns None when there
        are too few clean bars — R4 then falls back to the screener RV.
        """
        from RevenueChaser.core.vol_metrics import clean_ohlc, yang_zhang
        try:
            bars = clean_ohlc(ohlc, as_of=today_ny())
        except Exception as e:  # noqa: BLE001
            logger.debug("yz ohlc %s unavailable: %s", ticker, e)
            return None
        return yang_zhang(bars)

    def run(self, use_preselect: bool = True, watchlist: set = None) -> list[TradeCard]:
        logger.info(f"Fetching screener batch for {len(self.tickers)} tickers...")
        rule5_positioning.reset_peer_stats()
        self._peers_registered = False
        _ = self.screener_cache  # warm cache

        wl = watchlist or (rawstore.load_watchlist())
        self._watchlist = wl

        # The tickers that get the deep (12+ provider calls) score. A run past 20 names applies the
        # filter gates and the daily cap; before this the preselect result was dropped and every
        # calendar name was scored (#198).
        scoring = list(self.tickers)
        if use_preselect and len(self.tickers) > 20:
            scoring = self.preselect(wl)
            logger.info(f"Preselect kept {len(scoring)} of {len(self.tickers)} tickers for deep scoring")

        self._warm_short_interest()
        self._warm_dgs10()
        self._warm_rate_events()
        # One D01 read for the names that will be scored, before the scoring workers start (E5e, #372).
        self._ohlc_bars = stored_inputs.ohlc_bars(scoring)
        self._warm_estimates()
        if getattr(self, "_spine_gics", None) is None:
            self._spine_gics = _spine.spine_gics()   # once, before the workers start

        def score_one(ticker):
            try:
                card = self._score(ticker)
                if card:
                    status, reasons = self._filter_outcome.get(ticker, ("", []))
                    card.filter_status = status
                    card.filter_reasons = ", ".join(reasons)
                return card
            except Exception as e:
                logger.error(f"Score failed {ticker}: {e}")
                return None

        workers = max(1, min(SCORE_WORKERS, len(scoring)))
        if workers == 1:
            scored = [score_one(t) for t in scoring]
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=workers) as ex:
                scored = list(ex.map(score_one, scoring))
        cards = [c for c in scored if c]
        # The workers are done: the OHLC source counts are settled (E5e, #372).
        self.input_sources["ohlc"] = {"stored": len(self._ohlc_stored), "live": len(self._ohlc_live)}
        cards.sort(key=lambda c: (-(c.confidence_score + c.pre_score), -c.expected_move_pct))
        return cards

    def preselect(self, watchlist: set = None) -> list[str]:
        from RevenueChaser.core.config import (
            MIN_OPTION_VOLUME, MIN_TOTAL_OI,
            MIN_DOLLAR_OPTION_VOLUME, MIN_EXPECTED_MOVE_PCT, MAX_EXPECTED_MOVE_PCT,
            WATCHLIST_BYPASS, FLOW_SWEEP_BLOCK_MIN,
        )
        wl = watchlist or set()
        import math
        
        scored = []
        stats = {"opt_vol": 0, "oi": 0, "dollar_vol": 0, "em_low": 0, "em_high": 0}
        for t in self.tickers:
            self._filter_stats["total"] += 1
            cache = self.screener_cache
            scr = cache.get(t, {})
            cal = self.cal_meta.get(t, {})

            # Bypass check
            bypassed = False
            if WATCHLIST_BYPASS and t in wl:
                bypassed = True
                self._bypass_tickers.add(t)

            if bypassed:
                scored.append((t, 100.0))
                self._filter_stats["bypassed"] += 1
                self._filter_outcome[t] = ("bypassed", [])
                continue

            # ER-D option 1 (#372): a name with no screener row is unknown liquidity, so it is not
            # scored against gates the missing row would fail anyway — it is excluded as such (#383 E2).
            if t not in cache:
                scored.append((t, 0.0))
                self._filter_stats["filtered"] += 1
                self._filter_outcome[t] = ("excluded", ["no screener row"])
                self._filter_stats["excluded"].append({"ticker": t, "reasons": ["no screener row"]})
                self._filter_stats["by_reason"]["no screener row"] = (
                    self._filter_stats["by_reason"].get("no screener row", 0) + 1)
                continue

            opt_vol = int(float(scr.get("call_volume", 0) or 0) + float(scr.get("put_volume", 0) or 0))
            oi = int(float(scr.get("total_open_interest", 0) or 0))
            dollar_opt_vol = option_premium(scr)   # None when the screener served no premium: fails $Vol

            em = _expected_move_pct(cal)

            # Filter gates
            passed = True
            reasons = []
            if opt_vol < MIN_OPTION_VOLUME: passed = False; reasons.append("OptVol"); stats["opt_vol"] += 1
            if oi < MIN_TOTAL_OI: passed = False; reasons.append("OI"); stats["oi"] += 1
            if dollar_opt_vol is None or dollar_opt_vol < MIN_DOLLAR_OPTION_VOLUME:
                passed = False; reasons.append("$Vol"); stats["dollar_vol"] += 1
            if em < MIN_EXPECTED_MOVE_PCT: passed = False; reasons.append("EM<1.5%"); stats["em_low"] += 1
            if em > MAX_EXPECTED_MOVE_PCT: passed = False; reasons.append("EM>25%"); stats["em_high"] += 1
            
            if not passed:
                scored.append((t, 0.0))
                self._filter_stats["filtered"] += 1
                self._filter_outcome[t] = ("excluded", reasons)
                self._filter_stats["excluded"].append({"ticker": t, "reasons": reasons})
                for r in reasons:
                    self._filter_stats["by_reason"][r] = self._filter_stats["by_reason"].get(r, 0) + 1
                continue
            
            self._filter_stats["passed"] += 1
            self._filter_outcome[t] = ("passed", [])
            
            # Pre-score
            pre = (0.35 * math.log(1 + opt_vol) +
                   0.30 * math.log(1 + oi) +
                   0.20 * math.log(1 + dollar_opt_vol) +
                   0.15 * math.log(1 + em * 10))
            scored.append((t, round(pre, 2)))
        
        # Store scores for use in _score()
        self._pre_scores = {t: s for t, s in scored}
        
        # Sort by pre-score descending
        scored.sort(key=lambda x: -x[1])
        
        # Take the top N (bypassed names carry 100, so they come first). A name past the cap is
        # not scored: it is recorded as excluded by the cap, like a name that failed a gate.
        ranked = [t for t, s in scored if s > 0]
        result = ranked[:MAX_DEEP_CHECKS_PER_DAY]
        for t in ranked[MAX_DEEP_CHECKS_PER_DAY:]:
            kind = self._filter_outcome.get(t, ("passed", []))[0]
            self._filter_stats["bypassed" if kind == "bypassed" else "passed"] -= 1
            self._filter_stats["filtered"] += 1
            self._filter_outcome[t] = ("excluded", ["cap"])
            self._filter_stats["excluded"].append({"ticker": t, "reasons": ["cap"]})
            self._filter_stats["by_reason"]["cap"] = self._filter_stats["by_reason"].get("cap", 0) + 1
        return result

    def _flow_recent(self, ticker: str) -> tuple[list, bool]:
        """``(prints, failed)``: the recent option tape and whether the request itself failed.

        A client without ``flow_recent_result`` is taken at its word; a 404 is "no flow", not a failure.
        """
        fn = getattr(self.uw, "flow_recent_result", None)
        if fn is not None:
            res = fn(ticker)
            if getattr(res, "status", None) is not None:
                failed = res.status != "ok" and (res.error or {}).get("status") != 404
                return res.data or [], failed
        return self.uw.flow_recent(ticker) or [], False

    def _score(self, ticker: str) -> TradeCard:
        captured: dict = {}      # UW answers behind R4 that only a derived value keeps (the history row's inputs)
        rows_fn = getattr(self.uw, "earnings_rows", None)
        if rows_fn is not None:
            hist = rows_fn(ticker) or []        # one /earnings/{t} request for both reads
            earn = hist[0] if hist else {}
        else:
            earn = self.uw.earnings_data(ticker) or {}
            hist = self.uw.earnings_history(ticker) or []
        # R2 reads the raw EPS print series (/stock/{t}/earnings), not the
        # calendar/straddle tape that feeds R4. Fakes without the method fall
        # back to the calendar history so R2 stays available in tests.
        r2_hist = []
        if hasattr(self.uw, "stock_earnings"):
            try:
                r2_hist = self.uw.stock_earnings(ticker) or []
            except Exception as e:  # noqa: BLE001 — never kill a score on R2 fetch
                logger.debug("R2 stock_earnings %s unavailable: %s", ticker, e)
                r2_hist = []
        try:                                    # the store side never changes a score
            self.earnings_capture.add(ticker, earnings_store.EARNINGS, hist)
            self.earnings_capture.add(ticker, earnings_store.STOCK_EARNINGS, r2_hist)
        except Exception:
            logger.warning("Earnings prints capture failed for %s", ticker, exc_info=True)
        # E4 (#372 ER-5): only the endpoint's own rows are stored; the fallback copy is not.
        r2_own = bool(r2_hist)
        if not r2_hist:
            r2_hist = hist
        flow_recent_raw, flow_failed = self._flow_recent(ticker)
        flow = self.uw.aggregate_flow(ticker, flow_recent_raw) or {}
        cal_raw = self.cal_meta.get(ticker, {})
        scr = self.screener_cache.get(ticker, {})
        # ── Identity ──
        # R5 (#88): the sector is the spine's; the UW calendar / earnings sector no longer feeds it.
        # #266: a Morningstar sector name plus the spine's industry text (the cluster hints read it).
        # One store read per run, not per ticker (each read opens a connection, ~50 ms).
        if getattr(self, "_spine_gics", None) is None:
            self._spine_gics = _spine.spine_gics()
        gics = _spine.gics_of(ticker, self._spine_gics)
        sector = gics.sector
        industry = gics.industry
        cluster = classify_ticker(ticker, sector, industry)
        name = cal_raw.get("full_name") or earn.get("company_name")
        if not name:
            info = self.uw.ticker_info(ticker) or {}
            name = info.get("company_name") or info.get("name") or ticker
        report_time_raw = cal_raw.get("report_time") or cal_raw.get("earnings_time") or earn.get("report_time") or ""
        if report_time_raw in ("premarket", "pre-market", "BMO", "before"):
            report_time_label = "AM"
        elif report_time_raw in ("afterhours", "after-hours", "AMC", "after", "postmarket", "pm"):
            report_time_label = "PM"
        else:
            report_time_label = "—"

        rotation_quadrant = ""
        if self.etf_quadrant_map:
            rotation_quadrant = get_quadrant_for_ticker(ticker, cluster, self.etf_quadrant_map)

        em_pct = _expected_move_pct(cal_raw, earn)
        report_date = str(cal_raw.get("earnings_date") or cal_raw.get("report_date") or earn.get("report_date") or "") or ""
        if isinstance(report_date, str) and len(report_date) > 10:
            report_date = report_date[:10]
        ed = date.fromisoformat(report_date) if report_date else today_ny()

        agh = _agh_context(ticker)
        eps_est = cal_raw.get("street_mean_est") or earn.get("street_mean_est")

        # ── Price history from OHLC (needed by earnings reactions + price changes) ──
        # One request: the reaction columns need a year of sessions (four quarterly prints), every
        # other reader keeps the last READER_OHLC_SESSIONS bars.
        ohlc_year = self._year_of_bars(ticker, ed)
        ohlc = ohlc_year[-READER_OHLC_SESSIONS:]
        closes = [float(c["close"]) for c in ohlc if c.get("close")]

        # ── Earnings Reactions (5 columns, post-close vs today) ──
        year_closes = [float(c["close"]) for c in ohlc_year if c.get("close")]
        earn_reactions = _compute_earnings_reactions(hist, year_closes, ohlc_year)
        fin_records = []
        if self.uw:
            try:
                fin_fn = getattr(self.uw, "financials", None)
                if fin_fn is not None:
                    # One /financials request: R3/R7 map the body, the run stores it (D12, #297).
                    fin_raw = fin_fn(ticker) or {}
                    fin_records = self.uw._map_financials(ticker, fin=fin_raw) or []
                    try:                        # the store side never changes R3/R7
                        self.vendor.add_uw(ticker, fin_raw)
                    except Exception:
                        logger.warning("Earnings vendor capture failed for %s", ticker, exc_info=True)
                else:                           # fakes that only offer the mapped records
                    fin_records = self.uw._map_financials(ticker) or []
            except Exception:
                fin_records = []
        detail_json = json.dumps(_build_earnings_detail(hist, fin_records))

        # ── Price Action ──
        price_changes = _compute_price_action(ohlc, report_date, report_time_raw)
        return_since_er, last_er_label, since_er_chart = _compute_return_since_er(hist, closes, ohlc, report_date,
            cal_raw.get("report_time") or cal_raw.get("earnings_time") or earn.get("report_time") or report_time_raw)

        # ── Scoring ──
        spot = scr.get("close") or earn.get("price") or None
        spot = _fmt_price_float(spot)
        current_price = f"${spot:.2f}" if spot is not None else ""
        gex = self.uw.gex_levels(ticker) or {}
        spot_num = _fmt_price_float(earn.get("price") or spot)
        gex_profile = self._gex_profile(ticker, spot_num, em_pct, captured)
        scr_rv = _safe_float(scr.get("realized_volatility"))
        yz_rv30 = self._yz_rv30(ticker, ohlc)
        opt = {
            "underlying_price": spot_num,
            "expected_move_pct": em_pct,
            "gamma_flip": gex.get("gamma_flip"),
            "gex_profile": gex_profile,
            "implied_volatility": _safe_float(scr.get("iv30d")),
            # YZ-RV30 (offline from the OHLC above) when it exists, else the
            # screener RV — same construction the skill's 1.25 threshold assumes.
            "realized_volatility": yz_rv30 if yz_rv30 is not None else scr_rv,
            "rv_source": "yang_zhang" if yz_rv30 is not None else "screener",
            "ts_slope": self._term_slope(ticker, em_pct, captured),
            "iv_rank": _safe_float(scr.get("iv_rank")),
            "straddle_payoff_median": _straddle_payoff_median(hist),
        }
        edge_median, edge_ratio = _compute_edge(hist, em_pct)
        opt["hist_move_ratio"] = edge_ratio if edge_median is not None else None
        r2 = rule2_guidance.score(ticker, r2_hist)
        r3 = _score_financials(ticker, cluster, fin_records)
        r4 = rule4_volatility.score(opt) if em_pct else rule4_volatility.score(None)
        shorts = (self._shorts_cache or {}).get(ticker, {})
        r5 = rule5_positioning.score(
            short_interest_pct_float=shorts.get("si_pct"),
            days_to_cover=shorts.get("days_to_cover"),
            borrow_fee=shorts.get("borrow_fee"),
            utilization=shorts.get("utilization"),
            sector=sector,
            institutional_net_delta=flow.get("inst_lean_net"),
            institutional_call_premium=flow.get("inst_bull_side"),
            institutional_put_premium=flow.get("inst_bear_side"),
            ticker=ticker,
            institutional_side_served=flow.get("side_served"),
            institutional_package_premium=flow.get("inst_package"),
        )
        beta, beta_t = r6_beta(ohlc, self._dgs10_cache or {})
        # rate_event_overlap: FOMC/CPI within the print's ±1 trading day, from the stored economic calendar
        # (E5d, #372). None — the store does not span the window — keeps the old behaviour: an extreme duration
        # is a badge, never a veto (design doc). A noisy beta (|t| < 2) is a badge too.
        overlap = stored_inputs.rate_event_overlap(ed, self._warm_rate_events())
        if overlap is not None:
            self.input_sources["rate_events"] = "stored"
        r6 = rule6_macro.score(rate_sensitivity=beta, rate_event_overlap=overlap, beta_tstat=beta_t)
        r7 = _score_accounting(fin_records)
        r8 = _score_sentiment(flow)

        r1 = rule1_drivers.score((self._estimates or {}).get(ticker))
        composite = compute_composite_scores(r1=r1, r2=r2, r3=r3, r4=r4, r5=r5, r6=r6, r7=r7, r8=r8)
        direction = composite["direction_score"]
        vol_edge = composite["volatility_edge_score"]
        conf = composite["confidence_score"]
        veto = bool(r6.flags)
        action = select_action(direction, vol_edge, veto, conf)
        bias_label = classify_bias(direction)
        conviction = classify_conviction(direction, conf, composite.get("rule_results", {}))
        r4m = r4.raw_metrics if r4 else {}
        suggested_structure = recommend_options_structure(direction, spot, gex, gex_profile)

        # ── Phase 1B: trade summary (read, edge, liquidity, badges) ──
        has_row = ticker in self.screener_cache
        opt_vol_raw = int(float(scr.get("call_volume", 0) or 0) + float(scr.get("put_volume", 0) or 0))
        oi_raw = int(float(scr.get("total_open_interest", 0) or 0))
        # A name with no screener row has unknown liquidity, not thin liquidity (E2, #372); a present
        # row with zero volume/OI is still evaluated and stays "Thin".
        liquidity = _liquidity_label(opt_vol_raw, oi_raw) if has_row else "N/A"
        binary = em_pct > 25.0
        trade_read = _trade_read(direction, conf, veto, binary, liquidity)

        if trade_read == "AVOID":
            if veto:
                trade_reason = "Macro veto blocks this event"
            elif binary:
                trade_reason = f"Binary expected move ({em_pct:.0f}%)"
            else:
                trade_reason = "Thin options liquidity"
        else:
            parts = []
            if rotation_quadrant in ("LEADING", "IMPROVING"):
                parts.append("favorable rotation")
            elif rotation_quadrant == "LAGGING":
                parts.append("lagging rotation")
            flow_part = _trade_flow_part(flow, trade_read)
            if flow_part:
                parts.append(flow_part)
            if trade_read == "BULLISH":
                trade_reason = "Bullish — " + (" · ".join(parts) or "directional edge")
            elif trade_read == "BEARISH":
                trade_reason = "Bearish — " + (" · ".join(parts) or "directional edge")
            else:
                trade_reason = "Mixed — " + (" · ".join(parts) or "no clear edge")

        detail_qs = json.loads(detail_json)
        n_completed = len(detail_qs)
        has_implied = any(q.get("implied") is not None for q in detail_qs)
        badges = []
        if ticker in self._watchlist:
            badges.append("WATCHLIST")
        if n_completed < 3:
            badges.append("DATA PARTIAL")
        if liquidity == "Thin":
            badges.append("LOW LIQUIDITY")
        if not has_row:
            badges.append("NO SCREENER ROW")
        if binary:
            badges.append("BINARY RISK")
        if veto:
            badges.append("MACRO VETO")
        if not cluster or "unmapped" in str(cluster).lower():
            badges.append("UNMAPPED")
        if not has_implied:
            badges.append("HIST. IMPLIED N/A")

        thesis = []
        for rule_name, result in composite.get("rule_results", {}).items():
            if rule_name == "rule1_drivers":
                # R1 is logged only (E6): it speaks in its audit row, not in the thesis. The thesis is cut at
                # 200 characters and NRC reads "beat"/"miss" from it, so R1's text could move NRC (review S5).
                continue
            for reason in result.reasons:
                if reason and "no " not in reason[:5] and "neutral" not in reason[:10] and "fundamentals not" not in reason:
                    thesis.append(reason)

        nrc_info = self.nrc.get(ticker, {})
        nrc_score = nrc_info.get("nrc", 0)
        nrc_tier = nrc_info.get("tier", "")
        attention = _attention_score(conf, vol_edge, liquidity, rotation_quadrant, ed)
        inst_label, inst_net, inst_tone = _institutional_flow_comment(flow, failed=flow_failed)
        inst_prints, inst_span = _flow_window(flow_recent_raw)
        gex_struct = _gex_structure_label(spot, gex, gex_profile)

        # LEAPS is read-only here (C26, #383, LP-D): the label comes from leaps_cache, whose one
        # writer is the LEAPS Fetch. No UW call, no store write, no lock on the scoring pool.
        payload = leaps_load_cache(ticker) or {}
        leaps = payload.get("latest") or {}

        inst_call_premium = float(flow.get("inst_call", 0) or 0)
        inst_put_premium = float(flow.get("inst_put", 0) or 0)
        sweep_premium = float(flow.get("sweep", 0) or 0)
        block_premium = float(flow.get("block", 0) or 0)

        raw_packet = {
            "earnings_history": hist,
            "flow_recent": flow_recent_raw,
            "screener": scr,
            "gex_levels": gex,
            "gex_profile": gex_profile,
            "ohlc_year": ohlc_year,
            "short_interest": shorts,
            "rate_event_overlap": overlap,
            "spot_exposures": captured.get("spot_exposures"),
            "term_structure": captured.get("term_structure"),
            "fin_records": fin_records,
            "current_price": spot,
            "leaps": leaps,
            "calendar_event": cal_raw,
            "rotation_quadrant": rotation_quadrant,
            "watchlist": ticker in self._watchlist,
            "agh": agh,
            "filter_status": self._filter_outcome.get(ticker, ("", []))[0],
            "filter_reasons": self._filter_outcome.get(ticker, ("", []))[1],
        }
        if r2_own:                          # only the endpoint's own series, never the fallback copy (E4)
            raw_packet["r2_series"] = r2_hist
        raw_packet_json = json.dumps(raw_packet, default=str)

        # Build rule audit for expandable drawer
        rule_results = composite.get("rule_results", {})
        active_weight_sum = sum(RULE_WEIGHTS.get(rn, 0) for rn, r in rule_results.items() if r.available)
        rule_labels = {
            "rule1_drivers": "Drivers",
            "rule2_guidance": "Guidance", "rule3_financial": "Financials",
            "rule5_positioning": "Positioning", "rule7_accounting": "Accounting",
            "rule8_sentiment": "Sentiment",
        }
        rule_audit = {}
        for rule_name, result in rule_results.items():
            wt = RULE_WEIGHTS.get(rule_name, 0)
            rule_audit[rule_name] = {
                "label": rule_labels.get(rule_name, rule_name),
                "score": result.score,
                "weight": wt,
                "coverage": result.coverage,
                "available": result.available,
                "contribution": (result.score * wt / max(active_weight_sum, 0.01)) if result.available and result.score is not None else None,
                "reasons": result.reasons,
                "flags": result.flags,
            }

        # R4 is not a direction rule (0% weight, excluded from rule_results), so
        # surface it explicitly — otherwise its reasons never reach the Why chips
        # and OPTIONS_*/SHARES_* actions are a black box.
        if r4 is not None and r4.available and r4.score is not None:
            rule_audit["rule4_volatility"] = {
                "label": "Vol Edge",
                "score": r4.score,
                "weight": 0.0,
                "coverage": r4.coverage,
                "available": True,
                "contribution": None,
                "reasons": r4.reasons,
                "flags": r4.flags or [],
            }
        if r6 is not None and r6.available:
            rule_audit["rule6_macro"] = {
                "label": "Macro",
                "score": r6.score,
                "weight": 0.0,
                "coverage": r6.coverage,
                "available": True,
                "contribution": None,
                "reasons": r6.reasons,
                "flags": r6.flags or [],
            }

        # ── Liquidity from screener + endpoints ──
        stk_vol = _fmt_int(scr.get("stock_volume") or ohlc[-1].get("volume", 0) if ohlc else 0)
        mcap_raw = scr.get("marketcap") or cal_raw.get("marketcap") or ""
        mcap = ""
        if mcap_raw:
            try:
                val = float(mcap_raw)
                if val >= 1e12:
                    mcap = f"${val/1e12:.1f}T"
                elif val >= 1e9:
                    mcap = f"${val/1e9:.1f}B"
                elif val >= 1e6:
                    mcap = f"${val/1e6:.1f}M"
                else:
                    mcap = f"${val:,.0f}"
            except:
                mcap = str(mcap_raw) if mcap_raw else ""

        opt_vol = _fmt_int((scr.get("call_volume") or 0) + (scr.get("put_volume") or 0))
        oi = _fmt_int(scr.get("total_open_interest") or scr.get("open_interest"))

        # fallback: per-ticker endpoints if screener missed
        if not stk_vol:
            ohlc_latest = self.uw.ohlc_latest(ticker) or {}
            stk_vol = _fmt_int(ohlc_latest.get("volume"))
        if not mcap:
            info_raw = self.uw.ticker_info(ticker) or {}
            mcap_raw2 = info_raw.get("marketcap") or ""
            if mcap_raw2:
                try:
                    val = float(mcap_raw2)
                    mcap = f"${val/1e9:.1f}B" if val >= 1e9 else f"${val/1e6:.1f}M"
                except:
                    pass
        if not opt_vol or not oi:
            opt_data = self.uw.options_volume(ticker) or {}
            if isinstance(opt_data, list) and opt_data:
                opts = opt_data[0]
                if not opt_vol:
                    cv = opts.get("call_volume", 0) or 0
                    pv = opts.get("put_volume", 0) or 0
                    opt_vol = _fmt_int(cv + pv)
                if not oi:
                    oi = _fmt_int(opts.get("total_open_interest") or opts.get("open_interest"))
            elif isinstance(opt_data, dict):
                if not opt_vol:
                    cv = opt_data.get("call_volume", 0) or 0
                    pv = opt_data.get("put_volume", 0) or 0
                    opt_vol = _fmt_int(cv + pv)
                if not oi:
                    oi = _fmt_int(opt_data.get("total_open_interest") or opt_data.get("open_interest"))

        return TradeCard(
            ticker=ticker, name=name, company=ticker, cluster=get_cluster_label(cluster),
            sector=sector,
            earnings_date=ed, earnings_time=report_time_raw, report_time_label=report_time_label,
            final_action=action, direction_score=direction,
            bias_label=bias_label, conviction=conviction,
            suggested_structure=suggested_structure,
            r2_score=r2.display_score(), r3_score=r3.display_score(),
            r5_score=r5.display_score(), r7_score=r7.display_score(),
            r8_score=r8.display_score(), r4_score=r4.display_score(),
            volatility_edge_score=vol_edge, confidence_score=conf,
            macro_veto=veto, veto_reasons="; ".join(r6.flags),
            expected_move_pct=round(em_pct, 1),
            iv_rv_ratio=round(r4m.get("iv_rv") or 0, 2),
            key_thesis="; ".join(thesis)[:200] if thesis else "",
            data_completeness=conf,
            nrc_score=nrc_score, nrc_tier=nrc_tier,
            eps_estimate=str(eps_est) if eps_est else "",
            earn_m4_pct=earn_reactions.get("m4", ""),
            earn_m3_pct=earn_reactions.get("m3", ""),
            earn_m2_pct=earn_reactions.get("m2", ""),
            earn_m1_pct=earn_reactions.get("m1", ""),
            earn_m4_eps=earn_reactions.get("m4_eps", ""),
            earn_m3_eps=earn_reactions.get("m3_eps", ""),
            earn_m2_eps=earn_reactions.get("m2_eps", ""),
            earn_m1_eps=earn_reactions.get("m1_eps", ""),
            earn_current_pct=earn_reactions.get("current", ""),
            return_since_er=return_since_er,
            last_er_label=last_er_label,
            since_er_chart=since_er_chart,
            past_1d=price_changes.get("1d", ""),
            past_1w=price_changes.get("1w", ""),
            past_2w=price_changes.get("2w", ""),
            return_4w=price_changes.get("4w", ""),
            last_beat=earn_reactions.get("m1", ""),
            stock_volume=stk_vol, option_volume=opt_vol,
            market_cap=mcap, total_open_interest=oi,
            rotation_quadrant=rotation_quadrant,
            current_price=current_price,
            gex_call_wall=_fmt_gex(gex.get("call_wall")),
            gex_put_wall=_fmt_gex(gex.get("put_wall")),
            gex_gamma_flip=_fmt_gex(gex.get("gamma_flip")),
            gex_gamma_magnet=_fmt_gex(gex.get("gamma_magnet")),
            pre_score=self._pre_scores.get(ticker, 0.0),
            agh_points=agh.get("agh_points"),
            agh_accel=agh.get("agh_accel"),
            agh_owners=agh.get("agh_owners") or "",
            agh_dilution=bool(agh.get("agh_dilution")),
            leaps_label=leaps.get("label", "N/A"),
            leaps_as_of=str(leaps.get("as_of") or ""),
            leaps_score=int(leaps.get("score", 0) or 0),
            leaps_far_call_oi=int(leaps.get("far_call_oi", 0) or 0),
            leaps_far_put_oi=int(leaps.get("far_put_oi", 0) or 0),
            leaps_far_call_share=leaps.get("far_call_share"),
            leaps_growth_1d_pct=leaps.get("far_oi_growth_1d_pct"),
            leaps_growth_q_pct=leaps.get("far_oi_growth_q_pct"),
            leaps_iv_rank=leaps.get("iv_rank"),
            rule_audit=rule_audit,
            detail_json=detail_json,
            trade_read=trade_read,
            trade_read_reason=trade_reason,
            edge_ratio=round(edge_ratio, 2) if edge_ratio else 0.0,
            edge_median_realized=round(edge_median, 2) if edge_median else 0.0,
            liquidity_label=liquidity,
            badges_json=json.dumps(badges),
            attention_score=attention,
            inst_flow_label=inst_label,
            inst_flow_net=round(inst_net, 2) if inst_net else 0.0,
            inst_flow_tone=inst_tone,
            inst_flow_prints=inst_prints,
            inst_flow_span_hours=inst_span,
            gex_structure=gex_struct,
            inst_call_premium=inst_call_premium,
            inst_put_premium=inst_put_premium,
            sweep_premium=sweep_premium,
            block_premium=block_premium,
            raw_packet_json=raw_packet_json,
            flow_source="failed" if flow_failed else ("flow_recent" if flow_recent_raw else "none"),
        )


def option_premium(scr: dict) -> float | None:
    """The day's call plus put premium of a screener row, None when either is missing. The $Vol gate reads it
    (Fable review N3: volume x 100 made $Vol the OptVol gate again); the options data's earnings set reads the same
    rule (``rcdata/build/options/uw_screener.premium``)."""
    try:
        calls, puts = scr.get("call_premium"), scr.get("put_premium")
        if calls in (None, "") or puts in (None, ""):
            return None
        return float(calls) + float(puts)
    except (TypeError, ValueError):
        return None


def _expected_move_pct(*sources: dict) -> float:
    """The implied earnings move in percent from provider rows (0.0 when none usable).

    ``expected_move_perc`` is a ratio in every UW payload (0.05 = 5%), so it is always
    scaled by 100: a binary name's 1.2 is 120%, not 1.2%. A plain ``expected_move`` key
    is of unknown unit and keeps the legacy "below 1 means ratio" read.
    """
    for src in sources:
        raw = rule2_guidance.first_present(src or {}, "expected_move_perc")
        if raw is not None:
            try:
                return float(raw) * 100
            except (TypeError, ValueError):
                continue
    for src in sources:
        raw = (src or {}).get("expected_move")
        if raw:
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            return v * 100 if v < 1 else v
    return 0.0


def _compute_earnings_reactions(hist: list[dict], closes: list[float], ohlc: list[dict]) -> dict[str, str]:
    """-4E to -1E: post-close price vs today. Current: pre-close vs today. Capped at —50%.
    Also computes EPS surprise % (actual vs estimate) per quarter as mN_eps keys."""
    today_close = closes[-1] if closes else None
    if not today_close:
        return {"m4": "", "m3": "", "m2": "", "m1": "", "current": "",
                "m4_eps": "", "m3_eps": "", "m2_eps": "", "m1_eps": ""}

    dates, by_date = session_closes(ohlc)

    today_str = today_ny().isoformat()
    completed_dates = []  # (rd, anchor_close or None, actual_eps, estimate)
    upcoming_date = None

    for h in hist:
        a = h.get("actual_eps")
        estimate = rule2_guidance.first_present(h, "street_mean_est", "eps_estimate", "estimated_eps")
        rd = str(h.get("report_date") or "")
        if not rd:
            continue
        rd = rd[:10]
        if a is not None and rd <= today_str:
            # the reaction session's close (next session for an after-close print); None when
            # the print predates the OHLC window or its reaction session has no bar yet
            anchor = reaction_anchor(ohlc, rd, h.get("report_time"))
            completed_dates.append((rd, anchor[1] if anchor else None, a, estimate))
        elif a is None and rd == today_str:
            # reporting today, EPS not in yet: the move since the pre-report close is live.
            # A future event has no move to show (it read +0.0% for every upcoming name).
            if upcoming_date is None:
                upcoming_date = rd

    result = {"m4": "", "m3": "", "m2": "", "m1": "", "current": "",
              "m4_eps": "", "m3_eps": "", "m2_eps": "", "m1_eps": ""}

    if upcoming_date:
        pre_close = None
        for d in reversed(dates):
            if d < upcoming_date:
                pre_close = by_date[d]
                break
        if pre_close and pre_close > 0:
            pct = round((today_close / pre_close - 1) * 100, 2)
            sign = "+" if pct >= 0 else ""
            result["current"] = f"{sign}{pct:.1f}%"

    keys = ["m1", "m2", "m3", "m4"]
    for i, key in enumerate(keys):
        if i < len(completed_dates):
            _, post_close, actual, estimate = completed_dates[i]
            if post_close and post_close > 0:
                pct = round((today_close / post_close - 1) * 100, 2)
                sign = "+" if pct >= 0 else ""
                result[key] = f"{sign}{pct:.1f}%"
            else:
                result[key] = ""
            result[f"{key}_eps"] = _eps_surprise_pct(actual, estimate)
        else:
            result[key] = ""
            result[f"{key}_eps"] = ""

    return result


def _eps_surprise_pct(actual, estimate) -> str:
    """Signed EPS surprise as % of |estimate|. Empty if unavailable."""
    if actual is None or estimate is None:
        return ""
    try:
        a = float(actual)
        e = float(estimate)
    except (ValueError, TypeError):
        return ""
    if abs(e) < 1e-10:
        return ""
    pct = (a - e) / abs(e) * 100
    return f"{pct:+.1f}%"


def _quarter_label(fiscal_end: str) -> str:
    """Derive a 'Q{n} YYYY' label from a fiscal period end date like '2026-03-31'."""
    try:
        d = date.fromisoformat(str(fiscal_end)[:10])
        q = (d.month - 1) // 3 + 1
        return f"Q{q} {d.year}"
    except (ValueError, TypeError):
        return str(fiscal_end)[:10]


def _build_earnings_detail(hist: list[dict], fin_records: list[dict] = None,
                           rev_est: dict = None) -> list[dict]:
    """Build normalized quarterly detail for the earnings drawer.

    Returns completed quarters (oldest first), capped at the last 8. Historical
    implied move is only set when the record itself carries expected_move_perc
    (never reused from today). Missing values become None, not zero.

    ``fin_records`` maps UW financials (``_map_financials``) to add per-quarter
    actual revenue keyed by ``fiscal_date``. ``rev_est`` maps a quarter date to
    an analyst revenue estimate (Advanced+ earnings-estimates feed); absent ->
    None.
    """
    rev_est = rev_est or {}
    rev_by_date = {}
    for fr in (fin_records or []):
        fd = (fr.get("fiscal_date") or "")[:10]
        rev = fr.get("revenue")
        if fd and rev is not None:
            rev_by_date[fd] = float(rev)

    today_str = today_ny().isoformat()
    completed = []
    for h in hist:
        actual_raw = rule2_guidance.first_present(h, "actual_eps", "reported_eps")
        rd = str(h.get("report_date") or "")
        if not rd:
            continue
        rd = rd[:10]
        if actual_raw is None or rd > today_str:
            continue
        try:
            actual = float(actual_raw)
        except (ValueError, TypeError):
            continue

        estimate_raw = rule2_guidance.first_present(h, "street_mean_est", "estimated_eps")
        estimate = None
        if estimate_raw is not None:
            try:
                estimate = float(estimate_raw)
            except (ValueError, TypeError):
                estimate = None

        implied = None
        imp_raw = h.get("expected_move_perc")
        if imp_raw is not None:
            try:
                implied = round(float(imp_raw) * 100, 2)
            except (ValueError, TypeError):
                implied = None

        actual_move = None
        am_raw = h.get("post_earnings_move_1d")
        if am_raw is not None:
            try:
                actual_move = round(float(am_raw) * 100, 2)
            except (ValueError, TypeError):
                actual_move = None

        fiscal_end = str(h.get("ending_fiscal_quarter") or "")[:10]
        revenue_actual = rev_by_date.get(fiscal_end)

        surprise_dollar = None
        surprise_pct = None
        if estimate is not None:
            surprise_dollar = round(actual - estimate, 4)
            if abs(estimate) >= 0.01:
                surprise_pct = round(100 * (actual - estimate) / abs(estimate), 2)

        completed.append({
            "q": _quarter_label(h.get("ending_fiscal_quarter") or rd),
            "date": rd,
            "implied": implied,
            "actual": actual_move,
            "eps_actual": actual,
            "eps_est": estimate,
            "surprise_dollar": surprise_dollar,
            "surprise_pct": surprise_pct,
            "revenue_actual": revenue_actual,
            # Analyst revenue estimate for the same fiscal quarter (Advanced+),
            # mapped by date; None when the feed is off/unavailable.
            "revenue_est": (rev_est or {}).get(fiscal_end),
        })

    completed.sort(key=lambda x: x["date"])
    return completed[-8:]


def _liquidity_label(opt_vol_raw: float, oi_raw: float) -> str:
    if opt_vol_raw >= 10000 and oi_raw >= 10000:
        return "Excellent"
    if opt_vol_raw >= 500 and oi_raw >= 200:
        return "Tradeable"
    return "Thin"


def _compute_edge(hist: list[dict], em_pct: float):
    """Median absolute historical 1D move (last 4 completed) vs current implied.
    Returns (median, ratio) or (None, None) if insufficient data."""
    today_str = today_ny().isoformat()
    moves = []
    for h in hist:
        if len(moves) >= 4:
            break
        a = rule2_guidance.first_present(h, "actual_eps", "reported_eps")
        rd = str(h.get("report_date") or "")[:10]
        if a is None or rd > today_str:
            continue
        am = h.get("post_earnings_move_1d")
        if am is None:
            continue
        try:
            moves.append(abs(float(am)) * 100)
        except (ValueError, TypeError):
            pass
    if len(moves) < 3 or not em_pct or em_pct <= 0:
        return None, None
    median = sorted(moves)[len(moves) // 2]
    return median, median / em_pct


def _straddle_payoff_median(hist: list[dict]):
    """Median 1-day long-straddle payoff (percent) across the last 4 completed
    prints, or None with <3 usable events. UW reports it as a decimal fraction
    of premium (0.05 = +5%); missing field -> no signal (never 0)."""
    today_str = today_ny().isoformat()
    pays = []
    for h in hist or []:
        if len(pays) >= 4:
            break
        a = rule2_guidance.first_present(h, "actual_eps", "reported_eps")
        rd = str(h.get("report_date") or "")[:10]
        if a is None or not rd or rd > today_str:
            continue
        raw = h.get("long_straddle_1d")
        if raw is None:
            continue
        try:
            pays.append(float(raw) * 100)
        except (ValueError, TypeError):
            continue
    if len(pays) < 3:
        return None
    return sorted(pays)[len(pays) // 2]


def _trade_read(direction: float, confidence: float, veto: bool, binary: bool, liquidity: str) -> str:
    # Only "Thin" liquidity avoids: "N/A" (no screener row) is unknown, never a reason to avoid (E2, #372).
    if veto or binary or liquidity == "Thin":
        return "AVOID"
    if direction >= 12 and confidence >= 50:
        return "BULLISH"
    if direction <= -12 and confidence >= 50:
        return "BEARISH"
    if abs(direction) < 12:
        return "NEUTRAL"
    return "WATCH"


def _attention_score(conf: float, vol_edge: float, liquidity: str, rotation_quadrant: str, ed) -> float:
    liq = {"Excellent": 100, "Tradeable": 60, "Thin": 20}.get(liquidity, 40)
    rot = {"LEADING": 100, "IMPROVING": 60, "WEAKENING": 40, "LAGGING": 0}.get(rotation_quadrant, 30)
    try:
        days = (ed - today_ny()).days if ed else 999
    except TypeError:
        days = 999
    if days <= 1:
        prox = 100
    elif days <= 7:
        prox = 60
    elif days <= 30:
        prox = 30
    else:
        prox = 0
    return round(0.35 * conf + 0.25 * vol_edge + 0.20 * liq + 0.10 * rot + 0.10 * prox, 1)


def _flow_window(prints: list) -> tuple[int, float | None]:
    n = len(prints or [])
    ts = []
    for p in prints or []:
        raw = (p or {}).get("executed_at")
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts.append(dt)
    if len(ts) < 2:
        return n, None
    span = (max(ts) - min(ts)).total_seconds() / 3600.0
    return n, round(span, 1)


def _trade_flow_part(flow: dict, trade_read: str):
    """The trade reason's flow part: the institutional lean, only when the read is already on its side (R5 is a
    tag, "tape on the same side", never a vote; docs/rules/redesign.md R5). None otherwise."""
    _, _, tone = _institutional_flow_comment(flow)
    if (tone, trade_read) == ("bull", "BULLISH"):
        return "institutional bullish lean"
    if (tone, trade_read) == ("bear", "BEARISH"):
        return "institutional bearish lean"
    return None


def _institutional_flow_comment(flow: dict, failed: bool = False):
    """Deterministic institutional lean read. Returns (label, net, tone).

    The lean is read from the print side (``aggregate_flow``; docs/rules/redesign.md R5, review S4): net = bullish
    side premium (ask-side calls + bid-side puts) minus bearish (bid-side calls + ask-side puts), mid dropped. No
    print side served is no lean, never a call-minus-put guess. A package at or above ``INST_FLOW_MIN_NET`` is left
    out of the net and marked "(package risk)". ``failed`` says the flow request itself failed: "no flow" would be a
    claim about the market.
    """
    if failed:
        return "Flow unavailable (provider failure)", 0.0, "neutral"
    bull = float(flow.get("inst_bull_side", 0) or 0)
    bear = float(flow.get("inst_bear_side", 0) or 0)
    outside = float(flow.get("inst_mid", 0) or 0) + float(flow.get("inst_no_side", 0) or 0)
    package = float(flow.get("inst_package", 0) or 0)
    sweep = float(flow.get("sweep", 0) or 0)
    block = float(flow.get("block", 0) or 0)
    risk = " (package risk)" if package >= INST_FLOW_MIN_NET else ""

    if bull + bear + outside == 0:
        return ("Package flow only" + risk, 0.0, "neutral") if package else ("No institutional flow", 0.0, "neutral")
    if not flow.get("side_served"):
        return "Lean unavailable (no print side)" + risk, 0.0, "neutral"

    inst_net = bull - bear
    if abs(inst_net) < INST_FLOW_MIN_NET:
        return "Thin flow" + risk, inst_net, "neutral"

    imbalance = abs(inst_net) / (bull + bear)

    if imbalance < 0.15:
        label, tone = "Mixed institutional lean", "neutral"
    elif inst_net > 0:
        label, tone = "Institutional bullish lean", "bull"
    else:
        label, tone = "Institutional bearish lean", "bear"

    if sweep > 0 and imbalance >= 0.15:
        label = label.replace("Institutional", "Inst.") + " · sweep"
    elif block > 0 and imbalance >= 0.15:
        label = label.replace("Institutional", "Inst.") + " · block"

    return label + risk, inst_net, tone


def _gex_structure_label(spot, gex: dict, gex_profile: dict = None) -> str:
    """Concise structural read from spot vs gamma levels. 1% proximity = 'near'.

    ``gex_profile`` (Directionalized Volume) supplies the volume-book flip and
    magnet; when present they win over ``gex-levels`` for the regime and the
    structure text so an OI flip far from spot cannot mislabel the regime.
    """
    try:
        spot_f = float(spot)
    except (ValueError, TypeError):
        spot_f = None
    try:
        call_wall = float(gex.get("call_wall")) if gex.get("call_wall") is not None else None
        put_wall = float(gex.get("put_wall")) if gex.get("put_wall") is not None else None
        gamma_flip = float(gex.get("gamma_flip")) if gex.get("gamma_flip") is not None else None
    except (ValueError, TypeError):
        call_wall = put_wall = gamma_flip = None

    profile = gex_profile or {}
    prof_regime = profile.get("regime")
    prof_flip = profile.get("flip")
    if prof_flip is not None:
        try:
            gamma_flip = float(prof_flip)
        except (TypeError, ValueError):
            gamma_flip = gamma_flip
    magnet = profile.get("magnet") or {}

    if spot_f is None:
        return "GEX unavailable"
    if call_wall is None and put_wall is None and gamma_flip is None \
            and prof_regime is None and magnet.get("strike") is None:
        return "GEX unavailable"

    near = abs(spot_f) * 0.01
    parts = []
    if prof_regime == "short":
        parts.append("Negative gamma regime (volume)")
    elif prof_regime == "long":
        parts.append("Positive gamma regime (volume)")
    elif gamma_flip is not None:
        parts.append("Positive gamma regime" if spot_f > gamma_flip else "Negative gamma regime")
    if magnet.get("strike") is not None:
        side = "below" if magnet.get("side") == "below" else "above"
        parts.append(f"magnet ${magnet['strike']:.2f} {side}")
    if call_wall is not None:
        if spot_f >= call_wall:
            parts.append("At call-wall resistance")
        elif abs(spot_f - call_wall) <= near:
            parts.append("Approaching call wall")
        else:
            parts.append(f"call wall ${call_wall:.2f}")
    if put_wall is not None:
        if spot_f <= put_wall:
            parts.append("At put-wall support")
        elif abs(spot_f - put_wall) <= near:
            parts.append("Approaching put wall")
        else:
            parts.append(f"put wall ${put_wall:.2f}")

    return " · ".join(parts) if parts else "GEX unavailable"


def _compute_price_action(ohlc: list[dict], ed_str: str = "", timing: str = "") -> dict[str, str]:
    """Price Action: 1D/1W/2W/4W lookbacks ENDING at the earnings release anchor.
    AM (BMO): anchor = last close strictly before ed_str.
    PM (AMC): anchor = last close <= ed_str.
    Falls back to most recent close if no anchor found."""
    by_date = {}
    for c in ohlc:
        d = str(c.get("date") or c.get("datetime", ""))[:10]
        if c.get("close"):
            by_date[d] = float(c["close"])
    dates = sorted(by_date.keys())

    if not dates:
        return {"1d": "", "1w": "", "2w": "", "4w": ""}

    tim = (timing or "").lower().strip()
    is_am = tim in ("premarket", "pre-market", "bmo", "before", "am")
    ed_str = str(ed_str or "")[:10]

    anchor_date = None
    if ed_str:
        if is_am:
            for d in reversed(dates):
                if d < ed_str:
                    anchor_date = d
                    break
        else:
            for d in reversed(dates):
                if d <= ed_str:
                    anchor_date = d
                    break
    if anchor_date is None:
        anchor_date = dates[-1]

    if anchor_date not in by_date:
        return {"1d": "", "1w": "", "2w": "", "4w": ""}

    idx = dates.index(anchor_date)
    anchor_close = by_date[anchor_date]
    if anchor_close <= 0:
        return {"1d": "", "1w": "", "2w": "", "4w": ""}

    lookbacks = {"1d": 1, "1w": 5, "2w": 10, "4w": 20}
    result = {}
    for label, k in lookbacks.items():
        j = idx - k
        if j >= 0 and by_date[dates[j]] > 0:
            pct = (anchor_close / by_date[dates[j]] - 1) * 100
            result[label] = f"{pct:+.1f}%"
        else:
            result[label] = ""
    return result


def _compute_return_since_er(hist: list[dict], closes: list[float], ohlc: list[dict],
                             ed_str: str = "", timing: str = "") -> tuple[str, str, str]:
    """Since ER chart: pre-close vs current price. N/A if not yet released.
    BMO (AM): anchor = previous trading day close. Released from the 09:30 ET open.
    AMC (PM): anchor = same trading day close. Released from the 16:00 ET close.
    A future event, or one today that has not been released yet, returns blanks.
    The clock is New York time (``core.calendar``), not the machine's local time."""
    import json
    now = now_ny()
    today = now.date()
    now_hour = now.hour + now.minute / 60

    # Time gate: hide if earnings not yet released
    if ed_str:
        try:
            ed_date = date.fromisoformat(str(ed_str)[:10])
            if ed_date > today:
                return "", "", ""
            if ed_date == today:
                is_am = timing_kind(timing) == "bmo"
                is_pm = not is_am  # anything not AM is PM (afterhours, amc, pm, etc.)
                if is_am and now_hour < 9.5:
                    return "", "", ""
                if is_pm and now_hour < 16.0:
                    return "", "", ""
        except ValueError:
            pass

    today_close = closes[-1] if closes else None
    if not today_close:
        return "", "", ""

    by_date = {}
    for c in ohlc:
        d = str(c.get("date") or c.get("datetime", ""))[:10]
        if c.get("close"):
            by_date[d] = float(c["close"])
    dates = sorted(by_date.keys())

    if not ed_str:
        return "", "", ""

    ed_str = str(ed_str)[:10]
    tim = (timing or "").lower().strip()
    is_am = tim in ("premarket", "pre-market", "bmo", "before", "am")

    pre_close = None
    pre_date = ""
    if is_am:
        for d in reversed(dates):
            if d < ed_str:
                pre_close = by_date[d]
                pre_date = d
                break
        # Fallback: use most recent close if no previous day found
        if not pre_close and dates:
            pre_close = by_date[dates[-1]]
            pre_date = dates[-1]
    else:
        # AMC: anchor = close ON earnings date. If not yet available, use most recent.
        for d in reversed(dates):
            if d <= ed_str:
                pre_close = by_date[d]
                pre_date = d
                break
        if not pre_close and dates:
            pre_close = by_date[dates[-1]]
            pre_date = dates[-1]

    if not pre_close or pre_close <= 0:
        return "", "", ""

    pct = round((today_close / pre_close - 1) * 100, 2)
    anchor_label = f"{pre_date[-5:]} M"  # M = market close (anchor is always a regular session close)

    # Current time label
    is_market = 9.5 <= now_hour < 16.0
    now_label = "Market" if is_market else "Post-Mkt"

    chart_dates = [d for d in dates if d >= pre_date]
    if len(chart_dates) < 2:
        if dates and dates[-1] != chart_dates[0]:
            chart_dates.append(dates[-1])
        else:
            chart_dates = [chart_dates[0], chart_dates[0]]

    chart_points = []
    for d in chart_dates:
        chart_points.append([d[-5:], round(by_date[d], 2)])

    chart_json = json.dumps({
        "anchor": anchor_label, "anchor_price": round(pre_close, 2),
        "pct": pct, "now_label": now_label, "points": chart_points,
    })
    return f"{anchor_label}: {pct:+.1f}%", pre_date[-5:], chart_json


def _nrc_beat(card) -> float:
    """NRC's event-beat slot: 60 a strong beat history, 45 a beat, 30 a miss, 50 neither.

    Read from R2's own result in the card's rule audit (the sign of its score, and its "strong beat" reason), not
    from the thesis text, where R2's "beat trend declining" read as a beat (review N2). A card without an R2 audit
    keeps the keyword read, "miss" before "beat".
    """
    r2 = (getattr(card, "rule_audit", None) or {}).get("rule2_guidance")
    if isinstance(r2, dict):
        score = r2.get("score")
        if not r2.get("available") or score is None or score == 0:
            return 50.0
        if score < 0:
            return 30.0
        return 60.0 if any("strong beat" in str(r).lower() for r in r2.get("reasons") or []) else 45.0
    thesis = (card.key_thesis or "").lower()
    if "strong beat" in thesis:
        return 60.0
    if "miss" in thesis:
        return 30.0
    if "beat" in thesis:
        return 45.0
    return 50.0


def nrc_from_card(card, watchlist: set = None) -> dict:
    """Build one NRC result dict from a TradeCard (live slots only)."""
    growth = max(0.0, min(100.0, 50.0 + (card.direction_score or 0) * 0.5))
    beat = _nrc_beat(card)
    rot = card.rotation_quadrant
    rot_bonus = 10 if rot == "LEADING" else (5 if rot == "IMPROVING" else (-10 if rot == "LAGGING" else 0))
    accel = nrc_acceleration(card)
    quality = nrc_quality(card)
    nrc, tier = compute_nrc_score(
        growth=growth, quality=quality, beat=beat,
        acceleration=accel, rotation_bonus=rot_bonus,
    )
    wl = watchlist or set()
    return {
        "nrc": nrc, "tier": tier,
        "growth": round(growth, 1),
        "acceleration": accel,
        "quality": None if quality is None else round(quality, 1),
        "beat": round(beat, 1),
        "watchlist": card.ticker in wl,
    }


def compute_nrc_score(growth: float = None, quality: float = None, beat: float = None,
                      acceleration: float = None, rotation_bonus: float = 0) -> tuple[float, str]:
    """NRC from live slots only. Missing slots skip their weight (no 50-fill).

    ``revision`` / ``corroboration`` were dropped — they were always 50.
    """
    from RevenueChaser.core.config import NRC_WEIGHTS
    slots = {
        "growth": growth,
        "acceleration": acceleration,
        "quality": quality,
        "event": beat,
    }
    w = NRC_WEIGHTS
    live = {k: v for k, v in slots.items() if v is not None and k in w}
    tw = sum(w[k] for k in live)
    if tw <= 0:
        return 0.0, "Ignore"
    nrc = sum(w[k] * live[k] for k in live) / tw + rotation_bonus
    nrc = round(max(0, min(100, nrc)), 1)

    if quality is not None and quality < 12:
        nrc = min(nrc, 49)

    if nrc >= 70:
        tier = "Tier A — Revenue Catalyst"
    elif nrc >= 55:
        tier = "Tier B — Watchlist"
    elif nrc >= 40:
        tier = "Tier C — Emerging"
    else:
        tier = "Ignore"
    return nrc, tier


def _safe_float(v):
    """Coerce to float, or ``None`` when missing/malformed — never a fake 0.

    A missing IV must stay unknown (not 0), or IV/RV would read as "vol cheap".
    """
    try:
        return float(v) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _fmt_price_float(v):
    """Coerce a price (UW returns strings) to float, or None if unavailable."""
    try:
        return float(v) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _fmt_int(val) -> str:
    if val is None:
        return ""
    try:
        num = int(float(val))
        if num >= 1_000_000_000:
            return f"{num/1e9:.1f}B"
        if num >= 1_000_000:
            return f"{num/1e6:.1f}M"
        if num >= 1_000:
            return f"{num/1e3:.1f}K"
        return str(num) if num else ""
    except (ValueError, TypeError):
        return str(val) if val else ""


def _fmt_gex(val) -> str:
    """Format a GEX price level as a dollar string, empty if unavailable."""
    if val is None or val == "":
        return ""
    try:
        f = float(val)
        return f"${f:.2f}"
    except (ValueError, TypeError):
        return ""


def _score_financials(ticker: str, cluster: str, records: list) -> RuleResult:
    """Score R3 from already-fetched ``fin_records`` (no extra UW call)."""
    if not records:
        return RuleResult(score=None, available=False, coverage=0, reasons=["financial data unavailable"])
    try:
        return rule3_financial.score(ticker, cluster, records)
    except Exception as e:
        logger.debug(f"R3 financials failed {ticker}: {e}")
        return RuleResult(score=None, available=False, coverage=0, reasons=["financial data unavailable"])


def _score_accounting(records: list) -> RuleResult:
    """Score R7 from already-fetched ``fin_records`` (no extra UW call)."""
    if not records:
        return RuleResult(score=None, available=False, coverage=0, reasons=["accounting data unavailable"])
    try:
        return rule7_accounting.score(records)
    except Exception as e:
        logger.debug(f"R7 accounting failed: {e}")
        return RuleResult(score=None, available=False, coverage=0, reasons=["accounting data unavailable"])


def _score_sentiment(flow: dict) -> RuleResult:
    """R8: retail call share from flow-recent. No call premium → unavailable."""
    retail_call = float(flow.get("retail_call", 0) or 0)
    inst_call = float(flow.get("inst_call", 0) or 0)
    total_call = retail_call + inst_call
    if total_call > 0:
        retail_share = retail_call / total_call
        return rule8_sentiment.score(retail_call_share=retail_share)
    return rule8_sentiment.score()
