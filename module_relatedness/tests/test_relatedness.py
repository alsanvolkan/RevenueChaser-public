"""Relatedness core — profiles, similarity, engine (no network)."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from RevenueChaser.core import rawstore
from RevenueChaser.module_comovement.tests import prices_bars
from RevenueChaser.module_relatedness import config, engine, profiles, similarity


# ── profiles ────────────────────────────────────────────────────────────────
def test_parse_profile_cleans_and_limits():
    raw = json.dumps({
        "products": ["AI Accelerators", "gpu ", "GPU", ""],
        "end_markets": ["Data Center", "gaming"],
        "business_model": 7,
        "keywords": ["cuda"],
    })
    p = profiles.parse_profile(raw)
    assert p["products"] == ["ai accelerators", "gpu"]     # lowercased, de-duped
    assert p["end_markets"] == ["data center", "gaming"]
    assert p["business_model"] == []                       # wrong type -> empty
    assert set(p) == set(config.PROFILE_FIELDS)


def test_parse_profile_bad_json_is_empty():
    p = profiles.parse_profile("not json")
    assert all(p[f] == [] for f in config.PROFILE_FIELDS)


# ── similarity ──────────────────────────────────────────────────────────────
def _prof(**kw):
    base = {f: [] for f in config.PROFILE_FIELDS}
    base.update(kw)
    return base


def test_identical_profiles_score_one_and_unrelated_zero():
    import pytest
    a = _prof(products=["gpu"], end_markets=["data center"])
    b = _prof(products=["gpu"], end_markets=["data center"])
    c = _prof(products=["bread"], end_markets=["grocery"])
    assert similarity.score_pair(a, b)[0] == pytest.approx(1.0)
    assert similarity.score_pair(a, c)[0] == 0.0


def test_shared_terms_reported():
    a = _prof(products=["gpu", "cpu"], end_markets=["data center"])
    b = _prof(products=["gpu"], end_markets=["data center"])
    score, shared = similarity.score_pair(a, b)
    assert "gpu" in shared and "data center" in shared and score > 0


def test_percentile_bounds():
    assert similarity.percentile(0.5, [0.1, 0.2, 0.9]) == 66.7
    assert similarity.percentile(0.5, []) == 0.0


# ── engine ──────────────────────────────────────────────────────────────────
def _seed_universe(tickers):
    rawstore.replace_all(engine._UNIVERSE, [{
        "ticker": t, "cik": "", "name": t + " Inc", "market_cap": 1.0e10,
        "price": 10.0, "sector": "Technology", "industry": "Semiconductors",
        "quote": {"symbol": t},
    } for t in tickers])


# Item 1 texts a test seeds have to clear the profile floor (#313, ``config.MIN_INPUT_CHARS``): the engine never
# sends a shorter one to the writer. ``long_body`` pads a distinctive sentence past it; an empty text stays empty,
# because that is the ``no_text`` case.
_PAD = "This annual report describes the company's business in detail. " * 40


def long_body(text: str) -> str:
    return f"{text} {_PAD}" if text else text


def _seed_filings(tickers):
    rawstore.write_rows("chain_filings", [{
        "ticker": t, "cik": "1", "form": "10-K", "filed": "2026-02-01",
        "accession": "acc-" + t, "primary_doc": "http://x",
        "item_text": long_body("We design gpu accelerators for data center customers."),
        "quality": "ok", "fetched_at": 1.0,
    } for t in tickers])


def _seed_ohlc(prices: pd.DataFrame):
    rows = []
    for t in prices.columns:
        for ts, p in prices[t].items():
            rows.append({"ticker": t, "ts": ts.isoformat(), "open": float(p),
                         "high": float(p), "low": float(p), "close": float(p),
                         "volume": 1.0, "cached_at": 1.0})
    prices_bars.write_bars(rows)


def _correlated_prices(n: int = 260, seed: int = 7) -> pd.DataFrame:
    """Two names sharing both a market and an idiosyncratic common factor."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-02", periods=n, freq="B")
    market = rng.normal(0.0003, 0.010, n)
    common = rng.normal(0.0002, 0.008, n)
    nvda = market + common + rng.normal(0.0, 0.002, n)
    amd = market + common + rng.normal(0.0, 0.002, n)
    spy = market + rng.normal(0.0, 0.001, n)

    def px(r):
        return 100.0 * np.exp(np.cumsum(r))

    return pd.DataFrame({"NVDA": px(nvda), "AMD": px(amd), "SPY": px(spy)}, index=idx)


