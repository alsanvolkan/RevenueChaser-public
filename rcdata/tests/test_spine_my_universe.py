"""``my_universe``: the list of names RevenueChaser itself keeps, and ``my_universe_changes``, who entered or left.

avo's rules (4 Nov 2026, the plan ``2026-11-04_2025_rc-our-universe.md``, decisions 1-2 and 13-15):

- a stock is a ``security`` row of Tier A or D whose type is common, ADR, REIT or MLP -- no royalty trust, no BDC,
  no non-SEC bank, and nothing Tier B or C whatever its type;
- an ETF is an ``etf`` row that is no ETN and no CEF, with an AUM at or over 100 million, or, where N-CEN gives it
  no AUM, a 20-session average daily dollar volume at or over 10 million;
- a delisted name never enters (neither source holds one);
- avo's manual file adds and removes names by hand.
"""
from __future__ import annotations

import json
from datetime import date

import duckdb
import pytest

from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.spine import etf, my_universe, security


def _plain(schema):
    return [(c, t.replace(" PRIMARY KEY", "")) for c, t in schema]


def _ctx(as_of=date(2026, 11, 4)) -> base.RunContext:
    return base.RunContext(run_id="run-test", dataset=security.NAME, as_of=as_of)


def _con(securities=(), etfs=()) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    base.rows_table(con, "security", _plain(security.SECURITY), list(securities))
    base.rows_table(con, "etf", _plain(etf.SCHEMA), list(etfs))
    return con


def _sec(ticker, security_type="common", tier="A", **kw) -> dict:
    return {"ticker": ticker, "security_type": security_type, "tier": tier, "name": f"{ticker} Inc",
            "exchange": "Nasdaq", "source": "sec/company_tickers_exchange", "source_as_of": date(2026, 11, 2),
            "fetched_at": 1.0, **kw}


def _etf(ticker, aum=None, adv=None, **kw) -> dict:
    return {"ticker": ticker, "name": f"{ticker} ETF", "aum": aum, "aum_as_of": date(2026, 6, 30) if aum else None,
            "adv_usd_20d": adv, "is_etn": False, "is_cef": False, "source": "both",
            "source_as_of": date(2026, 11, 2), "fetched_at": 1.0, **kw}


@pytest.fixture
def live_my_universe():
    """Write a live ``spine.duckdb`` holding ``my_universe`` and ``my_universe_changes`` rows, as a build left them."""
    def write(rows=(), changes=()):
        from RevenueChaser.rcdata import serving
        path = serving.live_path("spine")
        path.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(path))
        try:
            base.rows_table(con, my_universe.TABLE, _plain(my_universe.SCHEMA), list(rows))
            base.rows_table(con, my_universe.CHANGES, _plain(my_universe.CHANGES_SCHEMA), list(changes))
        finally:
            con.close()
    return write


def _rows(con, **kw):
    rows, changes, issues = my_universe.rows(_ctx(), con, **kw)
    return {r["ticker"]: r for r in rows}, changes, issues


# ── The rules file and the manual file ────────────────────────────────────────────────────────────────────────

def test_the_rules_file_holds_avos_numbers_and_the_module_reads_them():
    r = my_universe.load_rules()
    assert r.stock_tiers == ("A", "D")
    assert r.stock_types == ("adr", "common", "mlp", "reit")
    assert (r.etf_aum_floor, r.etf_adv_floor) == (100_000_000.0, 10_000_000.0)
    assert r.exclude_etn is True and r.exclude_cef is True


def test_the_manual_file_ships_empty_so_the_first_build_is_the_rules_alone():
    adds, removes = my_universe.load_manual()
    assert adds == [] and removes == []


def test_a_rules_file_without_a_rule_is_refused_rather_than_defaulted(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"stocks": {"tiers": ["A"]}}), encoding="utf-8")
    with pytest.raises(ValueError, match="types"):
        my_universe.load_rules(path)


# ── Stocks ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_tier_a_common_adr_reit_and_mlp_enter_and_a_royalty_trust_bdc_or_bank_does_not():
    con = _con([_sec("MSFT"), _sec("TM", "adr"), _sec("O", "reit"), _sec("EPD", "mlp"),
                _sec("DMLP", "royalty_trust"), _sec("ARCC", "bdc"), _sec("TWNO", "bank_nonsec")])
    rows, _, _ = _rows(con)
    assert sorted(rows) == ["EPD", "MSFT", "O", "TM"]
    assert rows["EPD"]["kind"] == "stock" and rows["EPD"]["rule"] == "stock"
    assert rows["TM"]["security_type"] == "adr"


