"""Regression tests for EarningsPipeline._score, focused on the string-price crash.

UW returns numeric fields as strings (e.g. close="6.355"). A previous bug did
`f"${spot:.2f}"` on that string and raised ValueError, silently dropping every
ticker that had a price — leaving only excluded/no-data tickers in the report.
These tests drive _score end-to-end with a fake client to ensure string prices
are coerced and a card is always produced.
"""
from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline, _safe_float
from RevenueChaser.module_earnings_radar.models import TradeCard
from RevenueChaser.module_earnings_radar.rules import rule5_positioning

import pytest


@pytest.fixture(autouse=True)
def _isolate_leaps_cache(rawstore_tmp_db):
    """Keep LEAPS reads/writes out of the real data dir."""
    return rawstore_tmp_db


@pytest.fixture(autouse=True)
def options_set(monkeypatch):
    """Every card reads a list set (G9: the options store) whose rows a test fills: ``strikes`` (spot exposures by
    strike), ``term`` (the term structure), ``gex`` (the four levels) and ``prints``. Empty by default, as the old
    fake UW answered; the three-way rule itself is tested in ``test_options_store_g9``."""
    from RevenueChaser.module_earnings_radar import store_reads
    state = {"set": "list", "session": None, "gex": {}, "strikes": [], "term": [], "prints": [], "volume": {}}
    monkeypatch.setattr(store_reads, "card_inputs", lambda ticker, report_date, listed: dict(state))
    return state


class _FakeUW:
    """Minimal UW client returning empty data except what each test overrides."""

    def __init__(self, **overrides):
        self._overrides = overrides

    def _ov(self, name, default):
        return self._overrides.get(name, default)

    def earnings_data(self, ticker):
        return self._ov("earnings_data", {})

    def earnings_history(self, ticker):
        return self._ov("earnings_history", [])

    def flow_recent(self, ticker):
        return self._ov("flow_recent", [])

    def aggregate_flow(self, ticker, flow):
        return self._ov("aggregate_flow", {})

    def screener_batch(self, tickers):
        return self._ov("screener_batch", {})

    def ticker_info(self, ticker):
        return self._ov("ticker_info", {})

    def etf_ohlc(self, ticker, days=60, candle_size="1d"):
        return self._ov("etf_ohlc", [])

    def _map_financials(self, ticker):
        return self._ov("map_financials", [])

    def gex_levels(self, ticker):
        return self._ov("gex_levels", {})

    def ohlc_latest(self, ticker):
        return self._ov("ohlc_latest", {})

    def options_volume(self, ticker):
        return self._ov("options_volume", {})


def _make_pipeline(fake_uw, screener_cache, cal_meta=None):
    p = EarningsPipeline(["AAPL"], uw=fake_uw, cal_meta=cal_meta or {})
    p._screener_cache = screener_cache  # bypass lazy screener_batch()
    return p


class TestScoreStringPrice:
    def test_string_close_from_screener(self):
        """close="6.355" (str) must not crash and must format as "$6.36"."""
        fake = _FakeUW()
        scr = {"AAPL": {"close": "6.355", "call_volume": "0", "put_volume": "0",
                        "total_open_interest": "0"}}
        cal = {"AAPL": {"earnings_date": "2026-08-14", "full_name": "Aple",
                        "sector": "Technology", "expected_move_perc": 0.05}}
        p = _make_pipeline(fake, scr, cal)
        card = p._score("AAPL")
        assert isinstance(card, TradeCard)
        assert card.current_price == "$6.36"

    def test_string_price_from_earnings_data(self):
        """earn['price']="150.25" (str) fallback must not crash and format correctly."""
        fake = _FakeUW(earnings_data={"price": "150.25", "company_name": "Aple"})
        scr = {"AAPL": {}}
        cal = {"AAPL": {"earnings_date": "2026-08-14", "full_name": "Aple",
                        "sector": "Technology", "expected_move_perc": 0.05}}
        p = _make_pipeline(fake, scr, cal)
        card = p._score("AAPL")
        assert card.current_price == "$150.25"

    def test_numeric_close_still_works(self):
        """Numeric close (float) should also produce a formatted price."""
        fake = _FakeUW()
        scr = {"AAPL": {"close": 100.0}}
        cal = {"AAPL": {"earnings_date": "2026-08-14", "full_name": "Aple",
                        "sector": "Technology", "expected_move_perc": 0.05}}
        p = _make_pipeline(fake, scr, cal)
        card = p._score("AAPL")
        assert card.current_price == "$100.00"

    def test_no_price_yields_empty(self):
        """No price anywhere -> current_price stays empty (no crash)."""
        fake = _FakeUW()
        scr = {"AAPL": {}}
        cal = {"AAPL": {"earnings_date": "2026-08-14", "full_name": "Aple",
                        "sector": "Technology", "expected_move_perc": 0.05}}
        p = _make_pipeline(fake, scr, cal)
        card = p._score("AAPL")
        assert card.current_price == ""

    def test_garbage_price_yields_empty(self):
        """Non-numeric price must degrade to empty, not raise."""
        fake = _FakeUW()
        scr = {"AAPL": {"close": "n/a"}}
        cal = {"AAPL": {"earnings_date": "2026-08-14", "full_name": "Aple",
                        "sector": "Technology", "expected_move_perc": 0.05}}
        p = _make_pipeline(fake, scr, cal)
        card = p._score("AAPL")
        assert card.current_price == ""