def test_select_pilot_is_the_shared_universe():
    from RevenueChaser.core.universe import classification_universe
    tickers = ["NVDA", "AMD", "TSM", "AAPL"]
    _seed_universe(tickers)
    pilot = engine.select_pilot()
    assert pilot == classification_universe()
    assert engine.select_pilot(limit=2) == pilot[:2]


def test_run_no_fetch_reads_cached_profiles(rawstore_tmp_db, monkeypatch, tmp_path):
    tickers = ["NVDA", "AMD", "TSM"]
    _seed_universe(tickers)
    _seed_filings(tickers)
    monkeypatch.setattr(engine.config, "RELATEDNESS_JSON", tmp_path / "r.json")
    # pre-cache profiles in the durable store
    from RevenueChaser.module_relatedness import store
    for t, prof in {
        "NVDA": _prof(products=["gpu"], end_markets=["data center"]),
        "AMD": _prof(products=["gpu"], end_markets=["data center"]),
        "TSM": _prof(products=["foundry"], end_markets=["semiconductors"]),
    }.items():
        store.save_profile(t, "cik-" + t, prof, "k", "m")
    monkeypatch.setattr(engine.vc_engine, "fetch_corpus",
                        lambda *a, **k: {"no_fetch": True})

    payload = engine.run(no_fetch=True, tickers=tickers)
    assert payload["n"] == 3
    assert "universe_total" in payload
    assert payload["companies"][0].get("index") is not None
    peers = {p["peer"] for p in payload["peers"]["NVDA"]}
    assert "AMD" in peers
    nvda_amd = next(p["score"] for p in payload["peers"]["NVDA"] if p["peer"] == "AMD")
    assert nvda_amd > 0
    # zero-cosine peers are omitted (the peer matrix is sparse)
    assert "TSM" not in peers


def test_peer_move_hides_when_prices_missing(rawstore_tmp_db):
    _seed_universe(["NVDA", "AMD"])
    peers = {
        "NVDA": [{"peer": "AMD", "score": 1.0, "percentile": 100, "shared": []}],
        "AMD": [{"peer": "NVDA", "score": 1.0, "percentile": 100, "shared": []}],
    }
    engine._attach_peer_market(peers)
    for entries in peers.values():
        for p in entries:
            assert p.get("move") is None
            assert p.get("beyond_spy") is None
            assert p.get("cluster") is None


def test_peer_move_from_ohlc(rawstore_tmp_db):
    _seed_universe(["NVDA", "AMD", "SPY"])
    _seed_ohlc(_correlated_prices())
    peers = {
        "NVDA": [{"peer": "AMD", "score": 1.0, "percentile": 100, "shared": []}],
        "AMD": [{"peer": "NVDA", "score": 1.0, "percentile": 100, "shared": []}],
    }
    engine._attach_peer_market(peers)
    p = peers["NVDA"][0]
    assert p.get("move") is not None
    assert set(p["move"].keys()) == {1, 3, 5, 10}
    assert all(-1.0 <= float(v) <= 1.0 for v in p["move"].values())
    assert float(p["move"][5]) > 0.3
    assert p.get("beyond_spy") is not None
    assert -1.0 <= float(p["beyond_spy"]) <= 1.0
    assert float(p["beyond_spy"]) > 0.3


def test_companies_carry_theme_levels(rawstore_tmp_db):
    from RevenueChaser.module_relatedness import store
    _seed_universe(["NVDA"])
    store.save_profile("NVDA", "cik-NVDA", _prof(products=["gpu"]), "k", "m")
    rawstore.write_rows("theme_runs", [{
        "id": "nmf-test", "created_at": "2026-01-01T00:00:00Z", "k_fine": 1,
        "k_medium": 1, "k_coarse": 1, "jaccard": 1.0, "is_current": 1}])
    rawstore.write_rows("ticker_themes", [
        {"ticker": "NVDA", "theme_id": "nmf-test:c0", "theme": "Semiconductors",
         "level": "coarse", "theme_run_id": "nmf-test"},
        {"ticker": "NVDA", "theme_id": "nmf-test:m0", "theme": "Chips",
         "level": "medium", "theme_run_id": "nmf-test"},
        {"ticker": "NVDA", "theme_id": "nmf-test:f0", "theme": "AI Accelerators",
         "level": "fine", "theme_run_id": "nmf-test"},
    ])
    payload = engine._payload(store.load_profiles(["NVDA"]), {})
    row = payload["companies"][0]
    assert row["theme_coarse"] == "Semiconductors"
    assert row["theme_medium"] == "Chips"
    assert row["theme_fine"] == "AI Accelerators"


