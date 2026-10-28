"""``spine/security`` over every source of the fixture: the plan's 10 names end to end (tier, link, CUSIP, ISIN,
FIGI, GICS, index membership with its periods) and the build issues written to ops.sqlite once the file is live."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.spine import funds, openfigi_map, sec_tickers, security, tier, wrds

from . import spine_fixture as fx


@pytest.fixture
def built(monkeypatch, http_mock):
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
    yield http_mock
    wrds.reset()


def run_all(day: str, names=None) -> list[dict]:
    out = []
    for name in names or (sec_tickers.NAME, wrds.SEC_IDHIST.name, wrds.CO_HGIC.name, wrds.IDXCST_HIS.name,
                          openfigi_map.NAME, funds.NAME, security.NAME):
        attended = name.startswith("wrds/")
        params = {"mode": "backfill", "to": day} if attended else {"as_of": day}
        out.append(base.run_request({"dataset": name, "requested_by": "cli", "params": params}))
    return out


def _q(sql: str, *args) -> list[tuple]:
    with serving.open_read("spine") as con:
        return con.execute(sql, list(args)).fetchall()


def _sec(ticker: str) -> dict:
    with serving.open_read("spine") as con:
        cur = con.execute("SELECT * FROM security WHERE ticker = ?", [ticker])
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, cur.fetchone()))


def _issues() -> set[tuple[str, str]]:
    return {(i["kind"], i["key"]) for i in ops.open_issues(security.NAME, limit=500)[1]}


def test_the_ten_names_tier_and_link(built):
    want = {"MSFT": ("A", "common", "012141", "01", "tic"), "HEI": ("A", "common", "005567", "01", "tic"),
            "HEI-A": ("A", "common", "005567", "02", "tic"), "ETN": ("A", "common", "016478", "01", "tic"),
            "TM": ("A", "adr", "010581", "90", "tic"), "CNA": ("A", "common", "002968", "01", "tic"),
            "FDX": ("A", "common", "004598", "01", "tic"), "NVR": ("A", "unknown", "025648", "01", "tic"),
            "WFC-PC": ("B", "preferred", None, None, None), "VYLR": ("A", "common", "040001", "01", "tic")}
    for t, (tr, typ, gvkey, iid, how) in want.items():
        s = _sec(t)
        assert (s["tier"], s["security_type"], s["gvkey"], s["iid"], s["link_source"]) == (tr, typ, gvkey, iid, how), t
    assert {_sec(t)["tier"] for t in ("SPY",)} == {"B"} and _sec("OTCX")["tier"] == "C" and _sec("NOEX")["tier"] == "D"


def test_identifiers_and_gics_of_the_ten_names(built):
    msft = _sec("MSFT")
    assert (msft["cusip"], msft["isin"], msft["figi"], msft["gsubind"]) == (
        "594918104", "US5949181045", "BBG000BPH459", "45103020")
    assert (msft["sector"], msft["industry"]) == ("Technology", "Software & Services")
    assert _sec("HEI-A")["cusip"] == "422806208" and _sec("HEI")["cusip"] == "422806109"
    etn = _sec("ETN")
    assert etn["cusip"] == "G29183103" and etn["isin"] is None          # a CINS: no US ISIN is derived
    assert _sec("NVR")["figi"] is None and _sec("NVR")["cusip"] == "62944T105"
    assert _sec("TM")["sector"] == "Consumer Cyclical"
    assert _sec("WFC-PC")["cusip"] is None


def test_history_tables(built):
    assert _q("SELECT valid_from, valid_to FROM gics_history WHERE gvkey = '005567' ORDER BY valid_from") == [
        (date(1999, 1, 1), date(2016, 1, 1)), (date(2016, 1, 1), None)]
    assert ("HEI-A", "tic", "HEI.A") in _q("SELECT ticker, field, value FROM security_history WHERE gvkey = '005567'")
    assert _q("SELECT count(*) FROM security_history WHERE gvkey = '777777'") == [(0,)]   # not a linked company
    assert ("MSFT", "exchange", "Nasdaq", date(2026, 10, 27), None) in _q(
        "SELECT ticker, field, value, valid_from, valid_to FROM security_history WHERE source = ?", sec_tickers.NAME)


def test_membership_from_the_fund_files_matched_by_cusip_ticker_and_squashed_ticker(built):
    rows = _q("SELECT index_slug, ticker, valid_from, valid_to, files_held, files_counted FROM index_membership "
              "WHERE source = ? ORDER BY index_slug, ticker", funds.NAME)
    assert rows == [
        ("ndx", "MSFT", date(2026, 10, 27), None, 1, 1),
        ("sp400", "HEI", date(2026, 10, 27), None, 3, 3),
        ("sp400", "HEI-A", date(2026, 10, 27), None, 3, 3),      # iShares' HEIA, squashed back to HEI-A
        ("sp500", "ETN", date(2026, 10, 27), None, 3, 3),
        ("sp500", "FDX", date(2026, 10, 27), None, 3, 3),        # Vanguard's line has only the CUSIP
        ("sp500", "MSFT", date(2026, 10, 27), None, 3, 3),
        ("sp500", "NVR", date(2026, 10, 27), None, 3, 3),
        ("sp500", "WFC", date(2026, 10, 27), None, 3, 3)]


def _funds_answer(http_mock, **kw) -> None:
    """Replace the fund-file routes (the SEC and OpenFIGI routes stay)."""
    http_mock.routes[:] = [r for r in http_mock.routes if r[0] in ("company_tickers_exchange.json",
                                                                   "figi.test/v3/mapping")]
    fx.add_fund_routes(http_mock, **kw)


def test_membership_periods_open_close_and_reopen(built):
    _funds_answer(built, sp500=[h for h in fx.SP500 if h[0] != "NVR"])
    run_all("2026-10-28", names=(funds.NAME,))
    _funds_answer(built)
    run_all("2026-10-29", names=(funds.NAME,))
    assert _q("SELECT valid_from, valid_to FROM index_membership WHERE source = ? AND index_slug = 'sp500' "
              "AND ticker = 'NVR' ORDER BY valid_from", funds.NAME) == [
        (date(2026, 10, 27), date(2026, 10, 28)), (date(2026, 10, 29), None)]


def test_a_day_without_a_counting_file_is_no_absence(built):
    _funds_answer(built, fail=("SPY", "IVV", "VOO"))
    run_all("2026-10-28", names=(funds.NAME,))
    assert _q("SELECT valid_from, valid_to FROM index_membership WHERE source = ? AND index_slug = 'sp500' "
              "AND ticker = 'NVR'", funds.NAME) == [(date(2026, 10, 27), None)]


def test_files_that_disagree_vote_and_are_an_issue(built):
    _funds_answer(built, sp500=[h for h in fx.SP500 if h[0] != "NVR"])
    built.routes.insert(0, ("fileName=IVV_holdings", lambda r: (200, fx.ishares_csv(fx.SP500), {}), 200, {}))
    run_all("2026-10-28", names=(funds.NAME,))
    assert _q("SELECT valid_to, files_held, files_counted FROM index_membership WHERE source = ? "
              "AND index_slug = 'sp500' AND ticker = 'NVR'", funds.NAME) == [(date(2026, 10, 28), 3, 3)]
    assert ("membership_files_disagree", "sp500:NVR") in _issues()


def test_a_line_whose_cusip_conflicts_with_the_tickers_known_cusip_is_no_match(built):
    """Should-fix 4a: a WFC line carrying another issuer's CUSIP (written before the ticker was reused) is not WFC."""
    sp500 = [h for h in fx.SP500 if h[0] != "WFC"] + [("WFC", "999999999", "WFC OLD ISSUER")]
    _funds_answer(built, sp500=sp500)
    run_all("2026-10-28", names=(funds.NAME,))
    assert _q("SELECT valid_from, valid_to FROM index_membership WHERE source = ? AND index_slug = 'sp500' "
              "AND ticker = 'WFC'", funds.NAME) == [(date(2026, 10, 27), date(2026, 10, 28))]
    assert ("member_not_in_sec", "sp500:WFC") in _issues()


