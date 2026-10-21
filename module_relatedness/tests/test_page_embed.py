"""REL-4 (#388, CL-A): the page embeds the companies without the two keys its script never reads.

``weights`` and ``profile`` were about a third of the 35.75 MB Relatedness page (family review REL-4). The page's
JavaScript reads neither, so only the embed drops them. The engine payload, ``relatedness_latest.json`` and the
result-history payload keep them: profiles are latest-only in raw.duckdb, so the history is their only per-run copy.
Offline; the store is a temp DuckDB, no LLM call.
"""
from __future__ import annotations

import json

from RevenueChaser.module_relatedness import engine, report
from RevenueChaser.module_relatedness.tests.test_relatedness_history_388 import (  # noqa: F401 - fixture
    _headers, _unpack, seeded)


def _embedded_companies(html: str) -> list[dict]:
    """The ``DATA`` array the page script gets (``var DATA = <json>;``)."""
    marker = "var DATA = "
    start = html.index(marker) + len(marker)
    end = html.index(";\n", start)
    return json.loads(html[start:end])


def test_the_page_embed_drops_weights_and_profile(seeded):
    payload = engine.run(no_fetch=False, tickers=seeded, run_id="r1")
    assert all("weights" in c and "profile" in c for c in payload["companies"])
    page = report.render_relatedness_dashboard(payload)
    embedded = _embedded_companies(page)
    assert [c["ticker"] for c in embedded] == [c["ticker"] for c in payload["companies"]]
    assert not any("weights" in c or "profile" in c for c in embedded)
    # Every other key the script reads is still there.
    assert {"ticker", "name", "sector", "theme_coarse", "stale"} <= set(embedded[0])
    # The render does not strip the caller's payload.
    assert all("weights" in c and "profile" in c for c in payload["companies"])


def test_the_saved_payload_and_the_history_keep_the_profiles(seeded):
    engine.run(no_fetch=False, tickers=seeded, run_id="r1")
    saved = json.loads(engine.config.RELATEDNESS_JSON.read_text(encoding="utf-8"))
    assert all(c.get("profile") and "weights" in c for c in saved["companies"])
    (head,) = _headers()
    stored = _unpack(head["payload"])
    assert len(stored["companies"]) == len(seeded)
    assert all(c.get("profile", {}).get("products") for c in stored["companies"])