def test_report_renders(rawstore_tmp_db, tmp_path, monkeypatch):
    from RevenueChaser.module_relatedness import report
    payload = {
        "as_of": "2026-09-19T00:00:00+00:00", "model": "deepseek-flash", "n": 2,
        "companies": [
            {"ticker": "NVDA", "name": "NVIDIA", "sector": "Technology",
             "industry": "Semiconductors", "market_cap": 1e12, "weights": {"gpu": 1.0}, "profile": {},
             "theme_coarse": "Semiconductors", "theme_medium": "Chips",
             "theme_fine": "AI Accelerators"},
            {"ticker": "AMD", "name": "Advanced Micro Devices", "sector": "Technology",
             "industry": "Semiconductors", "market_cap": 1e11, "weights": {"gpu": 1.0},
             "profile": {}, "theme_coarse": "Semiconductors",
             "theme_medium": "Chips", "theme_fine": "CPUs"},
        ],
        "peers": {
            "NVDA": [{"peer": "AMD", "name": "Advanced Micro Devices", "score": 1.0,
                      "shared": ["gpu"], "percentile": 100,
                      "move": {1: 0.67, 3: 0.74, 5: 0.76, 10: 0.75},
                      "beyond_spy": 0.72,
                      "cluster": {"coarse": True, "medium": True, "fine": False}}],
            "AMD": [{"peer": "NVDA", "name": "NVIDIA", "score": 1.0,
                     "shared": ["gpu"], "percentile": 100}],
        },
        "scores": [1.0], "thinking_compare": {}, "fields": list(config.PROFILE_FIELDS),
    }
    html = report.render_relatedness_dashboard(payload)
    assert "Relatedness" in html
    assert "What am I looking at?" in html
    assert "NVDA" in html and "AMD" in html
    assert 'id="q"' in html
    # Native A/B focal selects and a closed <details class="filters"> are gone.
    assert '<select id="a"' not in html
    assert '<select id="b"' not in html
    assert '<details class="filters">' not in html
    # The shared searchable combobox is the focal picker.
    assert 'id="ticker-combo"' in html
    assert 'role="combobox"' in html
    assert "RCCombobox.mount(" in html
    assert "clear: true" in html.split("RCCombobox.mount(", 1)[1].split("});", 1)[0]   # the × works
    # The shared filter bar (#383 C1b) comes before the combobox (chrome order); the constants kit is gone.
    assert '<section class="rcf-bar" id="rc-filters"></section>' in html
    assert html.index('id="rc-filters"') < html.index('id="ticker-combo"')
    for gone in ('id="mc-tabs"', 'id="index-tabs"', 'id="eweek-tabs"', '<select id="theme"', "initTickerCombo",
                 'MC = "all"'):
        assert gone not in html, f"constants kit left: {gone}"
    # Peer row copy for move / beyond SPY / cluster (payload carries them).
    assert "move" in html and "beyond SPY" in html
    assert "cluster" in html
    assert '"name": "NVIDIA"' in html


def test_report_empty_state():
    from RevenueChaser.module_relatedness import report
    html = report.render_relatedness_dashboard({"as_of": "", "companies": []})
    assert "No profiles yet" in html


def _two_sector_board():
    from RevenueChaser.module_relatedness import report
    co = lambda t, sec, ind, theme: {
        "ticker": t, "name": t, "sector": sec, "industry": ind, "market_cap": 1e12,
        "weights": {"gpu": 1.0}, "profile": {}, "theme_coarse": theme}
    return report.render_relatedness_dashboard({
        "as_of": "2026-09-19T00:00:00+00:00", "model": "m", "n": 2,
        "companies": [co("MSFT", "Technology", "Software - Application", "Cloud"),
                      co("JPM", "Financial Services", "Banks - Diversified", "Banking")],
        "peers": {"MSFT": [], "JPM": []}, "scores": []})


