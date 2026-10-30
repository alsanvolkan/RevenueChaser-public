"""A fetch request carries its own scope: the spine tiers, an explicit name list, or a date range (#265).

Before this, the names a daily fetch asks were written in the code (``prices.scope`` took the spine's Tier A), so a
fill of one tier, or of ten names, or of one missing day, was not a thing a request could say; the only way to fetch
less was to edit the source. The queue row's ``params`` carries it now, ``base.parse_scope`` is the one place that
reads it, and ``RunContext.scope`` is how a dataset sees it.

A dataset that cannot honour a scope (its names come from somewhere else than the spine: the SEC's daily index, a
whole-market page) says so by not declaring ``honours_scope``, and a request that gave one gets a note on its check
row rather than silently fetching something else.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build import DATASETS, base
from RevenueChaser.rcdata.build import prices

from . import prices_fixture


# ── What a request may say ─────────────────────────────────────────────────────────────────────────────────────

def test_a_scope_is_read_from_the_requests_own_params():
    s = base.parse_scope({"mode": "daily", "tiers": ["b", " a "], "names": ["ko", "msft", "KO"]})
    assert s.tiers == ("A", "B"), "the tiers are normalised and ordered, so two requests for the same set are one"
    assert s.names == ("KO", "MSFT"), "a name is upper-cased and asked once"
    assert s


def test_no_scope_in_the_params_is_an_empty_scope_not_a_guess():
    assert not base.parse_scope({}) and base.parse_scope({}).tiers == ()
    assert not base.parse_scope({"tiers": [], "names": []})


def test_a_tier_the_spine_does_not_have_is_refused_before_anything_is_fetched():
    with pytest.raises(ValueError, match="tier"):
        base.parse_scope({"tiers": ["A", "Z"]})


# ── How a dataset sees it ──────────────────────────────────────────────────────────────────────────────────────

class _Recorder:
    """A dataset that fetches nothing and records the scope its run was given."""
    name = "test/scope"
    kind = "market"
    providers = ()
    tables = ()
    cadence = "daily"
    honours_scope = True
    seen: list[base.Scope] = []

    def default_as_of(self):
        return date(2026, 10, 28)

    def fetch(self, ctx, as_of):
        type(self).seen.append(ctx.scope)
        return []

    def to_parquet_all(self, ctx, raw):
        return []

    def build(self, ctx, con):
        return {}

    def check(self, ctx, con):
        return {"dataset": self.name, "result": "unchanged"}


@pytest.fixture()
def recorder(monkeypatch):
    ds = _Recorder()
    type(ds).seen = []
    monkeypatch.setitem(DATASETS, ds.name, ds)
    monkeypatch.setattr(base, "build_kind", lambda ctx, d: d.check(ctx, None))
    return ds


def test_the_scope_of_the_request_reaches_the_datasets_fetch(recorder):
    row = base.run_request({"dataset": recorder.name, "params": {"tiers": ["A"], "names": ["KO"]}})
    assert row["result"] == "unchanged", row
    assert recorder.seen == [base.Scope(tiers=("A",), names=("KO",))]


def test_a_dataset_that_ignores_the_scope_says_so_on_its_check_row(recorder, monkeypatch):
    monkeypatch.delattr(type(recorder), "honours_scope")
    row = base.run_request({"dataset": recorder.name, "params": {"tiers": ["A"]}})
    assert "scope" in (row["note"] or "").lower() and recorder.name in (row["note"] or "")


def test_a_request_with_no_scope_says_nothing_about_one(recorder, monkeypatch):
    monkeypatch.delattr(type(recorder), "honours_scope")
    row = base.run_request({"dataset": recorder.name, "params": {}})
    assert "scope" not in (row["note"] or "").lower()


def test_a_bad_scope_is_a_failed_check_row_not_an_exception_out_of_the_builder(recorder):
    row = base.run_request({"dataset": recorder.name, "params": {"tiers": ["Z"]}})
    assert row["result"] == "failed" and "tier" in row["note"]
    assert recorder.seen == [], "nothing was fetched"


def test_every_dataset_either_honours_the_scope_or_is_named_here():
    """The list is the promise: a dataset that ignores a request's scope is named, with why. A new dataset is one or
    the other on purpose, not by forgetting."""
    ignores = {
        "alpaca/corporate_actions": "asks the whole market in pages of 1,000, cheaper than asking the scope",
        "alphavantage/earnings_calendar": "a calendar, asked by day",
        "finra/short_interest": "one file a settlement cycle for the whole market",
        "fundamentals/tables": "no fetch",
        "funds/index_holdings": "the index funds' own holdings files",
        "ibkr/iv_history": "the options universe, from the option list",
        "ibkr/shortable": "IBKR's one shortable-shares file",
        "market_series": "series, not names",
        "openfigi/map": "the spine rows with no FIGI yet",
        "ownership/serve": "no fetch",
        "prices/serving": "no fetch",
        "regsho/threshold": "one file a day for the whole market",
        "sec/13dg": "the SEC daily index's 13D/G filers",
        "sec/13f": "the SEC daily index's 13F filers",
        "sec/company_tickers_exchange": "the SEC file is the spine's own source: the tiers come out of it",
        "sec/companyfacts": "the SEC daily index's statement filers among the Tier A CIKs",
        "sec/cover_shares": "its names are the SEC daily index's filers among the Tier A CIKs",
        "sec/filings": "the Tier A companies' own filing lists",
        "sec/form4": "the SEC daily index's Form 4 filers",
        "sec/fsds": "the SEC's quarterly financial-statement data sets",
        "sec/ftd": "one fails-to-deliver file for the whole market",
        "spine/security": "builds the spine, and its tiers, itself",
        "uw/financials": "the fundamentals universe (its own PR)",
        "uw/option_list": "UW's own list of optionable names",
        "uw/option_screener": "UW's screener pages the whole market",
        "uw/spot_expiry_strike": "SPX only",
        "wrds/co_hgic": "attended, a whole-history pull",
        "wrds/firm_ratio": "attended, a whole-history pull",
        "wrds/funda": "attended, a whole-history pull",
        "wrds/fundq": "attended, a whole-history pull",
        "wrds/idxcst_his": "attended, a whole-history pull",
        "wrds/prices": "attended, a whole-history pull",
        "wrds/sec_idhist": "attended, a whole-history pull",
        "wrds/segments": "attended, a whole-history pull",
        "yfinance/estimates": "the fundamentals universe (its own PR)",
    }
    honours = {n for n, d in DATASETS.items() if getattr(d, "honours_scope", False)}
    assert honours == {"alpaca/bars", "yfinance/float", "uw/total_float"}, honours
    unnamed = sorted(set(DATASETS) - honours - set(ignores))
    assert not unnamed, (f"these datasets neither honour a scope nor are named as ignoring it: {unnamed}. "
                         "Declare honours_scope = True, or name it here with the reason.")


# ── The prices scope itself (#265) ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def spine():
    prices_fixture.write_spine()


def _tickers(rows):
    return sorted(r["ticker"] for r in rows)


def test_the_default_scope_asks_tier_a_and_tier_b(spine, monkeypatch):
    """avo, 30 Oct 2026: the universe is A+B (7,670 names on the live spine), with no market-cap floor. Tier A alone
    was 5,655, so the bars' daily calls grow by about a third."""
    monkeypatch.setattr(prices, "cover_classes", lambda rows: [])
    rows = prices.scope(watched=set())
    assert prices.DEFAULT_TIERS == ("A", "B")
    assert "SPY" in _tickers(rows) and {"AAPL", "BRK-B", "FLG", "GAPX"} <= set(_tickers(rows))
    assert all(r.get("security_id") for r in rows), "every asked name carries the id the store keys it under"


