"""Earnings Radar board -> result history (#278, G1 writer).

``record`` is a step of an Earnings Fetch (``app/runners.run_earnings_range``), after the board file is written. It
hands ``core/history.record_run`` one ``earnings_cards_history`` row per scored card: the columns the board sorts and
filters on, the whole card (``card``), the UW answers the card was scored on (``inputs``, the card's raw packet) and
the rule slots that were live (``slots``). The mapping lives here, in the module, not in ``core/``. A Refresh never
calls it (06 rule 9); the Earnings board has no Refresh path, it is live-only.

Earnings is a run board: every run keeps its rows (``core/history``), and a degraded run is kept too, marked
'partial' with the reason in ``quality_note`` (design C rule E: header-only is for session boards, whose permanent
snapshot a bad run would shut out). The reasons, all run-level:

- the UW key was rejected, the UW client stopped sending requests (breaker open), or some UW calls failed (the
  board's own "Data incomplete" banner shows the same runs);
- the Rotation quadrant map failed or came back empty (the quadrant is blank on every card);
- a calendar session came back with the 500 rows the client asks for, so the list may be cut (G2-14 measured 157 and
  161 rows on the heaviest days: that is no cap).

A card's own gaps (a short OHLC history leaves the earnings-reaction columns blank) are not a run-level reason: they
show in ``data_completeness`` and in the card.
"""
from __future__ import annotations

import dataclasses
import json
from collections import Counter

from RevenueChaser.core import config, history, ledger

BOARD, TABLE = "module_earnings_radar", "earnings_cards_history"
# Bump when the board's scoring changes in a way the config hash does not see (a rule's formula, not a threshold).
# 2: a missing screener row is N/A, not Thin (E2, #372).
# 3: R6 vetoes on a real FOMC/CPI overlap from the stored calendar and the run reads the stored inputs (E5, #372).
SELECTION_VERSION = "3"
UW_CALENDAR_ROW_LIMIT = 500          # the limit the client asks for (unusual_whales.py:960). G2-14 measured 157 and
                                     # 161 rows on heavy days with page=1 empty: 100 is no cap, 500 would be


def effective_config() -> dict:
    """What the board scores and filters with: the gates, the rule and NRC weights, the rule tunables."""
    from RevenueChaser.module_earnings_radar import store_reads
    from RevenueChaser.module_earnings_radar.stored_inputs import MAX_LAG
    return {
        "MAX_DEEP_CHECKS_PER_DAY": config.MAX_DEEP_CHECKS_PER_DAY, "MIN_OPTION_VOLUME": config.MIN_OPTION_VOLUME,
        "MIN_TOTAL_OI": config.MIN_TOTAL_OI, "MIN_DOLLAR_OPTION_VOLUME": config.MIN_DOLLAR_OPTION_VOLUME,
        "MIN_EXPECTED_MOVE_PCT": config.MIN_EXPECTED_MOVE_PCT, "MAX_EXPECTED_MOVE_PCT": config.MAX_EXPECTED_MOVE_PCT,
        "WATCHLIST_BYPASS": config.WATCHLIST_BYPASS, "FLOW_SWEEP_BLOCK_MIN": config.FLOW_SWEEP_BLOCK_MIN,
        "INST_FLOW_MIN_NET": config.INST_FLOW_MIN_NET, "RULE_WEIGHTS": dict(config.RULE_WEIGHTS),
        "NRC_WEIGHTS": dict(config.NRC_WEIGHTS), "TUNABLES": dict(config.TUNABLES),
        # How stale a stored input may be before the run reads live instead (E5, #372), or, for the market and
        # fundamentals stores' inputs (``store_reads``), before it is not read at all.
        "MAX_LAG": {**MAX_LAG, **store_reads.MAX_LAG}, "MAX_AGE_DAYS": dict(store_reads.MAX_AGE_DAYS),
    }


def model_version() -> str:
    return ledger.model_version(SELECTION_VERSION, effective_config())


def _plain(value):
    """JSON-safe copy: dates and other non-JSON values become strings."""
    return json.loads(json.dumps(value, default=str))


def packet_ohlc(inputs: dict | None) -> list:
    """The OHLC reader slice a stored ``inputs`` packet holds.

    Rows written before E4 keep the 60-bar ``ohlc`` slice; since E4 it is rebuilt from ``ohlc_year``
    (E4, #372 ER-5), so the two are not stored twice.
    """
    if not inputs:
        return []
    if "ohlc" in inputs:
        return inputs.get("ohlc") or []
    from RevenueChaser.module_earnings_radar.pipeline import READER_OHLC_SESSIONS
    return (inputs.get("ohlc_year") or [])[-READER_OHLC_SESSIONS:]


def packet_r2_series(inputs: dict | None) -> list:
    """The R2 print series a stored ``inputs`` packet holds.

    Rows written before E4 keep a top-level ``r2_series``; since E4 it is only stored when the R2
    endpoint answered, so an empty one rebuilds from ``earnings_history`` (E4, #372 ER-5).
    """
    if not inputs:
        return []
    if "r2_series" in inputs:
        return inputs.get("r2_series") or []
    return inputs.get("earnings_history") or []