class TestScoreStringCloseEndToEnd:
    def test_run_produces_card_for_string_close(self, monkeypatch):
        """Full run() path: string close must not drop the ticker from cards."""
        from RevenueChaser.module_earnings_radar import store_reads
        monkeypatch.setattr(store_reads, "screener_set", lambda tickers, latest=None: (
            {"AAPL": {"close": "6.355", "call_volume": "1000", "put_volume": "1000", "total_open_interest": "2000"}},
            {"status": "ok", "error": None, "session": None}))
        fake = _FakeUW()
        p = EarningsPipeline(["AAPL"], uw=fake, cal_meta={
            "AAPL": {"earnings_date": "2026-08-14", "full_name": "Aple",
                     "sector": "Technology", "expected_move_perc": 0.05},
        })
        cards = p.run(use_preselect=False)
        assert len(cards) == 1
        assert cards[0].current_price == "$6.36"


class TestR5ShortInterestWiring:
    """R5's squeeze inputs must reach the scorer from the ownership store (G9)."""

    def _pipeline(self, fake):
        cal = {
            "AAPL": {"earnings_date": "2026-08-14", "full_name": "Aple",
                     "sector": "Technology", "expected_move_perc": 0.05},
            "MSFT": {"earnings_date": "2026-08-14", "full_name": "Micro",
                     "sector": "Technology", "expected_move_perc": 0.05},
        }
        p = EarningsPipeline(["AAPL", "MSFT"], uw=fake, cal_meta=cal)
        p._screener_cache = {}
        return p

    def test_score_uses_shorts_for_squeeze_signal(self):
        """AAPL has flow {} (no inst signal) but a borrow fee in the store -> R5 must be available from the
        squeeze tag alone; without wiring it would be unavailable (G9: the ownership store's row)."""
        p = self._pipeline(_FakeUW())
        p._shorts_cache = {"AAPL": {"si_pct": None, "borrow_fee": 35.0, "short_interest": 1_000_000.0}}
        card = p._score("AAPL")
        audit = (card.rule_audit or {}).get("rule5_positioning", {})
        assert audit.get("available") is True

    def test_no_stored_row_falls_back_to_flow_only(self):
        """No ownership file: the warm path reads nothing, asks no provider, and R5 has no squeeze."""
        fake = _FakeUW()
        p = self._pipeline(fake)
        p._warm_short_interest()
        assert p._shorts_cache == {}
        card = p._score("AAPL")
        audit = (card.rule_audit or {}).get("rule5_positioning", {})
        assert audit.get("available") is False


def _duration_ohlc_and_yields(n=25, duration=10.0):
    from datetime import date, timedelta
    ohlc, yld = [], {}
    price, y = 100.0, 4.00
    start = date(2026, 1, 5)
    for i in range(n):
        d = (start + timedelta(days=i)).isoformat()
        ohlc.append({"date": d, "close": price})
        yld[d] = y
        step = 0.10 * (0.5 + (i % 3) * 0.5)
        price *= 1.0 + (-duration * (step / 100.0))
        y += step
    return ohlc, yld