def test_a_request_can_ask_one_tier(spine, monkeypatch):
    monkeypatch.setattr(prices, "cover_classes", lambda rows: [])
    rows = prices.scope(watched=set(), want=base.Scope(tiers=("B",)))
    # Tier B of the fixture is SPY; the benchmarks and the rotation ETFs come besides any tier.
    assert "SPY" in _tickers(rows) and "AAPL" not in _tickers(rows)


def test_a_request_can_ask_exactly_its_own_names(spine, monkeypatch):
    monkeypatch.setattr(prices, "cover_classes", lambda rows: [])
    rows = prices.scope(watched={"NVDA"}, want=base.Scope(names=("AAPL", "ZZZZ")))
    assert _tickers(rows) == ["AAPL", "ZZZZ"], "a name list is the whole ask: no benchmark, no watch list, no tier"
    by = {r["ticker"]: r for r in rows}
    assert by["AAPL"]["tier"] == "A" and by["AAPL"]["cik"] == "320193", "a known name keeps its spine row"
    assert by["ZZZZ"]["security_id"] == "sym:ZZZZ", "a name the spine does not hold is still asked, under its symbol"


def test_a_tier_and_a_name_list_in_one_request_are_both_asked(spine, monkeypatch):
    """Karar 82 replaced the earlier "a name list wins over the tiers" rule: a request that names both means both
    ("tier B, and these two names besides"), and each name is asked once."""
    monkeypatch.setattr(prices, "cover_classes", lambda rows: [])
    rows = prices.scope(watched=set(), want=base.Scope(tiers=("B",), names=("AAPL", "ZZZZ")))
    assert _tickers(rows) == ["AAPL", "SPY", "ZZZZ"]
    assert _tickers(prices.scope(watched=set(), want=base.Scope(tiers=("A",), names=("AAPL",)))).count("AAPL") == 1


