"""Hostile-fixture registry for the board HTML guard (ADR-X1 §5, #144).

One entry per dashboard board: a renderer that builds the board from a
synthetic payload in which every text field holds ``H(field)``. ``H`` fires
``window.__x(tag)`` if it lands raw in text, in a quoted attribute, in a JS string
inside an inline handler, or after a ``</script>`` breakout; it also carries a
``<!--<script>`` that would swallow the board's own ``</script>``.

Used by ``core/tests/test_board_html_guard.py`` (static) and
``module_testing/e2e/test_board_html_browser.py`` (headless Chrome). A new board
in ``TABS`` must add an entry here, or the completeness test fails. Not a test
module (no ``test_`` prefix).
"""
from __future__ import annotations

import copy
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Callable

# Keys whose values a renderer parses (dates, enums, numbers-as-strings, routes)
# rather than shows: a hostile value there only makes the fixture crash.
_SKIP_KEY = re.compile(
    r"((^|_)date($|_|s$)|_at$|^at$|asof|as_of|time|filed|expir|period|iso|generated|through|"
    r"window_end|fetched|^cut$|^cuts$|benchmark|horizon|status|^id$|cluster_id|"
    r"color|level|^kind$|^unit$|route|quadrant|^key$|^relation$|^form$|^quality$|"
    r"^tool_key$|^action$|bias|^side$|^flag$|^signal$|^type$|^cp$|option_symbol|"
    r"^tier$|^label_cls$|^mode$|^role$|^r$|currency|^source$)", re.I)

RAW_MARK = "<img src=x onerror=__x("


_COMMENT = {"on": True}


@contextmanager
def no_comment_opener():
    """Drop the ``<!--<script>`` tail for a browser run: raw in markup it would
    comment out the rest of the page and hide every later sink."""
    _COMMENT["on"] = False
    try:
        yield
    finally:
        _COMMENT["on"] = True


def H(tag: str) -> str:
    """A payload that runs ``__x`` from every sink that fails to escape it."""
    tag = re.sub(r"[^A-Za-z0-9_]", "_", tag)[:40]
    return (f"{tag}');__x('{tag}J');//\"'><img src=x onerror=__x('{tag}T')>"
            f"</script><img src=x onerror=__x('{tag}S')>"
            + ("<!--<script>" if _COMMENT["on"] else ""))


def hostile(obj, key: str = "root"):
    if isinstance(obj, dict):
        return {k: (v if _SKIP_KEY.search(str(k)) else hostile(v, str(k))) for k, v in obj.items()}
    if isinstance(obj, list):
        return [hostile(v, key) for v in obj]
    if isinstance(obj, str):
        return H(key)
    return obj


def health(tag: str) -> dict:
    """A failing provider health (Common K3) with hostile strings: every board shows its banner (#383 C4b)."""
    return {"provider": H(tag + "_provider"), "calls": 5, "failed": 2, "auth_error": None,
            "failures": [{"endpoint": H(tag + "_endpoint"), "error": H(tag + "_error")}]}


@dataclass(frozen=True)
class Board:
    render: Callable  # (tmp_path, monkeypatch) -> html
    # Why the board is not yet clean, with the slice or issue that fixes it. The
    # guard expects it to fail (strict xfail), so a fix forces removing the note.
    # ``open_item`` is the static check; ``executes`` is the browser check.
    open_item: str | None = None
    executes: str | None = None