class TestR6YieldBetaWiring:
    def test_injected_dgs10_badges_not_vetoes_without_rate_overlap(self):
        """|beta| > 4 with no FOMC/CPI overlap -> badge, NOT macro_veto."""
        ohlc, yld = _duration_ohlc_and_yields(duration=10.0)
        fake = _FakeUW(etf_ohlc=ohlc)
        p = _make_pipeline(fake, {"AAPL": {"close": "100", "call_volume": "0",
                                           "put_volume": "0", "total_open_interest": "0"}},
                           {"AAPL": {"earnings_date": "2026-08-14", "full_name": "Aple",
                                     "sector": "Technology", "expected_move_perc": 0.05}})
        p._dgs10_cache = yld
        card = p._score("AAPL")
        assert card.macro_veto is False          # overlap not assumed
        assert "MACRO VETO" not in (card.badges_json or "")
        audit = (card.rule_audit or {}).get("rule6_macro", {})
        assert audit.get("available") is True
        # extreme sensitivity surfaced as a reason/badge, not a veto flag
        assert audit.get("flags") == []
        assert any("extreme rate sensitivity" in x for x in audit.get("reasons") or [])

    def test_no_dgs10_leaves_r6_unavailable(self):
        fake = _FakeUW()
        p = _make_pipeline(fake, {}, {"AAPL": {"earnings_date": "2026-08-14",
                                               "full_name": "Aple", "sector": "Technology",
                                               "expected_move_perc": 0.05}})
        p._dgs10_cache = {}
        card = p._score("AAPL")
        assert card.macro_veto is False
        assert (card.rule_audit or {}).get("rule6_macro") is None

    def test_the_market_store_series_is_what_r6_scores(self, monkeypatch):
        """The yield series comes from the market store and nothing is fetched: the board has no FRED reader left."""
        from datetime import datetime, timezone
        from RevenueChaser.core import calendar
        from RevenueChaser.module_earnings_radar import macro_watch
        from RevenueChaser.module_earnings_radar.tests.market_store_fixture import serve as serve_market
        monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 1, 30, 23, 0, tzinfo=timezone.utc))
        ohlc, yld = _duration_ohlc_and_yields(duration=10.0)       # 2026-01-05 .. 01-29: fresh on Jan 30
        serve_market(dict(yld))
        assert not hasattr(macro_watch, "fred_daily_map")
        p = _make_pipeline(_FakeUW(etf_ohlc=ohlc),
                           {"AAPL": {"close": "100", "call_volume": "0", "put_volume": "0",
                                     "total_open_interest": "0"}},
                           {"AAPL": {"earnings_date": "2026-08-14", "full_name": "Aple",
                                     "sector": "Technology", "expected_move_perc": 0.05}})
        p._warm_dgs10()
        assert p._dgs10_cache == yld
        assert p.input_sources["dgs10"] == "stored"
        card = p._score("AAPL")
        audit = (card.rule_audit or {}).get("rule6_macro", {})
        assert audit.get("available") is True and card.macro_veto is False     # no rate event stored


class TestR7ReusesFinRecords:
    """R3 and R7 must share the _map_financials payload already fetched in _score."""

    def test_map_financials_called_once(self):
        calls = []

        class _CountUW(_FakeUW):
            def _map_financials(self, ticker):
                calls.append(ticker)
                return [{"net_income": 100, "cash_from_operations": 90,
                         "total_assets": 1000, "revenue": 500}]

        fake = _CountUW()
        p = _make_pipeline(fake, {"AAPL": {"close": "10", "call_volume": "0",
                                           "put_volume": "0", "total_open_interest": "0"}},
                           {"AAPL": {"earnings_date": "2026-08-14", "full_name": "Aple",
                                     "sector": "Technology", "expected_move_perc": 0.05}})
        p._dgs10_cache = {}
        card = p._score("AAPL")
        assert calls == ["AAPL"]
        audit = (card.rule_audit or {}).get("rule7_accounting", {})
        assert audit.get("available") is True
        assert audit.get("score") == 20.0

def _strike(cg=None, pg=None):
    return {"strike": "0", "call_gamma_vol": None if cg is None else str(cg),
            "put_gamma_vol": None if pg is None else str(pg),
            "call_gamma_oi": None, "put_gamma_oi": None,
            "call_charm_vol": None, "put_charm_vol": None,
            "call_vanna_vol": None, "put_vanna_vol": None}


