"""Every need in the catalog can actually be asked for (the independent review's C1 and C2).

The catalog said which function answers a need, but not which arguments a caller must give it, and nothing checked
that the two matched. Thirteen of the forty-two needs named a reader with a required argument the catalog never
declared: ``api.get`` raised ``TypeError`` and ``GET /api/data/<need>`` answered 400 for a third of the catalog,
with green tests, because every test called one of the handful of needs that happen to take no argument.

These tests need no store. They bind the declared parameters to the reader's own signature, which raises exactly
where the real call would, and they are over **every** need rather than a chosen one, so a need added without a way
to ask for it fails here rather than in a page.
"""
from __future__ import annotations

import inspect

import pytest

from RevenueChaser.app import dashboard
from RevenueChaser.rcdata import api


def _required(need) -> list[str]:
    """The reader's parameters with no default: what a caller must supply or the call is a ``TypeError``."""
    sig = inspect.signature(need.reader)
    return [p.name for p in sig.parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]


def _suppliable(need) -> set[str]:
    """The parameter names a caller can reach: the need's own ask parameter, and the route's scalars."""
    return ({need.ask_param} if need.ask_param else set()) | dashboard._DATA_SCALARS


def _declared_params(need) -> dict:
    """A call built from what the catalog declares, with a stand-in value per required scalar."""
    params: dict = {}
    if need.ask_param:
        params[need.ask_param] = ["X"] if need.ask_list else "X"
    for name in _required(need):
        params.setdefault(name, "2026-10-29" if name.endswith(("date", "_on", "as_of")) else "X")
    return params


@pytest.mark.parametrize("name", sorted(api.catalog.NEEDS))
def test_every_required_argument_of_a_needs_reader_can_be_asked_for(name):
    need = api.catalog.NEEDS[name]
    missing = [p for p in _required(need) if p not in _suppliable(need)]
    assert not missing, (f"{name}: {need.reader_name} requires {missing}, which neither the need's ask_param "
                         f"({need.ask_param!r}) nor the route's scalars can supply, so the need answers 400")


@pytest.mark.parametrize("name", sorted(api.catalog.NEEDS))
def test_the_declared_parameters_fit_the_readers_signature(name):
    need = api.catalog.NEEDS[name]
    try:
        inspect.signature(need.reader).bind(**_declared_params(need))
    except TypeError as exc:
        pytest.fail(f"{name}: the catalog's parameters do not fit {need.reader_name}: {exc}")


def test_the_four_market_series_needs_each_name_their_own_series():
    """C2: four needs read one table. Without a key of their own every one of them summarises the whole table, so a
    series that stopped updating reads fresh as long as any other series in that table is current."""
    series = {n: api.catalog.NEEDS[n] for n in ("macro.dgs10", "market.vix", "market.cot", "market.series")}
    for name, need in series.items():
        assert need.table == "market_series" and need.ask_param == "series_ids"
    assert series["macro.dgs10"].filter_values == ("DGS10",)
    assert series["market.vix"].filter_values and all(v.startswith("V") for v in series["market.vix"].filter_values)
    assert series["market.cot"].filter_prefix and not series["market.cot"].filter_values
    # The general need is the one without a filter: a caller names the series it wants.
    assert not series["market.series"].filter_values and not series["market.series"].filter_prefix


def test_a_filtered_need_asks_its_own_series_without_being_told(monkeypatch):
    seen = {}

    def fake(series_ids=(), start=None, end=None, prefix=None):
        seen.update(series_ids=tuple(series_ids), prefix=prefix)
        return []
    monkeypatch.setattr(api.catalog.read, "market_series", fake)
    api.get("macro.dgs10")
    assert seen == {"series_ids": ("DGS10",), "prefix": None}
    seen.clear()
    api.get("market.cot")
    assert seen["prefix"] == api.catalog.NEEDS["market.cot"].filter_prefix


def test_a_filtered_needs_summary_counts_its_own_series_only(monkeypatch):
    seen = {}

    def fake_summary(table, day_column=None, key_column=None, sample=0, key_values=(), key_prefix=None):
        seen.update(table=table, key_values=tuple(key_values), key_prefix=key_prefix)
        return {"row_count": 0, "key_count": 0, "newest_day": None, "status": "never checked", "dataset": None,
                "sample_rows": []}
    monkeypatch.setattr(api.serve.read, "table_summary", fake_summary)
    api.summary("macro.dgs10")
    assert seen["key_values"] == ("DGS10",), "the summary counts every series in the table"
    api.summary("market.cot")
    assert seen["key_prefix"] == api.catalog.NEEDS["market.cot"].filter_prefix


# ── I1: one reference day for every freshness answer ──