def _comovement(tmp_path, monkeypatch):
    from RevenueChaser.module_comovement import report
    m = lambda t: {"ticker": t, "name": "A", "sector": "Financial Services", "market_cap": 1e9,
                   "industry": "Banks - Diversified", "gics": ["Financial Services", "Banks - Diversified"],
                   "index": ["S&P 500"]}
    p = {"as_of": "2026-09-17T00:00:00+00:00", "eligible": 2, "universe_total": 2,
         "cuts": [20], "fetch": {}, "horizon": 5, "benchmark": "spy",
         "benchmark_applied": True, "prices_through": "2026-09-17", "benchmark_status": "applied", "oos_note": "note",
         "clusters": {"20": [{"id": "1", "label": "Cluster 1", "n": 2, "mean_corr": 0.72,
                              "last_residual_pct": -3.4, "members": [m("AAA"), m("BBB")]}]}}
    hp = hostile(p)
    hp["provider_health"] = health("comovement")
    # The theme labels reach the tooltip through the catalog and each member's ids;
    # set after ``hostile`` so the ids stay valid keys.
    hp["theme_catalog"] = {"t1": {"label": H("theme_catalog_label"), "level": "fine"}}
    for mem in hp["clusters"]["20"][0]["members"]:
        mem["theme_ids"] = ["t1"]
    return report.render_comovement_dashboard(hp)


def _relatedness(tmp_path, monkeypatch):
    from RevenueChaser.module_relatedness import config, report
    co = lambda t, fine: {"ticker": t, "name": t + " Inc", "sector": "Technology", "industry": "Semiconductors",
                          "market_cap": 1e12, "weights": {"gpu": 1.0},
                          "profile": {"business_summary": "s", "products": ["p"]},
                          "theme_coarse": "Semiconductors", "theme_medium": "Chips",
                          "theme_fine": fine}
    p = {"as_of": "2026-09-19T00:00:00+00:00", "model": "deepseek-flash", "n": 2,
         "companies": [co("NVDA", "AI Accelerators"), co("AMD", "CPUs")],
         "peers": {"NVDA": [{"peer": "AMD", "name": "AMD Inc", "score": 1.0, "shared": ["gpu"],
                             "percentile": 100, "move": {1: 0.67, 5: 0.76}, "beyond_spy": 0.72,
                             "cluster": {"coarse": True, "medium": True, "fine": False}}],
                   "AMD": [{"peer": "NVDA", "name": "NVDA Inc", "score": 1.0, "shared": ["gpu"],
                            "percentile": 100}]},
         "scores": [1.0], "thinking_compare": {}, "fields": list(config.PROFILE_FIELDS)}
    hp = hostile(p)
    hp["provider_health"] = health("relatedness")
    # Keep the peers keyed by the (hostile) tickers so the focal resolves.
    t0, t1 = hp["companies"][0]["ticker"], hp["companies"][1]["ticker"]
    hp["peers"] = {t0: hp["peers"]["NVDA"], t1: hp["peers"]["AMD"]}
    for row in hp["peers"][t0]:
        row["peer"] = t1
    return report.render_relatedness_dashboard(hp)


def _value_chain(tmp_path, monkeypatch):
    from RevenueChaser.module_value_chain import report
    p = {"as_of": "2026-09-17T00:00:00+00:00",
         "filings": [{"ticker": "NVDA", "name": "NVIDIA Corporation", "form": "10-K",
                      "filed": "2026-02-01", "quality": "ok", "market_cap": 1e12, "sector": "IT",
                      "industry": "Software - Application"},
                     {"ticker": "TSM", "name": "TSMC", "form": "20-F", "filed": "2026-02-01",
                      "quality": "ok", "market_cap": 1e12}],
         "edges": [{"src": "NVDA", "dst_name": "TSMC", "dst_ticker": "TSM", "relation": "supplier",
                    "snippet": "we purchase wafers from TSMC", "filed": "2026-02-01"}],
         "universe_total": 1, "pending": 0}
    hp = hostile(p)
    hp["provider_health"] = health("value_chain")
    hp["edges"][0]["src"] = hp["filings"][0]["ticker"]
    hp["edges"][0]["dst_ticker"] = hp["filings"][1]["ticker"]
    return report.render_value_chain_dashboard(hp)