def test_tier_d_enters_and_tier_b_and_c_never_do_whatever_the_type_says():
    con = _con([_sec("TCBI", tier="D"), _sec("OTCX", tier="C"), _sec("WFC-PC", "preferred", tier="B"),
                _sec("SPY", "etp", tier="B"), _sec("BEEP", "common", tier="B")])
    rows, _, _ = _rows(con)
    assert sorted(rows) == ["TCBI"] and rows["TCBI"]["tier"] == "D"


def test_an_untyped_tier_a_name_stays_out_until_it_is_typed():
    con = _con([_sec("MSGS", None), _sec("UROY", "unknown"), _sec("MSFT")])
    rows, _, _ = _rows(con)
    assert sorted(rows) == ["MSFT"]


# ── ETFs ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_an_etf_at_or_over_the_aum_floor_enters_and_one_under_it_does_not():
    con = _con(etfs=[_etf("XLK", aum=80_000_000_000.0), _etf("FLOOR", aum=100_000_000.0),
                     _etf("CCOI", aum=50_000_000.0)])
    rows, _, _ = _rows(con)
    assert sorted(rows) == ["FLOOR", "XLK"]
    assert rows["XLK"]["rule"] == "etf_aum" and rows["XLK"]["kind"] == "etf"
    assert rows["XLK"]["security_type"] == "etf" and rows["XLK"]["aum"] == 80_000_000_000.0


def test_an_etf_with_no_aum_enters_on_a_20_session_dollar_volume_at_or_over_the_floor():
    con = _con(etfs=[_etf("SPY", adv=40_000_000_000.0), _etf("FLOOR", adv=10_000_000.0),
                     _etf("THIN", adv=9_999_999.0)])
    rows, _, _ = _rows(con)
    assert sorted(rows) == ["FLOOR", "SPY"]
    assert rows["SPY"]["rule"] == "etf_adv" and rows["SPY"]["adv_usd_20d"] == 40_000_000_000.0


def test_a_known_aum_under_the_floor_is_the_answer_and_the_volume_test_does_not_rescue_it():
    con = _con(etfs=[_etf("CCOI", aum=50_000_000.0, adv=500_000_000.0)])
    rows, _, _ = _rows(con)
    assert rows == {}


def test_an_etn_or_a_cef_never_enters_however_large_it_is():
    con = _con(etfs=[_etf("TAPR", aum=5_000_000_000.0, is_etn=True),
                     _etf("DHY", aum=5_000_000_000.0, is_cef=True),
                     _etf("BNKU", adv=50_000_000.0, is_etn=True)])
    rows, _, _ = _rows(con)
    assert rows == {}


def test_every_undecided_etf_is_one_issue_between_them_not_one_each():
    """``source_never_run``'s rule: thousands of rows for one input gap is noise, so the undecided funds are one
    issue that names them. On the live store today this would otherwise open ~4,200 rows in ops.sqlite."""
    con = _con(etfs=[_etf("NEWE"), _etf("QYLD"), _etf("XLK", aum=80_000_000_000.0)])
    rows, _, issues = _rows(con)
    assert sorted(rows) == ["XLK"]
    assert [(k, key) for k, key, _ in issues] == [("my_universe_etf_undecided", "etf")]
    assert "2 ETFs" in issues[0][2] and "NEWE, QYLD" in issues[0][2]


def test_a_leveraged_or_inverse_fund_enters_on_its_aum_like_any_other():
    con = _con(etfs=[_etf("SOXL", aum=11_000_000_000.0, is_lev_inverse=True)])
    rows, _, _ = _rows(con)
    assert sorted(rows) == ["SOXL"]


# ── The manual file ──────────────────────────────────────────────────────────────────────────────────────────

