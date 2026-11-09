"""The incremental calendar build (News board plan §5, DECISIONS S16): upsert by key, retire never delete."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_news import calendar as cal

T0 = datetime(2026, 11, 10, 14, 30, tzinfo=cal.AMS).timestamp()     # Tue 10 Nov 14:30 Amsterdam
HOUR = 3600.0


def _raw(rows, fetched_at, run_id="sweep"):
    """One sweep's rows. The raw key carries the sweep's own session date (D37), one per fetch here."""
    session = (date(2026, 11, 1) + timedelta(days=int(fetched_at // 100))).isoformat()
    out = []
    for ts, event, kind, *rest in rows:
        fc = rest[0] if rest else None
        out.append({"session_date": session, "event_ts": ts, "event": event, "type": kind,
                    "forecast": fc, "prev": "0.3%", "reported_period": "October", "raw_row": "{}", "source": "uw",
                    "fetched_at": fetched_at, "run_id": run_id, "run_at": fetched_at})
    rawstore.write_rows("economic_calendar", out)


def _events():
    return {(r["event"]): r for r in rawstore.query("SELECT * FROM news_events ORDER BY event_ts", db="news")}


def test_first_build_upserts_every_event_into_the_board_file():
    _raw([(T0, "CPI, M/M%", "report", "0.2%"), (T0 + HOUR, "Wholesale inventories", "report")], fetched_at=100.0)
    out = cal.build(now=200.0)
    ev = _events()
    assert out["read"] == 2 and out["retired"] == 0
    assert ev["CPI, M/M%"]["importance"] == 3 and ev["CPI, M/M%"]["forecast"] == "0.2%"
    assert ev["CPI, M/M%"]["first_seen"] == 100.0 and ev["CPI, M/M%"]["retired_at"] is None


def test_a_second_build_reads_only_new_rows_and_keeps_first_seen():
    _raw([(T0, "CPI, M/M%", "report", "0.2%")], fetched_at=100.0)
    cal.build(now=200.0)
    assert cal.build(now=250.0)["read"] == 0
    _raw([(T0, "CPI, M/M%", "report", "0.3%")], fetched_at=300.0)
    out = cal.build(now=400.0)
    row = _events()["CPI, M/M%"]
    assert out["read"] == 1
    assert row["first_seen"] == 100.0 and row["last_seen"] == 300.0 and row["forecast"] == "0.3%"


def test_an_event_the_newest_fetch_drops_inside_its_span_is_retired_not_deleted():
    _raw([(T0, "CPI, M/M%", "report"), (T0 + HOUR, "Housing starts", "report"),
          (T0 + 2 * HOUR, "GDP", "report")], fetched_at=100.0)
    cal.build(now=200.0)
    _raw([(T0, "CPI, M/M%", "report"), (T0 + 2 * HOUR, "GDP", "report")], fetched_at=300.0)
    out = cal.build(now=400.0)
    ev = _events()
    assert out["retired"] == 1
    assert ev["Housing starts"]["retired_at"] == 400.0
    assert ev["CPI, M/M%"]["retired_at"] is None and len(ev) == 3


def test_an_event_outside_the_newest_fetch_span_stays_live():
    _raw([(T0 - 24 * HOUR, "Retail sales", "report"), (T0, "CPI, M/M%", "report")], fetched_at=100.0)
    cal.build(now=200.0)
    _raw([(T0, "CPI, M/M%", "report")], fetched_at=300.0)
    cal.build(now=400.0)
    assert _events()["Retail sales"]["retired_at"] is None


def test_a_key_seen_only_in_an_older_fetch_of_the_same_batch_is_retired_not_lost():
    """Two fetches land before one build: what the newer one dropped inside its span is retired, and still stored."""
    _raw([(T0, "CPI, M/M%", "report"), (T0 + HOUR, "Housing starts", "report"),
          (T0 + 2 * HOUR, "GDP", "report")], fetched_at=100.0)
    _raw([(T0, "CPI, M/M%", "report"), (T0 + 2 * HOUR, "GDP", "report")], fetched_at=300.0)
    out = cal.build(now=400.0)
    ev = _events()
    assert "Housing starts" in ev and ev["Housing starts"]["retired_at"] == 400.0
    assert out["retired"] == 1 and out["upserted"] == 2


def test_a_retired_event_that_comes_back_is_live_again():
    _raw([(T0, "CPI, M/M%", "report"), (T0 + HOUR, "Housing starts", "report"),
          (T0 + 2 * HOUR, "GDP", "report")], fetched_at=100.0)
    _raw([(T0, "CPI, M/M%", "report"), (T0 + 2 * HOUR, "GDP", "report")], fetched_at=300.0)
    cal.build(now=400.0)
    _raw([(T0, "CPI, M/M%", "report"), (T0 + HOUR, "Housing starts", "report")], fetched_at=500.0)
    cal.build(now=600.0)
    row = _events()["Housing starts"]
    assert row["retired_at"] is None and row["first_seen"] == 100.0


def test_no_new_rows_retires_nothing():
    _raw([(T0, "CPI, M/M%", "report")], fetched_at=100.0)
    cal.build(now=200.0)
    out = cal.build(now=300.0)
    assert out["read"] == 0 and out["retired"] == 0
    assert _events()["CPI, M/M%"]["retired_at"] is None


def test_the_highlight_is_the_most_important_live_event_earliest_on_a_tie():
    _raw([(T0 - HOUR, "Wholesale inventories", "report"), (T0, "CPI, M/M%", "report", "0.2%"),
          (T0 + HOUR, "Retail sales", "report")], fetched_at=100.0)
    cal.build(now=T0 - 5 * HOUR)
    hl = rawstore.query("SELECT * FROM news_highlight", db="news")
    assert len(hl) == 1 and hl[0]["session_date"] == date(2026, 11, 10)
    assert hl[0]["event"] == "CPI, M/M%" and hl[0]["text_source"] == "template"
    assert "beklenti %0,2" in hl[0]["text"]


def test_a_retired_event_is_never_the_highlight():
    _raw([(T0, "CPI, M/M%", "report"), (T0 + HOUR, "Housing starts", "report")], fetched_at=100.0)
    cal.build(now=T0 - 5 * HOUR)
    _raw([(T0 + HOUR, "Housing starts", "report")], fetched_at=300.0)
    _raw([(T0 - HOUR, "Wholesale inventories", "report"), (T0 + HOUR, "Housing starts", "report")], fetched_at=400.0)
    cal.build(now=T0 - 4 * HOUR)
    hl = rawstore.query("SELECT event FROM news_highlight WHERE session_date = '2026-11-10'", db="news")
    assert hl == [{"event": "Housing starts"}]


def test_the_build_writes_nothing_to_raw_duckdb():
    _raw([(T0, "CPI, M/M%", "report")], fetched_at=100.0)
    cal.build(now=200.0)
    assert rawstore.query("SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'news_%'") == []


@pytest.mark.parametrize("event, kind, prev, fc, want", [
    ("Core CPI, M/M%", "report", "0.3%", "0.2%", "Gıda ve enerji hariç"),
    ("Fed Governor Waller speaks", "fed-speaker", None, None, "Fed yetkilisinin"),
    ("Baker Hughes rig count", "report", None, None, "öne çıkan verisi."),
    ("PPI, M/M%", "report", "0.1%", None, "beklenti yayımlanmadı"),
])
def test_explain_template(event, kind, prev, fc, want):
    assert want in cal.explain({"event": event, "type": kind, "prev": prev, "forecast": fc})


def test_on_a_tie_a_release_beats_a_fed_speech():
    _raw([(T0 - HOUR, "Fed President Musalem speaks", "fed-speaker"),
          (T0, "NY Fed consumer inflation expectations", "report")], fetched_at=100.0)
    cal.build(now=T0 - 5 * HOUR)
    assert rawstore.query("SELECT event FROM news_highlight", db="news") == [
        {"event": "NY Fed consumer inflation expectations"}]