def test_an_acquired_member_keeps_its_history_after_it_leaves_the_sec_file(built):
    """Should-fix 4b: each day's lines match that day's SEC snapshot, so NVR's period survives NVR leaving."""
    without_nvr = dict(fx.SEC_FILE, data=[r for r in fx.SEC_FILE["data"] if r[2] != "NVR"])
    built.routes[:] = [r for r in built.routes if r[0] != "company_tickers_exchange.json"]
    built.add("company_tickers_exchange.json", without_nvr)
    _funds_answer(built, sp500=[h for h in fx.SP500 if h[0] != "NVR"])
    # One row of 14 leaving is over the swap's 1% shrink guard: in this tiny fixture the drop is allowed.
    base.run_request({"dataset": sec_tickers.NAME, "params": {"as_of": "2026-11-03", "allow_shrink": True}})
    run_all("2026-11-03", names=(funds.NAME,))
    assert _q("SELECT valid_from, valid_to FROM index_membership WHERE source = ? AND index_slug = 'sp500' "
              "AND ticker = 'NVR'", funds.NAME) == [(date(2026, 10, 27), date(2026, 11, 3))]
    assert _q("SELECT count(*) FROM security WHERE ticker = 'NVR'") == [(0,)]


def test_wrds_constituents_are_kept_with_exclusive_ends(built):
    assert _q("SELECT ticker, valid_from, valid_to FROM index_membership WHERE source = ? AND index_slug = 'sp400' "
              "ORDER BY ticker", wrds.IDXCST_HIS.name) == [
        ("HEI", date(2010, 1, 4), date(2026, 10, 3)), ("HEI-A", date(2010, 1, 4), None)]