def test_with_no_aum_anywhere_n_cen_has_not_run_and_no_fund_is_called_undecided():
    """``aum`` comes from N-CEN alone. Before it has answered every fund would be "undecided", which is the
    missing source's story (``source_never_run``), not the funds'."""
    con = _con(etfs=[_etf("SPY"), _etf("GLD"), _etf("NEWE")])
    rows, _, issues = _rows(con)
    assert rows == {} and issues == []


def test_an_undecided_fund_a_manual_remove_took_out_is_not_named_undecided():
    """``my_universe_etf_undecided`` is built after the manual lines (#444): a fund avo removed by hand is gone on
    purpose, not a fund the rule cannot judge."""
    con = _con(etfs=[_etf("ZZZ"), _etf("XLK", aum=80_000_000_000.0)])
    rows, _, issues = _rows(con, manual=([], [{"ticker": "ZZZ", "reason": "avo took it out"}]))
    assert sorted(rows) == ["XLK"]
    assert issues == []


def test_an_undecided_fund_a_manual_add_let_in_is_not_named_undecided():
    """The other side of the same rule: the fund is in the list, by hand, so the rule's silence is no longer news."""
    con = _con(etfs=[_etf("ZZZ"), _etf("XLK", aum=80_000_000_000.0)])
    rows, _, issues = _rows(con, manual=([{"ticker": "ZZZ", "reason": "avo wants it"}], []))
    assert sorted(rows) == ["XLK", "ZZZ"] and rows["ZZZ"]["rule"] == "manual"
    assert issues == []


def test_the_undecided_issue_still_names_the_funds_the_manual_file_says_nothing_about():
    con = _con(etfs=[_etf("ZZZ"), _etf("WWW"), _etf("XLK", aum=80_000_000_000.0)])
    _, _, issues = _rows(con, manual=([], [{"ticker": "ZZZ", "reason": "avo took it out"}]))
    kind, key, detail = issues[0]
    assert (kind, key) == ("my_universe_etf_undecided", my_universe.ETF)
    assert detail.startswith("1 ETFs") and "WWW" in detail and "ZZZ" not in detail


def test_the_undecided_issue_names_the_first_twenty_funds_and_counts_the_rest():
    """One issue, not thousands of rows, and it names at most :data:`my_universe.MANY` of them."""
    many = [f"U{i:03d}" for i in range(my_universe.MANY + 2)]
    con = _con(etfs=[_etf(t) for t in many] + [_etf("XLK", aum=80_000_000_000.0)])
    _, _, issues = _rows(con)
    detail = issues[0][2]
    assert issues[0][0] == "my_universe_etf_undecided"
    assert detail.endswith("and 2 more") and many[my_universe.MANY - 1] in detail
    assert many[my_universe.MANY] not in detail


def test_a_name_a_manual_remove_took_out_says_so_in_the_changes_table(live_my_universe):
    """The append-only changes table told every leaver "the rules no longer hold", even one avo removed by hand,
    which is the one case where the rules were not applied at all (#444)."""
    live_my_universe(rows=[{"ticker": "SPY", "kind": "etf", "rule": "etf_aum", "entered_on": date(2026, 10, 1)}])
    con = _con(etfs=[_etf("SPY", aum=80_000_000_000.0), _etf("XLK", aum=80_000_000_000.0)])
    rows, changes, _ = _rows(con, manual=([], [{"ticker": "SPY", "reason": "avo took it out"}]))
    assert sorted(rows) == ["XLK"]
    gone = [c for c in changes if c["change"] == "left"]
    assert [(c["ticker"], c["reason"]) for c in gone] == [
        ("SPY", "taken out by hand in my_universe_manual.json: avo took it out")]


def test_a_manual_add_enters_with_its_reason_and_the_spines_own_name():
    con = _con([_sec("MSGS", None)], [_etf("NEWE")])
    rows, _, _ = _rows(con, manual=([{"ticker": "msgs", "reason": "avo: a real stock, untyped"},
                                     {"ticker": "NEWE", "reason": "avo: new fund, watch it"}], []))
    assert sorted(rows) == ["MSGS", "NEWE"]
    assert rows["MSGS"]["rule"] == "manual" and rows["MSGS"]["reason"] == "avo: a real stock, untyped"
    assert rows["MSGS"]["kind"] == "stock" and rows["MSGS"]["name"] == "MSGS Inc"
    assert rows["NEWE"]["kind"] == "etf"