class TestR4GexProfileWiring:
    def test_strike_profile_reaches_r4_and_structure(self, options_set):
        # COO-like: red band 65/70 inside the EM window => volume-book short.
        rows = [
            _strike(332, 0),      # 60 -> net +332
            _strike(-588, 0),     # 65
            _strike(-2304, 0),    # 70
            _strike(2968, 0),     # 75
            _strike(12941, 0),    # 80
        ]
        for i, s in enumerate((60, 65, 70, 75, 80)):
            rows[i]["strike"] = str(s)
        options_set["strikes"] = rows
        fake = _FakeUW()
        p = _make_pipeline(fake, {"AAPL": {"close": "66.83", "call_volume": "0",
                                           "put_volume": "0", "total_open_interest": "0"}},
                           {"AAPL": {"earnings_date": "2026-09-09", "full_name": "Cooper",
                                     "sector": "Healthcare", "expected_move_perc": 0.0743}})
        p._dgs10_cache = {}
        card = p._score("AAPL")
        audit = (card.rule_audit or {}).get("rule4_volatility", {})
        assert audit.get("available") is True
        reasons = " ".join(audit.get("reasons") or [])
        assert "volume book" in reasons and "short" in reasons
        assert "Negative gamma regime" in (card.gex_structure or "")

    def test_no_stored_strikes_fall_back_cleanly(self):
        fake = _FakeUW()
        p = _make_pipeline(fake, {"AAPL": {"close": "66.83", "call_volume": "0",
                                           "put_volume": "0", "total_open_interest": "0"}},
                           {"AAPL": {"earnings_date": "2026-09-09", "full_name": "Cooper",
                                     "sector": "Healthcare", "expected_move_perc": 0.0743}})
        p._dgs10_cache = {}
        card = p._score("AAPL")
        assert card is not None
        audit = (card.rule_audit or {}).get("rule4_volatility", {})
        assert audit.get("available") is True


def _drift_bars_dated(n, start_iso="2026-07-01", drift=0.005):
    """Chronological OHLC bars dated before today so the YZ partial-bar guard
    keeps them all (dates are strictly before the 2026-09-10 run day)."""
    from datetime import date
    start = date.fromisoformat(start_iso)
    bars = []
    prev = 100.0
    for i in range(n):
        o = prev
        c = prev * (1 + drift)
        h = max(o, c) * 1.002
        l = min(o, c) * 0.998
        bars.append({"date": date.fromordinal(start.toordinal() + i).isoformat(),
                     "open": o, "high": h, "low": l, "close": c})
        prev = c
    return bars


class TestR4TermStructureWiring:
    def test_term_slope_reaches_r4_as_a_penalty(self, options_set):
        # Near 7 DTE at 0.60 vs 45 DTE at 0.425 -> deep backwardation
        # (-0.00461/day). EM 7.4 (+15) minus the -8 backwardation penalty.
        options_set["term"] = [
            {"dte": "7", "volatility": "0.60"},
            {"dte": "30", "volatility": "0.425"},
            {"dte": "45", "volatility": "0.425"},
        ]
        fake = _FakeUW()
        p = _make_pipeline(fake, {"AAPL": {"close": "66.83", "call_volume": "0",
                                           "put_volume": "0", "total_open_interest": "0"}},
                           {"AAPL": {"earnings_date": "2026-09-09", "full_name": "Cooper",
                                     "sector": "Healthcare", "expected_move_perc": 0.0743}})
        p._dgs10_cache = {}
        card = p._score("AAPL")
        audit = (card.rule_audit or {}).get("rule4_volatility", {})
        reasons = " ".join(audit.get("reasons") or [])
        assert "backwardated" in reasons
        assert card.volatility_edge_score == 7.0  # +15 EM, -8 backwardation

    def test_no_stored_term_structure_falls_back_cleanly(self):
        fake = _FakeUW()
        p = _make_pipeline(fake, {"AAPL": {"close": "66.83", "call_volume": "0",
                                           "put_volume": "0", "total_open_interest": "0"}},
                           {"AAPL": {"earnings_date": "2026-09-09", "full_name": "Cooper",
                                     "sector": "Healthcare", "expected_move_perc": 0.0743}})
        p._dgs10_cache = {}
        card = p._score("AAPL")
        audit = (card.rule_audit or {}).get("rule4_volatility", {})
        assert audit.get("available") is True
        assert card.volatility_edge_score == 15.0  # EM only, no slope penalty
        assert "term structure" not in " ".join(audit.get("reasons") or [])


class _YzUW(_FakeUW):
    """Fake UW that serves enough daily OHLC bars for an offline YZ-RV30."""

    def etf_ohlc(self, ticker, days=60, candle_size="1d"):
        return self._ov("etf_ohlc", _drift_bars_dated(40))


