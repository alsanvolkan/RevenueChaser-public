"""R2 — EPS track record vs the street, from the stock earnings print series.

Despite the legacy "Guidance" name this scores consensus-EPS surprises, not
management guidance language. Phase 1 (design doc) makes the inputs robust and
recency-aware without changing the −100..+100 ladder:

- surprise uses ``(actual − estimate) / max(|estimate|, 0.10)`` so a near-zero
  estimate cannot blow the % up (winsor ±50% stays as a second guard);
- the central read is a **recency-weighted** mean (weights 8..1, newest first)
  PLUS the median — one outlier no longer inflates the headline;
- a beat requires ``surprise > +1%`` (a 0.00 / penny print is not a beat);
- sign needs the recency-mean AND the median on the same side AND a beat rate
  ≥ 5/8 — otherwise the vote is mixed;
- the trend term adds a **consecutive-beats** gate so one +11% print alone
  cannot be labelled "accelerating".

Unavailable below 3 usable quarters (never 0).
"""
from RevenueChaser.module_earnings_radar.models import RuleResult
from RevenueChaser.core.config import (
    R2_MAX_WINDOW as MAX_WINDOW,
    R2_RECENT_QUARTERS as RECENT_QUARTERS,
    R2_SURPRISE_CLIP as SURPRISE_CLIP,
    R2_EST_FLOOR as EST_FLOOR,
    R2_BEAT_MIN as BEAT_MIN,
    R2_BEAT_RATE_MIN as BEAT_RATE_MIN,
    R2_TREND_DELTA as TREND_DELTA,
    R2_CONSECUTIVE_MIN as CONSECUTIVE_MIN,
)


def first_present(row: dict, *keys: str):
    """First value among ``keys`` that is not None (a numeric 0 / 0.0 is a real value)."""
    for k in keys:
        v = row.get(k)
        if v is not None:
            return v
    return None


def _usable_rows(history) -> list[dict]:
    """Rows with a valid actual EPS + non-zero estimate, kept in order.

    Accepts both the calendar-history shape (``actual_eps`` +
    ``street_mean_est``) and the stock-earnings print shape (``reported_eps`` +
    ``estimated_eps``). When ``report_type`` is present only quarterly rows
    count.
    """
    rows = []
    for h in history or []:
        rt = str(h.get("report_type") or "").lower()
        if rt and rt != "quarterly":
            continue
        reported = first_present(h, "actual_eps", "eps_reported", "reported_eps", "eps_actual")
        estimated = first_present(h, "street_mean_est", "eps_estimate", "estimated_eps")
        if reported is None or estimated is None:
            continue
        try:
            actual = float(reported)
            est = float(estimated)
        except (TypeError, ValueError):
            continue
        if abs(est) <= 0.001:
            continue
        # Sort key: prefer fiscal_date_ending (print series) then report_date.
        date = str(h.get("fiscal_date_ending") or h.get("report_date") or "")[:10]
        rows.append({"date": date, "actual": actual, "est": est})
    # Chronological order: sort by the dates that exist and drop undated rows. An
    # undated row used to switch the sort off, so a newest-first series was read
    # backwards. A series with no dates at all keeps its given (oldest-first) order.
    if any(r["date"] for r in rows):
        rows = sorted((r for r in rows if r["date"]), key=lambda r: r["date"])
    return rows


def _coverage(n_quarters: int) -> float:
    if n_quarters >= 8:
        return 0.80
    if n_quarters >= 5:
        return 0.60
    return 0.40


