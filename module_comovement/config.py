"""Co-movement board configuration.

Clustering membership comes from daily **returns**, not from the sector or
industry classification or filing text. Everything here is a knob so the numbers
are testable rather than magic. See docs/plan_rest.md.
"""
from __future__ import annotations

from RevenueChaser.core.config import OUTPUT_DIR

COMOVEMENT_LATEST = OUTPUT_DIR / "comovement_latest.html"
COMOVEMENT_JSON = OUTPUT_DIR / "comovement" / "comovement_latest.json"

# ── Universe / OHLC ─────────────────────────────────────────────────────────
# Cluster the liquid slice of the shared universe. Microcaps/OTC have thin or
# absent history, which is noise for a return-correlation hierarchy. The floor is
# NOT defined here: ``core.universe.DEFAULT_MC_MIN`` ($300M) is the one definition
# and it is not editable (issue #40); read it from there.
# The span of D01 the board reads, back from D01's newest session (``ohlc.history_start``; #379 D01 switch): the
# history the old ``swing_ohlc`` cache held. The bars themselves come from Daily Bars only.
OHLC_PERIOD = "2y"

# ── Return matrix ───────────────────────────────────────────────────────────
WINDOW = 252          # last N sessions
MIN_OBS = 126         # drop a ticker with fewer non-null returns
MIN_PERIODS = 60      # pairwise Pearson minimum overlap
# A window never needs more than this share of its sessions (Co-movement review S2): ``min(MIN_OBS, window)`` asked
# the 6-month window for 126 of 126 returns, and one missing daily bar, which Daily Bars never goes back to fill,
# dropped the name for good. 0.95 lets the 126 window miss 7; the longer windows keep MIN_OBS (126).
MIN_OBS_MAX_SHARE = 0.95

# Window is a board control (like horizon); ``WINDOW`` above is only its default.
WINDOW_OPTIONS = (21, 42, 126, 189, 252, 378)   # 1m / 2m / 6m / 9m / 1y / 1.5y
# Below this many sessions the run scales MIN_OBS = MIN_PERIODS = window // 2
# (never under SHORT_MIN_FLOOR), locks the horizon to 1 and skips the lagged
# persistence test. Measured workable on the live store: 21 -> 3,603 clustered.
SHORT_WINDOW = 126
SHORT_MIN_FLOOR = 15

# Settings bounds (core/board_settings.py reads these; values default to the
# constants in this file).
MIN_OBS_LO = 60
MIN_PERIODS_LO = 20
OOS_HOLDOUT_LO, OOS_HOLDOUT_HI = 42, 126
WINSOR_K_LO, WINSOR_K_HI = 4.0, 8.0
BENCH_STALE_LO, BENCH_STALE_HI = 0, 5

# ── Robust inputs (S10) ─────────────────────────────────────────────────────
# A column is winsorised to median ± WINSOR_K · 1.4826 · MAD before the
# correlation, so one earnings gap or SPAC print cannot decide a group.
WINSOR_K = 5.0
# A name whose close does not move for FLAT_RUN sessions or more, over more
# than FLAT_SHARE of the window, is a halt/flat printer: excluded, and reported.
FLAT_RUN = 5
FLAT_SHARE = 0.02

