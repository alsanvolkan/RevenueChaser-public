"""A mock of the UW endpoints the options datasets call, answered at the HTTP boundary (``http_mock``).

``UWOptions(session)`` is one callable route for ``uw.test/api``: it answers each endpoint for any ticker from a
small, deterministic chain (``expiries`` expiries, ``strikes`` strikes, a call and a put each), with every number a
string as UW sends them. ``fail`` maps a path substring to an HTTP status to answer instead; ``contracts`` maps a
ticker to the number of contracts each of its expiries answers (paged at 500).
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from urllib.parse import parse_qs

from RevenueChaser.core import calendar

SESSION = date(2026, 10, 26)
HEADERS = {"content-type": "application/json"}


def occ(root: str, expiry: date, cp: str, strike: float) -> str:
    return f"{root}{expiry:%y%m%d}{cp}{int(round(strike * 1000)):08d}"


class UWOptions:
    def __init__(self, session: date = SESSION, *, expiries: int = 2, strikes: int = 3):
        self.session = session
        self.expiries = [calendar.next_trading_day(session) + timedelta(days=7 * i) for i in range(expiries)]
        self.strikes = [100.0 + 5 * i for i in range(strikes)]
        self.fail: dict[str, int] = {}
        self.contracts: dict[str, int] = {}
        self.screener: dict[str, dict] = {}        # ticker -> screener fields (step 3)
        self.calendar: dict[tuple[date, str], list[str]] = {}   # (report day, premarket|afterhours) -> tickers
        self.requests: list[tuple[str, dict]] = []

    # ── the answers ──
    def _contracts(self, ticker: str, expiry: date) -> list[dict]:
        root = "SPXW" if ticker == "SPX" else ticker.replace(".", "")
        n = self.contracts.get(ticker)
        strikes = self.strikes if n is None else [100.0 + 0.5 * i for i in range((n + 1) // 2)]
        out = []
        for k in strikes:
            for cp in ("C", "P"):
                out.append({"option_symbol": occ(root, expiry, cp, k), "nbbo_bid": "1.10", "nbbo_ask": "1.20",
                            "last_price": "1.15", "high_price": "1.30", "low_price": "1.00", "avg_price": "1.14",
                            "implied_volatility": "0.31", "delta": "0.5" if cp == "C" else "-0.5", "gamma": "0.02",
                            "theta": "-0.05", "vega": "0.11", "rho": "0.01", "open_interest": "120", "prev_oi": "100",
                            "volume": "40", "ask_volume": "20", "bid_volume": "15", "mid_volume": "5",
                            "no_side_volume": "0", "sweep_volume": "3", "multi_leg_volume": "2",
                            "stock_multi_leg_volume": "0", "floor_volume": "0", "cross_volume": "0",
                            "total_premium": "4600", "last_tape_time": f"{self.session}T19:59:00Z"})
        return out[:n] if n is not None else out

    def _prints(self, ticker: str) -> list[dict]:
        out = []
        for i in range(60):
            out.append({"id": f"{ticker}-p{i}", "executed_at": f"{self.session}T{14 + i // 60:02d}:{i % 60:02d}:00Z",
                        "option_chain_id": occ(ticker, self.expiries[0], "C", self.strikes[0]), "option_type": "call",
                        "expiry": self.expiries[0].isoformat(), "strike": f"{self.strikes[0]}", "price": "1.15",
                        "size": "10", "premium": "1150", "nbbo_bid": "1.10", "nbbo_ask": "1.20",
                        "tags": ["ask_side", "bullish"], "report_flags": [], "upstream_condition_detail": "auto",
                        "multi_vol": "0", "stock_multi_vol": "0", "ask_vol": "10", "bid_vol": "0", "mid_vol": "0",
                        "no_side_vol": "0", "canceled": False, "exchange": "CBOE"})
        prev = calendar.previous_trading_day(self.session - timedelta(days=1))
        out.append({"id": f"{ticker}-old", "executed_at": f"{prev}T15:00:00Z", "option_type": "put", "price": "2",
                    "size": "1", "premium": "200"})
        return out

    def body(self, path: str, q: dict):
        s = self.session.isoformat()
        parts = path.split("/")
        if path.startswith("/screener/stocks"):
            tickers = [t for t in (q.get("ticker") or [""])[0].split(",") if t]
            return {"data": [{"ticker": t, **{k: str(v) for k, v in self.screener[t].items()}}
                             for t in tickers if t in self.screener]}
        if path.startswith("/earnings/"):
            day = date.fromisoformat(q["date"][0])
            page, limit = int((q.get("page") or ["0"])[0]), int((q.get("limit") or ["50"])[0])
            rows = [{"symbol": t, "report_date": day.isoformat(), "full_name": f"{t} Inc"}
                    for t in self.calendar.get((day, parts[2]), [])]
            return {"data": rows[page * limit:(page + 1) * limit]}
        ticker, ep = parts[2], "/".join(parts[3:])
        if ep == "expiry-breakdown":
            # ``expires``, not the spec's ``expiry``: what UW sent in the first real sample (2026-10-28).
            return {"data": [{"expires": e.isoformat(), "chains": len(self.strikes) * 2, "open_interest": 1000,
                              "volume": 200} for e in self.expiries]}
        if ep == "option-contracts":
            rows = self._contracts(ticker, date.fromisoformat(q["expiry"][0]))
            page, limit = int((q.get("page") or ["0"])[0]), int((q.get("limit") or ["500"])[0])
            return {"data": rows[page * limit:(page + 1) * limit]}
        if ep == "greeks":
            return {"data": [{"date": s, "expiry": q["expiry"][0], "strike": f"{k}", "call_charm": "0.7",
                              "put_charm": "-0.7", "call_vanna": "0.3", "put_vanna": "-0.3", "call_volatility": "0.32",
                              "put_volatility": "0.33"} for k in self.strikes]}
        if ep == "spot-exposures/strike":
            return {"data": [{"strike": f"{k}", "time": f"{s}T20:15:00Z", "price": "101.5",
                              "call_gamma_oi": "1000.5", "put_gamma_oi": "-900.25"} for k in self.strikes]}
        if ep in ("greek-exposure/strike", "greek-exposure/expiry"):
            key = "strike" if ep.endswith("strike") else "expiry"
            values = self.strikes if key == "strike" else [e.isoformat() for e in self.expiries]
            return {"data": [{"date": s, key: f"{v}", "call_gex": "5000", "put_gex": "-4000", "call_delta": "10",
                              "put_delta": "-9"} for v in values]}
        if ep == "flow-per-expiry":
            return {"data": [{"date": s, "ticker": ticker, "expiry": e.isoformat(), "call_premium": "12000",
                              "call_trades": "30", "put_volume": "70"} for e in self.expiries]}
        if ep == "volatility/term-structure":
            return {"data": [{"date": s, "expiry": e.isoformat(), "dte": (e - self.session).days, "volatility": "0.3",
                              "implied_move": "4.2", "implied_move_perc": "0.04"} for e in self.expiries]}
        if ep == "gex-levels":
            return {"data": {"call_wall": "110", "gamma_flip": "102.5", "gamma_magnet": "105", "put_wall": "95"}}
        if ep == "options-volume":
            prev = calendar.previous_trading_day(self.session - timedelta(days=1)).isoformat()
            return {"data": [{"date": s, "call_volume": "600", "put_volume": "400", "call_premium": "5000"},
                             {"date": prev, "call_volume": "1", "put_volume": "1"}]}
        if ep == "interpolated-iv":
            return {"data": [{"date": s, "days": d, "volatility": f"0.{20 + d}", "implied_move_perc": "0.05",
                              "percentile": "60"} for d in (1, 5, 7, 14, 30)]}
        if ep == "iv-rank":
            prev = calendar.previous_trading_day(self.session - timedelta(days=1)).isoformat()
            return {"data": [{"date": prev, "iv_rank_1y": "0.4", "volatility": "0.28", "close": "100"},
                             {"date": s, "iv_rank_1y": "0.5", "volatility": "0.3", "close": "101"}]}
        if ep == "flow-recent":
            return {"data": self._prints(ticker)}
        return None

    def __call__(self, request):
        path = request.url.path.split("/api", 1)[1]
        q = parse_qs(request.url.query.decode())
        self.requests.append((path, q))
        for sub, status in self.fail.items():
            if sub in path + "?" + request.url.query.decode():
                return status, b"refused", HEADERS
        body = self.body(path, q)
        if body is None:
            return 404, b"no route", HEADERS
        return 200, json.dumps(body).encode(), HEADERS

    def calls(self, substring: str = "") -> list[tuple[str, dict]]:
        return [r for r in self.requests if substring in r[0]]

    def per_name(self) -> int:
        """The calls one list name takes: 11 plus 2 an expiry (no extra page)."""
        return 11 + 2 * len(self.expiries)
