"""Value Chain core — aliases, splitter, mentions, engine (no network)."""
from __future__ import annotations

from RevenueChaser.core import rawstore
from RevenueChaser.module_value_chain import aliases, engine, mentions, splitter

_FILLER = ("We operate in a highly competitive industry and invest heavily in "
           "research and development across our product lines and geographies. ") * 30

_TEN_K = (
    "UNITED STATES SECURITIES AND EXCHANGE COMMISSION\n"
    "Item 1. Business\n"
    "We design graphics processors. We purchase wafers from Taiwan Semiconductor "
    "Manufacturing Company Limited (TSMC), our primary foundry supplier. Our "
    "customers include Microsoft, Meta Platforms and Amazon. We compete with "
    "Advanced Micro Devices.\n" + _FILLER +
    "Item 1A. Risk Factors\n"
    "We depend on a limited number of suppliers."
)

_TWENTY_F = (
    "Item 4. Information on the Company\n"
    "We are a foundry. Our customers include NVIDIA Corporation and Advanced "
    "Micro Devices. We purchase equipment from ASML.\n" + _FILLER +
    "Item 4A. Unresolved Staff Comments\n"
    "Nothing."
)


# ── aliases ─────────────────────────────────────────────────────────────────
# ── splitter ────────────────────────────────────────────────────────────────
def test_slice_10k_stops_at_item_1a():
    item, quality = splitter.slice_item(_TEN_K, "10-K")
    assert "graphics processors" in item
    assert "Risk Factors" not in item
    assert quality == "ok"


def test_slice_20f_stops_at_item_4a():
    item, quality = splitter.slice_item(_TWENTY_F, "20-F")
    assert "foundry" in item
    assert "Unresolved Staff Comments" not in item
    assert quality == "ok"


def test_slice_short_and_missing():
    item, quality = splitter.slice_item("Item 1. Business\nToo short.", "10-K")
    assert quality == "short"
    assert splitter.slice_item("no item header here", "10-K") == (None, "missing")


def test_slice_skips_table_of_contents():
    """The TOC lists Item 1 and Item 1A back to back — pick the real section."""
    text = ("Table of Contents\nItem 1. Business .... 3\nItem 1A. Risk Factors .... 10\n"
            + _FILLER
            + "\nItem 1. Business\n" + ("We design and sell processors. " * 120)
            + "\nItem 1A. Risk Factors\nWe face many risks.")
    item, quality = splitter.slice_item(text, "10-K")
    assert quality == "ok"
    assert "We design and sell processors" in item
    assert "Table of Contents" not in item


def test_strip_html_drops_markup_and_unescapes():
    text = splitter.strip_html("<p>R&amp;D <b>spend</b></p><script>x()</script>")
    assert "R&D" in text and "spend" in text and "x()" not in text


# ── mentions ────────────────────────────────────────────────────────────────
def test_mentions_relations():
    found = {(m["dst_ticker"], m["relation"]) for m in mentions.find_mentions(_TEN_K)}
    assert ("TSM", "supplier") in found
    assert ("MSFT", "customer") in found
    assert ("AMD", "competitor") in found


def test_20f_mentions():
    found = {(m["dst_ticker"], m["relation"]) for m in mentions.find_mentions(_TWENTY_F)}
    assert ("NVDA", "customer") in found
    assert ("ASML", "supplier") in found


# ── engine / graph ──────────────────────────────────────────────────────────
def test_focal_graph_two_hops_resolved_only():
    edges = [
        {"src": "NVDA", "dst_name": "TSMC", "dst_ticker": "TSM", "relation": "supplier",
         "snippet": "purchase from TSMC", "filed": "2026-02-01"},
        {"src": "TSM", "dst_name": "Apple", "dst_ticker": "AAPL", "relation": "customer",
         "snippet": "customers include Apple", "filed": "2026-03-01"},
        {"src": "NVDA", "dst_name": "PrivateCo", "dst_ticker": "", "relation": "supplier",
         "snippet": "buy from PrivateCo", "filed": "2026-02-01"},
    ]
    g = engine.focal_graph(edges, "NVDA", depth=2)
    hops = {(l["src"], l["dst"], l["hop"]) for l in g["links"]}
    assert ("NVDA", "TSM", 1) in hops
    assert ("TSM", "AAPL", 2) in hops            # hop 2 follows the resolved edge
    # a private name shows at hop 1 but never seeds a hop 2
    assert any(l["dst"] == "PrivateCo" and l["hop"] == 1 for l in g["links"])
    assert not any(l["hop"] == 2 and not l["dst_ticker"] for l in g["links"])