def test_the_bars_ask_the_requests_names_and_nothing_else(spine, monkeypatch):
    """The fetch path, not the helper: ``alpaca/bars`` is the dataset #265 is about."""
    from RevenueChaser.rcdata.build.prices import bars

    asked = []
    monkeypatch.setattr(prices, "cover_classes", lambda rows: [])
    monkeypatch.setattr(bars.AlpacaBars, "_ask", lambda self, ctx, folder, adj, names, *a, **k: asked.append(
        sorted(n["symbol"] for n in names)) or [])
    monkeypatch.setattr(bars.calendar, "bars_complete", lambda day, now: True)
    ctx = base.RunContext(run_id="t", dataset=bars.NAME, scope=base.Scope(names=("AAPL",)))
    bars.DATASET.fetch(ctx, date(2026, 10, 28))
    assert asked and all(a == ["AAPL"] for a in asked), asked


def test_a_scope_no_spine_row_answers_is_a_clear_refusal_before_any_call(monkeypatch):
    """No spine file (or a tier it holds nothing of): the benchmarks and the rotation ETFs alone are not a bar run,
    and the refusal says so instead of asking Alpaca for five symbols. With a scope it is the request's own doing,
    so it is ``NothingInScope`` (a skipped row) rather than a failure - see the two F1 cases below, which the
    review showed this one was too weak to catch on its own."""
    from RevenueChaser.rcdata.build.prices import bars

    monkeypatch.setattr(bars.calendar, "bars_complete", lambda day, now: True)
    monkeypatch.setattr(prices, "cover_classes", lambda rows: [])
    ctx = base.RunContext(run_id="t", dataset=bars.NAME, scope=base.Scope(tiers=("D",)))
    with pytest.raises(base.NothingInScope, match="tiers D matches no name"):
        bars.DATASET.fetch(ctx, date(2026, 10, 28))


def test_the_floats_keep_their_tier_a_rule_but_follow_a_name_list(spine, monkeypatch):
    """A fund has no float (#265's open point), so a float run stays at Tier A even when the request says A+B; an
    explicit name list is a person asking for those names, and is taken as it is."""
    from RevenueChaser.rcdata.build.prices import floats

    monkeypatch.setattr(prices, "cover_classes", lambda rows: [])
    assert _tickers(floats.asked(base.Scope(tiers=("A", "B")))) == ["AAPL", "BRK-B", "FLG", "GAPX"]
    assert _tickers(floats.asked(base.Scope(names=("SPY",)))) == ["SPY"]


def test_the_other_tiers_coverage_is_reported_beside_tier_as(spine):
    """#265's second point: the acceptance ">= 99% of Tier A" stays Tier A's, so a Tier B fund with no Alpaca bar
    cannot fail the run - but it is reported, or nobody would know B is empty."""
    from RevenueChaser.rcdata.build.prices import bars

    ctx = base.RunContext(run_id="t", dataset=bars.NAME)
    asked = {"2026-10-28": {"AAPL": "A", "GAPX": "A", "SPY": "B", "OTCX": "C"}}
    rows = {(s, date(2026, 10, 28)): {} for s in ("AAPL", "SPY")}
    bars.AlpacaBars._coverage(ctx, rows, asked)
    assert ctx.pending[bars.NAME] == {"as_of": "2026-10-28", "asked": 2, "with_bar": 1}, "the acceptance is Tier A's"
    assert ctx.notes == ["1 of 2 Tier A names with a bar on 2026-10-28",
                         "1 of 1 Tier B names with a bar on 2026-10-28",
                         "0 of 1 Tier C names with a bar on 2026-10-28"]


# ── The independent review of 2488db1 ──────────────────────────────────────────────────────────────────────────

