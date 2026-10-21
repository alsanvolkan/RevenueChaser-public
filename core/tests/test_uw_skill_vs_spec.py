"""Drift guard: RevenueChaser's UW endpoints + the UW skill whitelist must stay
truthful to the OpenAPI spec.

Three files are the contract:

- ``docs/openapi.yaml``          — authoritative Unusual Whales API export (paths:).
- ``core/providers/unusual_whales.py``,
  ``core/index_registry.py``,
  ``module_spx_gamma/snapshot.py`` — the code that actually calls the API.
- ``.opencode/skills/unusual-whales-api/SKILL.md`` — the anti-hallucination
  reference an agent consults before any UW call. It lives outside this repo
  (``C:/projects/OpenCode`` since 2026-09-25); ``_find_skill`` locates it, and
  ``RC_UW_SKILL`` overrides the path.

Every endpoint RevenueChaser references must exist in the spec AND appear in the
skill's whitelist; every whitelisted route must exist in the spec; and nothing
on the skill's blacklist may exist in the spec. Pure file reads — no network, no
provider imports, so this runs anywhere the repo does.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

_FILE = Path(__file__).resolve()
RC_ROOT = _FILE.parents[2]          # RevenueChaser/
WORKSPACE_ROOT = _FILE.parents[3]   # parent of RevenueChaser/ (C:/projects/Github)
SPEC = RC_ROOT / "docs" / "openapi.yaml"
_SKILL_REL = Path(".opencode") / "skills" / "unusual-whales-api" / "SKILL.md"


def _find_skill() -> Path | None:
    """Locate the UW skill; it lives outside this repo, so its home can move.

    ``RC_UW_SKILL`` wins. Otherwise try the OpenCode workspace
    (``<projects>/OpenCode``, where it has lived since the 2026-09-25 folder
    move), the old ``<projects>/Github`` location, then OpenCode's global
    skills dir. ``None`` when none exists — the skill tests then skip instead of
    failing on a machine that has no OpenCode workspace.
    """
    override = (os.getenv("RC_UW_SKILL") or "").strip()
    if override:
        return Path(override).expanduser()
    candidates = (
        WORKSPACE_ROOT.parent / "OpenCode" / _SKILL_REL,   # C:/projects/OpenCode (current)
        WORKSPACE_ROOT / _SKILL_REL,                        # C:/projects/Github (pre-move)
        Path.home() / ".config" / "opencode" / "skills" / "unusual-whales-api" / "SKILL.md",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


SKILL = _find_skill()
_needs_skill = pytest.mark.skipif(
    SKILL is None, reason="unusual-whales-api SKILL.md not found (set RC_UW_SKILL)")

# Endpoint templates referenced by RevenueChaser source today, in canonical spec
# form (/api prefix, {param} braces). Mirrors: the UW provider's call sites
# (core/providers/unusual_whales.py), the index registry
# (core/index_registry.py) and the SPX-gamma snapshot / client
# (module_spx_gamma/snapshot.py). Adding a new UW call site here keeps the
# skill honest; each entry is checked against both files below.
RC_ENDPOINTS = (
    # Calendar & earnings history (calendar_day_raw / earnings_data / earnings_history)
    "/api/earnings/premarket",
    "/api/earnings/afterhours",
    "/api/earnings/{ticker}",
    # Screener + ticker directory (screener_batch / screener_stocks / index registry)
    "/api/screener/stocks",
    "/api/stock-directory/ticker-exchanges",
    # Fundamentals (financials / income / balance / cash-flow)
    "/api/stock/{ticker}/financials",
    "/api/stock/{ticker}/income-statements",
    "/api/stock/{ticker}/balance-sheets",
    "/api/stock/{ticker}/cash-flows",
    # Flow (flow_recent / aggregate_flow / flow_per_expiry / spx flow-per-strike)
    "/api/stock/{ticker}/flow-recent",
    "/api/stock/{ticker}/flow-per-expiry",
    "/api/stock/{ticker}/flow-per-strike",
    # Dark pool
    "/api/darkpool/{ticker}",
    # Market
    "/api/market/oi-change",
    # Greeks & GEX (greeks / gex_levels / greek_exposure* / spot_exposures_by_strike)
    "/api/stock/{ticker}/greeks",
    "/api/stock/{ticker}/gex-levels",
    "/api/stock/{ticker}/greek-exposure",
    "/api/stock/{ticker}/greek-exposure/strike",
    "/api/stock/{ticker}/greek-exposure/expiry",
    "/api/stock/{ticker}/greek-exposure/strike-expiry",
    "/api/stock/{ticker}/spot-exposures/strike",
    # OI / IV / volume (oi_per_expiry / iv_rank / options_volume)
    "/api/stock/{ticker}/oi-per-expiry",
    "/api/stock/{ticker}/iv-rank",
    "/api/stock/{ticker}/options-volume",
    # Option tape & chains (option_trades / flow_alerts / option-contracts / expiry-breakdown)
    "/api/option-trades",
    "/api/option-trades/flow-alerts",
    "/api/stock/{ticker}/option-contracts",
    "/api/stock/{ticker}/expiry-breakdown",
    # SPX-gamma extras (interpolated_iv / max-pain / volatility*)
    "/api/stock/{ticker}/interpolated-iv",
    "/api/stock/{ticker}/max-pain",
    "/api/stock/{ticker}/volatility/term-structure",
    "/api/stock/{ticker}/volatility/realized",
    "/api/stock/{ticker}/volatility/variance-risk-premium",
    # Shorts / insider / 13F ownership / ETF holdings
    "/api/short_screener",
    "/api/shorts/{ticker}/interest-float/v2",
    "/api/insider/{ticker}",
    "/api/institution/{ticker}/ownership",
    "/api/etfs/{ticker}/holdings",
    # Price / info (ohlc / info)
    "/api/stock/{ticker}/ohlc/{candle_size}",
    "/api/stock/{ticker}/info",
)

_SPEC_PATH_RE = re.compile(r"^  (/api/\S+):\s*$")
_BULLET_ROUTE_RE = re.compile(r"^\s*-\s+[^`]*`(/api/[^`]+)`")
_BLACKLIST_ROUTE_RE = re.compile(r"- ❌ `(/api/[^`]+)`")


def _spec_paths() -> set[str]:
    assert SPEC.exists(), f"spec not found: {SPEC}"
    found = set()
    for line in SPEC.read_text(encoding="utf-8").splitlines():
        m = _SPEC_PATH_RE.match(line)
        if m:
            found.add(m.group(1))
    return found


def _skill_text() -> str:
    assert SKILL is not None and SKILL.exists(), f"skill not found: {SKILL}"
    return SKILL.read_text(encoding="utf-8")


def _skill_reference_routes() -> set[str]:
    """Routes under the 'Valid Endpoint Reference' section only — never the
    blacklist or concept map, which legitimately name non-endpoint strings."""
    text = _skill_text()
    start = text.index("## Valid Endpoint Reference")
    end = text.index("## Ground truth", start)
    found = set()
    for line in text[start:end].splitlines():
        m = _BULLET_ROUTE_RE.match(line)
        if m:
            found.add(m.group(1))
    return found


def _skill_blacklist_routes() -> set[str]:
    text = _skill_text()
    found = set()
    for line in text.splitlines():
        m = _BLACKLIST_ROUTE_RE.search(line)
        if m and not m.group(1).endswith("..."):
            found.add(m.group(1))
    return found


def test_rc_endpoints_all_exist_in_spec():
    spec = _spec_paths()
    assert spec, "no /api paths parsed from the spec — extraction may be broken"
    assert len(spec) >= 200, f"expected ~207 spec paths, parsed {len(spec)}"
    missing = [e for e in RC_ENDPOINTS if e not in spec]
    assert not missing, (
        "RevenueChaser calls endpoints that are not in docs/openapi.yaml:\n  "
        + "\n  ".join(missing)
    )


@_needs_skill
def test_skill_whitelist_lists_all_rc_endpoints():
    skill = _skill_reference_routes()
    assert skill, "no routes parsed from the skill reference — extraction may be broken"
    missing = [e for e in RC_ENDPOINTS if e not in skill]
    assert not missing, (
        "Endpoints RevenueChaser uses are missing from the skill whitelist:\n  "
        + "\n  ".join(missing)
    )


@_needs_skill
def test_skill_whitelist_routes_all_exist_in_spec():
    spec = _spec_paths()
    skill = _skill_reference_routes()
    stale = sorted(skill - spec)
    assert not stale, (
        "Skill whitelist names routes that are not in docs/openapi.yaml "
        "(stale or typo'd — remove or fix them):\n  "
        + "\n  ".join(stale)
    )


@_needs_skill
def test_skill_blacklist_routes_are_not_real():
    spec = _spec_paths()
    real = sorted(_skill_blacklist_routes() & spec)
    assert not real, (
        "Blacklisted routes now exist in the spec — the blacklist is stale:\n  "
        + "\n  ".join(real)
    )