def test_a_manual_remove_takes_a_name_the_rules_let_in():
    con = _con([_sec("MSFT"), _sec("TM", "adr")])
    rows, _, _ = _rows(con, manual=([], [{"ticker": "TM", "reason": "avo: not interested"}]))
    assert sorted(rows) == ["MSFT"]


def test_a_manual_remove_wins_over_a_manual_add_of_the_same_ticker():
    con = _con([_sec("MSFT")])
    rows, _, _ = _rows(con, manual=([{"ticker": "MSFT", "reason": "x"}], [{"ticker": "MSFT", "reason": "y"}]))
    assert rows == {}


def test_a_manual_line_naming_a_ticker_the_spine_does_not_hold_is_an_issue_not_a_row():
    con = _con([_sec("MSFT")])
    rows, _, issues = _rows(con, manual=([{"ticker": "NOPE", "reason": "r"}],
                                         [{"ticker": "GONE", "reason": "r"}]))
    assert sorted(rows) == ["MSFT"]
    assert sorted((k, key) for k, key, _ in issues) == [("my_universe_manual_unknown", "GONE"),
                                                        ("my_universe_manual_unknown", "NOPE")]


def test_a_manual_add_of_an_etf_security_also_holds_as_an_etp_row_is_the_etf():
    """SPY is a row of the SEC equity file too, typed ``etp`` and so Tier B. Resolving a manual add against
    ``security`` first would write it as a stock with ``security_type='etp'`` and no AUM."""
    con = _con([_sec("SPY", "etp", tier="B")], [_etf("SPY", aum=50_000_000.0)])   # under the floor: the rules say no
    rows, _, _ = _rows(con, manual=([{"ticker": "SPY", "reason": "avo: the benchmark"}], []))
    assert rows["SPY"]["kind"] == "etf" and rows["SPY"]["security_type"] == "etf"
    assert rows["SPY"]["aum"] == 50_000_000.0 and rows["SPY"]["rule"] == "manual"


def test_a_manual_add_of_an_untyped_stock_is_still_a_stock_even_with_no_etf_row():
    con = _con([_sec("MSGS", None)])
    rows, _, _ = _rows(con, manual=([{"ticker": "MSGS", "reason": "avo: a real stock"}], []))
    assert rows["MSGS"]["kind"] == "stock"


def test_a_manual_add_of_a_name_the_rules_already_let_in_keeps_the_rules_row():
    con = _con([_sec("MSFT")])
    rows, _, _ = _rows(con, manual=([{"ticker": "MSFT", "reason": "r"}], []))
    assert rows["MSFT"]["rule"] == "stock"


# ── Both kinds at once ───────────────────────────────────────────────────────────────────────────────────────

def test_a_ticker_both_rules_claim_is_the_stock_once_and_an_issue():
    con = _con([_sec("TWIN")], [_etf("TWIN", aum=500_000_000.0)])
    rows, _, issues = _rows(con)
    assert sorted(rows) == ["TWIN"] and rows["TWIN"]["kind"] == "stock"
    assert [(k, key) for k, key, _ in issues] == [("my_universe_both_kinds", "TWIN")]


# ── The changes table ────────────────────────────────────────────────────────────────────────────────────────

def test_with_no_live_file_every_name_entered_today_and_nothing_left():
    con = _con([_sec("MSFT")], [_etf("XLK", aum=80_000_000_000.0)])
    rows, changes, _ = _rows(con)
    assert {(c["ticker"], c["change"]) for c in changes} == {("MSFT", "entered"), ("XLK", "entered")}
    assert {c["as_of"] for c in changes} == {date(2026, 11, 4)}
    assert {r["entered_on"] for r in rows.values()} == {date(2026, 11, 4)}


def test_the_changes_of_the_live_file_are_carried_forward_and_todays_appended(live_my_universe):
    live_my_universe(rows=[{"ticker": "MSFT", "kind": "stock", "entered_on": date(2026, 10, 1)},
                           {"ticker": "GONE", "kind": "stock", "entered_on": date(2026, 10, 1)}],
                     changes=[{"ticker": "MSFT", "kind": "stock", "change": "entered", "reason": "stock",
                               "as_of": date(2026, 10, 1), "run_id": "old"}])
    con = _con([_sec("MSFT"), _sec("TM", "adr")])
    rows, changes, _ = _rows(con)
    assert sorted(rows) == ["MSFT", "TM"]
    assert rows["MSFT"]["entered_on"] == date(2026, 10, 1)      # it did not enter again today
    assert rows["TM"]["entered_on"] == date(2026, 11, 4)
    assert [(c["ticker"], c["change"], c["as_of"]) for c in changes] == [
        ("MSFT", "entered", date(2026, 10, 1)),
        ("GONE", "left", date(2026, 11, 4)),
        ("TM", "entered", date(2026, 11, 4))]