def _fundamentals(tmp_path, monkeypatch):
    from RevenueChaser.module_fundamentals.pipeline import build_payload
    from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard
    from RevenueChaser.module_fundamentals.tests.test_fundamental import _m
    row = {"ticker": "AAA", "name": "Test", "price": 50.0, "market_cap": 1e9,
           "sector": "Technology", "industry": "Software", "themes": ["Software"],
           "earnings_date": None, "period": "Q2/2026 vs Q2/2025",
           "metrics": _m(rev_growth_ttm=0.30, fcf_margin=0.20, ro40=50.0, gross_margin=0.35,
                         roic=0.20, net_debt_ebitda=2.0, dist_52w_pct=0.90, rs_6m=0.05,
                         above_200d=True, pe=15.0, pb=2.0)}
    p = build_payload([row], top_n=10)
    p["candidates"][0]["index"] = ["S&P 500"]
    hp = hostile(p)
    hp["provider_health"] = health("fundamentals")
    return render_fundamental_dashboard(hp)


def _swing(tmp_path, monkeypatch):
    from RevenueChaser.module_swing_potential.pipeline import build_payload
    from RevenueChaser.module_swing_potential.report import render_swing_dashboard
    gate = lambda n: {"name": n, "passed": True, "score": "3/3", "known": 1, "total": 1,
                      "rules": [{"label": "rule", "passed": True, "threshold": "> 1",
                                 "unit": "ratio", "actual": 1.2}]}
    p = build_payload([], run_stats={})
    p["candidates"] = [{"ticker": "AAA", "name": "Name", "price": 10.0, "change_pct": 1.0,
                        "change_1w": 0.01, "change_1m": 0.01, "market_cap": 1e9,
                        "sector": "Tech", "industry": "Soft", "themes": ["Theme"],
                        "index": ["S&P 500"], "earnings_date": None, "multiplier": 3,
                        "direction": "LONG", "quality": 80, "rs_rating": 90,
                        "ma_alignment": "stacked", "entry": 10.0, "target": 12.0, "stop": 9.0,
                        "rr": 2.0, "atr": 0.5,
                        "gates": {"minervini": gate("Minervini"), "weinstein": gate("Weinstein"),
                                  "qullamaggie": gate("Q")},
                        "flow": {"status": "not_scanned"}}]
    hp = hostile(p)
    hp["is_fixture"] = False
    hp["provider_health"] = health("swing")
    return render_swing_dashboard(hp)