def test_a_reused_ticker_does_not_give_an_old_members_history_to_its_new_owner(built):
    """Review should-fix 3: gvkey 123456 left the S&P 500 in 2009 and still carries tic VYLR; VYLR is now another
    CIK. Its constituent rows keep their gvkey and no ticker, and VYLR has no S&P 500 history."""
    assert _q("SELECT ticker, cik, valid_from, valid_to FROM index_membership WHERE gvkey = '123456'") == [
        (None, None, date(2001, 3, 1), date(2009, 7, 1))]
    from RevenueChaser.rcdata import read
    assert "VYLR" not in {r["ticker"] for r in read.membership("sp500", on=date(2008, 6, 30),
                                                                 source=wrds.IDXCST_HIS.name)}


def test_build_issues_name_the_plans_problems(built):
    issues = _issues()
    assert ("tier_a_no_figi", "NVR") in issues                     # OpenFIGI: absent
    assert ("cik_many_gvkeys", "21175") not in issues               # CNA: the ticker match decided
    assert ("membership_wrds_differs", "sp400:HEI") in issues       # left the MidCap in Compustat, still in the files
    assert not {k for k, _ in issues} & {"source_never_run", "tier_a_no_cusip", "tier_a_no_wrds_link"}
    assert ("member_not_tier_a", "sp500:WFC") not in issues and _sec("WFC")["tier"] == "A"