def test_f1_a_tier_the_spine_holds_nothing_of_is_refused_with_the_spine_in_place(spine, monkeypatch):
    """F1. The guard asked whether *any* returned name carried *any* tier, and the benchmarks and rotation ETFs are
    added beside the tier loop from the spine's own rows - so SPY alone satisfied it and a ``tiers=["D"]`` request
    spent 70 symbols' worth of calls twice. The earlier case passed only because it did not write a spine, which is
    the stub the review's probe went around."""
    from RevenueChaser.rcdata.build.prices import bars

    asked = []
    monkeypatch.setattr(prices, "cover_classes", lambda rows: [])
    monkeypatch.setattr(bars.AlpacaBars, "_ask", lambda *a, **k: asked.append(1) or [])
    monkeypatch.setattr(bars.calendar, "bars_complete", lambda day, now: True)
    ctx = base.RunContext(run_id="t", dataset=bars.NAME, scope=base.Scope(tiers=("D",)))
    with pytest.raises(base.NothingInScope, match="tiers D"):
        bars.DATASET.fetch(ctx, date(2026, 10, 28))
    assert asked == [], "the run asked Alpaca for the benchmarks of a tier nobody asked for"
    # The tiers that do hold names are unaffected.
    ctx = base.RunContext(run_id="t", dataset=bars.NAME, scope=base.Scope(tiers=("B",)))
    bars.DATASET.fetch(ctx, date(2026, 10, 28))
    assert asked, "a tier the spine holds was refused"


def test_f1_no_spine_at_all_is_still_a_failure_not_a_skip(monkeypatch):
    """The two cases must not collapse into one: an empty scope with no spine file is a broken build (a failed
    check row), while a scope the spine answers nothing for is the request's own doing (skipped)."""
    from RevenueChaser.rcdata.build.prices import bars

    monkeypatch.setattr(prices, "cover_classes", lambda rows: [])
    monkeypatch.setattr(bars.calendar, "bars_complete", lambda day, now: True)
    ctx = base.RunContext(run_id="t", dataset=bars.NAME)
    with pytest.raises(base.SourceError, match="spine") as e:
        bars.DATASET.fetch(ctx, date(2026, 10, 28))
    assert not isinstance(e.value, base.NothingInScope)


def test_f4_a_scope_this_dataset_can_ask_nothing_for_is_skipped_not_failed(recorder, monkeypatch):
    """F4. ``tiers=["B"]`` on a float dataset is a legitimate request whose true answer is "a fund has no float".
    It used to end as a **failed** check row blaming the spine, which on the Data screen reads as a broken build."""
    def fetch(self, ctx, as_of):
        raise base.NothingInScope("no Tier A name in the scope: a fund has no float")
    monkeypatch.setattr(type(recorder), "fetch", fetch)
    row = base.run_request({"dataset": recorder.name, "params": {"tiers": ["B"]}})
    assert row["result"] == "skipped" and "a fund has no float" in row["note"]


def test_f3_a_scoped_float_run_does_not_occupy_the_days_raw_file(spine, monkeypatch):
    """F3. ``uw/total_float`` writes one raw file a day and returned it before looking at the scope, so a
    ``names=["KO"]`` press wrote a one-row file and the day's later full run handed that same file back: the day
    ended with a float for one name and a check row that looked normal."""
    from RevenueChaser.rcdata.build.prices import floats

    sent = []

    class _Res:
        status, error = "ok", None

        def __init__(self, data):
            self.data = data

    class _Client:
        def short_screener_result(self, names):
            sent.append(sorted(names))
            return _Res({n: {"total_float": 1_000.0} for n in names})

    monkeypatch.setattr(prices, "cover_classes", lambda rows: [])
    monkeypatch.setattr(base.RunContext, "uw_client", lambda self: _Client())
    day = date(2026, 10, 28)
    narrow = base.RunContext(run_id="t1", dataset=floats.UW, scope=base.Scope(names=("AAPL",)))
    whole = base.RunContext(run_id="t2", dataset=floats.UW)
    ds = floats.UW_FLOAT
    assert ds.raw_path(day, narrow.scope) != ds.raw_path(day), "a scoped run writes the day's only file"
    ds.fetch(narrow, day)
    ds.fetch(whole, day)
    assert sent == [["AAPL"], ["AAPL", "BRK-B", "FLG", "GAPX"]], \
        "the day's full run was handed the narrow run's file and asked UW for nothing"
    # Asking the same narrow scope again is still a no-op: the raw file is that ask's own resume marker.
    ds.fetch(base.RunContext(run_id="t3", dataset=floats.UW, scope=base.Scope(names=("AAPL",))), day)
    assert len(sent) == 2