class TestR4YangZhangWiring:
    def test_yz_rv30_overrides_the_screener_rv(self):
        # Screener claims RV 0.99 (which alone would read CHEAP, +10); the
        # offline YZ-RV30 (~0.0845) must win, so IV/RV = 0.5/0.0845 ~ 5.9 -> the
        # 1.25-rich penalty fires on real (Yang-Zhang) realized vol. EM 5 (+5)
        # minus 5 -> 0 proves the YZ value, not the screener's, reached R4.
        fake = _YzUW(flow_recent=[], aggregate_flow={})
        scr = {"AAPL": {"close": "100.0", "iv30d": "0.5", "realized_volatility": "0.99",
                        "call_volume": "0", "put_volume": "0", "total_open_interest": "0"}}
        p = _make_pipeline(fake, scr,
                           {"AAPL": {"earnings_date": "2026-09-09", "full_name": "Aple",
                                     "sector": "Technology", "expected_move_perc": 0.05}})
        p._dgs10_cache = {}
        card = p._score("AAPL")
        audit = (card.rule_audit or {}).get("rule4_volatility", {})
        assert card.volatility_edge_score == 0.0
        assert "rich" in " ".join(audit.get("reasons") or [])

    def test_too_few_bars_falls_back_to_screener_rv(self):
        # Only 10 bars -> no YZ-RV30, so the screener RV 0.99 drives the pair:
        # IV/RV = 0.5/0.99 = 0.505 < 1.0 -> CHEAP (+10). EM 5 + 10 = 15.
        fake = _YzUW(etf_ohlc=_drift_bars_dated(10))
        scr = {"AAPL": {"close": "100.0", "iv30d": "0.5", "realized_volatility": "0.99",
                        "call_volume": "0", "put_volume": "0", "total_open_interest": "0"}}
        p = _make_pipeline(fake, scr,
                           {"AAPL": {"earnings_date": "2026-09-09", "full_name": "Aple",
                                     "sector": "Technology", "expected_move_perc": 0.05}})
        p._dgs10_cache = {}
        card = p._score("AAPL")
        audit = (card.rule_audit or {}).get("rule4_volatility", {})
        assert card.volatility_edge_score == 15.0
        assert "cheap" in " ".join(audit.get("reasons") or [])

class TestStoredOhlcWiring:
    """E5e (#372): the stored D01 year serves the reaction window and the offline YZ-RV30; UW is not asked."""

    def test_a_stored_year_of_bars_replaces_the_ohlc_request(self, monkeypatch):
        from datetime import date, datetime, timedelta, timezone
        from RevenueChaser.core import calendar, rawstore
        from RevenueChaser.module_earnings_radar import stored_inputs
        end = date(2026, 10, 14)
        monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc))
        days, d = [], end
        while len(days) < 60:
            if d.weekday() < 5:
                days.append(d)
            d -= timedelta(days=1)
        bars = [{"date": x.isoformat(), "open": 99.0 + i, "high": 101.0 + i, "low": 98.0 + i,
                 "close": 100.0 + i, "volume": 1000.0} for i, x in enumerate(reversed(days))]
        rawstore.write_rows("price_bars_daily", [
            {"ticker": "AAPL", "session_date": date.fromisoformat(b["date"]), "open": b["open"],
             "high": b["high"], "low": b["low"], "close": b["close"], "volume": b["volume"], "source": "uw"}
            for b in bars])

        class _CountingUW(_FakeUW):
            def __init__(self, **kw):
                super().__init__(**kw)
                self.ohlc_calls = []

            def etf_ohlc(self, ticker, days=60, candle_size="1d"):
                self.ohlc_calls.append(ticker)
                return super().etf_ohlc(ticker, days, candle_size)

        fake = _CountingUW()
        p = _make_pipeline(fake, {"AAPL": {"close": "100", "iv30d": "0.5", "realized_volatility": "9.99",
                                           "call_volume": "0", "put_volume": "0", "total_open_interest": "0"}},
                           {"AAPL": {"earnings_date": "2026-11-05", "full_name": "Aple",
                                     "sector": "Technology", "expected_move_perc": 0.05}})
        p._dgs10_cache = {}
        p._ohlc_bars = stored_inputs.ohlc_bars(["AAPL"])

        card = p._score("AAPL")

        assert fake.ohlc_calls == []
        assert p.input_sources["ohlc"] == {"stored": 1, "live": 0}
        assert card.current_price == "$100.00"
        # The offline Yang-Zhang RV30 got the stored bars, and it — not the screener's bogus 0.99 — drives the pair:
        # a ~1% daily drift over the stored series leaves IV/RV well above 1, where 0.5/9.99 would read cheap.
        audit = (card.rule_audit or {}).get("rule4_volatility", {})
        assert audit.get("available") is True
        assert card.iv_rv_ratio > 1.0


