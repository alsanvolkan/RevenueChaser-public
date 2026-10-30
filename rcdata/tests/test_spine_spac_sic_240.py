"""#240: a SPAC share is typed from Compustat's SIC 6770, so the 88 blank-check shells leave Tier A.

The identity master already types a SPAC from SIC 6770 (``core/identity/types.spac_signals``), but the spine passes
``"sec": None`` into ``classify_type``, so that rule never fires in a spine build, and the name rule
(``SPAC_NAME``) matches none of "Churchill Capital Corp XI", "Gores Holdings X" or "Cantor Equity Partners".
Compustat's company SIC is the spine's only SIC.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build.spine import funds, openfigi_map, sec_tickers, security, tier, wrds

from . import spine_fixture as fx
from .test_spine_security import run_all


# ── The rule itself ──────────────────────────────────────────────────────────────────────────────────────────

def test_sic_6770_types_a_name_openfigi_calls_common_as_a_spac():
    assert tier.security_type("CCCX", "Churchill Capital Corp XI", {"security_type": "Common Stock"},
                              sic="6770") == ("spac", "compustat_sic(6770)")


def test_sic_6770_types_a_name_no_source_typed_at_all():
    assert tier.security_type("GHIX", "Gores Holdings X", None, sic="6770") == ("spac", "compustat_sic(6770)")


def test_sic_6770_beats_compustat_tpci_common():
    assert tier.security_type("CEPO", "Cantor Equity Partners", None, tpci="0",
                              sic="6770") == ("spac", "compustat_sic(6770)")


@pytest.mark.parametrize("raw", ["6770", " 6770 ", "06770", "6770.0"])
def test_the_sic_is_read_as_a_number_so_compustats_text_shapes_all_land(raw):
    assert tier.security_type("CCCX", "Churchill Capital Corp XI", None, sic=raw)[0] == "spac"


@pytest.mark.parametrize("raw", [None, "", "7372", "6022", "677", "67700", "nan", "none", "blank check"])
def test_any_other_sic_types_nothing(raw):
    assert tier.security_type("MSFT", "MICROSOFT CORP", {"security_type": "Common Stock"},
                              sic=raw) == ("common", "openfigi")


def test_sic_6770_never_overrides_a_type_that_is_not_common_like():
    # An ETF whose Compustat company carries 6770 stays an ETP: the master's gate is common-like types only.
    assert tier.security_type("SPY", "SPDR S&P 500 ETF TRUST", {"security_type": "ETP"}, sic="6770")[0] == "etp"
    assert tier.security_type("WFC-PC", "WELLS FARGO & COMPANY/MN", None, sic="6770")[0] == "preferred"


def test_a_spac_warrant_keeps_its_warrant_type_rather_than_the_sic_rules_spac():
    # Both leave Tier A; the ticker says which instrument it is, and the ticker rule is the more precise source.
    assert tier.security_type("CCCX-WT", "Churchill Capital Corp XI", None,
                              sic="6770") == ("warrant", "ticker_rule(-WT)")


def test_the_sic_is_optional_so_every_caller_that_has_none_is_unchanged():
    assert tier.security_type("MSFT", "MICROSOFT CORP", {"security_type": "Common Stock"}) == ("common", "openfigi")


# ── Compustat is asked for the column ────────────────────────────────────────────────────────────────────────

def test_the_company_snapshot_asks_for_sic():
    assert "sic" in [c for c, _ in wrds.COMPANY]
    assert wrds.TABLES["company"]["sql"] == "SELECT gvkey, conm, cik, sic FROM comp.company"


def test_the_new_name_query_asks_for_the_same_columns_as_the_snapshot(monkeypatch, tmp_path):
    asked: list[str] = []
    monkeypatch.setattr(wrds, "_sec_ciks", lambda: {"0002000001"})
    monkeypatch.setattr(wrds, "_known", lambda *a: set())
    monkeypatch.setattr(wrds, "raw_dir", lambda name: tmp_path)

    def fake_ask(ctx, path, sql, as_of):
        asked.append(sql)
        raise wrds.base.SourceError("stop after the company query")

    monkeypatch.setattr(wrds, "_ask", fake_ask)
    with pytest.raises(wrds.base.SourceError):
        wrds.SEC_IDHIST._new_names(_Ctx(), date(2026, 10, 27))
    assert asked and asked[0].startswith(wrds.TABLES["company"]["sql"] + " WHERE cik IN (")


class _Ctx:
    notes: list[str] = []


# ── End to end: the shell leaves Tier A ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def built_with(monkeypatch, http_mock):
    """The fixture build, with one Compustat company's SIC replaced."""
    def build(gvkey: str, sic: str):
        monkeypatch.setattr(fx, "COMPANY", [{**c, "sic": sic} if c["gvkey"] == gvkey else c for c in fx.COMPANY])
        monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
        monkeypatch.setenv("RC_OPENFIGI_BASE", "https://figi.test")
        monkeypatch.setenv("OPENFIGI_API_KEY", "k")
        monkeypatch.setattr(funds, "FLOOR", {"sp500": 4, "sp400": 2, "sp600": 2, "ndx": 1})
        monkeypatch.setattr(wrds, "_factory", lambda: fx.FakeWrds())
        wrds.reset()
        http_mock.add("company_tickers_exchange.json", fx.SEC_FILE)
        http_mock.add("figi.test/v3/mapping", fx.figi_answer)
        fx.add_fund_routes(http_mock)
        run_all("2026-10-27")
    yield build
    wrds.reset()