def test_f5_the_name_cap_is_the_one_readers_own(monkeypatch):
    """F5. The cap was checked in the route only, so the CLI's ``--names`` and a hand-written queue row were
    uncapped. It belongs where the one reader is."""
    assert base.NAMES_MAX == 20_000
    with pytest.raises(ValueError, match="names"):
        base.parse_scope({"names": [f"T{i}" for i in range(base.NAMES_MAX + 1)]})
    assert len(base.parse_scope({"names": [f"T{i}" for i in range(base.NAMES_MAX)]}).names) == base.NAMES_MAX


def test_f6_a_narrow_fills_coverage_is_not_reported_as_the_universes(spine, monkeypatch):
    """F6. ``coverage`` is the number a person reads the ">= 99% of Tier A" acceptance off, and it was written per
    request - so a one-name fill left the dataset's newest check row at 1.0 or 0.0 until the next daily run."""
    from RevenueChaser.rcdata.build.prices import bars

    monkeypatch.setattr(bars.stage, "compare_live", lambda *a, **k: (0, 0))
    monkeypatch.setattr(bars.stage, "count", lambda *a, **k: 1)
    ctx = base.RunContext(run_id="t", dataset=bars.NAME, as_of=date(2026, 10, 28))
    ctx.pending[bars.NAME] = {"as_of": "2026-10-28", "asked": 1, "with_bar": 1}
    assert bars.DATASET.check(ctx, None)["coverage"] == 1.0, "the daily run reports its coverage as before"
    scoped = base.RunContext(run_id="t", dataset=bars.NAME, as_of=date(2026, 10, 28),
                             scope=base.Scope(names=("AAPL",)))
    scoped.pending[bars.NAME] = {"as_of": "2026-10-28", "asked": 1, "with_bar": 1}
    row = bars.DATASET.check(scoped, None)
    assert row["coverage"] is None and any("scope" in n for n in scoped.notes)


def test_f4_the_float_refusal_names_the_scope_not_the_spine(spine, monkeypatch):
    """The other half of F4, which the first round's mutation check found untested: with a spine in place and a
    tier that holds no company, the float run must blame the scope (and be skipped), not the spine file."""
    from RevenueChaser.rcdata.build.prices import floats

    monkeypatch.setattr(prices, "cover_classes", lambda rows: [])
    ctx = base.RunContext(run_id="t", dataset=floats.UW, scope=base.Scope(tiers=("B",)))
    with pytest.raises(base.NothingInScope, match="a fund has no float"):
        floats.UW_FLOAT.fetch(ctx, date(2026, 10, 28))
    with pytest.raises(base.NothingInScope, match="a fund has no float"):
        floats.YAHOO_FLOAT.fetch(base.RunContext(run_id="t", dataset=floats.YAHOO,
                                                 scope=base.Scope(tiers=("B",))), date(2026, 10, 28))


# ── Karar 82 (avo, 30 Oct 2026): a narrowed fill asks only what it names ───────────────────────────────────────

def test_karar_82_a_narrowed_fill_asks_no_benchmark_no_watch_list_and_no_cover_class(spine, monkeypatch):
    """Review F2, avo's option A: ``tiers=["B"]`` used to ask 2,015 names **plus** every Tier A issuer's other
    listed class and about 70 ETFs, which is the opposite of #265's point. The three extras are their own scopes
    now and a narrowed fill does not get them for free."""
    monkeypatch.setattr(prices, "cover_classes", lambda rows: [{"ticker": "BRK-A", "cik": "1067983", "tier": None,
                                                                "security_id": "class:1067983:a"}])
    rows = prices.scope(watched={"NVDA"}, want=base.Scope(tiers=("B",)))
    assert _tickers(rows) == ["SPY"], "a tier fill asked something it did not name"


def test_karar_82_the_anchors_are_a_scope_of_their_own(spine, monkeypatch):
    monkeypatch.setattr(prices, "cover_classes", lambda rows: [])
    rows = prices.scope(watched={"NVDA"}, want=base.Scope(include=("anchors",)))
    assert set(_tickers(rows)) == {*prices.BENCHMARKS, *prices.ROTATION}
    assert "AAPL" not in _tickers(rows) and "NVDA" not in _tickers(rows)


