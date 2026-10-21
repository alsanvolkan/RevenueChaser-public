"""``security_master`` is the single writer of the classification in ``equity_universe`` (issue #86, R3; #265).

``plan_apply`` is pure (dicts in, dicts out); ``apply_to_universe`` is the IO wrapper. The rules under test:
the master's sector and industry (UW's own names) are copied, the GICS-only ``industry_group`` and
``sub_industry`` columns are written NULL, and UW's ``next_earnings_date`` comes from the last good
``uw_sector`` cache row. Only differing rows are written; ``Unclassified`` is kept; a ticker with no master row
keeps its stored values (never blanked); an empty or missing master writes nothing; every other column stays.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.core.gics_map import UNCLASSIFIED
from RevenueChaser.core.identity import apply as A
from RevenueChaser.core.identity import cache, gics, master
from RevenueChaser.core.identity.sources import SourceResult

UNC = UNCLASSIFIED
COLS = ["sector", "industry", "industry_group", "sub_industry", "next_earnings_date"]


def m(ticker, sector, industry=UNC, **kw):
    """One ``security_master`` row (only the columns the apply reads)."""
    return {"ticker": ticker, "sector": sector, "industry": industry, **kw}


def u(ticker, sector=None, industry=None, group=None, sub=None, date=None, **kw):
    return {"ticker": ticker, "sector": sector, "industry": industry, "industry_group": group,
            "sub_industry": sub, "next_earnings_date": date, **kw}


def entry(ticker, sector, industry=UNC, date=None):
    return {"ticker": ticker, "sector": sector, "industry": industry, "industry_group": None,
            "sub_industry": None, "next_earnings_date": date}


NVDA = ("Technology", "Semiconductors")
MSFT = ("Technology", "Software - Infrastructure")


# ── plan_apply: pure ──────────────────────────────────────────────────────────────────────────
class TestPlanApply:
    def test_only_rows_that_differ_are_planned(self):
        master_rows = [m("NVDA", *NVDA), m("MSFT", *MSFT), m("AMD", *NVDA)]
        universe = [u("NVDA", *NVDA),                                              # identical
                    u("MSFT", "Industrials", MSFT[1]),                             # sector differs
                    u("AMD", NVDA[0], "Old industry text")]                        # industry differs
        assert A.plan_apply(master_rows, universe) == [entry("MSFT", *MSFT), entry("AMD", *NVDA)]

    def test_an_identical_universe_plans_nothing(self):
        assert A.plan_apply([m("NVDA", *NVDA)], [u("NVDA", *NVDA)]) == []

    def test_unclassified_stays_unclassified(self):
        plan = A.plan_apply([m("ZZZ", UNC)], [u("ZZZ", "Other", "Other")])
        assert plan == [entry("ZZZ", UNC, UNC)]

    def test_unclassified_overrides_a_vendor_sector(self):
        """The master is the single writer: its "unknown" replaces a vendor value too."""
        plan = A.plan_apply([m("ETFX", UNC)], [u("ETFX", "Financial Services", "Asset Management")])
        assert plan == [entry("ETFX", UNC, UNC)]

    def test_an_already_unclassified_row_is_not_rewritten(self):
        assert A.plan_apply([m("ZZZ", UNC)], [u("ZZZ", UNC, UNC)]) == []

    def test_a_ticker_without_a_master_row_gets_no_entry(self):
        plan = A.plan_apply([m("NVDA", *NVDA)],
                            [u("NVDA", "Other", "Other"), u("NEWCO", "Healthcare", "Biotechnology")])
        assert [e["ticker"] for e in plan] == ["NVDA"]

    def test_a_master_row_without_a_sector_is_not_an_answer(self):
        """A blank master sector must never blank a vendor value."""
        for blank in (None, ""):
            assert A.plan_apply([m("NVDA", blank)], [u("NVDA", *NVDA)]) == []

    def test_a_blank_master_industry_reads_unclassified(self):
        plan = A.plan_apply([m("NVDA", NVDA[0], None)], [u("NVDA", *NVDA)])
        assert plan == [entry("NVDA", NVDA[0], UNC)]

    def test_a_master_ticker_missing_from_the_universe_is_ignored(self):
        assert A.plan_apply([m("GONE", *NVDA)], [u("NVDA", *NVDA)]) == []

    def test_empty_inputs(self):
        assert A.plan_apply([], [u("NVDA", *NVDA)]) == []
        assert A.plan_apply([m("NVDA", NVDA[0])], []) == []

    def test_the_two_gics_only_levels_are_written_null_not_unclassified(self):
        """``industry_group`` and ``sub_industry`` stay as columns but nothing fills them any more."""
        plan = A.plan_apply([m("NVDA", *NVDA)],
                            [u("NVDA", *NVDA, group="Semiconductors & Semiconductor Equipment", sub="Semiconductors")])
        assert plan == [entry("NVDA", *NVDA)]
        assert plan[0]["industry_group"] is None and plan[0]["sub_industry"] is None

    def test_a_row_with_null_levels_is_not_rewritten(self):
        assert A.plan_apply([m("NVDA", *NVDA)], [u("NVDA", *NVDA, group=None, sub=None)]) == []

    def test_old_unclassified_group_text_is_cleared_to_null(self):
        plan = A.plan_apply([m("ZZZ", UNC)], [u("ZZZ", UNC, UNC, group=UNC, sub=UNC)])
        assert plan == [entry("ZZZ", UNC, UNC)]

    def test_entries_carry_exactly_the_five_columns_and_the_key(self):
        plan = A.plan_apply([m("NVDA", *NVDA)], [u("NVDA", "Other", "Other", market_cap=1.0, name="x")])
        assert set(plan[0]) == {"ticker", *COLS}

    def test_the_earnings_date_comes_from_the_uw_rows(self):
        plan = A.plan_apply([m("NVDA", *NVDA)], [u("NVDA", *NVDA, date="2026-08-01")],
                            earnings={"NVDA": "2026-11-19"})
        assert plan == [entry("NVDA", *NVDA, date="2026-11-19")]

    def test_a_uw_row_without_a_date_clears_a_stored_one(self):
        plan = A.plan_apply([m("NVDA", *NVDA)], [u("NVDA", *NVDA, date="2026-08-01")], earnings={"NVDA": None})
        assert plan == [entry("NVDA", *NVDA, date=None)]

    def test_a_ticker_with_no_uw_row_keeps_its_stored_date(self):
        plan = A.plan_apply([m("NVDA", "Energy", "Oil")], [u("NVDA", *NVDA, date="2026-11-19")], earnings={})
        assert plan == [entry("NVDA", "Energy", "Oil", date="2026-11-19")]

    def test_a_changed_date_alone_is_a_plan_entry(self):
        plan = A.plan_apply([m("NVDA", *NVDA)], [u("NVDA", *NVDA)], earnings={"NVDA": "2026-11-19"})
        assert plan == [entry("NVDA", *NVDA, date="2026-11-19")]
        assert A.plan_apply([m("NVDA", *NVDA)], [u("NVDA", *NVDA, date="2026-11-19")],
                            earnings={"NVDA": "2026-11-19"}) == []

    def test_the_plan_is_pure(self, monkeypatch):
        """The evidence script computes the plan without a write connection: no store access at all."""
        def boom(*a, **k):
            raise AssertionError("plan_apply must not touch the store")
        for name in ("query", "update_columns", "write_rows", "connect", "get_all"):
            monkeypatch.setattr(rawstore, name, boom)
        assert A.plan_apply([m("NVDA", *NVDA)], [u("NVDA")]) == [entry("NVDA", *NVDA)]

    def test_master_rows_are_not_mutated(self):
        rows = [m("NVDA", *NVDA)]
        snap = [dict(r) for r in rows]
        A.plan_apply(rows, [u("NVDA")])
        assert rows == snap


class TestPlanFromBuiltRows:
    def test_rows_built_by_the_master_plan_only_morningstar_names_or_unclassified(self):
        """The real builder's output (every precedence branch) goes through plan_apply."""
        inputs = {
            "UW": {"uw": {"sector": "Technology", "industry": "Software - Application"}},
            "YF": {"yahoo": {"sector": "Healthcare", "industry": "Biotechnology"}},
            "UWYF": {"uw": {"sector": "Energy", "industry": "Oil & Gas E&P"},
                     "yahoo": {"sector": "Healthcare", "industry": "Biotechnology"}},
            "SIC": {"sec": {"sic": "3674", "status": "ok"}},
            "NONE": {},
            "ETP": {"openfigi": {"security_type": "ETP", "security_type2": "ETP"}, "uw": {"sector": "Energy"}},
            "CEF": {"openfigi": {"security_type": "Closed-End Fund", "security_type2": "Mutual Fund"}},
        }
        rows, _ = master.build({t: dict({"name": t}, **i) for t, i in inputs.items()}, {}, now="2026-10-01T00:00:00Z")
        plan = A.plan_apply(rows, [u(r["ticker"], "Other", "Other", group="x", sub="x") for r in rows])
        assert {e["ticker"] for e in plan} == set(inputs)
        for e in plan:
            assert e["sector"] in gics.MORNINGSTAR_SECTORS or e["sector"] == UNC, e
            assert e["industry_group"] is None and e["sub_industry"] is None
        by = {e["ticker"]: e for e in plan}
        assert (by["UW"]["sector"], by["UW"]["industry"]) == ("Technology", "Software - Application")
        assert (by["CEF"]["sector"], by["CEF"]["industry"]) == ("Financial Services", "Asset Management")
        assert (by["ETP"]["sector"], by["ETP"]["industry"]) == (UNC, UNC)


