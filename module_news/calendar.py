"""The incremental calendar build: ``market.economic_event`` -> ``news.news_events`` and ``news.news_highlight``.

Plan 2026-11-09 §5 and the incremental rule (DECISIONS S16, migration plan §10):

- Only the rows fetched after the last build (``news_meta`` key ``calendar_mark``) are read.
- Each event key ``(event_ts, event, type)`` is upserted: its first sighting is kept, its last sighting, forecast and
  previous value move forward.
- An event the newest fetch no longer lists, inside the time span that fetch covers, is marked ``retired_at``; it is
  never deleted. A fetch with no rows (a failed or empty sweep) retires nothing.
- The highlight of an Amsterdam day is its most important live event; on a tie a release beats a Fed speech, then
  the earliest wins.

No network: the builder's hourly ``uw/economic_calendar`` serves the rows (avo, 11 Nov 2026: "Raw tamamen
kalkmalı"; the raw ``economic_calendar`` table and its D37 sweep are retired).
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from RevenueChaser.core import econ_importance, rawstore
from RevenueChaser.rcdata import read as market_read

AMS = ZoneInfo("Europe/Amsterdam")
MARK_KEY = "calendar_mark"
EVENTS, HIGHLIGHT, META = "news_events", "news_highlight", "news_meta"
_EVENT_COLS = ("event_ts", "event", "type", "importance", "forecast", "prev", "reported_period", "first_seen",
               "last_seen", "retired_at", "run_id", "run_at")


def local_day(ts: float) -> date:
    return datetime.fromtimestamp(float(ts), timezone.utc).astimezone(AMS).date()


def _mark() -> float:
    rows = rawstore.query(f"SELECT value FROM {META} WHERE key = ?", [MARK_KEY], db="news")
    try:
        return float(rows[0]["value"]) if rows else 0.0
    except (TypeError, ValueError):
        return 0.0


def _new_raw(mark: float) -> list[dict]:
    return market_read.economic_events(fetched_after=mark)


def _stored(keys: list[tuple]) -> dict[tuple, dict]:
    if not keys:
        return {}
    lo, hi = min(k[0] for k in keys), max(k[0] for k in keys)
    rows = rawstore.query(f"SELECT * FROM {EVENTS} WHERE event_ts BETWEEN ? AND ?", [lo, hi], db="news")
    return {(r["event_ts"], r["event"], r["type"]): r for r in rows}


def build(now: float | None = None, run_id: str | None = None) -> dict:
    """One incremental pass. Returns counts: ``read``, ``upserted``, ``retired``, ``highlights``."""
    now = time.time() if now is None else now
    run_id = run_id or f"news-{int(now)}"
    mark = _mark()
    raw = _new_raw(mark)
    if not raw:
        hl = _highlights({local_day(now)}, now, run_id)
        return {"read": 0, "upserted": 0, "retired": 0, "highlights": hl}

    newest_fetch = max(float(r["fetched_at"]) for r in raw)
    newest = [r for r in raw if float(r["fetched_at"]) == newest_fetch]
    seen: dict[tuple, dict] = {}
    for r in raw:                                   # oldest first, so the latest sighting of a key wins
        key = (float(r["event_ts"]), r["event"], r["type"])
        seen[key] = r
    stored = _stored(list(seen))
    rows = []
    for key, r in seen.items():
        old = stored.get(key) or {}
        rows.append({
            "event_ts": key[0], "event": key[1], "type": key[2],
            "importance": econ_importance.importance(key[1], key[2]),
            "forecast": r.get("forecast"), "prev": r.get("prev"), "reported_period": r.get("reported_period"),
            "first_seen": old.get("first_seen") or float(r["fetched_at"]),
            "last_seen": float(r["fetched_at"]), "retired_at": None, "run_id": run_id, "run_at": now,
        })

    # Retire: inside the newest fetch's span, every key that fetch no longer lists — a live stored one, or one seen only
    # in an older fetch of this batch. It keeps its row with ``retired_at`` set; nothing is deleted.
    newest_keys = {(float(r["event_ts"]), r["event"], r["type"]) for r in newest}
    lo, hi = min(k[0] for k in newest_keys), max(k[0] for k in newest_keys)
    retired = 0
    for row in rows:
        if lo <= row["event_ts"] <= hi and (row["event_ts"], row["event"], row["type"]) not in newest_keys:
            row["retired_at"] = now
            retired += 1
    live = rawstore.query(f"SELECT * FROM {EVENTS} WHERE event_ts BETWEEN ? AND ? AND retired_at IS NULL",
                          [lo, hi], db="news")
    for r in live:
        key = (r["event_ts"], r["event"], r["type"])
        if key not in seen:
            rows.append({**{c: r.get(c) for c in _EVENT_COLS}, "retired_at": now, "run_id": run_id, "run_at": now})
            retired += 1

    rawstore.write_rows(EVENTS, rows)
    rawstore.write_rows(META, [{"key": MARK_KEY, "value": repr(newest_fetch), "run_at": now}])
    days = {local_day(k[0]) for k in seen} | {local_day(now)}
    hl = _highlights(days, now, run_id)
    return {"read": len(raw), "upserted": len(rows) - retired, "retired": retired, "highlights": hl}


def _highlights(days: set[date], now: float, run_id: str) -> int:
    """Store the highlight of each Amsterdam day in ``days`` with live events: most important, a release before a Fed
    speech, then earliest. Returns the count."""
    if not days:
        return 0
    lo = min(days)
    start = datetime(lo.year, lo.month, lo.day, tzinfo=AMS).timestamp()
    live = rawstore.query(f"SELECT * FROM {EVENTS} WHERE event_ts >= ? AND retired_at IS NULL ORDER BY event_ts",
                          [start], db="news")
    by_day: dict[date, list[dict]] = {}
    for r in live:
        d = local_day(r["event_ts"])
        if d in days:
            by_day.setdefault(d, []).append(r)
    out = []
    for d, events in by_day.items():
        top = min(events, key=lambda e: (-(e.get("importance") or 1), str(e.get("type")).lower() == "fed-speaker",
                                         e["event_ts"]))
        out.append({"session_date": d.isoformat(), "event_ts": top["event_ts"], "event": top["event"],
                    "type": top["type"], "importance": top.get("importance"), "forecast": top.get("forecast"),
                    "prev": top.get("prev"), "text": explain(top), "text_source": "template",
                    "run_id": run_id, "run_at": now})
    rawstore.write_rows(HIGHLIGHT, out)
    return len(out)


# ── the highlight's text (plan decision B: a template, no model call) ────────────────────────────────────────────
_MEANS = (
    (("core", "cpi"), "Gıda ve enerji hariç tüketici fiyatlarındaki değişimi ölçer; Fed'in en yakından izlediği enflasyon verilerinden biri."),
    (("cpi",), "Tüketici fiyatlarındaki değişimi ölçer; faiz beklentilerini en çok oynatan veri."),
    (("pce",), "Fed'in hedeflediği enflasyon ölçüsü: kişisel tüketim harcamalarının fiyat endeksi."),
    (("ppi",), "Üretici fiyatlarındaki değişimi ölçer; tüketici enflasyonunun öncü göstergesi sayılır."),
    (("nonfarm",), "Tarım dışı istihdamdaki aylık değişim; iş piyasasının en önemli göstergesi."),
    (("employment", "report"), "Aylık istihdam raporu: yeni işler, işsizlik oranı ve ücretler."),
    (("employment", "situation"), "Aylık istihdam raporu: yeni işler, işsizlik oranı ve ücretler."),
    (("jobless", "claims"), "Haftalık ilk işsizlik başvuruları; iş piyasasındaki ani değişimleri erken gösterir."),
    (("jolts",), "Açık iş ilanları; iş gücü talebinin gücünü gösterir."),
    (("gdp",), "Ekonominin çeyreklik büyüme hızı."),
    (("retail", "sales"), "Perakende satışlar; tüketici harcamasının aylık nabzı."),
    (("ism",), "Satın alma yöneticileri anketi; 50'nin üstü genişleme demek."),
    (("inflation", "expectations"), "Hanehalkının ileriye dönük enflasyon beklentisi; Fed beklentilerin çapadan kaçıp kaçmadığına bakar."),
    (("consumer", "sentiment"), "Tüketici güveni anketi; harcama eğilimine dair erken sinyal."),
    (("housing", "starts"), "Yeni konut başlangıçları; faize duyarlı konut piyasasının göstergesi."),
    (("fomc",), "Fed'in faiz kararı; bütün piyasaların yönünü belirleyebilir."),
    (("rate", "decision"), "Fed'in faiz kararı; bütün piyasaların yönünü belirleyebilir."),
)


_NUM = re.compile(r"^(-?)(\d+(?:\.\d+)?)(%|[KMBT])?$")


def tr_value(value) -> str | None:
    """A served value in Turkish writing: ``3.9%`` -> ``%3,9``, ``1.25M`` -> ``1,25M``; anything else as served."""
    if value is None:
        return None
    text = str(value).strip()
    m = _NUM.match(text)
    if not m:
        return text
    sign, num, unit = m.group(1), m.group(2).replace(".", ","), m.group(3) or ""
    return f"{sign}%{num}" if unit == "%" else f"{sign}{num}{unit}"


def explain(event: dict) -> str:
    """Two short sentences: what the release measures, then its previous value and forecast when known."""
    words = econ_importance._words(event.get("event"))
    kind = str(event.get("type") or "").lower()
    means = next((text for keys, text in _MEANS if all(f" {k} " in words for k in keys)), None)
    if means is None and kind == "fed-speaker":
        means = "Bir Fed yetkilisinin konuşması; faiz yoluna dair ipuçları için izlenir."
    if means is None:
        means = "ABD ekonomik takviminde bugünün öne çıkan verisi."
    prev, fc = tr_value(event.get("prev")), tr_value(event.get("forecast"))
    tail = []
    if prev:
        tail.append(f"Önceki değer {prev}")
    if fc:
        tail.append(f"beklenti {fc}")
    elif prev:
        tail.append("beklenti yayımlanmadı")
    return means + (" " + ", ".join(tail) + "." if tail else "")