def _asymmetric_growth(tmp_path, monkeypatch):
    from RevenueChaser.module_asymmetric_growth.report import render_asymmetric_growth_hunter_html
    thr = {"rev_growth": 0.2, "gross_margin": 0.4, "net_debt_assets": 0.1, "runway": 2.0,
           "dist_52w": 0.1, "vol_ratio": 1.4, "rd_intensity": 0.15, "dilution": 0.2,
           "accelerating": True, "fcf": True, "above_sma50": True}
    card = {"ticker": "MU", "name": "Micron", "sector": "Technology", "industry": "Semiconductors",
            "index": ["S&P 500"], "themes": ["Memory"], "total_points": 8, "price": 100.0, "market_cap": 1e11,
            "rev_growth": 0.5, "gross_margin": 0.5, "ps_ratio": 3.0, "fcf": 1e9,
            "runway_years": 5, "rd_intensity": 0.2, "dilution_yoy": 0.01,
            "dilution_warning": True, "tech_breakthrough": True,
            "signals": [{"key": "growth", "name": "Growth", "passed": 1, "known": 1, "total": 1,
                         "subs": [{"label": "Revenue growth", "value": 0.5, "unit": "pct",
                                   "threshold": 0.2, "op": "gte", "param": "rev_growth"}]}],
            "inst_holders": 10, "inst_new": 1, "inst_exited": 0, "inst_buy_flow": 1.0,
            "inst_sell_flow": 0.0, "inst_shares_delta_pct": 0.1, "earnings_date": None}
    r = {"asof": "2026-08-23T00:00:00", "universe_total": 1,
         "universe_refreshed_by": "Asymmetric Growth",
         "universe_generated_at": "2026-08-24T10:00:00+00:00",
         "summary": {"scanned": 1, "pts_9": 0, "pts_8_7": 1, "pts_6_5": 0, "pts_lt5": 0,
                     "disruptive_themed": 0, "tech_breakthrough": 1},
         "thresholds": thr, "cards": [card]}
    from RevenueChaser.core import funnel
    r["funnel_steps"] = [
        funnel.step("universe", "Universe", 3, "start"),
        funnel.step("no_annual", "No annual financials", 2, "drop", by_design=False, tickers=["AAA", "BBB"]),
        funnel.step("scored", "Scored", 1, "result")]
    hr = hostile(r)
    hr["thresholds"] = thr
    hr["provider_health"] = health("asymmetric_growth")
    # hostile() turns every string hostile; the funnel keeps its real kinds (the renderer needs them), its
    # labels, keys and ticker names stay hostile.
    for st, kind in zip(hr["funnel_steps"], ("start", "drop", "result")):
        st["kind"] = kind
    for sig in hr["cards"][0]["signals"]:
        for sub in sig["subs"]:
            sub.update({"value": 0.5, "threshold": 0.2, "op": "gte", "param": "rev_growth",
                        "unit": "pct"})
    # The cards come from the payload route (#380 S7, K10): the page loads them with rcPayload, so the hostile
    # cards ride in a fetch stub that answers it (static half: script JSON; browser half: the rendered cards).
    from RevenueChaser.core.tests.board_js import with_payload
    from RevenueChaser.module_asymmetric_growth.report import board_payload
    return with_payload(render_asymmetric_growth_hunter_html(hr), board_payload(hr))


def _oipd(tmp_path, monkeypatch):
    from RevenueChaser.module_oipd.report import render_oipd_dashboard
    p = {"ticker": "AAPL", "asof": "2026-09-01", "spot": 100.0, "n_contracts": 10,
         "horizon_days": 30, "risk_free": 0.04, "error": None, "warnings": ["warn one"],
         "expiries": []}
    hp = hostile(p)
    hp["ticker"] = H("ticker_query_param")
    hp["provider_health"] = health("oipd")
    return render_oipd_dashboard(hp)


def _settings(tmp_path, monkeypatch):
    from RevenueChaser.module_settings import report
    p = {"generated_at": "2026-09-25T20:00:00+00:00",
         "tickers": [{"ticker": "AAPL", "name": "Apple Inc."}],
         "rows": [{"key": "universe", "label": "Equity universe", "tables": ["equity_universe"],
                   "rows": 7403, "last_date": "—", "holes": 1710, "hole_label": "null market cap",
                   "fetch": {"route": "/run/universe-stats", "label": "Fetch caps"},
                   "note": "provenance"}],
         # An embedded row (not a poll): the browser guard stubs fetch, so only these
         # reach the panel's renderer. Every text field, the log tail included, is hostile.
         "automations": [{"name": "RC-Store-Backup", "label": "L", "schedule": "s",
                          "command": "c", "settings": "t", "log_path": "p",
                          "description": "d", "tables_written": ["t1"], "state": "enabled",
                          "result_kind": "red", "result_text": "r", "last_run": "x",
                          "next_run": "y", "missed_runs": "2", "last_success_age": "a",
                          "log_tail": ["l1", "l2"], "offpeak_gated": True,
                          "default_install": True,
                          "offmachine": {"configured": True, "kind": "red", "text": "t"}}]}
    p["newdata"] = newdata_fixture()
    hp = hostile(p)
    hp["provider_health"] = health("settings")
    # A live state, so the row's buttons (and their hostile data-name) render too.
    hp["automations"][0]["state"] = "enabled"
    return report.render_settings_dashboard(hp)


