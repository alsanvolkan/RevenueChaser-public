"""Tests for module_financials/run.py orchestration.

``run.py`` was previously untested. The load-bearing behaviour is that a
store-only render (``no_fetch=True``) must never die writing its report because
the output directory is missing: ``.cache`` is regenerable and may be cleared
while the server runs, and this module's write was the one writer in the repo
that did not recreate its parent — a cleared cache turned Refresh into a
FileNotFoundError that the caller could only swallow.
"""
from __future__ import annotations

import RevenueChaser.module_financials.run as fm


def test_run_writes_report_even_when_output_dir_was_deleted(rawstore_tmp_db,
                                                            tmp_path, monkeypatch,
                                                            financials):
    """A cleared .cache must not break a store-only render.

    Regression: ``run()`` wrote ``{OUTPUT_DIR}/{ticker}_report.html`` with a bare
    ``write_text`` and no ``parent.mkdir``, so after a cache clear the write
    raised FileNotFoundError and the Financials tab silently degraded.
    """
    from RevenueChaser.core.providers import cache as cache_manager

    missing = tmp_path / "gone" / "cache"
    monkeypatch.setattr(fm, "OUTPUT_DIR", missing)
    cache_manager.save("AAPL", dict(financials))

    res = fm.run("AAPL",
                 modules=["dcf", "3stmt", "comps", "credit", "sensitivity"],
                 output="html", no_fetch=True)

    report = missing / "AAPL_report.html"
    assert report.exists()
    assert res["paths"]["html"] == str(report)
    assert report.read_text(encoding="utf-8").strip()


def test_run_no_fetch_requires_a_cached_payload(rawstore_tmp_db, tmp_path, monkeypatch):
    """A store-only render for a ticker with no cached payload raises, so the
    caller can fall back to an empty state instead of touching the network."""
    monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)

    import pytest
    with pytest.raises(fm._NoCachedData):
        fm.run("ZZZZ", modules=["dcf"], output="html", no_fetch=True)


def _run_comps(monkeypatch, financials, spine_sector, peers=()):
    from RevenueChaser.core import rawstore
    from RevenueChaser.core.providers import cache as cache_manager
    rows = [_row("AAPL", 1e9, spine_sector)]
    rows += [_row(p, cap, spine_sector) for p, cap in peers]
    if spine_sector is not None:
        rawstore.replace_all("equity_universe", rows)
    vendor = dict(financials, sector="Technology")                        # what yfinance says
    monkeypatch.setattr(cache_manager, "get_or_fetch", lambda t, fn, use_cache=True: vendor)
    # No store card for AAPL, so quick remarks would fall back to a live yfinance score: stub it (the same
    # empty card a store-only render gets)
    qr = fm.quick_remarks_engine
    monkeypatch.setattr(qr, "run_quick_remarks", lambda ticker, no_fetch=False, financials=None: qr._from_card(ticker, {}, "yfinance"))
    seen = {}
    monkeypatch.setattr(fm.comps_engine, "run_comps",
                        lambda fin, peers, **kw: seen.update(peers=peers, sector=fin.get("sector")) or {})
    return seen


def _row(ticker, market_cap, sector):
    return {"ticker": ticker, "cik": "", "name": ticker, "market_cap": market_cap, "price": 10.0,
            "sector": sector, "industry": "x", "industry_group": "", "sub_industry": "",
            "quote": {"symbol": ticker}}


def test_comps_peers_come_from_the_spine_universe(rawstore_tmp_db, tmp_path, monkeypatch, financials):
    """FIN-3: peers are the spine's names in the target's industry, not a static sector list."""
    monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
    seen = _run_comps(monkeypatch, financials, "Energy",
                      peers=[("XOM", 1.1e9), ("CVX", 0.9e9)])
    fm.run("AAPL", modules=["comps"], output="html", no_fetch=False)
    assert seen["peers"] == ["XOM", "CVX"]
    assert seen["sector"] == "Energy"


def test_comps_without_a_spine_row_has_no_peers(rawstore_tmp_db, tmp_path, monkeypatch, financials):
    monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
    seen = _run_comps(monkeypatch, financials, None)
    fm.run("AAPL", modules=["comps"], output="html", no_fetch=False)
    assert seen["peers"] == []
    assert seen["sector"] == "Unclassified"


# ── the sector band keys are the Morningstar names the spine stores (#266) ────────────────────────────
import pytest


def test_embedded_sector_bands_match_the_json_file():
    """The embedded fallback and ``config/sector_defaults.json`` carry the same keys and numbers, so the DCF band
    is the same whether or not the file is readable."""
    import json
    from RevenueChaser.core.gics_map import MORNINGSTAR_SECTORS
    from RevenueChaser.module_financials import config
    from_file = json.loads(config.SECTOR_DEFAULTS_FILE.read_text(encoding="utf-8"))
    assert set(config.SECTOR_DEFAULTS) == set(from_file)
    assert set(config.SECTOR_DEFAULTS) - {"default"} <= set(MORNINGSTAR_SECTORS)
    for key, band in from_file.items():
        assert {k: tuple(v) for k, v in band.items()} == {k: tuple(v) for k, v in config.SECTOR_DEFAULTS[key].items()}


@pytest.mark.parametrize("sector", ["Technology", "Consumer Defensive", "Financial Services", "Energy", "Healthcare"])
def test_dcf_band_is_the_sector_band_with_or_without_the_json_file(tmp_path, monkeypatch, sector):
    """Every banded Morningstar sector gets its own band, not ``default``, from the file and from the embedded copy."""
    from RevenueChaser.module_financials import config
    default = config.sector_defaults("Unclassified")
    from_file = config.sector_defaults(sector)
    assert from_file != default
    monkeypatch.setattr(config, "SECTOR_DEFAULTS_FILE", tmp_path / "missing.json")
    embedded = config.sector_defaults(sector)
    assert {k: tuple(v) for k, v in embedded.items()} == {k: tuple(v) for k, v in from_file.items()}
    assert embedded != config.sector_defaults("Unclassified")