def test_the_focal_line_shows_the_sector_then_the_industry(tmp_path):
    """#266: ``sector · industry · theme``; the sub-industry the line used to show no longer exists."""
    from RevenueChaser.core.tests.board_js import run_board_js
    tail = 'FOCAL = "MSFT"; render(); console.log(JSON.stringify(_els["meta"].textContent));'
    assert run_board_js(_two_sector_board(), tail, tmp_path) == (
        "Technology · Software - Application · Cloud")


def test_the_sector_filter_narrows_the_picker_at_either_level(tmp_path):
    """The shared kit reads each card's ``sector`` and ``industry`` (its ``gics`` path when the payload has one)."""
    from RevenueChaser.core.tests.board_js import run_board_js
    tail = """
function pool(){ return DATA.filter(pass).map(function(c){ return c.ticker; }); }
var log = {all: pool()};
RCF.set({gics: {key: "Financial Services", label: "Financial Services", level: 1}}); log.sector = pool();
RCF.set({gics: {key: "Technology›Software - Application", label: "Software - Application", level: 2}});
log.industry = pool();
console.log(JSON.stringify(log));"""
    got = run_board_js(_two_sector_board(), tail, tmp_path)
    assert got == {"all": ["MSFT", "JPM"], "sector": ["JPM"], "industry": ["MSFT"]}


def test_the_theme_filter_reads_the_pinned_theme_ids(tmp_path):
    """#383 C1b: Theme is the kit's facet over ``theme_ids`` (the pinned run), not the local coarse-label select."""
    from RevenueChaser.core.tests.board_js import run_board_js
    from RevenueChaser.module_relatedness import report
    co = lambda t, ids: {"ticker": t, "name": t, "sector": "Technology", "industry": "Semiconductors",
                         "market_cap": 1e12, "theme_coarse": "Chips", "theme_ids": ids}
    html = report.render_relatedness_dashboard({
        "as_of": "2026-09-19T00:00:00+00:00", "model": "m", "n": 3,
        "companies": [co("NVDA", ["r:c0", "r:m0"]), co("AMD", ["r:c0", "r:m1"]), co("NEW", [])],
        "peers": {}, "scores": [],
        "theme_catalog": {"r:c0": {"label": "Chips", "level": "coarse", "parent": ""},
                          "r:m0": {"label": "GPUs", "level": "medium", "parent": "r:c0"},
                          "r:m1": {"label": "CPUs", "level": "medium", "parent": "r:c0"}}})
    tail = """
function pool(){ return DATA.filter(pass).map(function(c){ return c.ticker; }); }
var log = {all: pool(), themes: Object.keys(META.themes).sort()};
RCF.set({theme: {key: "r:m1", label: "CPUs", level: 2}}); log.medium = pool();
RCF.set({theme: {key: "__none__", label: "Unthemed", level: 1}}); log.none = pool();
console.log(JSON.stringify(log));"""
    got = run_board_js(html, tail, tmp_path)
    assert got == {"all": ["NVDA", "AMD", "NEW"], "themes": ["r:c0", "r:m0", "r:m1"],
                   "medium": ["AMD"], "none": ["NEW"]}


def test_no_filter_set_keeps_every_name_and_the_first_one_focal(tmp_path):
    """#383 C1b parity: no filter set = the same names as before the move (all of them), same default focal."""
    from RevenueChaser.core.tests.board_js import run_board_js
    got = run_board_js(_two_sector_board(), "console.log(JSON.stringify({pool: DATA.filter(pass).length, "
                       "n: DATA.length, focal: FOCAL, count: _els['count'].textContent, active: RCF.active()}));",
                       tmp_path)
    assert got == {"pool": 2, "n": 2, "focal": "MSFT", "count": "2 names", "active": False}


def test_companies_carry_the_filter_path_and_theme_ids(rawstore_tmp_db):
    """#383 C1b: each company is a kit row (``gics`` + ``theme_ids``) and the payload carries the theme catalog."""
    from RevenueChaser.module_relatedness import store
    _seed_universe(["NVDA"])
    store.save_profile("NVDA", "cik-NVDA", _prof(products=["gpu"]), "k", "m")
    payload = engine._payload(store.load_profiles(["NVDA"]), {})
    row = payload["companies"][0]
    assert row["gics"] == [row["sector"], row["industry"]] and row["theme_ids"] == []
    assert payload["theme_catalog"] == {}             # no pinned run in a fresh store


