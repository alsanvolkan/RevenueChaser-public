"""The Database tab's ticker lookup (avo, 11 Nov 2026): the served files only, and both ends of each range."""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from RevenueChaser.module_settings import ticker_lookup
from RevenueChaser.rcdata import serving


def _file(kind: str, statements: list[str]) -> None:
    path = serving.live_path(kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        for sql in statements:
            con.execute(sql)
    finally:
        con.close()


def _spine(rows=(("AAPL", "Apple Inc.", "0000320193"),)) -> None:
    stmts = ["CREATE TABLE security (ticker VARCHAR, name VARCHAR, cik VARCHAR, figi VARCHAR, sector VARCHAR, "
             "industry VARCHAR, tier VARCHAR)"]
    for ticker, name, cik in rows:
        stmts.append(f"INSERT INTO security VALUES ('{ticker}', '{name}', '{cik}', 'BBG1', 'Technology', "
                     "'Hardware', 'A')")
    _file("spine", stmts)


def _prices(bars: list[tuple[str, date]]) -> None:
    stmts = ["CREATE TABLE security_symbol (security_id VARCHAR, symbol VARCHAR, valid_to DATE)",
             "CREATE TABLE price_bar_daily (security_id VARCHAR, session_date DATE, close DOUBLE)",
             "INSERT INTO security_symbol VALUES ('sec:1', 'AAPL', NULL)"]
    for _ticker, day in bars:
        stmts.append(f"INSERT INTO price_bar_daily VALUES ('sec:1', DATE '{day.isoformat()}', 1.0)")
    _file("prices", stmts)


def test_unknown_name_is_none():
    _spine()
    assert ticker_lookup.lookup("ZZZZ") is None


def test_blank_is_none():
    assert ticker_lookup.lookup("  ") is None and ticker_lookup.lookup(None) is None


def test_identity_comes_from_the_spine():
    _spine()
    out = ticker_lookup.lookup("aapl")
    assert out["ticker"] == "AAPL" and out["name"] == "Apple Inc." and out["cik"] == "0000320193"
    assert out["sector"] == "Technology"


def test_every_served_file_has_a_group_even_when_absent():
    _spine()
    out = ticker_lookup.lookup("AAPL")
    assert [f["kind"] for f in out["files"]] == list(serving.KINDS)
    prices = next(f for f in out["files"] if f["kind"] == "prices")
    assert prices["tables"] == [] and prices["error"] == "no prices.duckdb yet"


def test_a_table_reports_both_ends_of_its_range():
    """The whole point of the rebuild: 'since when', not only the newest day."""
    _spine()
    _prices([("AAPL", date(2021, 1, 4)), ("AAPL", date(2026, 11, 10))])
    out = ticker_lookup.lookup("AAPL")
    bars = next(t for f in out["files"] if f["kind"] == "prices"
                for t in f["tables"] if t["table"] == "price_bar_daily")
    assert bars["rows"] == 2 and bars["first"] == "2021-01-04" and bars["last"] == "2026-11-10"
    assert bars["matched_on"] == "security_id", "a table with no symbol is matched through the spine's id"


def test_a_name_with_no_spine_row_still_reports_its_rows():
    _prices([("AAPL", date(2026, 11, 10))])
    out = ticker_lookup.lookup("AAPL")
    assert out is not None and out["name"] == "AAPL"
    assert any(t["table"] == "price_bar_daily" for f in out["files"] for t in f["tables"])


def test_empty_tables_are_left_out():
    _spine()
    _prices([("AAPL", date(2026, 11, 10))])
    prices = next(f for f in ticker_lookup.lookup("AAPL")["files"] if f["kind"] == "prices")
    assert [t["table"] for t in prices["tables"]] == ["price_bar_daily", "security_symbol"]
    assert prices["looked_at"] >= 2


def test_meta_tables_are_never_scanned():
    _spine()
    _file("market", ["CREATE TABLE serving_schema_version (kind VARCHAR, version INTEGER, ticker VARCHAR)",
                     "INSERT INTO serving_schema_version VALUES ('market', 1, 'AAPL')"])
    market = next(f for f in ticker_lookup.lookup("AAPL")["files"] if f["kind"] == "market")
    assert market["tables"] == [] and market["looked_at"] == 0


def test_a_table_with_no_key_column_is_skipped_not_guessed():
    _spine()
    _file("market", ["CREATE TABLE market_series (series_id VARCHAR, session_date DATE, value DOUBLE)",
                     "INSERT INTO market_series VALUES ('DGS10', DATE '2026-11-10', 4.0)"])
    market = next(f for f in ticker_lookup.lookup("AAPL")["files"] if f["kind"] == "market")
    assert market["tables"] == [] and market["looked_at"] == 0


def test_a_table_without_a_date_column_reports_rows_and_no_range():
    _spine()
    _file("ownership", ["CREATE TABLE ibkr_shortable (ticker VARCHAR, shortable BOOLEAN)",
                        "INSERT INTO ibkr_shortable VALUES ('AAPL', true)"])
    row = next(t for f in ticker_lookup.lookup("AAPL")["files"] if f["kind"] == "ownership"
               for t in f["tables"])
    assert row["rows"] == 1 and row["first"] is None and row["last"] is None and row["date_column"] is None


def test_the_cik_ties_a_fundamentals_table_to_the_name():
    _spine()
    _file("fundamentals", ["CREATE TABLE fundamental_fact (cik VARCHAR, period_end DATE, value DOUBLE)",
                           "INSERT INTO fundamental_fact VALUES ('0000320193', DATE '2024-09-28', 1.0)",
                           "INSERT INTO fundamental_fact VALUES ('0000320193', DATE '2026-06-27', 2.0)"])
    row = next(t for f in ticker_lookup.lookup("AAPL")["files"] if f["kind"] == "fundamentals"
               for t in f["tables"])
    assert row["matched_on"] == "cik" and row["rows"] == 2 and row["first"] == "2024-09-28"


def test_an_unreadable_file_is_a_named_line_never_a_raise(monkeypatch):
    _spine()

    def boom(kind):
        if kind == "options":
            raise RuntimeError("disk gone")
        raise serving.ServingFileMissing(kind)

    monkeypatch.setattr(ticker_lookup.serving, "open_read", boom)
    out = ticker_lookup.lookup("AAPL")
    assert out is not None and any("options: RuntimeError: disk gone" in e for e in out["errors"])


def test_nothing_reads_the_old_store():
    """HARD RULE: the lookup must not touch ``.store``.

    The module's own code is judged, not its prose: the docstring names the old store to say what this
    replaced, and a guard that forbade the word would push that explanation out of the file.
    """
    import ast as _ast
    tree = _ast.parse(open(ticker_lookup.__file__, encoding="utf-8").read())
    names = {n.id for n in _ast.walk(tree) if isinstance(n, _ast.Name)}
    names |= {n.attr for n in _ast.walk(tree) if isinstance(n, _ast.Attribute)}
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            names |= {a.name for a in node.names} | set((node.module or "").split("."))
        elif isinstance(node, _ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
    assert "rawstore" not in names and "inventory" not in names
    docs = {_ast.get_docstring(n, clean=False) for n in _ast.walk(tree)
            if isinstance(n, (_ast.Module, _ast.ClassDef, _ast.FunctionDef, _ast.AsyncFunctionDef))}
    strings = {n.value for n in _ast.walk(tree) if isinstance(n, _ast.Constant) and isinstance(n.value, str)}
    assert not [x for x in strings - docs if ".store" in x or "raw.duckdb" in x]


@pytest.mark.parametrize("kind", ["spine", "prices", "fundamentals", "ownership", "options", "market"])
def test_every_kind_is_covered_by_a_title(kind):
    assert ticker_lookup.FILE_TITLES[kind]


class TestRouteAndPage:
    """The route and where the lookup sits on the page (avo, 11 Nov 2026: back on the first tab)."""

    def _client(self):
        from RevenueChaser.app import dashboard
        from RevenueChaser.core.tests.panel_client import panel_test_client
        dashboard.app.config["TESTING"] = True
        return panel_test_client(dashboard.app)

    def test_bad_ticker_is_400(self):
        assert self._client().get("/api/settings/lookup/..%2F..%2FX").status_code in (400, 404)

    def test_unknown_ticker_is_404(self):
        _spine()
        r = self._client().get("/api/settings/lookup/ZZZZ")
        assert r.status_code == 404 and r.get_json()["ok"] is False

    def test_known_ticker_answers_its_files(self):
        _spine()
        _prices([("AAPL", date(2021, 1, 4))])
        body = self._client().get("/api/settings/lookup/aapl").get_json()
        assert body["ok"] is True and body["ticker"] == "AAPL"
        assert [f["kind"] for f in body["files"]] == list(serving.KINDS)

    def test_a_raise_is_a_500_not_an_unknown_ticker(self, monkeypatch):
        from RevenueChaser.module_settings import ticker_lookup as tl
        monkeypatch.setattr(tl, "lookup", lambda t: (_ for _ in ()).throw(RuntimeError("boom")))
        r = self._client().get("/api/settings/lookup/AAPL")
        assert r.status_code == 500 and r.get_json()["ok"] is False

    def test_the_lookup_is_on_the_database_tab_not_the_health_tab(self):
        from RevenueChaser.module_settings import report
        html = report.render_settings_dashboard({"tickers": [{"ticker": "AAPL", "name": "Apple Inc."}],
                                                 "newdata": {"error": "none read"}})
        data = html[html.index('id="panel-data"'):html.index('id="panel-health"')]
        assert 'id="ticker-drill"' in data, "the Ticker lookup heads the Database tab"
        assert 'id="ticker-drill"' not in html[html.index('id="panel-health"'):]

    def test_the_page_asks_the_new_route_only(self):
        from RevenueChaser.module_settings import report
        assert "/api/settings/lookup/" in report.DRILL_JS
        assert "/api/settings/ticker/" not in report.DRILL_JS