def test_run_persists_corpus_and_edges(rawstore_tmp_db, monkeypatch):
    monkeypatch.setattr(engine.sec_edgar, "_cik_map", lambda: {
        "NVDA": {"cik": "1045810"}, "TSM": {"cik": "1046179"}})

    def _fake_fetch(ticker, cik, client=None):
        return {"ticker": ticker, "cik": str(cik), "form": "10-K", "filed": "2026-02-01",
                "accession": "acc-" + ticker, "primary_doc": "http://x",
                "item_text": _TEN_K, "quality": "ok"}

    monkeypatch.setattr(engine.edgar_text, "fetch_filing", _fake_fetch)

    payload = engine.run(no_fetch=False, tickers=["NVDA", "TSM"])
    assert payload["fetch"]["fetched"] == 2
    assert rawstore.count("chain_filings") == 2
    assert rawstore.count("chain_mentions") > 0
    assert rawstore.count("chain_meta") == 3

    loaded = engine.load_payload()
    assert {f["ticker"] for f in loaded["filings"]} == {"NVDA", "TSM"}
    assert any(e["dst_ticker"] == "TSM" for e in loaded["edges"])
    assert "name" in loaded["filings"][0]
    assert "universe_total" in loaded


def test_run_no_fetch_makes_no_request(rawstore_tmp_db, monkeypatch):
    def _boom(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("no_fetch must not fetch")
    monkeypatch.setattr(engine.edgar_text, "fetch_filing", _boom)
    payload = engine.run(no_fetch=True, tickers=["NVDA"])
    assert payload["fetch"]["no_fetch"] is True
    assert rawstore.count("chain_filings") == 0


def test_run_no_fetch_reuses_stored_mentions(rawstore_tmp_db, monkeypatch):
    """Refresh must not rescan Item 1/4 text — that hangs on the live corpus."""
    rawstore.write_rows("chain_filings", [{
        "ticker": "NVDA", "cik": "1", "form": "10-K", "filed": "2026-02-01",
        "accession": "acc", "primary_doc": "http://x",
        "item_text": _TEN_K, "quality": "ok",
    }])
    rawstore.replace_all("chain_mentions", [{
        "src": "NVDA", "dst_name": "TSMC", "dst_ticker": "TSM",
        "relation": "supplier", "snippet": "from store", "filed": "2026-02-01",
    }])

    def _boom(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("no_fetch must not rescan filings")

    monkeypatch.setattr(engine.mentions_mod, "find_mentions", _boom)
    payload = engine.run(no_fetch=True, tickers=["NVDA"])
    assert payload["fetch"]["no_fetch"] is True
    assert any(e.get("snippet") == "from store" for e in payload["edges"])


def test_fetch_corpus_parallel_fetches_all(rawstore_tmp_db, monkeypatch):
    tickers = [f"T{i}" for i in range(6)]
    monkeypatch.setattr(engine.sec_edgar, "_cik_map",
                        lambda: {t: {"cik": str(1000 + i)} for i, t in enumerate(tickers)})

    def _fake(ticker, cik, client=None):
        return {"ticker": ticker, "cik": str(cik), "form": "10-K", "filed": "2026-02-01",
                "accession": "acc-" + ticker, "primary_doc": "http://x",
                "item_text": "We buy from ACME Corp.", "quality": "ok"}

    monkeypatch.setattr(engine.edgar_text, "fetch_filing", _fake)
    out = engine.fetch_corpus(tickers=tickers)
    assert out["fetched"] == 6 and out["failed"] == 0
    assert rawstore.count("chain_filings") == 6


def test_fetch_corpus_counts_per_ticker_failures(rawstore_tmp_db, monkeypatch):
    monkeypatch.setattr(engine.sec_edgar, "_cik_map",
                        lambda: {"AAA": {"cik": "1"}, "BBB": {"cik": "2"}})

    def _fake(ticker, cik, client=None):
        if ticker == "BBB":
            raise RuntimeError("boom")
        return {"ticker": ticker, "cik": str(cik), "form": "10-K", "filed": "2026-02-01",
                "accession": "acc", "primary_doc": "http://x", "item_text": "x",
                "quality": "ok"}

    monkeypatch.setattr(engine.edgar_text, "fetch_filing", _fake)
    out = engine.fetch_corpus(tickers=["AAA", "BBB"])
    assert out["fetched"] == 1 and out["failed"] == 1


def test_default_focal_is_the_most_connected():
    from RevenueChaser.module_value_chain import report
    assert report._default_focal(["A", "B", "C"], [
        {"src": "A", "dst_ticker": "B"}, {"src": "A", "dst_ticker": "C"}]) == "A"
    # tie -> deterministic alphabetical
    assert report._default_focal(["B", "A"], [{"src": "B", "dst_ticker": "A"}]) == "A"
    assert report._default_focal([], []) is None


# ── report (readability layer) ──────────────────────────────────────────────
def test_report_has_readability_layer():
    from RevenueChaser.module_value_chain import report

    payload = {
        "as_of": "2026-09-17T00:00:00+00:00",
        "filings": [{"ticker": "NVDA", "name": "NVIDIA Corporation", "form": "10-K",
                     "filed": "2026-02-01", "quality": "ok", "market_cap": 1e12}],
        "edges": [{"src": "NVDA", "dst_name": "TSMC", "dst_ticker": "TSM",
                   "relation": "supplier", "snippet": "we purchase wafers from TSMC",
                   "filed": "2026-02-01"}],
        "universe_total": 1, "pending": 0,
    }
    html = report.render_value_chain_dashboard(payload)
    assert "What am I looking at?" in html
    assert "Supplier" in html and "Customer" in html and "Focal" in html
    assert "v1 uses keyword rules" in html
    assert "TSM" in html
    assert 'id="q"' in html
    assert '"name": "NVIDIA Corporation"' in html


def test_report_labels_are_honest():
    """Review 2026-09-26 F6/F11/F16: counts what is drawn, no invented confidence,
    no promise of private names, relations worded as unverified."""
    from RevenueChaser.module_value_chain import report

    edge = {"src": "NVDA", "dst_name": "TSMC", "dst_ticker": "TSM", "relation": "supplier",
            "snippet": "we purchase wafers from TSMC", "filed": "2026-02-01"}
    other = dict(edge, relation="other", dst_ticker="MSFT", dst_name="Microsoft")
    comp = dict(edge, relation="competitor", dst_ticker="AMD", dst_name="AMD")
    html = report.render_value_chain_dashboard({
        "as_of": "2026-09-17T00:00:00+00:00",
        "filings": [{"ticker": "NVDA", "name": "NVIDIA Corporation", "form": "10-K",
                     "filed": "2026-02-01", "quality": "ok", "market_cap": 1e12}],
        "edges": [edge, other, comp], "universe_total": 1, "pending": 0,
    })
    assert '<div class="v">1</div><div class="l">Links drawn</div>' in html
    assert '<div class="v">3</div>' not in html
    assert "MEDIUM" not in html and "confidence:" not in html
    assert "Private companies appear by name" not in html
    assert "possible supplier" in html and "possible customer" in html
    assert "fixed list of about 50 large public companies" in html


def test_board_payload_carries_precision(rawstore_tmp_db):
    """Precision is measured on the labelled edges the board still serves."""
    from RevenueChaser.module_value_chain import precision
    labels = precision.load_labels()
    served = [{"src": l["src"], "dst_ticker": l["dst_ticker"], "relation": l["relation"]}
              for l in labels]
    payload = engine._board_payload([], served, "2026-09-17T00:00:00+00:00", None)
    assert (payload["precision"]["n"], payload["precision"]["tp"]) == (80, 31)
    good = [l for l in labels if l["label"]][:3]
    payload = engine._board_payload([], [{"src": l["src"], "dst_ticker": l["dst_ticker"],
                                          "relation": l["relation"]} for l in good],
                                    "2026-09-17T00:00:00+00:00", None)
    # three surviving labels are below the minimum sample: not a pass (#118 C27)
    assert (payload["precision"]["n"], payload["precision"]["passes"]) == (3, False)
    assert payload["precision"]["status"] == "insufficient sample"
    assert engine._board_payload([], [], "x", None)["precision"]["precision"] is None


def test_report_shows_measured_precision():
    from RevenueChaser.module_value_chain import report
    html = report.render_value_chain_dashboard({
        "as_of": "2026-09-17T00:00:00+00:00", "filings": [], "edges": [],
        "precision": {"n": 80, "tp": 31, "fp": 49, "precision": 0.3875, "passes": False},
    })
    assert '<div class="stat warn"><div class="v">39%</div><div class="l">Precision</div>' in html
    assert "hand-checked n=80" in html and "gate 70%" in html
    ok = report.render_value_chain_dashboard({
        "as_of": "2026-09-17T00:00:00+00:00", "filings": [], "edges": [],
        "precision": {"n": 40, "tp": 32, "fp": 8, "precision": 0.8, "passes": True},
    })
    assert '<div class="stat"><div class="v">80%</div><div class="l">Precision</div>' in ok
    none = report.render_value_chain_dashboard({"as_of": "", "filings": [], "edges": []})
    assert "not measured" in none


def test_report_uses_ticker_combobox_after_filters():
    """SSoT docs/dashboard_ui.md chrome: filters first, then ticker combobox,
    and filters are never buried in a closed <details>."""
    from RevenueChaser.module_value_chain import report

    payload = {
        "as_of": "2026-09-17T00:00:00+00:00",
        "filings": [{"ticker": "NVDA", "name": "NVIDIA Corporation", "form": "10-K",
                     "filed": "2026-02-01", "quality": "ok", "market_cap": 1e12}],
        "edges": [{"src": "NVDA", "dst_name": "TSMC", "dst_ticker": "TSM",
                   "relation": "supplier", "snippet": "we purchase wafers from TSMC",
                   "filed": "2026-02-01"}],
        "universe_total": 1, "pending": 0,
    }
    html = report.render_value_chain_dashboard(payload)
    assert 'role="combobox"' in html
    assert 'id="ticker-combo"' in html
    assert '<select id="focal"' not in html
    assert '<details class="filters">' not in html
    assert "RCCombobox.mount(" in html
    assert "clear: true" in html.split("RCCombobox.mount(", 1)[1].split("});", 1)[0]   # the × works
    # The shared filter bar (#383 C1b) before the combobox; the constants kit is gone.
    assert '<section class="rcf-bar" id="rc-filters"></section>' in html
    assert html.index('id="rc-filters"') < html.index('id="ticker-combo"')
    for gone in ('id="mc-tabs"', 'id="index-tabs"', 'id="eweek-tabs"', 'id="sector"', "initTickerCombo"):
        assert gone not in html, f"constants kit left: {gone}"
    assert "No companies with supplier / customer edges" in html


def test_report_hides_tickers_without_edges():
    from RevenueChaser.module_value_chain import report
    html = report.render_value_chain_dashboard({
        "as_of": "2026-09-17T00:00:00+00:00",
        "filings": [
            {"ticker": "NVDA", "name": "NVIDIA Corporation", "form": "10-K",
             "filed": "2026-02-01", "quality": "ok", "market_cap": 1e12},
            {"ticker": "AMZN", "name": "Amazon.com, Inc.", "form": "10-K",
             "filed": "2026-02-01", "quality": "short", "market_cap": 2e12},
            {"ticker": "ZZZ", "name": "Empty Co", "form": "10-K",
             "filed": "2026-02-01", "quality": "short", "market_cap": 400e6},
        ],
        "edges": [{"src": "NVDA", "dst_name": "TSMC", "dst_ticker": "TSM",
                   "relation": "supplier", "snippet": "we purchase wafers from TSMC",
                   "filed": "2026-02-01"}],
        "universe_total": 3, "pending": 0,
    })
    assert '"ticker": "NVDA"' in html
    assert '"ticker": "AMZN"' not in html and "Amazon.com, Inc." not in html
    assert '"ticker": "ZZZ"' not in html and "Empty Co" not in html
    assert 'id="q"' in html


def test_report_empty_state():
    from RevenueChaser.module_value_chain import report
    html = report.render_value_chain_dashboard({"as_of": "2026-09-17T00:00:00+00:00",
                                                "filings": [], "edges": []})
    assert "No companies with supplier / customer edges" in html


def test_board_rows_without_a_classification_read_unclassified(rawstore_tmp_db):
    """R3 (#86): a filing for a ticker the universe does not know gets the unknown sector bucket."""
    from RevenueChaser.module_value_chain import report
    rows = engine._board_filings([{"ticker": "NOPE", "form": "10-K", "filed": "2026-01-01"}])
    assert rows[0]["sector"] == "Unclassified"
    picker = report._picker_rows([], {"NOPE"})
    assert picker[0]["sector"] == "Unclassified"


def test_board_rows_carry_the_sector_and_the_industry_not_the_gics_levels(rawstore_tmp_db):
    """#266: a filing row is ``sector`` (Morningstar name) + ``industry`` (the provider text), nothing deeper.

    ``OLD`` is a row the identity master has not re-applied yet: it still holds the GICS spelling.
    """
    from RevenueChaser.module_value_chain import report
    rawstore.replace_all("equity_universe", [
        {"ticker": t, "name": t, "market_cap": 1e9, "sector": sec, "industry": ind, "price": 1.0,
         "quote": {}} for t, sec, ind in (
            ("MSFT", "Technology", "Software - Application"),
            ("OLD", "Information Technology", "Software - Infrastructure"),
            ("NOI", "Energy", None))])
    rows = engine._board_filings([{"ticker": t, "form": "10-K", "filed": "2026-01-01"}
                                  for t in ("MSFT", "OLD", "NOI")])
    assert {r["ticker"]: (r["sector"], r["industry"]) for r in rows} == {
        "MSFT": ("Technology", "Software - Application"),
        "OLD": ("Technology", "Software - Infrastructure"), "NOI": ("Energy", "")}
    assert all("sub_industry" not in r and "industry_group" not in r for r in rows)
    picker = {r["ticker"]: r for r in report._picker_rows(rows, set())}
    assert {t: (r["sector"], r["industry"]) for t, r in picker.items()} == {
        "MSFT": ("Technology", "Software - Application"),
        "OLD": ("Technology", "Software - Infrastructure"), "NOI": ("Energy", "")}
    assert all("sub_industry" not in r and "industry_group" not in r for r in picker.values())
    assert report._picker_rows([], {"NOPE"})[0]["industry"] == ""


def _two_sector_vc_board(**extra):
    from RevenueChaser.module_value_chain import report
    filing = lambda t, sec, ind, ids: {"ticker": t, "name": t, "form": "10-K", "filed": "2026-02-01",
                                       "quality": "ok", "market_cap": 1e12, "sector": sec, "industry": ind,
                                       "theme_ids": ids}
    return report.render_value_chain_dashboard({
        "as_of": "2026-09-17T00:00:00+00:00",
        "filings": [filing("MSFT", "Technology", "Software - Application", ["r:c0"]),
                    filing("JPM", "Financial Services", "Banks - Diversified", [])],
        "edges": [{"src": "MSFT", "dst_name": "JPMorgan", "dst_ticker": "JPM", "relation": "customer",
                   "snippet": "we sell to JPMorgan", "filed": "2026-02-01"}],
        "universe_total": 2, "pending": 0, **extra})


def test_the_board_picker_filters_by_sector_then_industry(tmp_path):
    """The shared kit reads each picker row's sector path (``gics``, else ``sector`` and ``industry``)."""
    from RevenueChaser.core.tests.board_js import run_board_js
    tail = """
function pool(){ return DATA.filter(pass).map(function(c){ return c.ticker; }); }
var log = {all: pool()};
RCF.set({gics: {key: "Financial Services", label: "Financial Services", level: 1}}); log.sector = pool();
RCF.set({gics: {key: "Technology›Software - Application", label: "Software - Application", level: 2}});
log.industry = pool();
console.log(JSON.stringify(log));"""
    got = run_board_js(_two_sector_vc_board(), tail, tmp_path)
    assert got == {"all": ["JPM", "MSFT"], "sector": ["JPM"], "industry": ["MSFT"]}


def test_the_board_picker_filters_by_theme(tmp_path):
    """#383 C1b: Value Chain gains the Theme facet (the pinned run's ``theme_ids``); the catalog rides the payload."""
    from RevenueChaser.core.tests.board_js import run_board_js
    html = _two_sector_vc_board(theme_catalog={"r:c0": {"label": "Cloud", "level": "coarse", "parent": ""}})
    tail = """
function pool(){ return DATA.filter(pass).map(function(c){ return c.ticker; }); }
var log = {themes: Object.keys(META.themes)};
RCF.set({theme: {key: "r:c0", label: "Cloud", level: 1}}); log.theme = pool();
RCF.set({theme: {key: "__none__", label: "Unthemed", level: 1}}); log.none = pool();
console.log(JSON.stringify(log));"""
    assert run_board_js(html, tail, tmp_path) == {"themes": ["r:c0"], "theme": ["MSFT"], "none": ["JPM"]}


def test_no_filter_set_keeps_every_name_and_the_default_focal(tmp_path):
    """#383 C1b parity: no filter set = every picker name, and the most-linked name stays the focal one."""
    from RevenueChaser.core.tests.board_js import run_board_js
    got = run_board_js(_two_sector_vc_board(), "console.log(JSON.stringify({pool: DATA.filter(pass).length, "
                       "n: DATA.length, focal: FOCAL, count: _els['count'].textContent, active: RCF.active()}));",
                       tmp_path)
    assert got == {"pool": 2, "n": 2, "focal": "JPM", "count": "2 names", "active": False}


def test_board_rows_carry_the_filter_path_and_theme_ids(rawstore_tmp_db):
    """#383 C1b: a filing row is a kit row (``gics`` + ``theme_ids``) and the payload carries the theme catalog."""
    from RevenueChaser.module_value_chain import report
    rows = engine._board_filings([{"ticker": "NOPE", "form": "10-K", "filed": "2026-01-01"}])
    assert rows[0]["gics"] == ["Unclassified", ""] and rows[0]["theme_ids"] == []
    picker = report._picker_rows(rows, set())
    assert picker[0]["gics"] == ["Unclassified", ""] and picker[0]["theme_ids"] == []
    assert engine._board_payload([], [], "2026-01-01", None)["theme_catalog"] == {}


# ── the security-type filter on the board (#87, Identity R4) ─────────────────
def _seed_typed_chain():
    rawstore.replace_all("equity_universe", [
        {"ticker": t, "market_cap": 1.0e9, "name": t, "sector": "Technology",
         "industry": "Software - Application", "price": 10.0, "quote": {"symbol": t}}
        for t in ("AAA", "BBB", "FUND", "NODST")])
    rawstore.ensure_table("security_master")
    rawstore.write_rows("security_master", [
        {"ticker": "AAA", "security_type": "common"},
        {"ticker": "FUND", "security_type": "cef"}])
    rawstore.write_rows("chain_filings", [
        {"ticker": t, "cik": "1", "form": "10-K", "filed": "2026-02-01",
         "accession": "acc-" + t, "primary_doc": "http://x", "item_text": "x",
         "quality": "ok", "fetched_at": 1.0} for t in ("AAA", "BBB", "FUND")])
    edges = [
        {"src": "AAA", "dst_name": "The Fund", "dst_ticker": "FUND", "relation": "customer",
         "snippet": "we sell to The Fund", "filed": "2026-02-01"},
        {"src": "FUND", "dst_name": "Alpha", "dst_ticker": "AAA", "relation": "supplier",
         "snippet": "we buy from Alpha", "filed": "2026-02-01"},
        {"src": "BBB", "dst_name": "Alpha", "dst_ticker": "AAA", "relation": "supplier",
         "snippet": "we buy from Alpha", "filed": "2026-02-01"}]
    rawstore.write_rows("chain_mentions", edges)
    rawstore.write_rows("chain_meta", [{"key": "last_sync", "value": "2026-09-30"}])
    return edges


def test_board_payload_drops_filings_and_source_edges_of_excluded_types(rawstore_tmp_db):
    edges = _seed_typed_chain()
    payload = engine._board_payload(rawstore.get_all("chain_filings"), edges, "x", None)
    assert [f["ticker"] for f in payload["filings"]] == ["AAA", "BBB"]
    assert [(e["src"], e["dst_ticker"]) for e in payload["edges"]] == [
        ("AAA", "FUND"), ("BBB", "AAA")]
    # a counterparty field is data about the filing's text: left alone
    assert payload["edges"][0]["dst_name"] == "The Fund"
    # the denominator is the operating universe; pending counts only its filings
    assert payload["universe_total"] == 3                      # AAA, BBB, NODST
    assert payload["pending"] == 1                             # NODST has no filing yet


def test_load_payload_filters_but_deletes_nothing(rawstore_tmp_db):
    _seed_typed_chain()
    payload = engine.load_payload()
    assert {f["ticker"] for f in payload["filings"]} == {"AAA", "BBB"}
    assert all(e["src"] != "FUND" for e in payload["edges"]) and len(payload["edges"]) == 2
    assert rawstore.count("chain_filings") == 3 and rawstore.count("chain_mentions") == 3


def test_board_payload_without_a_master_table_is_unchanged(tmp_path):
    rawstore.replace_all("equity_universe", [
        {"ticker": "AAA", "market_cap": 1.0e9, "name": "AAA", "price": 1.0,
         "quote": {"symbol": "AAA"}}])
    filings = [{"ticker": "AAA", "form": "10-K", "filed": "2026-02-01", "quality": "ok"}]
    payload = engine._board_payload(filings, [{"src": "AAA", "dst_ticker": "ZZZ",
                                               "dst_name": "Z", "relation": "customer"}],
                                    "x", None)
    assert len(payload["filings"]) == 1 and len(payload["edges"]) == 1


def test_the_universe_tile_says_operating_companies():
    from RevenueChaser.module_value_chain import report
    html = report.render_value_chain_dashboard({
        "as_of": "2026-09-17T00:00:00+00:00", "filings": [], "edges": [],
        "universe_total": 5, "pending": 0})
    assert ('<div class="l">Universe (≥$300M)</div>'
            '<div class="d">operating companies</div>') in html
    assert "classification spine" not in html


def test_the_kit_meta_filters_by_earnings_and_takes_the_registry_indexes(tmp_path):
    """#383 C1b review (N2): Value Chain mounts the kit as its own board, with the earnings facet as a filter (not a
    mark) and the index list from the registry."""
    from RevenueChaser.core.index_registry import filter_labels
    from RevenueChaser.core.tests.board_js import run_board_js
    got = run_board_js(_two_sector_vc_board(), "console.log(JSON.stringify({mode: META.earnings_mode, "
                       "board: META.board, indexes: META.indexes}));", tmp_path)
    assert got == {"mode": "filter", "board": "value_chain", "indexes": filter_labels()}