def test_the_build_refuses_when_the_volume_source_went_away_under_names_that_need_it(live_my_universe):
    """``my_universe`` is exempt from the swap guard's shrink limit, so an input outage would otherwise drop every
    ``etf_adv`` fund quietly and write a permanent ``left`` row. The build fails instead."""
    live_my_universe(rows=[{"ticker": "SPY", "kind": "etf", "rule": "etf_adv", "entered_on": date(2026, 10, 1)}])
    con = _con(etfs=[_etf("SPY"), _etf("XLK", aum=80_000_000_000.0)])     # no adv on any row: the prices file is gone
    with pytest.raises(ValueError, match="20-session dollar volume"):
        my_universe.rows(_ctx(), con)


def test_one_fund_losing_its_volume_is_held_even_while_another_still_measures(live_my_universe):
    """The guard is per name, not over the table (#424): a fund whose own volume went away keeps its live row and
    writes no ``left`` row, however many other funds still measure. ``adv_usd_20d`` NULL is unknown, not zero."""
    live_my_universe(rows=[{"ticker": "SPY", "kind": "etf", "security_type": "etf", "rule": "etf_adv",
                            "reason": "no AUM; 20-session dollar volume 40,000.0M USD",
                            "adv_usd_20d": 40_000_000_000.0, "entered_on": date(2026, 10, 1)}])
    con = _con(etfs=[_etf("SPY"), _etf("GLD", adv=50_000_000.0)])
    rows, changes, issues = _rows(con)
    assert sorted(rows) == ["GLD", "SPY"]
    assert rows["SPY"]["rule"] == "etf_adv" and rows["SPY"]["adv_usd_20d"] == 40_000_000_000.0
    assert rows["SPY"]["entered_on"] == date(2026, 10, 1)
    assert [(c["ticker"], c["change"]) for c in changes] == [("GLD", "entered")]
    assert [(k, key) for k, key, _ in issues] == [("my_universe_etf_volume_lost", "etf")]
    assert "SPY" in issues[0][2]


def test_a_held_fund_is_not_also_called_undecided(live_my_universe):
    """It is not a fund the rule cannot judge; it is a fund whose own number went missing, and one issue says so."""
    live_my_universe(rows=[{"ticker": "SPY", "kind": "etf", "rule": "etf_adv", "adv_usd_20d": 40_000_000_000.0,
                            "entered_on": date(2026, 10, 1)}])
    con = _con(etfs=[_etf("SPY"), _etf("GLD", aum=80_000_000_000.0), _etf("IBIT", adv=50_000_000.0)])
    _, _, issues = _rows(con)
    assert [k for k, _, _ in issues] == ["my_universe_etf_volume_lost"]


def test_a_fund_that_got_an_aum_under_the_floor_leaves_even_with_no_volume(live_my_universe):
    """The hold is only for a fund the rule would have judged **on its volume**, and the rule reads the volume only
    where N-CEN gives no AUM at all. A fund N-CEN now prices under the floor is answered by that AUM, whatever its
    volume says, so it leaves and Compustat is not blamed for it (review S1 of PR 428)."""
    live_my_universe(rows=[{"ticker": "SPY", "kind": "etf", "rule": "etf_adv", "adv_usd_20d": 4e10,
                            "entered_on": date(2026, 10, 1)}])
    con = _con(etfs=[_etf("SPY", aum=1_000_000.0), _etf("GLD", adv=50_000_000.0)])
    rows, changes, issues = _rows(con)
    assert sorted(rows) == ["GLD"]
    assert ("SPY", "left") in [(c["ticker"], c["change"]) for c in changes]
    assert issues == []