def test_a_dropped_cusip_is_a_build_issue_and_coming_back_resolves_it(built, monkeypatch):
    msft = [s for s in fx.SECURITY if s["tic"] == "MSFT"][0]
    monkeypatch.setitem(msft, "cusip", None)
    for p in wrds.parquet_dir(wrds.SEC_IDHIST.name).glob("snapshot=*"):
        for f in p.iterdir():
            f.unlink()
        p.rmdir()
    for p in wrds.raw_dir(wrds.SEC_IDHIST.name).glob("*"):
        for f in p.iterdir():
            f.unlink()
    row = base.run_request({"dataset": wrds.SEC_IDHIST.name, "requested_by": "cli",
                            "params": {"mode": "backfill", "to": "2026-10-28"}})
    assert row["result"] == "changed"
    assert ("tier_a_no_cusip", "MSFT") in _issues()
    monkeypatch.setitem(msft, "cusip", "594918104")
    base.run_request({"dataset": wrds.SEC_IDHIST.name, "requested_by": "cli",
                      "params": {"mode": "backfill", "to": "2026-12-01"}})
    assert ("tier_a_no_cusip", "MSFT") not in _issues()


def test_a_source_that_never_ran_is_one_issue_not_thousands(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    http_mock.add("company_tickers_exchange.json", fx.SEC_FILE)
    base.run_request({"dataset": sec_tickers.NAME, "params": {"as_of": "2026-10-27"}})
    kinds = {k for k, _ in _issues()}
    assert kinds == {"source_never_run"}
    assert {key for _, key in _issues()} == {wrds.SEC_IDHIST.name, wrds.CO_HGIC.name, wrds.IDXCST_HIS.name,
                                             openfigi_map.NAME, funds.NAME}


def test_coverage_is_tier_a_with_a_cusip(built):
    check = ops.latest_check(security.NAME)
    tier_a = _q("SELECT count(*), count(cusip) FROM security WHERE tier = 'A'")[0]
    assert check["coverage"] == round(tier_a[1] / tier_a[0], 4)


# ── The rule itself ──────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("exchange, typ, want", [
    ("Nasdaq", "common", "A"), ("NYSE", None, "A"), ("CBOE", "unknown", "A"), ("NYSE", "adr", "A"),
    ("NYSE", "etp", "B"), ("Nasdaq", "spac", "B"), ("NYSE", "preferred", "B"), ("NYSE", "warrant", "B"),
    ("OTC", "common", "C"), ("OTC", "etp", "C"), (None, "common", "D"), ("", None, "D"), ("LSE", "common", "D")])
def test_tier_rule_v1(exchange, typ, want):
    assert tier.tier(exchange, typ) == want


def test_the_type_comes_from_the_identity_rules():
    assert tier.security_type("WFC-PC", "WELLS FARGO", None)[0] == "preferred"
    assert tier.security_type("TM", "TOYOTA", {"security_type": "ADR"})[0] == "adr"
    assert tier.security_type("XYZ", "ACME ACQUISITION CORP", None)[0] == "spac"      # the name rule
    assert tier.security_type("SPY", "SPDR", {"security_type": "ETP"})[0] == "etp"


def test_the_match_of_a_holding_line():
    by_cusip, cusip_of, tickers = {"31428X106": "FDX"}, {"FDX": "31428X106"}, {"FDX", "BRK-B", "HEI-A"}
    m = lambda t, c: security.match_holding({"ticker": t, "cusip": c}, by_cusip, cusip_of, tickers,  # noqa: E731
                                            security.squash(tickers))
    assert m(None, "31428X106") == "FDX" and m("BRKB", None) == "BRK-B" and m("NOPE", None) is None
    assert m("FDX", "000000000") is None                  # a CUSIP that is not FDX's known one: no match (4a)
    assert m("HEI-A", "000000000") == "HEI-A"             # no known CUSIP for the ticker: the ticker decides


def test_two_tickers_that_squash_alike_match_neither():
    tickers = {"AB-C", "ABC", "A-BC"}
    squashed = security.squash(tickers)
    assert squashed["ABC"] is None
    assert security.match_holding({"ticker": "ABC", "cusip": None}, {}, {}, tickers, squashed) == "ABC"  # exact
    assert security.match_holding({"ticker": "A-B-C", "cusip": None}, {}, {}, tickers, squashed) is None
