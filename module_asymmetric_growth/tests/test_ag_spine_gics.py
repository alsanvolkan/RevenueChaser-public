"""R5 (#88), #266: the Growth Hunter reads the spine's sector and industry and skips the identity master's excluded types."""
from __future__ import annotations

from RevenueChaser.core import rawstore
from RevenueChaser.module_asymmetric_growth import engine
from RevenueChaser.module_asymmetric_growth.tests.test_score_ticker import _fin, _tech


def _seed(rows, master=None):
    rawstore.replace_all("equity_universe", [{
        "ticker": t, "cik": "", "name": t, "market_cap": 1e9, "price": 10.0,
        "sector": sec, "industry": ind, "quote": {"symbol": t}} for t, sec, ind in rows])
    if master:
        rawstore.replace_all("security_master", [
            {"ticker": t, "security_type": ty} for t, ty in master.items()])


def test_card_sector_and_industry_are_the_spine_not_yfinance(rawstore_tmp_db):
    _seed([("TEST", "Healthcare", "Biotechnology")])
    fin = _fin()
    fin["info"]["industry"] = "Software"                                       # the vendor says Software
    card = engine.score_ticker("TEST", fin, _tech(), {"sector": "Technology"})   # vendor says Technology
    assert card["sector"] == "Healthcare"
    assert card["industry"] == "Biotechnology"


def test_card_has_no_gics_group_or_sub_industry_keys(rawstore_tmp_db):
    _seed([("TEST", "Healthcare", "Biotechnology")])
    card = engine.score_ticker("TEST", _fin(), _tech())
    assert "industry_group" not in card and "sub_industry" not in card


def test_card_without_a_spine_row_is_unclassified(rawstore_tmp_db):
    _seed([("OTHR", "Energy", "Oil & Gas E&P")])
    card = engine.score_ticker("TEST", _fin(), _tech(), {"sector": "Technology"})
    assert (card["sector"], card["industry"]) == ("Unclassified", "Unclassified")


def test_card_with_a_blank_spine_industry_reads_unclassified(rawstore_tmp_db):
    _seed([("TEST", "Technology", "")])
    card = engine.score_ticker("TEST", _fin(), _tech())
    assert (card["sector"], card["industry"]) == ("Technology", "Unclassified")


def test_themes_are_the_pinned_run_never_the_vendor_industry_text(rawstore_tmp_db):
    """C21 (#383): the card's themes are the pinned medium labels; the vendor's free text no longer matches any."""
    _seed([("TEST", "Technology", "Software - Application")])
    rawstore.write_rows("theme_runs", [{"id": "r1", "created_at": "x", "k_fine": 1, "k_medium": 1,
                                        "k_coarse": 1, "jaccard": 1.0, "is_current": 1}])
    rawstore.write_rows("ticker_themes", [{"ticker": "TEST", "theme_id": "r1:m0", "theme": "Memory",
                                           "level": "medium", "theme_run_id": "r1"}])
    fin = _fin()
    fin["info"]["industry"] = "Airlines"        # a keyword the old matcher would have labelled "Airlines"
    card = engine.score_ticker("TEST", fin, _tech())
    assert card["industry"] == "Software - Application"     # still the spine's, not the vendor's (#266)
    assert card["themes"] == ["Memory"]


def test_hunter_skips_excluded_types(rawstore_tmp_db, monkeypatch):
    _seed([("OPCO", "Energy", "Oil & Gas E&P"), ("FUND", "Financial Services", "Asset Management")],
          master={"OPCO": "common", "FUND": "etp"})
    rows = [{"ticker": "OPCO"}, {"ticker": "FUND"}]
    monkeypatch.setattr(engine, "fetch_universe_stats", lambda *a, **k: (rows, {}))
    scored = []
    monkeypatch.setattr(engine, "enrich_and_score",
                        lambda t, r, nf=False, *a, **k: scored.append(t))
    import RevenueChaser.module_sec_13f.engine as s13
    monkeypatch.setattr(s13, "load_totals", lambda: {})
    out = engine.run_asymmetric_growth_hunter()
    assert scored == ["OPCO"]
    assert out["universe_total"] == 2
    assert out["summary"]["excluded_types"] == 1


def test_no_spine_gics_counts_the_unclassified_sector(rawstore_tmp_db, monkeypatch):
    """``no_spine_gics`` stays the count of cards whose sector is ``Unclassified``."""
    _seed([("OPCO", "Energy", "Oil & Gas E&P"), ("NOPE", "", ""), ("HALF", "Technology", "")])
    rows = [{"ticker": "OPCO"}, {"ticker": "NOPE"}, {"ticker": "HALF"}]
    monkeypatch.setattr(engine, "fetch_universe_stats", lambda *a, **k: (rows, {}))

    def fake_score(t, r, nf=False, spine=None, preload=None):
        return engine.score_ticker(t, _fin(), _tech(), {}, spine)

    monkeypatch.setattr(engine, "enrich_and_score", fake_score)
    import RevenueChaser.module_sec_13f.engine as s13
    monkeypatch.setattr(s13, "load_totals", lambda: {})
    out = engine.run_asymmetric_growth_hunter()
    by = {c["ticker"]: c for c in out["cards"]}
    assert (by["OPCO"]["sector"], by["OPCO"]["industry"]) == ("Energy", "Oil & Gas E&P")
    assert (by["NOPE"]["sector"], by["NOPE"]["industry"]) == ("Unclassified", "Unclassified")
    assert (by["HALF"]["sector"], by["HALF"]["industry"]) == ("Technology", "Unclassified")
    assert out["summary"]["no_spine_gics"] == 1                     # the sector decides, not the industry
