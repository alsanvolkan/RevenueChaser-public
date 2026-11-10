"""#255 item 2 on SPX Gamma: an /option-contracts answer of a full page (500) is paged, never kept cut."""
from __future__ import annotations

import json
from RevenueChaser.module_spx_gamma import store as spx_store

from .test_s2_fetch import DAY, _fetch, _handler

calls: list[dict] = []


def _contracts(n, start=0):
    return [{"option_symbol": f"SPXW260814C{7000000 + (start + i) * 5000:08d}", "strike": str(7000 + (start + i) * 5),
             "nbbo_bid": "1", "nbbo_ask": "2"} for i in range(n)]


def _paging(path, params):
    if path.endswith("/option-contracts"):
        calls.append(dict(params or {}))
        page = (params or {}).get("page", 0)
        return {"data": _contracts(500, 0) if page == 0 else _contracts(120, 500) if page == 1 else []}
    return _handler(path, params)


def test_a_full_first_page_is_followed_to_a_short_page(tmp_path):
    calls.clear()
    _uw, raw = _fetch(tmp_path, handler=_paging)
    chain_calls = [c for c in calls if c.get("expiry") == DAY]
    assert chain_calls[0].get("page") is None and chain_calls[1]["page"] == 1 and chain_calls[1]["limit"] == 500
    assert len(chain_calls) == 2
    assert len(raw["option_chain"]["data"]) == 620
    on_disk = json.loads(spx_store.raw_files(DAY)["option_chain.json"])
    assert len(on_disk["data"]) == 620 and on_disk["pages"] == [500, 120]


def test_a_repeated_key_across_pages_is_dropped_and_warned(tmp_path):
    def moved(path, params):
        if path.endswith("/option-contracts"):
            page = (params or {}).get("page", 0)
            return {"data": _contracts(500, 0) if page == 0 else _contracts(10, 495)}
        return _handler(path, params)
    _uw, raw = _fetch(tmp_path, handler=moved)
    assert len(raw["option_chain"]["data"]) == 505
    manifest = spx_store.raw_manifest(DAY)
    assert any("moved while paging" in w for w in manifest["warnings"])