def newdata_fixture() -> dict:
    """PR 2: the "New data system" section of Settings, every field filled (dataset names, notes, provider names,
    issue keys, queue rows, writers), so ``hostile`` reaches each sink of the section and of its JS data."""
    card = {"dataset": "market_series", "label": "Market series", "status": "late", "status_reason": "one missed",
            "cadence": "daily", "checked_on": "2026-10-27", "source_as_of": "2026-10-27", "next_check_due": "x",
            "rows": 10, "added": 1, "removed": 0, "calls": 3, "coverage": 0.5, "coverage_target": 0.95,
            "last_note": "note", "last_result": "changed", "table_rows": 10, "newest": "2026-10-27",
            "checks": [{"label": "Last run", "ok": True, "detail": "changed"},
                       {"label": "Coverage", "ok": False, "detail": "50%"}],
            "issues": [{"kind": "fetch_failed", "key": "k", "detail": "HTTP 500", "found_on": "2026-10-27"}],
            "issues_open": 1,
            "backfill": {"done": 3, "expected": 9, "last_partition": "2026-10-27", "missing": ["2026-10-20"],
                         "resumable": True, "calls_per_partition": 1, "from_earliest": "2025-01-16"},
            "wrds": {"text": "no WRDS run yet"},
            "schedule": {"paused": True, "paused_by": "settings", "paused_at": "2026-10-27T10:00:00+00:00"}}
    req = {"id": 1, "dataset": "market_series", "status": "queued", "requested_by": "settings", "note": "n",
           "params": {"mode": "backfill"}}
    return {
        "verified": {"builder_last_run": {"run_id": "r", "started_at": "s", "status": "ok"},
                     "swaps": {"market": "2026-10-27T10:00:00+00:00"}, "ops_schema": 1},
        "datasets": [card, {**card, "dataset": "uw/spot_expiry_strike", "status": "fresh",
                            "schedule": {"paused": False, "paused_by": None, "paused_at": None}}],
        "queue": {"active": [req], "finished": [{**req, "status": "done"}]},
        "spend": {"day": "2026-10-27", "week_from": "2026-10-26",
                  "providers": [{"provider": "uw", "cap": 150000, "today": 12, "week": 30, "failures_today": 1,
                                 "http_429_today": 0}],
                  "uw_remaining": {"daily_req_count": 1300, "daily_cap": 150000, "daily_remaining_computed": 148700,
                                   "per_minute_remaining": 118, "token_req_limit": 100000000, "sampled_at": "t"}},
        "backups": {"configured": True, "warn_days": 35,
                    "kinds": [{"kind": "ops", "newest": "n", "age_days": 1, "warn": False}],
                    "raw_copy": {"text": "not set", "warn": True, "age_days": None},
                    "portable": {"text": "not recorded", "warn": True, "age_days": None}},
        "retention": {"warn": True, "text": "kept without a limit", "source": "src"},
        "retirement": [{"table": "macro_series", "replaced_by": "market_series", "reads_14d": 2,
                        "writers": ["RC-Earnings-Inputs"]}],
        "watchlists": {"option": 1, "stock": 2},
    }


def _uw_interaction(tmp_path, monkeypatch):
    from RevenueChaser.module_uw_interaction.report import render_board
    p = {"tool_key": "flow_alerts", "label": "Flow Alerts", "family": "Options flow",
         "path": "/option-trades/flow-alerts", "help": "rule based",
         "params_used": {"limit": "10", "ticker": "AAPL"}, "columns": ["ticker", "premium"],
         "rows": [{"ticker": "AAPL", "premium": "100000"}], "row_count": 1,
         "error": "err", "http_status": None, "generated_at": "2026-09-10T12:00:00+00:00"}
    hp = hostile(p)
    hp["columns"] = ["ticker", "premium"]
    hp["provider_health"] = health("uw_interaction")
    return render_board(hp)