def _sec(ticker: str) -> dict:
    with serving.open_read("spine") as con:
        cur = con.execute("SELECT * FROM security WHERE ticker = ?", [ticker])
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, cur.fetchone()))


def test_a_listed_name_whose_compustat_company_is_a_blank_check_is_tier_b(built_with):
    built_with("002968", "6770")                       # CNA's gvkey, a linked Tier A common name in the fixture
    row = _sec("CNA")
    assert (row["tier"], row["security_type"], row["type_source"]) == ("B", "spac", "compustat_sic(6770)")


def test_an_operating_sic_leaves_the_same_name_in_tier_a(built_with):
    built_with("002968", "6331")
    row = _sec("CNA")
    assert (row["tier"], row["security_type"]) == ("A", "common")


# ── A column added after the Parquet was written ─────────────────────────────────────────────────────────────

def test_staging_reads_a_column_the_parquet_does_not_hold_as_null(tmp_path):
    """The migration this PR needs: a ``company`` snapshot taken before ``sic`` was asked for has no such column,
    and selecting it would fail the build instead of typing nothing."""
    import duckdb

    from RevenueChaser.rcdata.build.spine import stage

    con = duckdb.connect()
    old = tmp_path / "company.parquet"
    con.execute(f"COPY (SELECT '012141' AS gvkey, 'MICROSOFT CORP' AS conm, '0000789019' AS cik) "
                f"TO '{old}' (FORMAT parquet)")
    n = stage.create(con, "stg_x", [("gvkey", "TEXT"), ("conm", "TEXT"), ("cik", "TEXT"), ("sic", "TEXT")], [old],
                     added_later={"sic"})
    assert n == 1
    assert con.execute("SELECT gvkey, sic FROM stg_x").fetchall() == [("012141", None)]


def test_staging_still_reads_a_column_the_parquet_does_hold(tmp_path):
    import duckdb

    from RevenueChaser.rcdata.build.spine import stage

    con = duckdb.connect()
    new = tmp_path / "company.parquet"
    con.execute(f"COPY (SELECT '012141' AS gvkey, '7372' AS sic) TO '{new}' (FORMAT parquet)")
    stage.create(con, "stg_y", [("gvkey", "TEXT"), ("sic", "TEXT")], [new])
    assert con.execute("SELECT gvkey, sic FROM stg_y").fetchall() == [("012141", "7372")]


# ── The de-SPAC guard (review SHOULD 1) ──────────────────────────────────────────────────────────────────────