def _row(card, nrc: dict | None) -> dict:
    nrc = nrc or {}
    fields = dataclasses.asdict(card)
    packet = fields.pop("raw_packet_json", "") or ""
    audit = card.rule_audit or {}                  # what the board's drawer shows (report.py:1815-1816)
    inputs = json.loads(packet) if packet else None
    live = [k for k in ("growth", "acceleration", "quality", "beat") if nrc.get(k) is not None]
    return {
        "ticker": card.ticker, "earnings_date": card.earnings_date, "earnings_time": card.earnings_time,
        "fiscal_quarter": card.fiscal_quarter, "final_action": card.final_action,
        "direction_score": card.direction_score, "volatility_edge_score": card.volatility_edge_score,
        "confidence_score": card.confidence_score, "r2_score": card.r2_score, "r3_score": card.r3_score,
        "r4_score": card.r4_score, "r5_score": card.r5_score, "r7_score": card.r7_score, "r8_score": card.r8_score,
        "bias_label": card.bias_label, "conviction": card.conviction, "macro_veto": card.macro_veto,
        "expected_move_pct": card.expected_move_pct, "data_completeness": card.data_completeness,
        "pre_score": card.pre_score, "filter_status": card.filter_status, "filter_reasons": card.filter_reasons,
        "trade_read": card.trade_read, "attention_score": card.attention_score, "edge_ratio": card.edge_ratio,
        "nrc": nrc.get("nrc"), "nrc_tier": nrc.get("tier"), "nrc_growth": nrc.get("growth"),
        "nrc_acceleration": nrc.get("acceleration"), "nrc_quality": nrc.get("quality"), "nrc_beat": nrc.get("beat"),
        "watchlist": nrc.get("watchlist"),
        "card": _plain(fields), "inputs": inputs,
        "slots": _plain({"inputs": sorted(inputs) if isinstance(inputs, dict) else [],
                         "rules": {k: bool(a.get("available")) for k, a in audit.items()},
                         "contributions": {k: a["contribution"] for k, a in audit.items()
                                           if a.get("contribution") is not None},
                         "nrc_live": live}),
    }


def rows_from_cards(cards, nrc: dict) -> list[dict]:
    """One row per card with a ticker, the first of a repeated ticker (the table's key is run_id + ticker)."""
    out: dict[str, dict] = {}
    for card in cards:
        if card.ticker and card.ticker not in out:
            out[card.ticker] = _row(card, nrc.get(card.ticker))
    return list(out.values())


def screener_problem(screener) -> str | None:
    """The run-level reason when the screener set is missing or stale (G9), or a UW screener answer was
    partial/failed, else None (E2, #372).

    Shared by the history note and the board notice, so the board and the record say the same thing.
    """
    if not screener:
        return None
    status = screener.get("status")
    if status in ("missing", "stale"):          # G9: the stored screener set, not a live answer
        when = f"newest session {screener.get('session')}" if screener.get("session") else "no session stored"
        return (f"the stored screener set (uw/option_screener) is {status} ({when}): every card reads liquidity "
                "N/A and no name passes the gates")
    if status not in ("partial", "failed"):
        return None
    err = screener.get("error") or {}
    failed = err.get("failed_chunks", "?")
    chunks = err.get("chunks", "?")
    return (f"the UW screener answered {status} ({failed} of {chunks} chunks failed): "
            "cards without a screener row read liquidity N/A")


def _input_problems(uw_health, rotation_failed: bool, events, screener=None) -> list[str]:
    """Why this run's inputs are not whole (empty when they are)."""
    out = history.uw_problems(uw_health)
    screener_note = screener_problem(screener)
    if screener_note:
        out.append(screener_note)
    if rotation_failed:
        out.append("the rotation quadrant map failed or came back empty, so no card has a quadrant")
    per_session = Counter((str(e.get("earnings_date")), e.get("earnings_time"))
                          for e in events or [] if e.get("source") == "UW")
    for (day, when), n in sorted(per_session.items()):
        if n >= UW_CALENDAR_ROW_LIMIT:
            out.append(f"the UW calendar returned {n} rows for {day} {when}, the limit the client asks for; "
                       "names past it may be missing")
    return out


def record(cards, nrc: dict, *, run_id: str, run_at: float, uw_health: dict | None = None,
           filter_stats: dict | None = None, span=None, events=None, n_events: int | None = None,
           rotation_failed: bool = False, reason: str | None = None, screener: dict | None = None,
           input_sources: dict | None = None):
    """Record a finished Earnings Fetch: a header for every Fetch, with its rows; a degraded run is partial.

    ``reason`` says why a run has no cards (no events, all filtered); it is the header's note. ``screener``
    is the pipeline's batch outcome, so a partial/failed screener makes the run partial (E2, #372). Returns
    rows written.

    ``input_sources`` (E5, #372) says what the run read from the store and what it asked the provider for; it is
    kept in the header's ``context["inputs"]``, not in a column of its own.
    """
    rows = rows_from_cards(cards, nrc)
    problems = _input_problems(uw_health, rotation_failed, events, screener)
    note = "; ".join([reason or "no cards", *problems]) if not rows else "; ".join(problems) or None
    context = {"span": [d.isoformat() for d in span] if span else None,
               "events": n_events if n_events is not None else (len(events) if events is not None else None),
               "cards": len(cards)}
    if input_sources is not None:
        context["inputs"] = _plain(input_sources)
    return history.record_run(
        BOARD, run_id=run_id, run_at=run_at, model_version=model_version(), rows={TABLE: rows},
        params=effective_config(),
        context=context,
        funnel=filter_stats, partial=note, provider_health=uw_health)