def test_a_fund_a_manual_remove_takes_out_is_not_named_as_held(live_my_universe):
    """A fund avo removed by hand is gone on purpose, so the issue does not report it as held (review N1)."""
    live_my_universe(rows=[{"ticker": "SPY", "kind": "etf", "rule": "etf_adv", "adv_usd_20d": 4e10,
                            "entered_on": date(2026, 10, 1)}])
    con = _con(etfs=[_etf("SPY"), _etf("GLD", adv=50_000_000.0)])
    rows, _, issues = _rows(con, manual=([], [{"ticker": "SPY", "reason": "avo took it out"}]))
    assert sorted(rows) == ["GLD"]
    assert issues == []


def test_the_held_issue_counts_only_the_funds_the_manual_file_left_in(live_my_universe):
    """Two funds lose their volume and avo removes one by hand: the issue names and counts the other alone. The
    single-fund test above cannot tell the filtered list from the whole one (#443)."""
    live_my_universe(rows=[{"ticker": t, "kind": "etf", "rule": "etf_adv", "adv_usd_20d": 4e10,
                            "entered_on": date(2026, 10, 1)} for t in ("GLD", "SPY")])
    con = _con(etfs=[_etf("GLD"), _etf("SPY"), _etf("IBIT", adv=50_000_000.0)])
    rows, _, issues = _rows(con, manual=([], [{"ticker": "GLD", "reason": "avo took it out"}]))
    assert sorted(rows) == ["IBIT", "SPY"]
    kind, key, detail = issues[0]
    assert (kind, key) == ("my_universe_etf_volume_lost", my_universe.ETF)
    assert detail.startswith("1 ETFs") and "SPY" in detail and "GLD" not in detail


def test_the_held_issue_names_the_first_twenty_funds_and_counts_the_rest(live_my_universe):
    """One issue, not thousands of rows: it names :data:`my_universe.MANY` tickers and says how many more (N2)."""
    many = [f"F{i:03d}" for i in range(my_universe.MANY + 3)]
    live_my_universe(rows=[{"ticker": t, "kind": "etf", "rule": "etf_adv", "adv_usd_20d": 4e10,
                            "entered_on": date(2026, 10, 1)} for t in many])
    con = _con(etfs=[_etf(t) for t in many] + [_etf("GLD", adv=50_000_000.0)])
    rows, _, issues = _rows(con)
    assert sorted(rows) == sorted(many + ["GLD"])
    detail = issues[0][2]
    assert detail.endswith("and 3 more") and many[my_universe.MANY - 1] in detail
    assert many[my_universe.MANY] not in detail


def test_a_fund_whose_volume_fell_under_the_floor_still_leaves(live_my_universe):
    """A number under the floor is the rule's answer, not an outage: only NULL where the live row had one is held."""
    live_my_universe(rows=[{"ticker": "SPY", "kind": "etf", "rule": "etf_adv", "adv_usd_20d": 40_000_000_000.0,
                            "entered_on": date(2026, 10, 1)}])
    con = _con(etfs=[_etf("SPY", adv=1_000_000.0), _etf("GLD", adv=50_000_000.0)])
    rows, changes, issues = _rows(con)
    assert sorted(rows) == ["GLD"]
    assert [(c["ticker"], c["change"]) for c in changes] == [("SPY", "left"), ("GLD", "entered")]
    assert issues == []


def test_a_fund_that_entered_on_its_aum_is_not_held_by_the_volume_guard(live_my_universe):
    """The guard protects the names that entered **on the volume**; an ``etf_aum`` name leaving is the AUM's story."""
    live_my_universe(rows=[{"ticker": "CCOI", "kind": "etf", "rule": "etf_aum", "entered_on": date(2026, 10, 1)}])
    con = _con(etfs=[_etf("CCOI"), _etf("GLD", adv=50_000_000.0)])
    rows, changes, issues = _rows(con)
    assert sorted(rows) == ["GLD"]
    assert ("CCOI", "left") in [(c["ticker"], c["change"]) for c in changes]
    assert [k for k, _, _ in issues] == []


