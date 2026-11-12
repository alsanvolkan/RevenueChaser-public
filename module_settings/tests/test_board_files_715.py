"""#715: the boards' own DuckDB files have a section each on the Database tab.

avo, 13 Nov 2026: more DuckDB files exist now, the boards' own among them, and they did not seem to be in the
Data flow. They were only a small box each in the wide diagram and one line under their board on a phone.
"""
from __future__ import annotations

from RevenueChaser.core import rawstore
from RevenueChaser.module_settings import control, control_render
from RevenueChaser.module_settings.tests.test_control_view import NOW, control_nd
from RevenueChaser.rcdata import serving


def _view() -> dict:
    return control.build(control_nd(), now=NOW)


def test_there_is_one_card_for_each_board_file_with_its_tables_and_columns():
    cards = control.board_file_cards()
    assert sorted(c["name"] for c in cards) == sorted(f"{db}.duckdb" for db in rawstore.BOARD_DBS)
    by = {c["name"]: c for c in cards}
    wl = by["watchlists.duckdb"]
    assert wl["owner"] == "Watch lists" and wl["path"] == "boards/watchlists.duckdb"
    cols = dict(next(t for t in wl["tables"] if t["name"] == "wl_list")["columns"])
    assert cols["list_id"] == "TEXT PRIMARY KEY" and "PRIMARY" not in cols, "a table constraint is not a column"
    assert all(n == sum(1 for m in rawstore.MANIFEST.values() if m.get("db") == c["name"][:-7])
               for c in cards for n in [len(c["tables"])])


def test_a_card_reads_the_file_from_one_stat_and_never_opens_it(tmp_path, monkeypatch):
    monkeypatch.setattr(rawstore, "board_db_path", lambda db: tmp_path / f"{db}.duckdb")
    (tmp_path / "news.duckdb").write_bytes(b"x" * 2048)   # not a DuckDB file: opening it would raise
    by = {c["name"]: c for c in control.board_file_cards()}
    assert by["news.duckdb"]["bytes"] == 2048 and by["news.duckdb"]["written_at"] is not None
    assert by["welcome.duckdb"]["bytes"] is None and by["welcome.duckdb"]["written_at"] is None


def test_the_database_tab_draws_a_folded_section_for_each_board_file_under_the_served_files():
    html = control_render.render_data(_view())
    assert html.count('<details class="sec bfp fpanel"') == len(rawstore.BOARD_DBS)
    assert html.count('<details class="sec fpanel"') == len(serving.KINDS), "the served files keep their own count"
    assert html.index('class="fpanels"') < html.index('class="bfpanels"')
    assert 'data-file="boards/fundamentals.duckdb"' in html, "not the served fundamentals.duckdb's data-file"
    assert html.count('data-file="fundamentals.duckdb"') == 1
    one = html[html.index('data-file="boards/watchlists.duckdb"'):]
    one = one[:one.index("</div></div></details>")]
    assert "written by Watch lists · 4 tables · not on the disk yet" in one
    assert "<code class=\"p\">.store/boards/watchlists.duckdb</code>" in one
    assert '<span class="cn">list_id</span><span class="ct">text primary key</span>' in one
    import re
    assert re.search(r'class="sec bfp fpanel"[^>]* open>', html) is None, "all folded"


def test_a_file_no_board_writes_says_so_and_a_reader_is_named():
    cards = control.board_file_cards()
    forecast = control_render._board_panel(next(c for c in cards if c["name"] == "forecast.duckdb"))
    assert "written by a job, not a board" in forecast
    news = control_render._board_panel(next(c for c in cards if c["name"] == "news.duckdb"))
    assert "Also read by:" in news and "Welcome" in news