class TestSafeFloat:
    """Missing/malformed values are None, never a fabricated 0 (IV must not read as cheap)."""

    def test_missing_is_none(self):
        assert _safe_float(None) is None
        assert _safe_float("") is None

    def test_malformed_is_none(self):
        assert _safe_float("abc") is None

    def test_valid_parses(self):
        assert _safe_float("1.5") == 1.5
        assert _safe_float(0) == 0.0


class TestSectorIsTheSpine:
    """R5 (#88): the card's sector comes from the spine, not the UW calendar sector. #266: it is a Morningstar
    sector name plus the industry text; the cluster hints read that text."""

    @staticmethod
    def _seed_spine(rows):
        from RevenueChaser.core import rawstore
        rawstore.replace_all("equity_universe", [{
            "ticker": t, "cik": "", "name": t, "market_cap": 1e9, "price": 10.0,
            "sector": sec, "industry": ind, "industry_group": None, "sub_industry": None,
            "quote": {"symbol": t}} for t, sec, ind in rows])

    def _card(self, ticker="ZZZZ"):
        cal = {ticker: {"earnings_date": "2026-08-14", "full_name": "Zed",
                        "sector": "Technology", "expected_move_perc": 0.05}}   # vendor says Technology
        p = _make_pipeline(_FakeUW(), {ticker: {}}, cal)
        p.tickers = [ticker]
        return p._score(ticker)

    @staticmethod
    def _label(sector, industry):
        from RevenueChaser.module_earnings_radar.classifier import classify_ticker, get_cluster_label
        return get_cluster_label(classify_ticker("ZZZZ", sector, industry))

    def test_cluster_follows_the_spine_sector_not_the_calendar_sector(self):
        self._seed_spine([("ZZZZ", "Energy", "Oil & Gas E&P")])
        want = self._label("Energy", "Oil & Gas E&P")
        assert self._card().cluster == want == "Energy/Utilities"
        assert want != self._label("Technology", "")

    def test_the_cluster_hint_reads_the_spine_industry_text(self):
        """Two Technology names: a semiconductor industry routes to hardware, a software one stays SaaS."""
        self._seed_spine([("ZZZZ", "Technology", "Semiconductors")])
        assert self._card().cluster == "Hardware/Semis"
        self._seed_spine([("ZZZZ", "Technology", "Software - Application")])
        assert self._card().cluster == "SaaS/Software"

    def test_the_spine_is_read_once_per_run_not_per_ticker(self, monkeypatch):
        from RevenueChaser.core import universe
        self._seed_spine([("AAAA", "Energy", "Oil & Gas E&P"), ("BBBB", "Utilities", "Utilities - Regulated Electric")])
        calls = []
        real = universe.spine_gics
        monkeypatch.setattr(universe, "spine_gics", lambda t=None: calls.append(t) or real(t))
        cal = {t: {"earnings_date": "2026-08-14", "full_name": t, "sector": "Technology",
                   "expected_move_perc": 0.05} for t in ("AAAA", "BBBB")}
        p = _make_pipeline(_FakeUW(), {"AAAA": {}, "BBBB": {}}, cal)
        p.tickers = ["AAAA", "BBBB"]
        cards = [p._score(t) for t in p.tickers]
        assert len(calls) == 1
        assert cards[0].cluster == self._label("Energy", "Oil & Gas E&P")
        assert cards[1].cluster == self._label("Utilities", "Utilities - Regulated Electric")

    def test_ticker_without_a_spine_row_is_unclassified(self):
        self._seed_spine([("OTHR", "Energy", "Oil & Gas E&P")])
        assert self._card().cluster == self._label("Unclassified", "Unclassified")


def test_e18_edge_counts_a_zero_eps_print():
    # A reported EPS of exactly 0.0 is a completed print (the `or` chain dropped it).
    from RevenueChaser.module_earnings_radar.pipeline import _compute_edge
    hist = [{"actual_eps": 0.0, "report_date": "2026-01-10", "post_earnings_move_1d": 0.05}
            for _ in range(3)]
    median, ratio = _compute_edge(hist, 4.0)
    assert median == 5.0 and ratio == 1.25
