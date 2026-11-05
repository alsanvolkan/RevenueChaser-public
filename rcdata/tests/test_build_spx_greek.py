"""``uw/greek_expiry_strike`` through the builder, with UW mocked at the HTTP boundary."""
from __future__ import annotations

from datetime import date, datetime, timezone
from urllib.parse import parse_qs

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.rcdata import ops, read, serving
from RevenueChaser.rcdata.build import base, uw_greek_expiry_strike as uw

UW_HEADERS = {"x-uw-daily-req-count": "355", "x-uw-req-per-minute-remaining": "1000000",
              "x-uw-req-per-minute-reset": "60000", "x-uw-token-req-limit": "100000000",
              "content-type": "application/json"}

SESSION = date(2026, 10, 26)
# The chain shape of the live 30 Oct 2026 check, shortened: 0 DTE, 1 DTE, then one expiry per band the board
# reads (0-30, 31-90, 91-180, 181-365, 365+), so every DTE_BANDS row has a row to fall in.
CHAIN = [("2026-10-26", 0), ("2026-10-27", 1), ("2026-11-20", 25), ("2027-01-15", 81),
         ("2027-04-16", 172), ("2027-10-15", 354), ("2029-12-21", 1151)]
STRIKES_PER_EXPIRY = 3


def _chain_body(session: date = SESSION) -> dict:
    return {"data": [{"date": session.isoformat(), "expiry": e, "dte": d,
                      "call_gex": "1.0", "put_gex": "-1.0", "call_delta": "1.0", "put_delta": "-1.0",
                      "call_charm": "1.0", "put_charm": "-1.0", "call_vanna": "1.0", "put_vanna": "-1.0"}
                     for e, d in CHAIN]}


def _grid_rows(expiry: str, dte: int, session: date = SESSION, stamp: str | None = None) -> dict:
    rows = []
    for k in range(STRIKES_PER_EXPIRY):
        row = {"date": stamp if stamp is not None else session.isoformat(),
               "expiry": expiry, "strike": f"{7000 + 100 * k}", "dte": dte}
        row.update({f: f"{(k + 1) * 10.5 * (i + 1):.4f}" for i, f in enumerate(uw.NUMERIC_FIELDS)})
        rows.append(row)
    return {"data": rows}


def _second_series(rows: list[dict]) -> list[dict]:
    """The same strikes again, as UW answers them when two option series share the expiry date: every greek
    doubled, and one of them absent, which is what a live 0-DTE row looks like."""
    out = []
    for r in rows:
        twin = dict(r)
        twin.update({f: f"{float(r[f]) * 2:.4f}" for f in uw.NUMERIC_FIELDS})
        twin["put_gex"] = None
        out.append(twin)
    return out


def _route_grid(session: date = SESSION, *, stamp=None, missing: str | None = None, not_a_list: str | None = None,
                twice: str | None = None):
    """One handler for every ``strike-expiry`` call: it answers the expiry the query names."""
    dte_of = dict(CHAIN)

    def handler(request):
        expiry = parse_qs(request.url.query.decode())["expiry"][0]
        if expiry == not_a_list:
            return 200, {"data": {"message": "no grid"}}, UW_HEADERS
        if expiry == missing:
            return 200, {"data": []}, UW_HEADERS
        body = _grid_rows(expiry, dte_of[expiry], session, stamp)
        if expiry == twice:
            body = {"data": body["data"] + _second_series(body["data"])}
        return 200, body, UW_HEADERS

    return handler


@pytest.fixture(autouse=True)
def uw_env(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc))


def _daily(as_of="2026-10-26"):
    return base.run_request({"dataset": uw.NAME, "params": {"as_of": as_of}})


def _wire(http_mock, **kw):
    http_mock.add("/greek-exposure/strike-expiry", _route_grid(**kw), headers=UW_HEADERS)
    http_mock.add("/greek-exposure/expiry", _chain_body(), headers=UW_HEADERS)


def test_a_daily_run_asks_the_chain_once_then_one_call_per_expiry(http_mock):
    _wire(http_mock)
    row = _daily()
    assert row["result"] == "changed"
    assert row["rows"] == len(CHAIN) * STRIKES_PER_EXPIRY
    assert row["calls"] == 1 + len(CHAIN), "the chain call plus one per expiry"
    grid = http_mock.calls("strike-expiry")
    assert len(grid) == len(CHAIN)
    asked = [parse_qs(r.url.query.decode())["expiry"][0] for r in grid]
    assert asked == [e for e, _ in CHAIN], "every expiry of the chain, oldest first"
    for r in grid:
        q = parse_qs(r.url.query.decode())
        assert q["date"] == ["2026-10-26"], "every call names its session"
        assert r.headers["authorization"].startswith("Bearer ")