# ── Horizon / remove-market (see docs/dashboard_ui.md §8b) ──────────────────
# Overlapping ``log(P_t / P_{t-n})`` returns. One benchmark at a time: SPY,
# DIA and QQQ are collinear, so regressing on all three is unstable.
HORIZONS = (1, 3, 5, 10)
# Decision D4 (2026-09-26): the default is the 1-day return. At h=5 the 252-row
# window holds only ~50 independent observations, and split-half ARI is 0.23 vs
# 0.36 at 1d (review §4d), so 1d is the more stable of the two.
DEFAULT_HORIZON = 1
DEFAULT_BENCHMARK = "spy"
BENCHMARKS = {"off": None, "spy": "SPY", "dia": "DIA", "qqq": "QQQ"}
BENCH_STALE_SESSIONS = 2   # benchmark this many sessions behind → truncate + warn
# #95: windows a Fetch also computes and stores (store-only, after the board is
# written), so switching to them serves the stored run instead of re-clustering.
PRECOMPUTE_WINDOWS = (126, 252)   # 6m, 1y
# A stored run with an ``as_of`` older than this is never served by a window
# switch or precompute (it always re-clusters instead). Bump it to the current
# UTC time whenever a change alters the engine's numbers at the same config
# hash and prices (e.g. #117's multi-day move) — otherwise a stale stored run
# could be served as if it already reflected the change.
# #379 D01 switch: the closes are D01's adjusted ones, not swing_ohlc's. Stamped when the R8a stack was frozen
# (2026-10-17 03:30 Amsterdam); the merge gate checks before the restart that no stored run is newer, since a run the
# older build stored after this time would be served until the next D01 write.
ENGINE_EPOCH = "2026-10-17T01:30:00+00:00"
# The engine's method, part of ``engine.config_hash``: bump it when a change alters a run's numbers at the same
# inputs. A stored run, null or lagged test of an older method then never matches, so it is neither served nor
# reused, with no race against a run the older build stores before the restart (unlike ``ENGINE_EPOCH``).
# 2: the Fable review of 28 Oct 2026 (held-out test winsorised, S3; h>1 null and factor count, S1; null mean, N1).
METHOD = 2

# ── Clustering ──────────────────────────────────────────────────────────────
# Ward (review 2026-10-01 §1): average linkage chained a quarter to half of the
# universe into one catch-all group; Ward on the same √(2(1−ρ)) distance (a
# Euclidean distance on standardised series, so Ward is admissible) explained
# 0.306 of next-year residual co-movement at k=80 vs 0.195 average and 0.244
# for GICS sub-industry (a measurement from before #266 retired the GICS tree).
LINKAGE = "ward"

# Cut tiers by universe size: (min_n, cuts). The first tier whose min_n <= n
# applies. Three views — broad / medium / tight — never a single hidden k.
CUT_TIERS: tuple[tuple[int, tuple[int, ...]], ...] = (
    (2000, (20, 80, 250)),
    (500, (10, 30, 80)),
    (0, (5, 15, 40)),
)

# S12: a group holding more than this share of the clustered names at its cut
# is the catch-all — a weak shared move, not a theme. Derived, never stored.
CATCHALL_SHARE = 0.40

# ── Out-of-sample persistence (review 2026-10-01 §1, persistence.py) ───────
# Groups are also formed on the window ending OOS_HOLDOUT sessions ago and
# tested on that last quarter only (Fetch-only, one extra clustering). A current
# group whose best Jaccard overlap with a lagged group is at least
# PERSIST_MIN_JACCARD is "the same group a quarter ago"; below it, "new".
OOS_HOLDOUT = 63
OOS_MIN_SHARE = 0.63  # held-out returns a name needs = round(0.63 x OOS_HOLDOUT)
OOS_MIN_OBS = round(OOS_MIN_SHARE * OOS_HOLDOUT)   # 40 at the default holdout
PERSIST_MIN_JACCARD = 0.5

# ── Labeling ────────────────────────────────────────────────────────────────
# Legacy majority rule (label_cluster, research scripts only): more than half
# the members share one industry or theme, and the cluster is big enough.
LABEL_MIN_SHARE = 0.5
# Both rules: a cluster smaller than this is never named.
LABEL_MIN_N = 4
# Enrichment rule (label_level, #97). Against the names clustered at that cut,
# an industry (else a theme) must pass every gate: BH q, hits, share and lift.
LABEL_Q_MAX = 0.01
LABEL_MIN_HITS = 4
LABEL_MIN_ENRICH_SHARE = 0.25
LABEL_MIN_LIFT = 3.0


def cuts_for(n: int) -> tuple[int, ...]:
    """Cut set for a universe of ``n`` tickers (see ``CUT_TIERS``)."""
    for min_n, cuts in CUT_TIERS:
        if n >= min_n:
            return cuts
    return CUT_TIERS[-1][1]