def _sec_13f(tmp_path, monkeypatch):
    from RevenueChaser.core.tests.board_js import with_payload
    from RevenueChaser.module_sec_13f.report import board_payload, render_sec13f_dashboard
    q = lambda s: {"shares": s, "value": s * 10, "holders": 2, "new_holders": 1,
                   "exited_holders": 0, "top10_pct": 90.0, "hhi": 4000.0, "buy_flow": 1.0,
                   "sell_flow": 0.0}
    t = H("ticker")
    totals = {t: {"2025-03-31": q(100.0), "2025-06-30": q(150.0)}}
    meta = {t: {"name": H("name"), "sector": "Financials"}}
    enrich = {t: {"price": {"2025-03-31": 10.0, "2025-06-30": 12.0},
                  "shares_outstanding": 2000.0, "short_pct": 5.2, "insider_pct": 1.4}}
    # The rows come from the payload route (#429, K10 #383 C6a): the page loads them with rcPayload, so the
    # hostile ticker/name ride in a fetch stub that answers it (static half: script JSON; browser: the rows built).
    return with_payload(render_sec13f_dashboard(totals, meta, enrich, provider_health=health("sec_13f")),
                        board_payload(totals, meta, enrich))


def _leaps(tmp_path, monkeypatch):
    import RevenueChaser.module_leaps.report as ld
    monkeypatch.setattr(ld, "OUTPUT_DIR", tmp_path)
    snap = {"ticker": "ABC", "name": "Abc Corp", "label": "Accumulating", "score": 70,
            "frequency": 4, "notional": 400000.0, "net_notional": 100000.0,
            "ask_share": 0.7, "near_term_event": False}
    hs = hostile(snap)
    hs["label"] = H("label")
    out = ld.render_leaps_dashboard([hs], [dict(hs)], provider_health=health("leaps"))
    return out.read_text(encoding="utf-8") if hasattr(out, "read_text") else out


def _financials(tmp_path, monkeypatch):
    from RevenueChaser.module_financials.reporting.html_builder import build_report_html
    fin = {"ticker": "TEST", "sector": "Technology", "shares_outstanding": 1000.0,
           "market_cap": 100_000.0, "current_price": 100.0, "currency": "USD",
           "source": "test", "info": {"longName": "Test Corp"}, "annual": []}
    hf = hostile(fin)
    hf["source"] = H("source")
    res = {"ticker": H("ticker"), "financials": hf, "modules": {}, "warnings": [H("warning")],
           "quick_remarks": {"ticker": H("qr_ticker"), "name": H("qr_name"),
                             "sector": H("qr_sector"), "remarks": [H("remark")],
                             "warnings": [H("qr_warning")]},
           "provider_health": health("financials")}
    return build_report_html(res)


def _spx_gamma(tmp_path, monkeypatch):
    from RevenueChaser.module_spx_gamma import render
    from RevenueChaser.module_spx_gamma.tests.conftest import make_snapshot
    hs = copy.deepcopy(make_snapshot())
    for k in ("ticker", "source", "price_source"):
        if k in hs:
            hs[k] = H("spx_" + k)
    if isinstance(hs.get("header"), dict):
        hs["header"]["ticker"] = H("spx_header_ticker")
    hs["provider_health"] = health("spx")
    return render.render_html(hs)


def _earnings(tmp_path, monkeypatch):
    import RevenueChaser.module_earnings_radar.report as er
    from RevenueChaser.module_earnings_radar.models import TradeCard
    monkeypatch.setattr(er, "OUTPUT_DIR", tmp_path)
    (tmp_path / "earnings").mkdir(parents=True, exist_ok=True)
    t = H("er_ticker")
    card = TradeCard(ticker=t, name=H("er_name"), cluster=H("er_cluster"),
                     earnings_date=date(2026, 8, 11), report_time_label="PM",
                     direction_score=20.0, confidence_score=65.0, bias_label="BULL",
                     expected_move_pct=5.0, eps_estimate="1.25", final_action="OPTIONS_BULL",
                     rule_audit={"rule2_guidance": {"reasons": [H("er_reason")],
                                                    "available": True}})
    out = er.export_html([card], [{"ticker": t, "name": H("cal_name")}],
                         {t: {"nrc": 85, "tier": "Ignore"}}, filename="earnings/x.html",
                         provider_health=health("earnings"))
    return out.read_text(encoding="utf-8") if hasattr(out, "read_text") else out