def test_an_operating_gics_sector_blocks_the_sic_rule():
    """A company whose Compustat record still says 6770 after its merger, but which Compustat has already given an
    operating GICS sector, is not a shell. The spine has no ``shell_closed`` signal, so the sector is the guard."""
    assert tier.security_type("DSPC", "De-Spacced Industries", {"security_type": "Common Stock"},
                              sic="6770", gsector="45") == ("common", "openfigi")


@pytest.mark.parametrize("sector", [None, "", "  ", "40"])
def test_the_sic_rule_fires_with_no_gics_row_or_a_financials_one(sector):
    assert tier.security_type("CCCX", "Churchill Capital Corp XI", None,
                              sic="6770", gsector=sector) == ("spac", "compustat_sic(6770)")


def test_every_other_sector_blocks_it():
    for sector in ("10", "15", "20", "25", "30", "35", "45", "50", "55", "60"):
        assert tier.security_type("CCCX", "Churchill Capital Corp XI", None,
                                  sic="6770", gsector=sector)[0] != "spac", sector


def test_a_blank_check_with_an_operating_sector_stays_in_tier_a_end_to_end(built_with):
    built_with("012141", "6770")                       # MSFT's gvkey, whose current GICS sector is 45
    row = _sec("MSFT")
    assert (row["tier"], row["security_type"]) == ("A", "common")


# ── The staging fallback is no longer silent or general (review SHOULD 2) ────────────────────────────────────

def test_a_missing_column_outside_the_declared_set_still_fails_the_build(tmp_path):
    import duckdb

    from RevenueChaser.rcdata.build.spine import stage

    con = duckdb.connect()
    f = tmp_path / "company.parquet"
    con.execute(f"COPY (SELECT '012141' AS gvkey) TO '{f}' (FORMAT parquet)")
    with pytest.raises(duckdb.Error):
        stage.create(con, "stg_z", [("gvkey", "TEXT"), ("cusip", "TEXT")], [f], added_later={"sic"})


def test_a_column_read_as_null_is_logged(tmp_path, caplog):
    import duckdb

    from RevenueChaser.rcdata.build.spine import stage

    con = duckdb.connect()
    f = tmp_path / "company.parquet"
    con.execute(f"COPY (SELECT '012141' AS gvkey) TO '{f}' (FORMAT parquet)")
    with caplog.at_level("WARNING"):
        stage.create(con, "stg_w", [("gvkey", "TEXT"), ("sic", "TEXT")], [f], added_later={"sic"})
    assert "sic" in caplog.text and "stg_w" in caplog.text


def test_the_company_table_declares_sic_as_added_later():
    assert wrds.ADDED_LATER == {"company": {"sic"}}


# ── A stored snapshot written before the column existed (review SHOULD 3) ────────────────────────────────────

def test_a_spine_build_on_a_snapshot_without_the_sic_column_still_builds(built_with, monkeypatch):
    """The migration, end to end rather than at ``stage.create``: the stored Parquet of a snapshot taken before
    ``sic`` was asked for has no such column, and the attended backfill that would add it needs a person. So the
    whole spine build has to run on that file and type nothing from the SIC, not fail."""
    import duckdb as ddb

    built_with("002968", "6770")                       # a first build, whose Parquet does carry sic
    assert _sec("CNA")["security_type"] == "spac"       # the rule fired, so the removal below is a real test

    snap = wrds.parquet_dir("wrds/sec_idhist") / "snapshot=2026-10-27" / "company.parquet"
    assert snap.is_file(), snap
    con = ddb.connect()
    try:
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{snap}')").fetchall()]
        assert "sic" in cols
        keep = ", ".join(f'"{c}"' for c in cols if c != "sic")
        tmp = snap.with_suffix(".old.parquet")
        con.execute(f"COPY (SELECT {keep} FROM read_parquet('{snap}')) TO '{tmp}' (FORMAT parquet)")
    finally:
        con.close()
    tmp.replace(snap)                                   # the snapshot as it was before #240

    run_all("2026-10-27", names=[security.NAME])        # the build that must survive it
    row = _sec("CNA")
    assert (row["tier"], row["security_type"]) == ("A", "common")
    assert row["type_source"] != "compustat_sic(6770)"
