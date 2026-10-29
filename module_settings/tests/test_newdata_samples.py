"""Step 7: five example rows per card, read from the real serving file (A16, K13).

What the sample is for: a reader who cannot open DuckDB still wants to see what a day of this dataset looks like,
what the columns are called and what type each one is, and to be warned about the cases that look like bugs and are
not. What it must not be: a second read of forty tables on every page render, or a write. So the rows come from the
one read-only connection ``_serving_facts`` already opens per kind, cached by the hour, and a kind whose file has
never been built shows its schema instead of rows.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from RevenueChaser.module_settings import newdata, report
from RevenueChaser.module_settings.tests.test_newdata_findings import _card as _card_html
from RevenueChaser.module_settings.tests.test_newdata_findings import _page, _section

NOW = datetime(2026, 10, 29, 18, 30, tzinfo=timezone.utc)


# ── the hourly cache ──

def test_the_sample_is_cached_by_the_hour_and_read_again_in_the_next_one():
    newdata.clear_sample_cache()
    reads = []

    def read(table):
        reads.append(table)
        return {"columns": [{"name": "ticker", "type": "VARCHAR"}], "rows": [["AAPL"]]}

    first = newdata._cached_sample("price_bar_daily", NOW, read)
    again = newdata._cached_sample("price_bar_daily", NOW + timedelta(minutes=29), read)
    assert first == again and reads == ["price_bar_daily"], "one read inside the hour"
    newdata._cached_sample("price_bar_daily", NOW + timedelta(hours=1), read)
    assert reads == ["price_bar_daily", "price_bar_daily"], "read again in the next hour"


def test_each_table_is_cached_on_its_own():
    newdata.clear_sample_cache()
    reads = []
    read = lambda t: (reads.append(t), {"columns": [], "rows": []})[1]
    newdata._cached_sample("a", NOW, read)
    newdata._cached_sample("b", NOW, read)
    newdata._cached_sample("a", NOW, read)
    assert reads == ["a", "b"]


def test_the_cache_key_is_the_hour_the_page_was_read_not_the_minute():
    assert newdata._sample_hour(NOW) == newdata._sample_hour(NOW.replace(minute=59))
    assert newdata._sample_hour(NOW) != newdata._sample_hour(NOW + timedelta(hours=1))


# ── the sample's shape ──

def test_a_sample_names_each_column_with_its_type_and_the_dataset_that_writes_it():
    sample = {"columns": [{"name": "session_date", "type": "DATE"}, {"name": "close", "type": "DOUBLE"}],
              "rows": [["2026-10-27", 1.0]]}
    out = newdata._sample_block("price_bar_daily", sample)
    assert out["dataset"] == "alpaca/bars"
    assert [c["name"] for c in out["columns"]] == ["session_date", "close"]
    assert out["rows"] == [["2026-10-27", 1.0]]


def test_a_table_whose_file_was_never_built_shows_its_schema_and_says_so():
    out = newdata._sample_block("price_bar_daily", None)
    assert out["built"] is False
    assert ("session_date", "DATE") in [(c["name"], c["type"]) for c in out["columns"]]
    assert out["rows"] == []


def test_a_table_no_schema_declares_is_not_invented():
    out = newdata._sample_block("no_such_table", None)
    assert out["built"] is False and out["columns"] == [] and out["rows"] == []


def test_the_hard_cases_of_a_table_are_named_beside_its_rows():
    out = newdata._sample_block("security_symbol", {"columns": [], "rows": []})
    assert any("HEI" in n for n in out["notes"]), out["notes"]
    assert newdata._sample_block("price_bar_daily", {"columns": [], "rows": []})["notes"]


def test_every_named_hard_case_belongs_to_a_table_the_screen_knows():
    for table in newdata.SAMPLE_NOTES:
        assert table in newdata.DATE_COLUMN, f"{table} is not a table the screen dates"


# ── the sample is read-only, and at most five rows ──

def test_at_most_five_rows_are_asked_for():
    assert newdata.SAMPLE_ROWS == 5
    asked = []
    con = type("C", (), {"execute": lambda self, sql: asked.append(sql) or _Cursor()})()
    newdata._read_sample(con, "price_bar_daily", "session_date")
    assert len(asked) == 1
    assert 'ORDER BY "session_date" DESC' in asked[0], "the newest rows, not the first five on disk"
    assert asked[0].endswith(f"LIMIT {newdata.SAMPLE_ROWS}"), asked[0]
    assert "SELECT" in asked[0] and not any(w in asked[0].upper()
                                            for w in ("INSERT", "UPDATE", "DELETE", "CREATE", "ATTACH"))


class _Cursor:
    description = (("session_date", "DATE"), ("close", "DOUBLE"))

    def fetchall(self):
        return [("2026-10-27", 1.0)]


def test_a_date_in_a_sample_row_is_written_as_text():
    from datetime import date
    con = type("C", (), {"execute": lambda self, sql: _DateCursor()})()
    out = newdata._read_sample(con, "price_bar_daily", "session_date")
    assert out["rows"] == [["2026-10-27", 1.0]], "a date cannot go through script_json as a date"
    assert isinstance(out["rows"][0][0], str)
    del date


class _DateCursor:
    description = (("session_date", "DATE"), ("close", "DOUBLE"))

    def fetchall(self):
        from datetime import date
        return [(date(2026, 10, 27), 1.0)]


# ── the card ──

def _with_sample(sample: dict | None):
    from RevenueChaser.core.tests.board_fixtures import newdata_fixture
    nd = newdata_fixture()
    nd["datasets"] = [{**nd["datasets"][0], "samples": [sample] if sample else []}]
    return nd


def test_the_card_shows_the_rows_under_their_column_names_with_the_type():
    nd = _with_sample({"table": "market_series", "dataset": "market_series", "built": True,
                       "columns": [{"name": "series_id", "type": "VARCHAR"},
                                   {"name": "value", "type": "DOUBLE"}],
                       "rows": [["DGS10", 4.0], ["VIX", 17.5]], "notes": ()})
    card = _card_html(_section(_page(nd)), "market_series")
    assert "Example rows" in card
    assert "series_id" in card and "VARCHAR" in card and "DGS10" in card and "17.5" in card


def test_the_card_of_a_file_that_was_never_built_says_so_and_still_shows_the_columns():
    nd = _with_sample({"table": "price_bar_daily", "dataset": "alpaca/bars", "built": False,
                       "columns": [{"name": "session_date", "type": "DATE"}], "rows": [], "notes": ()})
    card = _card_html(_section(_page(nd)), "market_series")
    assert "not built yet" in card and "session_date" in card and "DATE" in card


def test_a_hard_case_is_shown_with_its_warning_sign():
    nd = _with_sample({"table": "security_symbol", "dataset": "spine/security", "built": True,
                       "columns": [], "rows": [], "notes": ("HEI and HEI-A are two share classes of one issuer",)})
    card = _card_html(_section(_page(nd)), "market_series")
    assert "HEI and HEI-A" in card and "⚠" in card


def test_a_card_with_no_sample_shows_no_empty_block():
    nd = _with_sample(None)
    assert "Example rows" not in _card_html(_section(_page(nd)), "market_series")


def test_a_hostile_value_in_a_sample_row_is_escaped():
    nd = _with_sample({"table": "market_series", "dataset": "market_series", "built": True,
                       "columns": [{"name": "<img src=x>", "type": "<b>"}],
                       "rows": [["<script>alert(1)</script>"]], "notes": ("<i>note</i>",)})
    card = _card_html(_section(_page(nd)), "market_series")
    assert "<script>alert(1)</script>" not in card and "&lt;script&gt;" in card
    assert "<img src=x>" not in card and "<i>note</i>" not in card


# ── against a real serving file ──

@pytest.fixture
def prices_store(tmp_path, monkeypatch):
    """A live prices.duckdb with two bars, built the way ``test_newdata_prices`` builds it."""
    import duckdb

    from RevenueChaser.core import calendar, config
    from RevenueChaser.rcdata import ops, serving
    from RevenueChaser.rcdata.build.prices import serving as prices_serving
    monkeypatch.setattr(config, "MARKET_DIR", tmp_path / "market")
    monkeypatch.setattr(config, "MARKET_RAW_DIR", tmp_path / "market_raw")
    monkeypatch.setattr(config, "MARKET_RAW_COPY_DIR", None)
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(ops, "read_log", ops.ReadLog(process="test"))
    monkeypatch.setattr(calendar, "_now", lambda: NOW)
    newdata.clear_sample_cache()
    path = serving.live_path("prices")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        cols = ", ".join(f'"{c}" {t}' for c, t in prices_serving.SCHEMAS["price_bar_daily"])
        con.execute(f"CREATE TABLE price_bar_daily ({cols})")
        con.execute("INSERT INTO price_bar_daily (security_id, session_date, close, source) VALUES "
                    "('001690:01', DATE '2026-10-27', 202.0, 'alpaca/bars'), "
                    "('001690:01', DATE '2026-10-28', 203.0, 'alpaca/bars')")
    finally:
        con.close()
    return tmp_path


def _collected(dataset: str) -> dict:
    return next(c for c in newdata.collect(NOW)["datasets"] if c["dataset"] == dataset)


def test_the_bars_card_carries_the_newest_rows_of_the_real_file(prices_store):
    block = next(b for b in _collected("alpaca/bars")["samples"] if b["table"] == "price_bar_daily")
    assert block["built"] is True
    names = [c["name"] for c in block["columns"]]
    assert {"security_id", "session_date", "close"} <= set(names)
    assert len(block["rows"]) == 2
    newest = block["rows"][0][names.index("session_date")]
    assert newest == "2026-10-28", "the newest session first, and written as text"


def test_a_card_whose_file_is_not_built_carries_its_schema(prices_store):
    """The ownership file is not built here, so the card shows the columns ``rcdata/scaffold.py`` declares."""
    block = next(b for b in _collected("finra/short_interest")["samples"]
                 if b["table"] == "finra_short_interest")
    assert block["built"] is False and block["rows"] == []
    assert "settlement_date" in [c["name"] for c in block["columns"]]


def test_the_whole_page_renders_the_real_rows(prices_store):
    html = report.render_settings_dashboard({"newdata": newdata.collect(NOW), "rows": []})
    assert "Example rows · price_bar_daily" in html and "2026-10-28" in html
