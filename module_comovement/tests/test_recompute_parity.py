"""#93: the recompute speed-up must not change a single number.

``golden_recompute_parity.json`` was captured from the code *before* #93 (the
per-column ``residualize``). The fixture is a small market: 160 names in 8
blocks on a SPY factor over 400 sessions, plus one short-history name and two
names with missing bars, so the residual mask, the short-history drop and the
benchmark split all take part. Every payload key is compared except the two
clock stamps (``as_of`` / ``generated_at``); floats compare with the tolerance
of ``test_settings._diff``.

#266 regenerated the golden once: the members now carry UW's two levels (``sector``, ``industry``, ``gics`` =
``[sector, industry]``) instead of the GICS keys, and the seed stores the eight old sub-industry strings as
``industry``. Before regenerating, ``payloads()`` was compared with the old golden with those member keys
removed: no difference in any other key (labels, ids, numbers). Then ``json.dumps(payloads())`` was written.

#97 (enrichment labels) regenerated it again: compared with the old golden with ``label`` removed and each cut sorted
by cluster id, no other key differed (numbers within the 1e-9 tolerance), so only the label text and the card order
it drives changed. Then ``json.dumps(payloads())`` was written.

#379 D01 switch regenerated it again: the market is seeded as D01 bars (``d01_bars.write_bars``) and read through
``core.prices.daily_bars(basis="adjusted")``. Compared with the old golden with ``source`` removed, no key differed
(floats within the 1e-9 tolerance; the last digits move by about 1e-15); ``source`` now names D01 and drops
``ttl_days``. Then ``json.dumps(payloads())`` was written.

#379 review RB-8 regenerated it once more: a Fetch downloads no prices, so the ``no_prices`` and ``missing_bars``
funnel steps carry ``fix`` = null instead of "Fetch prices". Compared key by key with the old golden: exactly those
8 ``fix`` values differ (4 cases x 2 steps), nothing else. Then ``json.dumps(payloads())`` was written.

Data Health S11 (#80, #124) regenerated it once more: every payload gains the additive ``provider_health`` key for
the provider-health banner. Compared with the old golden with ``provider_health`` removed: no key differed (floats
within the 1e-9 tolerance). Then ``json.dumps(payloads())`` was written.

The Fable review of 28 Oct 2026, S3 (the held-out quarter read on winsorised residuals) regenerated it once more.
Compared key by key with the old golden: the four ``config_hash`` values (the hash now carries ``config.METHOD``)
and, in the persisted case only, three ``oos_r`` and two ``oos_base`` values moved (by under 2e-4); no ``held``
verdict, member, label or other number changed. Then ``json.dumps(payloads())`` was written.

S1 of the same review (h > 1: the null from shuffled daily returns summed over h sessions, the factor count on every
h-th row) regenerated it again. Compared key by key: only ``w252_h5``'s ``n_factors`` moved, 10 -> 6 (the fixture
has eight blocks; 125-odd overlapping rows read as independent had counted more than there are). The 5-day case is
store-only, so its null is the stored one and no verdict moved. Then ``json.dumps(payloads())`` was written.

S2 (a window needs at most 95 % of its sessions) regenerated it again. Compared key by key: only ``w126_h1`` moved,
its ``min_obs`` 126 -> 119 (in ``settings``, ``funnel`` and the "Missing price bars" label) and so its
``config_hash``; the fixture's two gappy names miss more than 7 sessions and stay dropped, and no cluster changed.
Then ``json.dumps(payloads())`` was written.
"""
from __future__ import annotations

import json
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from RevenueChaser.core import rawstore
from RevenueChaser.module_comovement import engine
from RevenueChaser.module_comovement import returns as returns_mod
from RevenueChaser.module_comovement.tests import d01_bars
from RevenueChaser.module_comovement.tests.test_settings import _diff