def test_karar_82_the_watch_list_and_the_cover_classes_are_scopes_too(spine, monkeypatch):
    monkeypatch.setattr(prices, "cover_classes", lambda rows: [{"ticker": "BRK-A", "cik": "1067983", "tier": None,
                                                                "security_id": "class:1067983:a"}])
    assert _tickers(prices.scope(watched={"NVDA"}, want=base.Scope(include=("watchlist",)))) == ["NVDA"]
    assert _tickers(prices.scope(watched={"NVDA"}, want=base.Scope(include=("cover_classes",)))) == ["BRK-A"]
    # And they compose with a tier.
    rows = prices.scope(watched={"NVDA"}, want=base.Scope(tiers=("A",), include=("watchlist",)))
    assert "NVDA" in _tickers(rows) and "AAPL" in _tickers(rows) and "SPY" not in _tickers(rows)


def test_karar_82_the_daily_run_with_no_scope_is_unchanged(spine, monkeypatch):
    """The one thing that must not move: a request that names nothing is the daily run, which asks the tiers and
    all three extras."""
    monkeypatch.setattr(prices, "cover_classes", lambda rows: [{"ticker": "BRK-A", "cik": "1067983", "tier": None,
                                                                "security_id": "class:1067983:a"}])
    rows = _tickers(prices.scope(watched={"NVDA"}))
    assert {"AAPL", "SPY", "NVDA", "BRK-A", *prices.BENCHMARKS} <= set(rows)


def test_karar_82_the_watch_list_is_not_read_when_it_was_not_asked_for(spine, monkeypatch):
    """A tier fill must not even open the watch list: the read is a store call, and asking for it is the request's
    choice now."""
    monkeypatch.setattr(prices, "cover_classes", lambda rows: [])
    calls = []

    def fake_watched(kind):
        calls.append(kind)
        return set()
    from RevenueChaser.rcdata import read
    monkeypatch.setattr(read, "watched", fake_watched)
    prices.scope(want=base.Scope(tiers=("A",)))
    assert calls == []
    prices.scope(want=base.Scope(include=("watchlist",)))
    assert calls == ["stock"]


def test_karar_82_the_groups_a_request_may_name_are_checked():
    s = base.parse_scope({"include": ["Anchors", "watchlist", "anchors"]})
    assert s.include == ("anchors", "watchlist") and s
    assert base.SCOPE_GROUPS == ("anchors", "watchlist", "cover_classes")
    with pytest.raises(ValueError, match="include"):
        base.parse_scope({"include": ["etfs"]})


def test_karar_82_a_tier_fill_that_matches_nothing_is_refused_before_a_call(spine, monkeypatch):
    """With the extras gone from a narrowed fill, ``tiers=["D"]`` returns nothing at all - so the refusal is the
    plain "this scope matches no name", with no benchmark left to hide it."""
    from RevenueChaser.rcdata.build.prices import bars

    asked = []
    monkeypatch.setattr(prices, "cover_classes", lambda rows: [])
    monkeypatch.setattr(bars.AlpacaBars, "_ask", lambda *a, **k: asked.append(1) or [])
    monkeypatch.setattr(bars.calendar, "bars_complete", lambda day, now: True)
    ctx = base.RunContext(run_id="t", dataset=bars.NAME, scope=base.Scope(tiers=("D",)))
    with pytest.raises(base.NothingInScope, match="no name"):
        bars.DATASET.fetch(ctx, date(2026, 10, 28))
    assert asked == []


def test_a_spine_with_no_name_of_the_asked_tiers_is_a_failure_even_if_a_benchmark_has_a_tier(monkeypatch):
    """The daily run's guard must count the tiers it asked for. A spine holding only names outside A+B still gives
    the benchmarks a tier (SPY as Tier C here), so "does any name carry any tier" would pass and the run would ask
    Alpaca for five ETFs and call that a bars run."""
    from RevenueChaser.rcdata.build.prices import bars

    prices_fixture.write_spine([{"ticker": "SPY", "cik": "884394", "name": "SPDR S&P 500 ETF TRUST",
                                 "exchange": "NYSE", "tier": "C", "security_type": "etf", "gvkey": None,
                                 "iid": None, "composite_figi": None}])
    monkeypatch.setattr(prices, "cover_classes", lambda rows: [])
    monkeypatch.setattr(bars.calendar, "bars_complete", lambda day, now: True)
    ctx = base.RunContext(run_id="t", dataset=bars.NAME)
    with pytest.raises(base.SourceError, match="no name of the asked tiers"):
        bars.DATASET.fetch(ctx, date(2026, 10, 28))
