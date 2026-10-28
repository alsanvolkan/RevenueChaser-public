"""``rcdata.read.bars`` / ``actions`` / ``shares`` / ``market_cap`` / ``security_ids``: the prices store's read API,
over the store ``prices/serving`` builds from the fixture week of ``test_prices_serving`` (FLG's 1-for-3 reverse split
ex 22 Oct, AAPL's 0.26 dividend ex 23 Oct). The factors are applied on read; nothing stored is adjusted."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.rcdata import ops, read

from .test_prices_serving import AAPL, FLG, M, built, env  # noqa: F401 - the built prices store of the week


def test_nothing_built_yet_is_no_rows():
    assert read.bars([AAPL]) == [] and read.actions([AAPL]) == [] and read.shares([AAPL]) == []
    assert read.market_cap([AAPL]) == [] and read.security_ids(["AAPL"]) == {}
    assert read.latest_market(["AAPL"]) == {}


@pytest.mark.usefixtures("built")
def test_security_ids_of_tickers():
    assert read.security_ids(["aapl", "FLG", "NOPE"]) == {"AAPL": AAPL, "FLG": FLG}


@pytest.mark.usefixtures("built")
def test_bars_nominal_split_and_total_return_apply_the_factors_on_read():
    nominal = {r["session_date"]: r for r in read.bars([FLG, AAPL], basis="nominal") if r["security_id"] == FLG}
    split = {r["session_date"]: r for r in read.bars([FLG], basis="split")}
    tr = {r["session_date"]: r for r in read.bars([AAPL], basis="total_return")}
    day = date(2026, 10, 21)
    assert nominal[day]["close"] == 12.1 and nominal[day]["volume"] == 1_000_000
    assert split[day]["close"] == pytest.approx(12.1 * 3) and split[day]["volume"] == pytest.approx(1_000_000 / 3)
    assert split[day]["split_factor"] == 3 and split[day]["basis"] == "split"
    assert split[date(2026, 10, 22)]["close"] == 36.6                     # on and after the ex-date: as traded
    assert tr[date(2026, 10, 22)]["close"] == pytest.approx(200.0 * M)
    assert tr[date(2026, 10, 23)]["close"] == 199.9
    assert {r["status"] for r in tr.values()} == {ops.latest_check("alpaca/bars")["result"]}
    assert {r["source"] for r in tr.values()} == {"alpaca/bars"}


@pytest.mark.usefixtures("built")
def test_bars_are_bounded_by_start_and_end_and_refuse_an_unknown_basis():
    rows = read.bars([AAPL], start=date(2026, 10, 22), end="2026-10-23")
    assert [r["session_date"] for r in rows] == [date(2026, 10, 22), date(2026, 10, 23)]
    assert read.bars([]) == []
    with pytest.raises(ValueError, match="basis"):
        read.bars([AAPL], basis="adjusted")


@pytest.mark.usefixtures("built")
def test_actions_shares_and_market_cap_by_security():
    acts = read.actions([FLG])
    assert [(a["action"], a["ex_date"], a["applied"]) for a in acts] == [("split", date(2026, 10, 22), True)]
    out = read.shares([AAPL], measure="outstanding")
    assert [(r["value"], r["as_of"], r["source"]) for r in out] == [(14_800_000_000, date(2026, 10, 17),
                                                                      "sec/cover_shares")]
    assert {r["measure"] for r in read.shares([AAPL])} == {"outstanding", "float"}
    caps = read.market_cap([AAPL], start=date(2026, 10, 27))
    assert [(r["session_date"], r["market_cap"]) for r in caps] == [(date(2026, 10, 27), 202.0 * 14_800_000_000)]


@pytest.mark.usefixtures("built")
def test_latest_market_is_each_tickers_newest_close_and_cap():
    """The universe's market fields: each ticker's newest nominal close and its market cap, the class's own and the
    issuer's sum over its listed classes (GOOGL and GOOG are one company)."""
    out = read.latest_market(["aapl", "GOOGL", "GOOG", "NOPE"])
    assert set(out) == {"AAPL", "GOOGL", "GOOG"}
    a = out["AAPL"]
    assert (a["security_id"], a["price"], a["price_session"]) == (AAPL, 202.0, date(2026, 10, 27))
    assert a["market_cap"] == a["issuer_market_cap"] == 202.0 * 14_800_000_000
    assert a["cap_session"] == date(2026, 10, 27)
    assert out["GOOGL"]["market_cap"] == pytest.approx(255.0 * 5_800_000_000)
    assert out["GOOGL"]["issuer_market_cap"] == pytest.approx(255.0 * 5_800_000_000 + 256.0 * 5_450_000_000)
    assert read.latest_market([]) == {}