def test_a_ticker_the_stock_rule_already_kept_is_not_overwritten_by_the_held_row(live_my_universe):
    """A ticker that is already in today's list stays the row the rules wrote; the hold is only for a name that
    would otherwise be dropped."""
    live_my_universe(rows=[{"ticker": "SPY", "kind": "etf", "rule": "etf_adv", "adv_usd_20d": 4e10,
                            "entered_on": date(2026, 10, 1)}])
    con = _con([_sec("SPY")], [_etf("SPY"), _etf("GLD", adv=50_000_000.0)])
    rows, _, issues = _rows(con)
    assert rows["SPY"]["kind"] == "stock" and rows["SPY"]["rule"] == "stock"
    assert [k for k, _, _ in issues] == []


def test_a_fund_the_etf_table_no_longer_holds_at_all_is_not_held_by_this_guard(live_my_universe):
    """A vanished ``etf`` row is the ETF universe's own source failing, wider than this guard: holding the row would
    keep a name alive on no evidence. It leaves, and the swap guard's "may not empty" rule is what is left."""
    live_my_universe(rows=[{"ticker": "SPY", "kind": "etf", "rule": "etf_adv", "adv_usd_20d": 4e10,
                            "entered_on": date(2026, 10, 1)}])
    con = _con(etfs=[_etf("GLD", adv=50_000_000.0)])
    rows, changes, issues = _rows(con)
    assert sorted(rows) == ["GLD"]
    assert ("SPY", "left") in [(c["ticker"], c["change"]) for c in changes]
    assert issues == []


def test_every_held_fund_is_one_issue_between_them_naming_them_all(live_my_universe):
    live_my_universe(rows=[{"ticker": t, "kind": "etf", "rule": "etf_adv", "adv_usd_20d": 5e10,
                            "entered_on": date(2026, 10, 1)} for t in ("GLD", "SLV", "SPY")])
    con = _con(etfs=[_etf("GLD"), _etf("SLV"), _etf("SPY"), _etf("IBIT", adv=50_000_000.0)])
    rows, _, issues = _rows(con)
    assert sorted(rows) == ["GLD", "IBIT", "SLV", "SPY"]
    assert len(issues) == 1
    kind, key, detail = issues[0]
    assert (kind, key) == ("my_universe_etf_volume_lost", "etf")
    assert "GLD" in detail and "SLV" in detail and "SPY" in detail and "3" in detail


def test_a_live_table_written_before_a_column_existed_is_read_anyway():
    """The build reads its whole live row to carry a fund forward, so it asks for today's columns of a file an
    older build wrote. A column that file does not hold is NULL, not a broken build."""
    from RevenueChaser.rcdata import serving
    path = serving.live_path("spine")
    path.parent.mkdir(parents=True, exist_ok=True)
    old = duckdb.connect(str(path))
    try:                                                        # the four columns the build read before #424
        base.rows_table(old, my_universe.TABLE,
                        [("ticker", "TEXT"), ("kind", "TEXT"), ("rule", "TEXT"), ("entered_on", "DATE")],
                        [{"ticker": "SPY", "kind": "etf", "rule": "etf_adv", "entered_on": date(2026, 10, 1)}])
        base.rows_table(old, my_universe.CHANGES, _plain(my_universe.CHANGES_SCHEMA), [])
    finally:
        old.close()
    con = _con(etfs=[_etf("SPY"), _etf("GLD", adv=50_000_000.0)])
    rows, changes, issues = _rows(con)
    assert sorted(rows) == ["GLD", "SPY"]
    assert rows["SPY"]["adv_usd_20d"] is None                   # the old file holds no such column
    assert rows["SPY"]["entered_on"] == date(2026, 10, 1)
    assert [(c["ticker"], c["change"]) for c in changes] == [("GLD", "entered")]
    assert [k for k, _, _ in issues] == ["my_universe_etf_volume_lost"]


def test_a_name_that_left_names_the_rule_it_held_under(live_my_universe):
    """The build cannot know why a name left (its source row is simply gone); it names the rule it held under."""
    live_my_universe(rows=[{"ticker": "CCOI", "kind": "etf", "rule": "etf_aum", "entered_on": date(2026, 10, 1)}])
    con = _con(etfs=[_etf("CCOI", aum=50_000_000.0)])
    rows, changes, _ = _rows(con)
    assert rows == {}
    assert [(c["ticker"], c["change"], c["reason"]) for c in changes] == [
        ("CCOI", "left", "the rules no longer hold (was etf_aum)")]