def _recency_weights(n: int) -> list[float]:
    """Weights 1..n so the newest print (last row) weighs the most."""
    return [float(i + 1) for i in range(n)]


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def score(ticker: str, earnings_history: list[dict] = None) -> RuleResult:
    rows = _usable_rows(earnings_history)
    window = rows[-MAX_WINDOW:]
    if len(window) < 3:
        return RuleResult(score=None, available=False, coverage=0, reasons=["insufficient EPS data"])

    n = len(window)
    weights = _recency_weights(n)
    surprises = []
    beats = 0
    for r in window:
        s = (r["actual"] - r["est"]) / max(abs(r["est"]), EST_FLOOR)
        s = max(-SURPRISE_CLIP, min(SURPRISE_CLIP, s))
        surprises.append(s)
        if s > BEAT_MIN:
            beats += 1

    wsum = sum(weights)
    recency_avg = sum(w * s for w, s in zip(weights, surprises)) / wsum
    median_surp = _median(surprises)
    beat_rate = beats / n
    reasons = []
    raw = {
        "avg_bias": round(recency_avg, 4),
        "median_surprise": round(median_surp, 4),
        "beat_rate": round(beat_rate, 2),
        "quarters": n,
        "window": MAX_WINDOW,
    }

    # ── Trend context (evaluated before the sign gate so a mixed read can note
    #    a clear decline instead of silently returning neutral) ──
    recent = surprises[-RECENT_QUARTERS:]
    trailing = surprises[:-RECENT_QUARTERS] if n > RECENT_QUARTERS else []
    trend_ok = len(recent) >= 2 and len(trailing) >= 2
    improving = None
    if trend_ok:
        recent_bias = sum(recent) / len(recent)
        trailing_bias = sum(trailing) / len(trailing)
        improving = recent_bias - trailing_bias >= TREND_DELTA
        declining = trailing_bias - recent_bias >= TREND_DELTA

        # Consecutive beats scanning back from the newest print.
        consec = 0
        for s in reversed(surprises):
            if s > BEAT_MIN:
                consec += 1
            else:
                break

        if improving and consec >= CONSECUTIVE_MIN:
            raw["trend"] = "improving"
            raw["consecutive_beats"] = consec
        elif declining:
            raw["trend"] = "declining"
            raw["consecutive_beats"] = consec

    # ── Sign gate: recency-mean AND median agree AND beat rate is high enough ──
    bull = recency_avg > 0 and median_surp > 0 and beat_rate >= BEAT_RATE_MIN
    bear = recency_avg < 0 and median_surp < 0 and beat_rate <= 1 - BEAT_RATE_MIN
    if not bull and not bear:
        if trend_ok and raw.get("trend") == "declining":
            return RuleResult(
                score=0.0, available=True, coverage=_coverage(n),
                reasons=[f"mixed guidance quality — beat trend declining "
                         f"(recent {recent_bias:.1%} vs earlier {trailing_bias:.1%})"],
                raw_metrics=raw,
            )
        return RuleResult(
            score=0.0, available=True, coverage=_coverage(n),
            reasons=["mixed guidance quality"], raw_metrics=raw,
        )

    score_val = 0.0
    if bull:
        if recency_avg > 0.05:
            score_val += 30
            reasons.append(f"strong beat history ({recency_avg:.1%} recency-wtd)")
        elif recency_avg > 0.02:
            score_val += 15
        if beat_rate >= 0.75:
            score_val += 15
            reasons.append(f"high beat rate ({beat_rate:.0%})")
    else:
        if recency_avg < -0.05:
            score_val -= 30
            reasons.append(f"consistent miss ({recency_avg:.1%})")
        if beat_rate < 0.40:
            score_val -= 15

    # ── Trend points only inside a decided sign ──
    if trend_ok and bull and raw.get("trend") == "improving":
        score_val += 10
        reasons.append(
            f"beat trend improving (recent {recent_bias:.1%} vs earlier {trailing_bias:.1%}, "
            f"{raw['consecutive_beats']} consecutive)"
        )
    elif trend_ok and bear and raw.get("trend") == "declining":
        score_val -= 15
        reasons.append(
            f"beat trend declining (recent {recent_bias:.1%} vs earlier {trailing_bias:.1%})"
        )

    from RevenueChaser.core.config import SCORE_RANGE

    score_val = max(SCORE_RANGE[0], min(SCORE_RANGE[1], score_val))

    return RuleResult(
        score=round(score_val, 1), available=True, coverage=_coverage(n),
        reasons=reasons if reasons else ["neutral guidance quality"],
        raw_metrics=raw,
    )