def _earnings_detail(tmp_path, monkeypatch):
    """The drawer charts read ``detail_json`` (quarter labels and dates come from the
    provider, #175). The hostile-ticker card above cannot open a drawer (its row selector
    is invalid), so a plain ticker carries the hostile quarters, alone on the board."""
    import RevenueChaser.module_earnings_radar.report as er
    from RevenueChaser.module_earnings_radar.models import TradeCard
    monkeypatch.setattr(er, "OUTPUT_DIR", tmp_path)
    (tmp_path / "earnings").mkdir(parents=True, exist_ok=True)
    detail = [{"q": H("er_q"), "date": H("er_date"), "implied": 5.0, "actual": 3.0, "eps_actual": 1.2,
               "eps_est": 1.0, "surprise_dollar": 0.2, "surprise_pct": 20.0,
               "revenue_actual": 1.0e9, "revenue_est": 9.0e8} for _ in range(2)]
    card = TradeCard(ticker="DTL", name="Detail Co", cluster="Tech",
                     earnings_date=date(2026, 8, 11), report_time_label="PM",
                     direction_score=20.0, confidence_score=65.0, bias_label="BULL",
                     expected_move_pct=5.0, eps_estimate="1.25", final_action="OPTIONS_BULL",
                     detail_json=json.dumps(detail))
    out = er.export_html([card], [], {}, filename="earnings/detail.html")
    return out.read_text(encoding="utf-8") if hasattr(out, "read_text") else out