def test_the_dte_column_keeps_uws_calendar_day_count(http_mock):
    """The board's DTE_BANDS are calendar-day bands, so a session-counted dte would move every band edge."""
    _wire(http_mock)
    _daily()
    with serving.open_read("market") as con:
        by_expiry = dict(con.execute("SELECT expiry, any_value(dte) FROM spx_greek_expiry_strike "
                                     "GROUP BY expiry ORDER BY expiry").fetchall())
        version = con.execute("SELECT kind, version FROM serving_schema_version").fetchone()
    assert by_expiry == {date.fromisoformat(e): d for e, d in CHAIN}
    # 2026-10-27 is the next trading session after 2026-10-26: 1 either way. 2026-11-20 is 25 calendar days
    # out but only 17 sessions, which is what the sibling dataset would have stored.
    assert by_expiry[date(2026, 11, 20)] == 25
    assert version == ("market", 2)


def test_every_greek_lands_typed_with_its_provenance(http_mock):
    _wire(http_mock)
    _daily()
    with serving.open_read("market") as con:
        types = dict(con.execute("SELECT column_name, data_type FROM information_schema.columns "
                                 "WHERE table_name = 'spx_greek_expiry_strike'").fetchall())
        row = con.execute("SELECT call_gex, put_gex, call_vanna, source, observed FROM spx_greek_expiry_strike "
                          "WHERE expiry = '2026-11-20' AND strike = 7000").fetchone()
    for f in uw.NUMERIC_FIELDS:
        assert types[f] == "DOUBLE", f
    assert types["dte"] == "INTEGER" and types["strike"] == "DOUBLE"
    assert row[0] == 10.5 and row[3] == "uw"
    assert row[1] is not None and row[2] is not None


def test_the_reader_serves_the_whole_chain_and_filters_by_band(http_mock):
    _wire(http_mock)
    _daily()
    assert len(read.spx_greek_expiry_strike(SESSION)) == len(CHAIN) * STRIKES_PER_EXPIRY
    band = read.spx_greek_expiry_strike(SESSION, min_dte=181, max_dte=365)
    assert {r["expiry"] for r in band} == {date(2027, 10, 15)}, "the 181-365 band the board's put wall reads"
    assert read.spx_greek_expiry_strike(SESSION, min_dte=0, max_dte=1)


def test_two_series_on_one_expiry_date_are_summed_not_dropped(http_mock):
    """SPX (AM, standard monthly) and SPXW (PM, weekly) share the third Friday, and UW answers one row per series
    with no field to tell them apart (docs/openapi.yaml, "Greek Exposure By Strike And Expiry": only expiry,
    strike and the eight greeks). The strike's exposure is the two together, so they are summed. Measured on the
    live answer of 2026-11-04: 1,570 of 14,551 rows were a second series, all of them on the nearest five third
    Fridays, and each one used to be dropped."""
    _wire(http_mock, twice="2026-11-20")
    row = _daily()
    assert row["result"] == "changed"
    assert row["rows"] == len(CHAIN) * STRIKES_PER_EXPIRY, "one row per strike, not one per series"
    with serving.open_read("market") as con:
        summed = con.execute("SELECT call_gex, put_gex FROM spx_greek_expiry_strike "
                             "WHERE expiry = '2026-11-20' AND strike = 7000").fetchone()
        untouched = con.execute("SELECT call_gex FROM spx_greek_expiry_strike "
                                "WHERE expiry = '2027-01-15' AND strike = 7000").fetchone()
    assert summed[0] == pytest.approx(10.5 + 21.0), "both series' call gamma"
    assert summed[1] == pytest.approx(21.0), "the second series answered none, so the first one's value stands"
    assert untouched[0] == pytest.approx(10.5), "an expiry with one series is unchanged"
    assert "duplicate_key" not in [i["kind"] for i in ops.open_issues(uw.NAME)[1]]


def test_one_expiry_without_a_list_of_rows_fails_the_day(http_mock):
    """A partial chain would silently narrow a DTE band, so nothing is stored and the next run asks again."""
    _wire(http_mock, not_a_list="2027-04-16")
    row = _daily()
    assert row["result"] == "failed"
    assert not uw.partition_path(SESSION).exists()
    assert not uw.raw_path(SESSION).exists()


def test_an_answer_stamped_on_another_day_is_refused(http_mock):
    _wire(http_mock, stamp="2026-10-23")
    row = _daily()
    assert row["result"] == "failed"
    assert not uw.partition_path(SESSION).exists()


def test_a_chain_past_the_ceiling_is_refused_before_the_grid_is_asked(http_mock, monkeypatch):
    monkeypatch.setattr(uw, "MAX_EXPIRIES", len(CHAIN) - 1)
    _wire(http_mock)
    row = _daily()
    assert row["result"] == "failed"
    assert http_mock.calls("strike-expiry") == [], "no grid call once the chain is refused"