# ── apply_to_universe: IO ─────────────────────────────────────────────────────────────────────
def seed_universe(rows):
    rawstore.replace_all("equity_universe", rows)


def uni(ticker, sector="Other", industry="Vendor Industry", cap=1.0e9, **kw):
    return {"ticker": ticker, "market_cap": cap, "sector": sector, "price": 10.0, "name": f"{ticker} Inc",
            "industry": industry, "industry_group": "Old Group", "sub_industry": "Old Sub",
            "quote": {"symbol": ticker}, **kw}


def seed_master(rows):
    rawstore.write_rows("security_master", rows)


def seed_uw(rows, at="2026-10-01T00:00:00Z"):
    cache.save_results([SourceResult("uw_sector", "ok", rows, None, at)])


def stored(ticker):
    return rawstore.get("equity_universe", ticker)


@pytest.fixture
def db(rawstore_tmp_db):
    return rawstore_tmp_db


class TestApplyToUniverse:
    def test_writes_the_master_values_and_returns_the_counts(self, db):
        seed_universe([uni("NVDA"), uni("MSFT", *MSFT), uni("NEWCO", "Healthcare", "Biotechnology")])
        seed_master([m("NVDA", *NVDA), m("MSFT", *MSFT)])
        counts = A.apply_to_universe()
        assert counts["applied"] == 2 and counts["unchanged"] == 0 and counts["not_in_master"] == 1
        nv = stored("NVDA")
        assert (nv["sector"], nv["industry"]) == NVDA
        assert nv["industry_group"] is None and nv["sub_industry"] is None

    def test_a_ticker_without_a_master_row_keeps_its_vendor_values(self, db):
        seed_universe([uni("NVDA"), uni("NEWCO", "Healthcare", "Biotechnology")])
        seed_master([m("NVDA", *NVDA)])
        A.apply_to_universe()
        nc = stored("NEWCO")
        assert (nc["sector"], nc["industry"], nc["industry_group"], nc["sub_industry"]) == (
            "Healthcare", "Biotechnology", "Old Group", "Old Sub")

    def test_unclassified_is_written(self, db):
        seed_universe([uni("ZZZ", "Technology", "Software")])
        seed_master([m("ZZZ", UNC)])
        assert A.apply_to_universe()["applied"] == 1
        z = stored("ZZZ")
        assert (z["sector"], z["industry"], z["industry_group"], z["sub_industry"]) == (UNC, UNC, None, None)

    def test_every_other_column_is_untouched(self, db):
        seed_universe([uni("NVDA", industry="Semiconductors", cap=2.5e12)])
        before = stored("NVDA")
        seed_master([m("NVDA", *NVDA)])
        A.apply_to_universe()
        after = stored("NVDA")
        for col in ("market_cap", "price", "name", "quote"):
            assert after[col] == before[col], col
        assert after["sector"] == NVDA[0]

    def test_a_second_apply_changes_nothing(self, db):
        seed_universe([uni("NVDA"), uni("ZZZ")])
        seed_master([m("NVDA", *NVDA), m("ZZZ", UNC)])
        first = A.apply_to_universe()
        second = A.apply_to_universe()
        assert first["applied"] == 2
        assert second["applied"] == 0 and second["unchanged"] == 2

    def test_the_earnings_date_is_copied_from_the_last_good_uw_row(self, db):
        seed_universe([uni("NVDA"), uni("MSFT"), uni("NODATE")])
        seed_master([m("NVDA", *NVDA), m("MSFT", *MSFT), m("NODATE", *NVDA)])
        seed_uw([{"ticker": "NVDA", "sector": "Technology", "industry": "Semiconductors",
                  "next_earnings_date": "2026-11-19"},
                 {"ticker": "NODATE", "sector": "Technology", "industry": "Semiconductors",
                  "next_earnings_date": None}])
        A.apply_to_universe()
        assert stored("NVDA")["next_earnings_date"] == "2026-11-19"
        assert stored("NODATE")["next_earnings_date"] is None
        assert stored("MSFT")["next_earnings_date"] is None          # no UW row at all: nothing to copy

    def test_a_failed_uw_run_never_overwrote_the_cache_so_the_old_date_is_used(self, db):
        seed_universe([uni("NVDA")])
        seed_master([m("NVDA", *NVDA)])
        seed_uw([{"ticker": "NVDA", "sector": "Technology", "industry": "Semiconductors",
                  "next_earnings_date": "2026-11-19"}])
        cache.save_results([SourceResult("uw_sector", "error", [], "boom", "2026-10-02T00:00:00Z")])
        A.apply_to_universe()
        assert stored("NVDA")["next_earnings_date"] == "2026-11-19"

    def test_no_uw_cache_leaves_a_stored_date_alone(self, db):
        seed_universe([uni("NVDA", next_earnings_date="2026-12-01")])
        seed_master([m("NVDA", *NVDA)])
        A.apply_to_universe()
        assert stored("NVDA")["next_earnings_date"] == "2026-12-01"

    def test_a_universe_rebuild_loses_the_date_and_the_apply_puts_it_back(self, db):
        """``_save_universe`` rewrites every row without the column; the apply right after restores it."""
        seed_universe([uni("NVDA", next_earnings_date="2026-11-19")])
        seed_master([m("NVDA", *NVDA)])
        seed_uw([{"ticker": "NVDA", "sector": "Technology", "industry": "Semiconductors",
                  "next_earnings_date": "2026-11-19"}])
        seed_universe([uni("NVDA")])                       # the rebuild: no date column
        assert stored("NVDA")["next_earnings_date"] is None
        A.apply_to_universe()
        assert stored("NVDA")["next_earnings_date"] == "2026-11-19"

    def test_an_empty_master_writes_nothing_and_says_so(self, db, monkeypatch):
        seed_universe([uni("NVDA")])
        monkeypatch.setattr(rawstore, "update_columns", lambda *a, **k: pytest.fail("must not write"))
        counts = A.apply_to_universe()
        assert counts["applied"] == 0 and counts["unchanged"] == 0 and counts["not_in_master"] == 1
        assert counts["skipped"] and "security_master" in counts["skipped"]
        assert stored("NVDA")["sector"] == "Other"

    def test_a_missing_master_table_writes_nothing(self, isolate_rawstore, monkeypatch):
        """No manifest applied: ``security_master`` does not exist at all."""
        seed_universe([uni("NVDA")])
        monkeypatch.setattr(rawstore, "update_columns", lambda *a, **k: pytest.fail("must not write"))
        counts = A.apply_to_universe()
        assert counts["applied"] == 0 and counts["skipped"]

    def test_an_empty_universe_is_a_no_op(self, db):
        seed_master([m("NVDA", *NVDA)])
        counts = A.apply_to_universe()
        assert (counts["applied"], counts["unchanged"], counts["not_in_master"]) == (0, 0, 0)

    def test_the_plan_is_written_in_chunks(self, db, monkeypatch):
        tickers = [f"T{i:02d}" for i in range(5)]
        seed_universe([uni(t) for t in tickers])
        seed_master([m(t, *NVDA) for t in tickers])
        calls = []
        real = rawstore.update_columns

        def spy(name, key, rows, cols):
            calls.append((name, key, len(rows), list(cols)))
            return real(name, key, rows, cols)

        monkeypatch.setattr(rawstore, "update_columns", spy)
        monkeypatch.setattr(A, "CHUNK", 2)
        counts = A.apply_to_universe()
        assert counts["applied"] == 5
        assert [c[2] for c in calls] == [2, 2, 1]
        assert all(c[:2] == ("equity_universe", "ticker") and c[3] == COLS for c in calls), \
            "only the classification columns and the date: never cap, price, name or quote"
        assert all(stored(t)["sector"] == NVDA[0] for t in tickers)

    def test_a_large_plan_goes_through_the_bulk_update_path(self, db):
        """``update_columns`` switches to one ``UPDATE ... FROM`` statement for big batches (>= 64 rows)."""
        n = 300
        tickers = [f"B{i:03d}" for i in range(n)]
        seed_universe([uni(t) for t in tickers])
        seed_master([m(t, *NVDA) if i % 3 else m(t, UNC) for i, t in enumerate(tickers)])
        seed_uw([{"ticker": t, "sector": "Technology", "industry": "x",
                  "next_earnings_date": "2026-11-19" if i % 2 else None} for i, t in enumerate(tickers)])
        counts = A.apply_to_universe()
        assert (counts["applied"], counts["unchanged"], counts["not_in_master"]) == (n, 0, 0)
        got = {t: (stored(t)["sector"], stored(t)["industry"], stored(t)["industry_group"],
                   stored(t)["sub_industry"], stored(t)["next_earnings_date"]) for t in tickers}
        assert got["B001"] == (*NVDA, None, None, "2026-11-19")        # a date, and NULLs through the bulk path
        assert got["B000"] == (UNC, UNC, None, None, None)
        assert got["B004"] == (*NVDA, None, None, None)
        assert sum(1 for v in got.values() if v[0] == UNC) == n // 3