def _rotation(tmp_path, monkeypatch):
    """Hostile ETF identity (#175): a ticker, a configured name and a group name that no
    config entry spells. They reach the quadrant points, the table, the filter buttons
    and the summary chips; the config names are what ``_build_svg`` and the table read."""
    import RevenueChaser.module_sector_rotation.report as rd
    from RevenueChaser.module_sector_rotation.models import ETFRotationSnapshot
    monkeypatch.setattr(rd, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(rd, "coarse_theme_catalog", lambda: {H("theme"): ["XLK"]})
    hostile = H("rot_ticker")
    monkeypatch.setitem(rd.ROTATION_ETFS, hostile, {"name": H("rot_name"), "group": H("rot_group")})
    snap = lambda t, q, group="sector": ETFRotationSnapshot(
        as_of_date=date(2026, 8, 14), ticker=t, group_name=group, close_price=100.0,
        rs_1w=1.0, rs_1m=2.0, rs_3m=3.0, rs_ratio=2.0, rs_momentum=1.0,
        rs_ratio_z=1.0, rs_momentum_z=0.5, rrg_quadrant=q)
    holdings = {"XLK": [{"ticker": H("holding_ticker"), "name": H("holding_name"), "weight": 8.5,
                         "close": 500.0, "rs_1w": 1.0, "rs_1m": 2.0, "rs_3m": 3.0}]}
    out = rd.export_rotation_dashboard(
        [snap("XLK", "LEADING"), snap("XLV", "IMPROVING"),
         snap(hostile, "LAGGING", group=H("rot_group")), snap(hostile, "WEAKENING")],
        filename="guard.html", uw=None, holdings=holdings, provider_health=health("rotation"))
    return out.read_text(encoding="utf-8")


def _rotation_trail(tmp_path, monkeypatch):
    """The trail page (``/view/rotation/trail``) is a second page with its own markup
    and its own picker: the same hostile ETF identity, over two sampled weeks."""
    import RevenueChaser.module_sector_rotation.report as rd
    monkeypatch.setattr(rd, "coarse_theme_catalog", lambda: {H("theme"): ["XLK"]})
    hostile = H("trail_ticker")
    monkeypatch.setitem(rd.ROTATION_ETFS, hostile,
                        {"name": H("trail_name"), "group": H("trail_group")})
    entry = lambda t, rz, mz: {"ticker": t, "rs_ratio_z": rz, "rs_momentum_z": mz,
                               "quadrant": H("trail_quadrant"), "rs_ratio": 1.0,
                               "rs_momentum": 0.5, "rs_1w": 1.0, "rs_1m": 2.0, "rs_3m": 3.0}
    series = {"2026-08-07": [entry(hostile, 0.5, 0.2), entry("XLK", 0.4, 0.1)],
              "2026-08-14": [entry(hostile, 1.0, 0.5), entry("XLK", 0.6, 0.3)]}
    return rd._trail_html(date(2026, 8, 3), date(2026, 8, 14), ["2026-08-07", "2026-08-14"], series)


def _logs(tmp_path, monkeypatch):
    from RevenueChaser.app import logview
    from RevenueChaser.core import config
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    # A request path is attacker-chosen (any page can make the browser GET /<markup>) and werkzeug logs it.
    (tmp_path / "dashboard.err.log").write_text(
        f'127.0.0.1 - - [x] "GET /{H("log_line")} HTTP/1.1" 404 -\n'
        f"Traceback (most recent call last):\n{H('traceback')}\n", encoding="utf-8")
    p = logview.page_payload()
    p["log_dir"] = H("log_dir")
    return logview.render_page(p)


def _runs(tmp_path, monkeypatch):
    from RevenueChaser.app import runview
    from RevenueChaser.core import rawstore
    # A module name is code-chosen, but a run's message, error and params are not: a ticker comes from the
    # box, an error text from a provider or a traceback. All of them are shown by the run log.
    rawstore.write_rows("job_runs", [{
        "run_id": "r1", "job_id": "r1", "module": H("module"), "lane": "short", "trigger": H("trigger"),
        "source": H("source"), "pid": 7, "registry_name": H("registry_name"),
        "params": {"ticker": H("ticker")}, "queued_at": 1.0, "started_at": 2.0, "finished_at": 63.0,
        "status": "error", "error": H("error"), "message": H("message"), "counts": {"rows": 1},
    }])
    return runview.render_page(runview.page_payload())



BOARDS: dict[str, Board] = {
    "module_earnings_radar": Board(_earnings),
    # Not a tab: the same board with an openable drawer.
    "module_earnings_radar_detail": Board(_earnings_detail),
    "module_spx_gamma": Board(_spx_gamma),
    "module_oipd": Board(_oipd),
    "module_financials": Board(_financials),
    "module_uw_interaction": Board(_uw_interaction),
    "module_comovement": Board(_comovement),
    "module_value_chain": Board(_value_chain),
    "module_relatedness": Board(_relatedness),
    "module_sector_rotation": Board(_rotation),
    # Not a tab: the second Rotation page, served at /view/rotation/trail.
    "module_sector_rotation_trail": Board(_rotation_trail),
    "module_asymmetric_growth": Board(_asymmetric_growth),
    "module_swing_potential": Board(_swing),
    "module_fundamentals": Board(_fundamentals),
    "module_sec_13f": Board(_sec_13f),
    "module_leaps": Board(_leaps),
    # Executes nothing, but "<!--<script>" in a name still breaks its script.
    "module_settings": Board(_settings),
    # Not a data board: the dashboard's own log files, whose request lines carry attacker-chosen paths.
    "logs": Board(_logs),
    # Not a data board: the run log over job_runs, whose messages and params are stored text (#26 PL5-b).
    "runs": Board(_runs),
}