def test_companies_with_an_unknown_sector_read_unclassified(rawstore_tmp_db):
    """R3 (#86): the legacy vendor ``Other`` and a missing sector both read as the unknown bucket."""
    from RevenueChaser.module_relatedness import store
    rawstore.replace_all("equity_universe", [
        {"ticker": t, "name": t, "market_cap": 1e9, "sector": sec, "price": 1.0, "industry": "x",
         "quote": {}}
        for t, sec in (("LEG", "Other"), ("NUL", None), ("OKK", "Energy"))])
    for t in ("LEG", "NUL", "OKK"):
        store.save_profile(t, f"cik-{t}", _prof(products=["x"]), "k", "m")
    payload = engine._payload(store.load_profiles(["LEG", "NUL", "OKK"]), {})
    assert {c["ticker"]: c["sector"] for c in payload["companies"]} == {
        "LEG": "Unclassified", "NUL": "Unclassified", "OKK": "Energy"}


def test_companies_carry_the_sector_and_the_industry_not_the_gics_levels(rawstore_tmp_db):
    """#266: a card is ``sector`` (Morningstar name) + ``industry`` (the provider text), nothing deeper.

    ``OLD`` is a row the identity master has not re-applied yet: it still holds the GICS spelling.
    """
    from RevenueChaser.module_relatedness import store
    rows = (("NVDA", "Technology", "Semiconductors"), ("JPM", "Financial Services", "Banks - Diversified"),
            ("OLD", "Information Technology", "Software - Application"), ("NOI", "Energy", ""))
    rawstore.replace_all("equity_universe", [
        {"ticker": t, "name": t, "market_cap": 1e9, "sector": sec, "industry": ind, "price": 1.0,
         "quote": {}} for t, sec, ind in rows])
    for t, _sec, _ind in rows:
        store.save_profile(t, f"cik-{t}", _prof(products=["x"]), "k", "m")
    payload = engine._payload(store.load_profiles([r[0] for r in rows]), {})
    cards = {c["ticker"]: c for c in payload["companies"]}
    assert {t: (c["sector"], c["industry"]) for t, c in cards.items()} == {
        "NVDA": ("Technology", "Semiconductors"), "JPM": ("Financial Services", "Banks - Diversified"),
        "OLD": ("Technology", "Software - Application"), "NOI": ("Energy", "")}
    assert all("sub_industry" not in c and "industry_group" not in c for c in cards.values())


def test_the_payload_reads_the_universe_once_for_the_company_fields(rawstore_tmp_db, monkeypatch):
    """#49: ``_payload`` used to read ``equity_universe`` twice — its own name/sector/cap query beside
    ``core.universe.filter_fields`` — and the two reads could only ever disagree. One read must give the same
    card: the name, the Morningstar sector and the cap the seeded row holds."""
    from RevenueChaser.module_relatedness import store
    rows = (("AAA", "Aaa Corp", "Information Technology", "Software - Application", 2.0e9),
            ("BBB", "Bee Corp", "Energy", "", 1.0e9),
            ("CCC", "", "Utilities", "Regulated Electric", None))
    rawstore.replace_all("equity_universe", [
        {"ticker": t, "name": name, "market_cap": cap, "sector": sec, "industry": ind, "price": 1.0,
         "quote": {}} for t, name, sec, ind, cap in rows])
    for t, _name, _sec, _ind, _cap in rows:
        store.save_profile(t, f"cik-{t}", _prof(products=["x"]), "k", "m")

    reads = []
    real = rawstore.query

    def counting(sql, *a, **k):
        if "FROM equity_universe" in sql:
            reads.append(sql)
        return real(sql, *a, **k)

    monkeypatch.setattr(rawstore, "query", counting)
    payload = engine._payload(store.load_profiles([r[0] for r in rows]), {})

    # filter_fields (the card fields) + classification_universe (universe_total): the third read is gone.
    assert len([sql for sql in reads if "ticker, name, sector, industry, market_cap" in sql]) == 1, reads
    cards = {c["ticker"]: c for c in payload["companies"]}
    assert {t: (c["name"], c["sector"], c["market_cap"]) for t, c in cards.items()} == {
        "AAA": ("Aaa Corp", "Technology", 2.0e9),
        "BBB": ("Bee Corp", "Energy", 1.0e9),
        # an empty stored name falls back to the ticker; a NULL cap stays NULL (no cap, not zero)
        "CCC": ("CCC", "Utilities", None)}


