"""Filesystem roots, API-key reads and rule tunables, in one place.

Every engine derives its paths from BASE_DIR / DATA_DIR / OUTPUT_DIR, so they
stay inside the RC_STORE_DIR / RC_CACHE_DIR / RC_EDGAR_DOCS_DIR overrides that
_root() reads here and nowhere else; set them before the first RevenueChaser
import. .store/ (DuckDB) and edgar_docs/ are durable, .cache/ is regenerable,
and keys come from the environment, then .env. The rule tunables sit in TUNABLES,
the flat name -> value registry the rule modules and the calibration replay
read. See docs/data_architecture.md; core/tests/test_store_layout.py
enforces the path-derivation rule.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env FIRST: the roots below read RC_STORE_DIR / RC_CACHE_DIR /
# RC_EDGAR_DOCS_DIR, and a value set in .env must count. (Real environment
# variables still win — load_dotenv never overrides them.)
load_dotenv(BASE_DIR / ".env")

# Both roots are overridable by environment variable, read HERE and nowhere
# else. Dozens of module-level constants are derived from them at import time
# (app/runners.py's *_LATEST, core/rawstore.py's DB_PATH, each module config's
# paths), and several modules re-bind those into their own namespace — so there
# is no reliable way to redirect the tree by monkeypatching after import. The
# env var is the single seam, and it must be set BEFORE the first RevenueChaser
# import. Used by the end-to-end harness to run a module against a throwaway
# store/cache; unset in normal use, where the repo-local defaults apply.
# (Same pattern as SPX_GAMMA_DATA_ROOT in module_spx_gamma/config.py.)
def _root(env_var: str, default: Path | None) -> Path | None:
    override = (os.getenv(env_var) or "").strip()
    return Path(override).expanduser().resolve() if override else default
# Durable raw data lives in one top-level folder (.store/) — the single DuckDB
# file plus nothing else. Everything in .store is DURABLE: it is the source of
# truth for caches/series and must not be deleted casually. Regenerable
# artifacts (rendered dashboards, *_latest.json/html, downloaded source zips)
# live in .cache/ and may be cleared at any time. See docs/data_architecture.md.
DATA_DIR = _root("RC_STORE_DIR", BASE_DIR / ".store")
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Raw content sits directly in .store/.
RAW_DATA_DIR = DATA_DIR
RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)

# Rendered outputs (HTML dashboards + their *_latest.json) live separately.
OUTPUT_DIR = _root("RC_CACHE_DIR", BASE_DIR / ".cache")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Regenerable dashboard artifacts (HTML + JSON). Clearable any time; re-running
# a module regenerates them. Distinct from CACHE_DIR (per-ticker raw cache).
CACHE_ROOT = OUTPUT_DIR
# The dashboard's own logs (start.ps1 writes launcher.log, dashboard.log and dashboard.err.log here; the
# Logs tab reads them) sit next to the scheduled tasks' logs (core/automations.py). Not created at import.
LOG_DIR = OUTPUT_DIR / "logs"
RAW_STORE_BACKUP_DIR = os.getenv("RAW_STORE_BACKUP_DIR", "")
# One raw snapshot per ISO week is kept in <backup dir>/weekly/ and never pruned (#277). It was a stopgap for the
# history that raw tables do not keep; the change history has landed (#279), so the owner turns it off with
# "0"/"false"/"no"/"off" (the PC .env does). The default stays "1"; copies already in weekly/ are left alone.
RAW_STORE_BACKUP_WEEKLY = os.getenv("RAW_STORE_BACKUP_WEEKLY", "1")
# A second folder OUTSIDE this machine's disk (a Google Drive for desktop folder or an external disk). After each
# verified nightly backup the app copies the newest snapshot set and the three mirror folders (not the weekly
# copies, #349) into its sub-folder RevenueChaser-backup, verified again, and keeps the newest N there (#275); nothing else in
# the folder is touched. Empty = off: the step does nothing. The folder must already exist (only its last level is
# created) and must not be the backup folder, inside it or around it; set it in .env, never in code.
RAW_STORE_BACKUP_MIRROR = os.getenv("RAW_STORE_BACKUP_MIRROR", "")


def _warn_gb(name: str, default: float) -> float:
    """A positive number of GB from the environment; empty, unparsable, zero or negative keeps ``default``."""
    try:
        value = float(str(os.getenv(name, "")).strip())
    except ValueError:
        return default
    return value if value > 0 else default


# #309 part 3: the Settings row of RC-Store-Backup shows the size of the local backup folder and of the off-machine
# folder (GB = 10^9 bytes) and warns when one is ABOVE its threshold. Defaults from avo (2026-10-16): 20 GB locally
# (the folder was 12.8 GB that day), 320 GB off-machine (80% of the 400 GB Google quota; the Drive trash counts
# toward it too, #349). One line each to change.
BACKUP_WARN_GB_DEFAULT = 20.0
BACKUP_MIRROR_WARN_GB_DEFAULT = 320.0
RAW_STORE_BACKUP_WARN_GB = _warn_gb("RAW_STORE_BACKUP_WARN_GB", BACKUP_WARN_GB_DEFAULT)
RAW_STORE_BACKUP_MIRROR_WARN_GB = _warn_gb("RAW_STORE_BACKUP_MIRROR_WARN_GB", BACKUP_MIRROR_WARN_GB_DEFAULT)

# Raw EDGAR 10-K/20-F HTML — the one file-based DURABLE artifact, keyed
# {cik}/{accession}.html.gz. Deliberately NOT under .store/ (that stays
# DuckDB-only) and NOT .cache/ (it is the durable raw record, not regenerable).
# See docs/data_architecture.md (the EDGAR store; the Phase 4 history is in the private notes).
EDGAR_DOCS_DIR = _root("RC_EDGAR_DOCS_DIR", BASE_DIR / "edgar_docs")
EDGAR_DOCS_DIR.mkdir(parents=True, exist_ok=True)

# The market store (#206, docs/data_architecture.md "The market store"): the builder's serving DuckDB files and
# ops.sqlite on the SSD, its raw downloads and Parquet on the HDD. The server sets both in its paths file; the
# repo-local defaults keep a checkout, the tests and the cloud off any server path. Not created at import: rcdata
# makes each folder when it first writes there.
MARKET_DIR = _root("RC_MARKET_DIR", BASE_DIR / ".market")
MARKET_RAW_DIR = _root("RC_MARKET_RAW_DIR", BASE_DIR / ".market_raw")
# Where the backup copies the newest raw days when they do not fit on the SSD (D3). Empty = off: the step logs a
# warning and copies nothing, so there is no default folder.
MARKET_RAW_COPY_DIR = _root("RC_MARKET_RAW_COPY_DIR", None)


def resolved_roots() -> dict[str, Path]:
    """Each root's override variable and the folder this process resolved it to, read at call time.

    A process started without the variables (a systemd unit, #158) would fall back to the checkout's own folders,
    so whoever starts one hands it these values. The one map of variable to root, kept beside ``_root``.
    """
    return {"RC_STORE_DIR": DATA_DIR, "RC_CACHE_DIR": OUTPUT_DIR, "RC_EDGAR_DOCS_DIR": EDGAR_DOCS_DIR}


UW_API_KEY = os.getenv("UNUSUAL_WHALES_API_KEY", "")
FRED_API_KEY = os.getenv("FRED_API_KEY", "")

# Plotly.js CDN used by HTML dashboards. Point at a local file to work offline.
PLOTLY_CDN_URL = os.getenv("PLOTLY_CDN_URL", "https://cdn.plot.ly/plotly-2.35.2.min.js")

CALENDAR_HORIZON_DAYS = 21

SCORE_RANGE = (-100, 100)
DIRECTION_RANGE = (-100, 100)
VOLATILITY_EDGE_RANGE = (0, 100)
CONFIDENCE_RANGE = (0, 100)

RULE_WEIGHTS = {
    # R1 reserved at 0.0: no driver data is wired yet (10-Q/bookings/revisions
    # would only re-read R3). Composite keeps the slot; restoring a weight > 0
    # re-activates it for a future rule1_drivers scorer.
    "rule1_drivers": 0.0,
    "rule2_guidance": 0.20,
    "rule3_financial": 0.20,
    # R5 is a TAG (lean/squeeze/package), never a direction vote (design doc).
    # Weight 0 keeps it out of the composite; the drawer surfaces its read.
    "rule5_positioning": 0.0,
    "rule7_accounting": 0.15,
    "rule8_sentiment": 0.10,
}

NRC_WEIGHTS = {
    "growth": 0.25,
    "acceleration": 0.20,
    "quality": 0.15,
    "event": 0.10,
}

# ── Two-Stage Funnel Filtering ──
MAX_DEEP_CHECKS_PER_DAY = 500
# Tickers scored at the same time inside an earnings run (the provider client is thread-safe).
EARNINGS_SCORE_WORKERS = 4
MIN_OPTION_VOLUME = 500
MIN_TOTAL_OI = 200
# compared with the day's call plus put premium the screener serves (pipeline.option_premium; Fable review N3).
MIN_DOLLAR_OPTION_VOLUME = 50_000
MIN_EXPECTED_MOVE_PCT = 1.5
MAX_EXPECTED_MOVE_PCT = 25.0
WATCHLIST_BYPASS = True
FLOW_SWEEP_BLOCK_MIN = 25_000
# Inst. Flow column: below this |net| lean premium the lean is Thin, and a package at or above it is material
# (package-risk). docs/rules/redesign.md R5.
INST_FLOW_MIN_NET = 25_000
# R5 institutional print: size >= 10 AND premium at or above this (redesign.md R5 "Institutional gate"; review S4).
INST_PRINT_MIN_PREMIUM = 10_000

# ── R4 GEX profile ──
# Phase 2 enrichment: pull strike-level spot exposures (Directionalized Volume)
# per deep-scored ticker to compute the volume-book regime flip, magnet, charm
# pin/peak and vanna lobes instead of trusting a single gex-levels flip whose
# book is ambiguous. Each fetch adds one UW call per ticker.
R4_GEX_PROFILE_ENABLED = True

# GEX contribution to the R4 vol-edge. Regime is spot vs the volume-book flip;
# a magnet within GEX_MAGNET_NEAR_PCT of spot nudges the edge (below = more
# room down, above = pin cap). The whole GEX block is clamped so EM/history
# keep the lead in the 0-100 edge.
GEX_REGIME_SHORT_GAMMA = 15.0
GEX_REGIME_LONG_GAMMA = -10.0
GEX_MAGNET_NEAR_PCT = 0.05
GEX_MAGNET_BELOW_POINTS = 8.0
GEX_MAGNET_ABOVE_POINTS = -8.0
GEX_BLOCK_MAX = 20.0
GEX_BLOCK_MIN = -15.0

# Fetch the IV term-structure curve per deep-scored ticker for R4's slope term
# (one extra UW call, same gating as R4_GEX_PROFILE_ENABLED). Off or a fetch
# failure -> R4 simply has no slope term; it never fabricates a curve.
R4_TERM_STRUCTURE_ENABLED = True

# ── R4 IV/RV + term-structure slope (carried from uw-earnings-vol-scan) ──
# IV/RV: rich-vol ceiling lowered from 1.5 to the skill's calibrated 1.25 — an
# earnings-front rich IV is a drag for a premium buyer. Cheap vol (< 1.0) is the
# +10 edge. The term-structure slope rewards contango (short-dated vol cheap)
# and penalises backwardation (front IV rich): values carried from the
# uw-earnings-vol-scan skill; for R4 they are a new calibration, not the skill's
# own filter claim.
IV_RV_RICH = 1.25
IV_RV_CHEAP = 1.0
IV_RV_CHEAP_POINTS = 10.0
IV_RV_RICH_POINTS = -5.0
TS_SLOPE_BACKWARDATION = -0.00406
TS_SLOPE_HALF = -0.002
TS_SLOPE_BACK_POINTS = -8.0
TS_SLOPE_MILD_POINTS = -3.0
TS_SLOPE_CONTANGO_POINTS = 5.0

# Strike window around spot used to qualify magnet / charm / vanna.
GEX_ATM_WINDOW_PCT = 0.15
# Charm Pin qualification: near spot AND dominant (top vs runner-up).
CHARM_PIN_NEAR_PCT = 0.05
CHARM_PIN_DOMINANCE_RATIO = 1.50

# ── Shared raw cache (per-ticker payloads in .store/raw.duckdb) ──
CACHE_TTL_DAYS = 1

# ── Rule tunables (single source of truth) ──
# Every rule reads these; rule modules expose them as module globals so the
# calibration engine can replay/sweep by patching either the module attribute
# or these config values. All are captured in config_snapshot() and therefore
# hashed into every decision (model_version / config_hash).
# R2 — guidance (EPS track record)
R2_MAX_WINDOW = 8
R2_RECENT_QUARTERS = 4
R2_SURPRISE_CLIP = 0.50
R2_EST_FLOOR = 0.10
R2_BEAT_MIN = 0.01
R2_BEAT_RATE_MIN = 5 / 8
R2_TREND_DELTA = 0.02
R2_CONSECUTIVE_MIN = 4
# R3 — financial quality (universal GAAP OM layer)
R3_OM_MAG_DOWN_BEAR = 8.0
R3_OM_MAG_DOWN_MILD = 2.0
R3_OM_MAG_UP_BULL = 2.0
R3_GM_HELD_YOY_BP = -0.005
R3_SGA_Z_SPIKE = 3.0
R3_MIN_OM_QUARTERS = 4
# R6 — macro (rate sensitivity)
R6_MIN_OBS = 20
R6_RATE_HIGH = 2.0
R6_RATE_VETO = 4.0
# R7 — accounting (Sloan)
R7_NI_MISMATCH_TOL = 0.01
# R8 — sentiment (retail call share). SIGN = +1 treats crowded retail call
# buying as momentum; -1 fades it (PEAD). Flip to retune the rule's polarity.
R8_RETAIL_FADE = 0.65
R8_SIGN = 1

# Registry captured by config_snapshot() (name -> value). Kept flat so a change
# is a one-line hash diff and the calibration replay can enumerate parameters.
TUNABLES = {
    "R2_MAX_WINDOW": R2_MAX_WINDOW,
    "R2_RECENT_QUARTERS": R2_RECENT_QUARTERS,
    "R2_SURPRISE_CLIP": R2_SURPRISE_CLIP,
    "R2_EST_FLOOR": R2_EST_FLOOR,
    "R2_BEAT_MIN": R2_BEAT_MIN,
    "R2_BEAT_RATE_MIN": R2_BEAT_RATE_MIN,
    "R2_TREND_DELTA": R2_TREND_DELTA,
    "R2_CONSECUTIVE_MIN": R2_CONSECUTIVE_MIN,
    "R3_OM_MAG_DOWN_BEAR": R3_OM_MAG_DOWN_BEAR,
    "R3_OM_MAG_DOWN_MILD": R3_OM_MAG_DOWN_MILD,
    "R3_OM_MAG_UP_BULL": R3_OM_MAG_UP_BULL,
    "R3_GM_HELD_YOY_BP": R3_GM_HELD_YOY_BP,
    "R3_SGA_Z_SPIKE": R3_SGA_Z_SPIKE,
    "R3_MIN_OM_QUARTERS": R3_MIN_OM_QUARTERS,
    "R4_IV_RV_RICH": IV_RV_RICH,
    "R4_IV_RV_CHEAP": IV_RV_CHEAP,
    "R4_IV_RV_CHEAP_POINTS": IV_RV_CHEAP_POINTS,
    "R4_IV_RV_RICH_POINTS": IV_RV_RICH_POINTS,
    "R4_TS_SLOPE_BACKWARDATION": TS_SLOPE_BACKWARDATION,
    "R4_TS_SLOPE_HALF": TS_SLOPE_HALF,
    "R4_TS_SLOPE_BACK_POINTS": TS_SLOPE_BACK_POINTS,
    "R4_TS_SLOPE_MILD_POINTS": TS_SLOPE_MILD_POINTS,
    "R4_TS_SLOPE_CONTANGO_POINTS": TS_SLOPE_CONTANGO_POINTS,
    "R6_MIN_OBS": R6_MIN_OBS,
    "R6_RATE_HIGH": R6_RATE_HIGH,
    "R6_RATE_VETO": R6_RATE_VETO,
    "R7_NI_MISMATCH_TOL": R7_NI_MISMATCH_TOL,
    "R8_RETAIL_FADE": R8_RETAIL_FADE,
    "R8_SIGN": R8_SIGN,
}


def to_float(v):
    """API değerini float'a çevirir; eksik/NaN/geçersiz -> None (asla 0)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN guard
        return None
    return f