def test_the_lag_is_counted_against_one_shared_reference_day(monkeypatch):
    """The envelope counted the lag against the newest complete session while ``core.earnings_dates`` counted it
    against today, so on a trading day whose session is not over the two differed by one: a card could read
    ``fresh`` while the earnings-week filter had already dropped that layer. Both read one function now."""
    from datetime import date as _date

    from RevenueChaser.core import earnings_dates

    assert api.reference_session is api.serve.reference_session
    seen = []
    monkeypatch.setattr(api.serve.calendar, "latest_complete_session", lambda: (seen.append(1), _date(2026, 10, 28))[1])
    # Thursday 29 Oct: the newest complete session is Wednesday 28th, and the layer's session is Tuesday 27th.
    ans = api.Answer(need="earnings.next", rows=[], state="fresh",
                     freshness={"as_of_session": _date(2026, 10, 27), "lag_sessions": 1, "max_lag": 1,
                                "unit": "sessions"},
                     coverage={"asked": 0, "served": 0, "missing": [], "from_fallback": 0},
                     provenance=[], status="changed")
    need = api.catalog.NEEDS["earnings.next"]
    monkeypatch.setattr(earnings_dates, "_d26_dates", lambda today: {})
    dates = earnings_dates._layer_dates(ans, _date(2026, 10, 29), need)
    assert seen, "the board counted the lag against its own day, not the shared reference"
    assert dates == {}, "no rows to serve, but the call must not have raised"


# ── m1 and m4 ──

def test_a_summary_of_a_table_the_file_does_not_have_is_no_rows_not_an_error():
    """m1: ``_rows`` grew a guard for a table a file built before it existed does not have; the summary of the same
    table still raised, which on the Settings page is a 500 where "no rows yet" is the truth."""
    from RevenueChaser.rcdata import read, scaffold
    scaffold.create_empty("prices")
    import duckdb
    con = duckdb.connect(str(read.serving.live_path("prices")))
    con.execute("DROP TABLE wrds_price_diff")
    con.close()
    s = read.table_summary("wrds_price_diff", day_column="as_of", key_column="comparison")
    assert s["row_count"] == 0 and s["newest_day"] is None


def test_a_row_without_a_status_column_does_not_put_the_word_none_on_a_card(monkeypatch):
    """m4: ``rows[0].get("status")`` with no fallback wrote the literal string ``"None"`` wherever a reader's rows
    carry no status column."""
    monkeypatch.setattr(api.catalog.read, "universe", lambda **kw: [{"ticker": "MSFT"}])
    ans = api.get("identity.universe")
    assert ans.status != "None" and ans.status


def test_a_filtered_summary_counts_only_its_own_series_over_real_rows():
    """The same C2 guard over the store rather than over a stub: the mutation that drops the filter must not pass."""
    import duckdb

    from RevenueChaser.rcdata import read, scaffold, serving
    scaffold.create_empty("market")
    con = duckdb.connect(str(serving.live_path("market")))
    con.execute("INSERT INTO market_series (series_id, obs_date, source, value) VALUES "
                "('DGS10', DATE '2026-10-01', 'fred', 4.1), ('VIX', DATE '2026-10-28', 'yfinance', 17.0)")
    con.close()
    whole = read.table_summary("market_series", day_column="obs_date", key_column="series_id")
    assert whole["row_count"] == 2
    mine = read.table_summary("market_series", day_column="obs_date", key_column="series_id",
                              key_values=("DGS10",))
    assert mine["row_count"] == 1 and mine["newest_day"].isoformat() == "2026-10-01", \
        "the summary counted every series, so a dead one reads as fresh on another's newest day"


def test_on_a_day_whose_session_is_not_closed_the_card_and_the_filter_agree(monkeypatch):
    """Karar 66 A: the one reference is the **last completed session**, for the card and for the earnings-date
    filter alike. Thursday morning, with Wednesday's session the newest complete one: a layer session of Wednesday
    is lag 0 and is read. Counted against *today* it was lag 1, so the filter dropped a layer the card called
    fresh - the same data, two answers."""
    from datetime import date as _date

    from RevenueChaser.core import earnings_dates

    tuesday, wednesday, thursday = _date(2026, 10, 27), _date(2026, 10, 28), _date(2026, 10, 29)
    monkeypatch.setattr(api.serve.calendar, "latest_complete_session", lambda: wednesday)
    need = api.catalog.NEEDS["earnings.next"]
    ans = api.Answer(need="earnings.next",
                     rows=[{"session_date": tuesday, "ticker": "KO", "report_date": _date(2026, 11, 3)}],
                     state="fresh",
                     freshness={"as_of_session": tuesday, "lag_sessions": 1, "max_lag": need.max_lag,
                                "unit": need.max_lag_unit},
                     coverage={"asked": 0, "served": 1, "missing": [], "from_fallback": 0},
                     provenance=[], status="changed")
    assert earnings_dates._layer_dates(ans, thursday, need) == {"KO": _date(2026, 11, 3)}