def test_a_stored_profile_of_an_excluded_type_leaves_companies_and_peer_lists(
        rawstore_tmp_db, monkeypatch, tmp_path):
    """#87: ``run`` loads profiles for the typed universe only, so a fund's stored
    profile (identical to NVDA's, hence its best peer) is never read."""
    from RevenueChaser.module_relatedness import store
    tickers = ["NVDA", "AMD", "TSM", "FUND"]
    _seed_universe(tickers)
    _seed_filings(tickers)
    rawstore.ensure_table("security_master")
    rawstore.write_rows("security_master", [
        {"ticker": "NVDA", "security_type": "common"},
        {"ticker": "FUND", "security_type": "cef"}])
    monkeypatch.setattr(engine.config, "RELATEDNESS_JSON", tmp_path / "r.json")
    monkeypatch.setattr(engine.vc_engine, "fetch_corpus",
                        lambda *a, **k: {"no_fetch": True})
    gpu = dict(products=["gpu"], end_markets=["data center"])
    for t, prof in {"NVDA": _prof(**gpu), "FUND": _prof(**gpu),
                    "AMD": _prof(**gpu),
                    "TSM": _prof(products=["foundry"], end_markets=["semiconductors"])}.items():
        store.save_profile(t, "cik-" + t, prof, "k", "m")

    payload = engine.run(no_fetch=True)               # tickers come from the universe
    assert "FUND" not in {c["ticker"] for c in payload["companies"]}
    assert payload["n"] == 3 and payload["universe_total"] == 3
    assert "FUND" not in payload["peers"]
    assert all(p["peer"] != "FUND" for peers in payload["peers"].values() for p in peers)
    assert {p["peer"] for p in payload["peers"]["NVDA"]} == {"AMD"}   # peers still computed
    # the stored profile is still there: nothing was deleted
    assert "FUND" in store.load_profiles(["FUND"])


def test_the_universe_tile_says_operating_companies():
    from RevenueChaser.module_relatedness import report
    html = report.render_relatedness_dashboard({
        "as_of": "2026-09-19T00:00:00+00:00", "n": 1, "universe_total": 5, "pending": 0,
        "companies": [{"ticker": "NVDA", "name": "NVIDIA", "sector": "Technology",
                       "industry": "Semiconductors", "market_cap": 1e12, "weights": {"gpu": 1.0},
                       "profile": {}}],
        "peers": {}, "scores": []})
    assert ('<div class="l">Universe</div>'
            '<div class="d">operating companies, no market-cap floor</div>') in html
    assert "Universe (≥" not in html               # the floor is gone; the tile must not claim one
    # ("< $300M" still appears: that is the Micro bucket of the cap filter, not a floor.)
    assert "classification spine" not in html


def test_beyond_spy_label_uses_default_horizon(monkeypatch):
    """DF63: the 'beyond SPY' residual is computed at DEFAULT_HORIZON; the label says so."""
    from RevenueChaser.module_comovement import config as comov_config
    from RevenueChaser.module_relatedness import report
    payload = {"as_of": "", "companies": []}
    html = report.render_relatedness_dashboard(payload)
    assert "(5d)'" not in html
    monkeypatch.setattr(comov_config, "DEFAULT_HORIZON", 3)
    assert "' (3d)'" in report.render_relatedness_dashboard(payload)


def test_the_kit_meta_filters_by_earnings_and_takes_the_registry_indexes(tmp_path):
    """#383 C1b review (N2): Relatedness mounts the kit as its own board, with the earnings facet as a filter (not a
    mark) and the index list from the registry."""
    from RevenueChaser.core.index_registry import filter_labels
    from RevenueChaser.core.tests.board_js import run_board_js
    got = run_board_js(_two_sector_board(), "console.log(JSON.stringify({mode: META.earnings_mode, board: META.board, "
                       "indexes: META.indexes}));", tmp_path)
    assert got == {"mode": "filter", "board": "relatedness", "indexes": filter_labels()}