_GOLDEN = Path(__file__).with_name("golden_recompute_parity.json")
# The same eight strings the golden was captured with (they were sub-industries then, #266 reads them
# from ``industry``): changing them would re-sort the equal-sized groups and move every cluster id.
_INDUSTRIES = ("Semiconductors", "Regional Banks", "Oil & Gas Exploration & Production",
               "Biotechnology", "Electric Utilities", "Application Software",
               "Restaurants", "Aerospace & Defense")
# (window, horizon, persist) — the persisted case runs last, so the three
# store-only cases see no stored run and judge nothing.
CASES = (("w252_h1", 252, 1, False), ("w126_h1", 126, 1, False),
         ("w252_h5", 252, 5, False), ("w252_h1_persist", 252, 1, True))


def _market(n: int = 400, blocks: int = 8, per: int = 20, seed: int = 93):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    mkt = rng.normal(0, 0.011, n)
    data = {"SPY": mkt}
    for b in range(blocks):
        factor = rng.normal(0, 0.006, n)
        for i in range(per):
            beta = 0.6 + 0.8 * rng.random()
            load = 0.5 + rng.random()
            data[f"B{b}N{i:02d}"] = (beta * mkt + load * factor
                                     + rng.normal(0, 0.008, n))
    ret = pd.DataFrame(data, index=idx)
    ret["SHORT"] = rng.normal(0, 0.01, n)
    ret.iloc[: n - 60, ret.columns.get_loc("SHORT")] = np.nan   # listed 60 sessions ago
    for name in ("B1N03", "B5N11"):                              # missing bars
        gaps = rng.choice(np.arange(1, n), size=25, replace=False)
        ret.iloc[gaps, ret.columns.get_loc(name)] = np.nan
    return ret


def _seed(ret: pd.DataFrame) -> list[str]:
    """Store the market (a NaN return = no bar that day) and its universe."""
    tickers = [t for t in ret.columns if t != "SPY"]
    rawstore.replace_all("equity_universe", [{
        "ticker": t, "market_cap": 1.0e10, "sector": "Technology",
        "price": 10.0, "name": t,
        "industry": (_INDUSTRIES[int(t[1])] if t.startswith("B") else "Unclassified"),
        "quote": {"symbol": t}} for t in tickers])
    now = time.time()
    rows = []
    for t in ret.columns:
        price = 100 * np.exp(ret[t].fillna(0.0).cumsum())
        for ts, p in price.items():
            if np.isnan(ret[t].loc[ts]):
                continue
            rows.append({"ticker": t, "ts": ts.isoformat(), "open": p, "high": p,
                         "low": p, "close": float(p), "volume": 1.0, "cached_at": now})
    d01_bars.write_bars(rows)
    return tickers


def _strip(p: dict) -> dict:
    out = {k: v for k, v in p.items() if k not in ("as_of", "generated_at")}
    return json.loads(json.dumps(out, default=str))


def payloads() -> dict:
    """The four case payloads on a freshly seeded store (also used to capture)."""
    tickers = _seed(_market())
    return {name: _strip(engine.run(no_fetch=True, tickers=tickers, window=w,
                                    horizon=h, persist=p))
            for name, w, h, p in CASES}


def test_recompute_reproduces_the_pre_change_payload(rawstore_tmp_db):
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    got = payloads()
    assert list(got) == list(golden)
    diffs = [d for name in golden for d in _diff(got[name], golden[name], name)]
    assert not diffs, diffs[:20]


def test_residualize_does_not_fragment_a_wide_frame():
    rng = np.random.default_rng(0)
    idx = pd.date_range("2025-01-01", periods=80, freq="B")
    ret = pd.DataFrame(rng.normal(0, 0.01, (80, 300)), index=idx,
                       columns=[f"T{i:03d}" for i in range(300)])
    market = pd.Series(rng.normal(0, 0.01, 80), index=idx)
    with warnings.catch_warnings():
        warnings.simplefilter("error", pd.errors.PerformanceWarning)
        out = returns_mod.residualize(ret, market)
    assert out.shape == ret.shape and list(out.columns) == list(ret.columns)
