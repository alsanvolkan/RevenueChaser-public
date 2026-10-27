"""Fetch and persist raw endpoint snapshots plus a SHA-256 audit manifest."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import uuid
from datetime import date, datetime, timezone

from RevenueChaser.core import calendar as nyse_calendar
from RevenueChaser.core import credentials, index_levels
from RevenueChaser.core.providers.cftc import fetch_cftc_tff
# The nightly D28/D29 sweeps store the VIX family and the COT in the same shape this board used to fetch live. The
# tables belong to the Earnings inputs step, so its reader is the one reader of them (see stored_inputs' docstring).
from RevenueChaser.module_earnings_radar import stored_inputs

from .candles import live_spot, price_window_sessions, stored_spx_candles, window_lag
from .config import RAW_ROOT, SPOT_EXPOSURE_WINDOW_PCT, TICKER
from .expiry_table import normalize_expiry_rows
from .vix import fetch_vix_family, vix_term_mode

logger = logging.getLogger(__name__)


class SnapshotError(RuntimeError):
    pass


class NoStoredClose(SnapshotError):
    """History mode: the store holds no ^GSPC close for the day, so no request was sent (a holiday, or a gap)."""


class UWApiError(RuntimeError):
    """One UW call that did not return a usable payload (status 0 = never got a response)."""

    def __init__(self, endpoint: str, status: int, message: str):
        self.endpoint = endpoint
        self.status = status
        self.message = message
        super().__init__(f"UW {endpoint} -> {status}: {message}")


def _uw_get(uw, path: str, params: dict | None, *, bare_list_ok: bool = False) -> dict:
    """One UW call through the shared ``UWClient.get_result`` (retry, backoff, breaker, health).

    Returns a payload with a ``data`` key, or raises :class:`UWApiError`. The one endpoint that
    answers with a bare JSON list (flow-per-strike) is wrapped when ``bare_list_ok``.
    """
    res = uw.get_result(path, params)
    if not res.ok:
        err = res.error or {}
        raise UWApiError(path, int(err.get("status") or 0), str(err.get("error") or "request failed"))
    payload = res.data
    if bare_list_ok and isinstance(payload, list):
        return {"data": payload}
    if not isinstance(payload, dict) or "data" not in payload:
        raise UWApiError(path, 200, "malformed payload: missing 'data'")
    return payload


def _mandatory_failure_message(name: str, path: str, tried: list[str],
                               status: int | None, detail: str) -> str:
    """Explain *why* a mandatory endpoint failed, not just that it did.

    "mandatory endpoint 'gex_levels' failed; aborting snapshot" gave no way to
    tell a dead API key from an unsupported date, which are the two things that
    actually happen. Name the status, the dates tried, and the fix.
    """
    if status in (401, 403):
        return credentials.rejected_message("unusual_whales", status)
    dates = " and ".join(tried) if tried else "the requested date"
    where = f"{name} ({path}) for {dates}"
    if status is None:
        return (f"SPX Gamma could not reach Unusual Whales for {where} — the "
                f"request failed before a response came back (network or timeout). "
                f"{detail}".strip())
    if status == 404:
        return (f"SPX Gamma has no gamma data for {dates}: {where} returned "
                f"HTTP 404. Unusual Whales serves gex-levels only for recent "
                f"sessions — pick a more recent trading day.")
    if status == 422:
        return (f"Unusual Whales rejected {dates} as invalid for {where} "
                f"(HTTP 422). This is usually a non-trading day or a session "
                f"that has not settled yet — pick an earlier weekday.")
    if status == 429:
        return (f"Unusual Whales rate-limited the SPX Gamma snapshot "
                f"({where}, HTTP 429). Wait a moment and retry.")
    return (f"SPX Gamma aborted: mandatory endpoint {where} returned "
            f"HTTP {status}. {detail}".strip())


# name -> (path template, base params). ``date`` params are filled with asof.
BASE_ENDPOINTS = {
    "gex_levels": ("/stock/{t}/gex-levels", {"date": None, "source": "oi"}),
    "gex_levels_vol": ("/stock/{t}/gex-levels", {"date": None, "source": "vol"}),
    "expiry_exposure": ("/stock/{t}/greek-exposure/expiry", {"date": None}),
    "strike_exposure": ("/stock/{t}/greek-exposure/strike", {"date": None}),
    "interpolated_iv": ("/stock/{t}/interpolated-iv", {}),
    "max_pain": ("/stock/{t}/max-pain", {}),
    "iv_rank": ("/stock/{t}/iv-rank", {}),
    "options_volume": ("/stock/{t}/options-volume", {}),
    "vol_term_structure": ("/stock/{t}/volatility/term-structure", {}),
    "variance_risk_premium": ("/stock/{t}/volatility/variance-risk-premium", {}),
    "realized_vol": ("/stock/{t}/volatility/realized", {}),
}
MANDATORY = ("gex_levels",)

# Spot-exposure strikes come back capped at this many rows per call (UW's limit).
SPOT_EXPOSURE_LIMIT = 500


_FILENAMES = {
    "gex_levels": "gex_levels.json",
    "gex_levels_vol": "gex_levels_vol.json",
    "expiry_exposure": "greek_exposure_expiry.json",
    "strike_exposure": "greek_exposure_strike.json",
    "interpolated_iv": "interpolated_iv.json",
    "spot_exposure": "spot_exposures_strike.json",
    "max_pain": "max_pain.json",
    "iv_rank": "iv_rank.json",
    "options_volume": "options_volume.json",
    "vol_term_structure": "vol_term_structure.json",
    "variance_risk_premium": "variance_risk_premium.json",
    "realized_vol": "realized_vol.json",
}

# Files that carry the CURRENT value whatever date was asked for (no ``date`` param),
# so a backfilled day holds today's number in them. Everything else is as-of the day.
# VIX and CFTC stay live until their store writers land (#176 comment 2026-10-04).
LIVE_FILES = frozenset(
    {_FILENAMES[n] for n, (_t, p) in BASE_ENDPOINTS.items() if "date" not in p}
    | {"option_chain.json", "vix_family.json", "cftc_tff.json"}
)

# History mode (the 2-year pull, D1 25 Oct 2026): only the calls that take a ``date`` (docs/openapi.yaml, the
# "Optional Market Date" parameter). The Fetch's table above is left as it is, so the board's Fetch requests and
# its LIVE_FILES do not change; here the five volatility endpoints that accept ``date`` are asked for the day.
# iv-rank is left out (the pull asks it once, as a series), options-volume takes no date at all.
HISTORY_ENDPOINTS = {
    name: (tmpl, {**params, "date": None})
    for name, (tmpl, params) in BASE_ENDPOINTS.items()
    if name in ("gex_levels", "gex_levels_vol", "expiry_exposure", "strike_exposure", "interpolated_iv",
                "max_pain", "vol_term_structure", "variance_risk_premium", "realized_vol")
}
# The endpoints above, flow-per-strike, the two spot-exposure halves and the 0DTE strike-expiry.
HISTORY_CALLS_PER_SESSION = len(HISTORY_ENDPOINTS) + 4
HISTORY_ORIGIN = "history_pull"
# A failure that may pass (throttled, server error, no response): a history day with such a hole is not kept,
# because a day that has its manifest is never pulled again.
_TRANSIENT = frozenset({0, 429})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _previous_est_trading_day(asof: str) -> str:
    """The NYSE session before ``asof`` (skips weekends and exchange holidays).

    Used to fall back when UW rejects today's date as "future in EST" (the gex
    endpoints reject a date whose America/New_York trading session has not yet
    settled). ISO strings in, ISO strings out. ``core.calendar`` covers 2025-2028
    and raises outside it; there the weekend-only rule is the best that is known.
    """
    from datetime import date as _date, timedelta
    try:
        d = _date.fromisoformat(asof) - timedelta(days=1)
    except ValueError:
        return asof
    try:
        return nyse_calendar.previous_trading_day(d).isoformat()
    except ValueError:
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d.isoformat()


def _stored_close(candle_fetcher, ticker: str, day: str) -> float:
    """The stored close of ``day`` itself (history mode's spot); :class:`NoStoredClose` when the store lacks it."""
    symbol = "^GSPC" if ticker == "SPX" else ticker
    for row in candle_fetcher(day, ticker=symbol):
        if row.get("date") == day and row.get("close") is not None:
            return float(row["close"])
    raise NoStoredClose(f"no stored {symbol} close for {day} in {index_levels.TABLE}; nothing was requested")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_snapshot(
    uw,
    asof_date: str,
    ticker: str = TICKER,
    data_root=None,
    mode: str = "open_interest",
    fetch_candles: bool = True,
    fetch_all_expiries: bool = False,
    overwrite: bool = True,
    vix_fetcher=None,
    cftc_fetcher=None,
    candle_fetcher=None,
    history_only: bool = False,
) -> dict | None:
    """Fetch one date's raw snapshot, replacing the cached copy only on success.

    ``uw`` is a ``core.providers.unusual_whales.UWClient`` (the job's own, so its call
    accounting and breaker see these calls). VIX and CFTC come from ``vix_fetcher()`` and
    ``cftc_fetcher()``, which default to the module's live fetch. Candles come from
    ``candle_fetcher(asof, ticker=...)``, which defaults to the stored ``index_levels_daily``
    rows (``module_spx_gamma.candles.stored_spx_candles``) — the snapshot makes no candle
    request of its own (D03). Each can be faked offline.

    ``overwrite=False`` (the backfill) never touches a day that already has its
    manifest: nothing is fetched and ``None`` comes back. The returned raw dict
    carries ``_meta`` with the requested and the effective (possibly fallback) date.

    The fetch stages into a scratch directory and is promoted over the existing
    ``{root}/{asof_date}`` at the very end. Deleting the target up-front (the
    old behaviour) destroyed a good cached day whenever the fetch then failed:
    clicking through the date picker with a dead API key wiped the stored GEX
    history and left empty directories behind — which in turn broke the
    "do we have a snapshot for this day?" checks that test for the directory.

    ``history_only=True`` (the history pull, ``module_spx_gamma.history_pull``) fetches a past session with dated
    calls only (``HISTORY_ENDPOINTS``, flow-per-strike, the spot-exposure halves, the 0DTE strike-expiry): spot is
    that day's stored ``index_levels_daily`` close (no stored close: :class:`NoStoredClose` before any request),
    VIX and COT come from the store or are left out, and there is no option chain, previous-day call, all-expiry
    ladder or previous-session fallback. A day with a transient failure (429, 5xx, no response) is not kept, and
    a day that completed meanwhile is never replaced. Its manifest says ``origin: history_pull``.
    """
    if history_only and fetch_all_expiries:
        raise ValueError("history mode never fetches the all-expiry ladder")
    root = data_root or RAW_ROOT
    if not overwrite and (root / asof_date / "manifest.json").exists():
        return None
    # One staging folder per call: a board Fetch's backfill and the history pull can stage the same day in one
    # process at once, and a shared name let each delete the other's files (#194 review).
    staging = root / f".partial-{os.getpid()}-{uuid.uuid4().hex[:12]}-{asof_date}"
    try:
        return _fetch_snapshot_staged(
            uw, asof_date, staging, root, ticker=ticker, mode=mode,
            fetch_candles=fetch_candles, fetch_all_expiries=fetch_all_expiries,
            vix_fetcher=vix_fetcher or fetch_vix_family,
            cftc_fetcher=cftc_fetcher or fetch_cftc_tff,
            candle_fetcher=candle_fetcher or stored_spx_candles, history_only=history_only)
    finally:
        # Promotion renames the staging dir away; anything left is a failed run.
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _fetch_snapshot_staged(
    uw,
    asof_date: str,
    raw_dir,
    root,
    ticker: str = TICKER,
    mode: str = "open_interest",
    fetch_candles: bool = True,
    fetch_all_expiries: bool = False,
    vix_fetcher=fetch_vix_family,
    cftc_fetcher=fetch_cftc_tff,
    candle_fetcher=stored_spx_candles,
    history_only: bool = False,
) -> dict:
    history_close = _stored_close(candle_fetcher, ticker, asof_date) if history_only else None
    if raw_dir.exists():
        shutil.rmtree(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Report date is captured before the gex fallback can reassign ``asof_date``
    # to the previous EST session; it is the front expiry the option chain is
    # fetched for (the live 0DTE chain for the report day).
    report_date = asof_date

    raw: dict = {}
    requests: list[dict] = []
    warnings: list[str] = []
    hashes: dict[str, str] = {}
    candle_source = "unusual_whales"
    uw_ohlc_status = None
    # No UW index-OHLC request is made any more: the window comes from the store (D03). The keys
    # stay for readers of older manifests, with attempted False and no entitlement reason.
    uw_ohlc_attempted = False

    # Last failure seen per endpoint name, so a mandatory abort can explain the
    # actual HTTP status instead of just "failed".
    failures: dict[str, dict] = {}

    # filename -> payload actually written, for the manifest's per-file table.
    written: dict[str, object] = {}
    limit_hits: dict[str, list[str]] = {}

    # per external input that is not a UW endpoint: read from the nightly store, or asked live (the manifest says so).
    input_sources: dict[str, dict] = {}

    def _request(name: str, path: str, params: dict) -> dict | None:
        try:
            payload = _uw_get(uw, path, params)
        except UWApiError as exc:
            requests.append({"name": name, "path": path, "params": params, "status": exc.status})
            warnings.append(f"{name}: {exc.message}")
            failures[name] = {"status": exc.status or None, "detail": exc.message,
                              "path": path, "date": params.get("date")}
            return None
        requests.append({"name": name, "path": path, "params": params, "status": 200})
        return payload

    def _persist(filename: str, payload, default=None) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=default).encode("utf-8")
        (raw_dir / filename).write_bytes(data)
        hashes[filename] = _sha256_bytes(data)
        written[filename] = payload

    def _call(name: str, path: str, params: dict, filename: str) -> dict | None:
        payload = _request(name, path, params)
        if payload is not None:
            _persist(filename, payload)
        return payload

    # 1. Base endpoints (gex_levels mandatory)
    for name, (tmpl, base_params) in (HISTORY_ENDPOINTS if history_only else BASE_ENDPOINTS).items():
        path = tmpl.format(t=ticker)
        params = {k: (asof_date if v is None else v) for k, v in base_params.items()}
        tried = [asof_date]
        payload = _call(name, path, params, _FILENAMES[name])
        if name in MANDATORY and payload is None and not history_only:
            # Retry once with the previous EST trading day: UW rejects a date
            # that is still "in the future" in America/New_York (early-morning /
            # pre-settlement runs), returning 422. Fall back so a stale-but-valid
            # prior session is used instead of aborting the whole snapshot.
            retry = _previous_est_trading_day(asof_date)
            if retry != asof_date:
                tried.append(retry)
                rparams = {k: (retry if v is None else v) for k, v in base_params.items()}
                rpayload = _call(name, path, rparams, _FILENAMES[name])
                if rpayload is not None:
                    warnings.append(f"{name}: {asof_date} rejected, used {retry}")
                    payload = rpayload
                    asof_date = retry
        if name in MANDATORY and payload is None:
            f = failures.get(name, {})
            raise SnapshotError(_mandatory_failure_message(
                name, path, tried, f.get("status"), f.get("detail", "")))
        if payload is not None:
            raw[name] = payload

    # 1a. flow-per-strike (answers with a bare JSON list, unlike the dict-wrapped endpoints).
    try:
        flow_payload = _uw_get(uw, f"/stock/{ticker}/flow-per-strike", {"date": asof_date},
                               bare_list_ok=True)
        requests.append({"name": "flow_per_strike", "path": f"/stock/{ticker}/flow-per-strike",
                         "params": {"date": asof_date}, "status": 200})
        raw["flow_per_strike"] = flow_payload
        _persist("flow_per_strike.json", flow_payload)
    except UWApiError as exc:  # flow degrades gracefully
        requests.append({"name": "flow_per_strike", "path": exc.endpoint,
                         "params": {"date": asof_date}, "status": exc.status})
        warnings.append(f"flow_per_strike failed: {exc.message}")

    # 1b. VIX family: the nightly D28 sweep's stored closes when the store has that session (no request at all),
    #     else yfinance (UW term-structure endpoint is a paid add-on). The readers are anchored on this snapshot's
    #     own session, so a re-render of an older day reads that day's stored closes rather than today's.
    try:
        asof_day = date.fromisoformat(asof_date)
    except (TypeError, ValueError):
        # No session to anchor on: the readers would default to the newest stored session, i.e. today's closes
        # stamped onto a day that is not today. Go live instead (review #103 S-1).
        asof_day = None
    try:
        vix, vix_session = stored_inputs.vol_index_rows(asof_day) if asof_day else ({}, None)
        if vix:
            input_sources["vix"] = {"source": "store", "session": vix_session.isoformat()}
        elif history_only:
            raise LookupError("not stored for this session (history mode asks no live source)")
        else:
            vix = vix_fetcher()
            input_sources["vix"] = {"source": "live"}
        vix["term_mode"] = vix_term_mode(vix.get("tickers", {}))
        raw["vix"] = vix
        _persist("vix_family.json", vix, default=str)
    except Exception as exc:  # noqa: BLE001 — VIX degrades gracefully
        warnings.append(f"vix_family failed: {exc}")

    # 1c. CFTC COT (TFF) — the nightly D29 row when it is stored, else the public Socrata read; best-effort
    try:
        cftc, cot_session = stored_inputs.cot_row(asof_day) if asof_day else ({}, None)
        if cftc:
            input_sources["cftc"] = {"source": "store", "session": cot_session.isoformat()}
        elif history_only:
            raise LookupError("not stored for this session (history mode asks no live source)")
        else:
            cftc = cftc_fetcher()
            input_sources["cftc"] = {"source": "live"}
        if cftc:
            raw["cftc"] = cftc
            _persist("cftc_tff.json", cftc, default=str)
        else:
            warnings.append("cftc_tff: no row returned")
    except Exception as exc:  # noqa: BLE001 — COT degrades gracefully
        warnings.append(f"cftc_tff failed: {exc}")

    # 2. Candles: the stored ^GSPC levels (D03), never a live fetch (UW index OHLC is permission-denied, G2-06).
    candles = []
    if fetch_candles:
        try:
            symbol = "^GSPC" if ticker == "SPX" else ticker
            candles = price_window_sessions(candle_fetcher(asof_date, ticker=symbol), asof_date)
            candle_source = f"{index_levels.TABLE} ({symbol})"
            if not candles:
                warnings.append(f"no stored index levels for {symbol} on {asof_date} (the daily bars "
                                f"--index-levels step has not written this window yet)")
            else:
                expected, last = window_lag(candles, asof_date)
                if expected:
                    warnings.append(f"stored index levels for {symbol} end {last}, before the last complete "
                                    f"session {expected} (the daily bars --index-levels step is behind)")
                    candle_source = f"{index_levels.TABLE} ({symbol}, stale to {last})"
        except Exception as exc:  # noqa: BLE001 — candles degrade gracefully
            warnings.append(f"ohlc/index_levels failed: {exc}")
            candle_source = "unavailable"
        ohlc = {"source": candle_source, "data": candles}
        raw["ohlc"] = ohlc
        _persist("ohlc_8wk.json", ohlc, default=str)

    # 3. Spot exposures by strike (charm/vanna), wide +/-25% window around spot.
    #    One call returns at most SPOT_EXPOSURE_LIMIT rows from the low end of the
    #    window, which cut off the upper wing on every stored day. Two calls on the
    #    same endpoint, below and above spot, each get their own cap.
    # Spot: live during an open session, the last complete close otherwise (the owner's choice, 2026-10-22).
    # It sets the +/-25% strike window below and the board's current_level, so a day-old spot moves which
    # strikes are fetched. The 8-week window above still costs no request.
    spot = candles[-1]["close"] if candles and candles[-1].get("close") is not None else None
    if history_only:
        # A past day's strike window is set from that day's close, never from today's level.
        spot = history_close
        raw["spot"] = {"source": f"index_levels_daily (close of {asof_date})", "value": spot}
    elif fetch_candles:
        live, live_error = live_spot(("^GSPC" if ticker == "SPX" else ticker))
        if live is not None:
            spot_source = "yfinance (live)"
            spot = live
        else:
            spot_source = "index_levels_daily (last complete close)"
            if live_error:                     # a failed live read is never silent
                warnings.append(f"live spot unavailable ({live_error}); using the last complete close "
                                f"{spot if spot is not None else 'none'}")
        raw["spot"] = {"source": spot_source, "value": spot}
    if spot is not None:
        lo = int(spot * (1 - SPOT_EXPOSURE_WINDOW_PCT))
        hi = int(spot * (1 + SPOT_EXPOSURE_WINDOW_PCT))
        mid = int(spot)
        path = f"/stock/{ticker}/spot-exposures/strike"
        halves = (("below", lo, mid), ("above", mid + 1, hi))
        merged: dict[str, dict] = {}
        failed_halves: list[str] = []
        for side, lo_s, hi_s in halves:
            params = {"date": asof_date, "min_strike": lo_s, "max_strike": hi_s,
                      "limit": SPOT_EXPOSURE_LIMIT}
            part = _request(f"spot_exposure_{side}", path, params)
            if part is None:
                failed_halves.append(side)
                continue
            rows = part.get("data") if isinstance(part, dict) else None
            rows = rows if isinstance(rows, list) else []
            if len(rows) >= SPOT_EXPOSURE_LIMIT:
                limit_hits.setdefault(_FILENAMES["spot_exposure"], []).append(side)
                warnings.append(
                    f"spot_exposure: limit of {SPOT_EXPOSURE_LIMIT} rows reached on the {side}-spot "
                    f"call ({lo_s}-{hi_s}); strikes beyond the last returned row are missing")
            for row in rows:
                merged.setdefault(str(row.get("strike")), row)
        if merged:
            payload = {"data": list(merged.values())}
            _persist(_FILENAMES["spot_exposure"], payload)
            raw["spot_exposure"] = payload
            if failed_halves:
                warnings.append(f"spot_exposure: partial, the {' and '.join(failed_halves)}-spot call failed")
    else:
        warnings.append("spot_exposure skipped: no spot price available")

    # 4. 0DTE strike-expiry for the same-day expiration only
    raw["strike_expiry"] = {}
    zero_dte = next((r for r in normalize_expiry_rows(raw.get("expiry_exposure"))
                     if r.get("expiry") == asof_date), None)
    if zero_dte:
        path = f"/stock/{ticker}/greek-exposure/strike-expiry"
        params = {"date": asof_date, "expiry": asof_date}
        filename = "zero_dte_strike_expiry.json"
        payload = _call("strike_expiry_0dte", path, params, filename)
        if payload is not None:
            raw["strike_expiry"][asof_date] = payload
    else:
        warnings.append("0DTE: no same-day expiration on asof_date")

    # 4b. All-expiry strike-expiry (for DTE-band walls: 181-365d put wall = 7500)
    raw["strike_expiry_all"] = {}
    if fetch_all_expiries:
        exp_rows = normalize_expiry_rows(raw.get("expiry_exposure"))
        for r in exp_rows:
            e = r.get("expiry")
            if not e:
                continue
            path = f"/stock/{ticker}/greek-exposure/strike-expiry"
            params = {"date": asof_date, "expiry": e}
            payload = _call(f"strike_expiry_{e}", path, params, f"strike_expiry_{e}.json")
            if payload is not None:
                raw["strike_expiry_all"][e] = payload
        if not raw["strike_expiry_all"]:
            warnings.append("strike_expiry_all: no expiries fetched")

    # 4c. 0DTE option chain (NBBO quotes + IV + Greeks + OI) for the Iron
    #     Butterfly "Pin Harvest" panel. Fetched for the report's front expiry;
    #     on an empty payload it retries the API-default live chain. Best-effort:
    #     the panel degrades to None when this is unavailable.
    raw["option_chain"] = {}
    if history_only:
        _oc = None                 # a chain is live whatever date is asked (LIVE_FILES): never in a past day
    else:
        _oc = _call("option_chain", f"/stock/{ticker}/option-contracts",
                    {"expiry": report_date}, "option_chain.json")
        if _oc is None or not (_oc.get("data") or []):
            warnings.append("option_chain: empty for report expiry; retrying live chain")
            _oc = _call("option_chain", f"/stock/{ticker}/option-contracts",
                        {}, "option_chain.json")
    if _oc is not None and (_oc.get("data") or []):
        raw["option_chain"] = _oc
    elif not history_only:
        warnings.append("option_chain: no chain available (panel degraded)")

    # 5. Previous-session strike exposure (for gamma vs prev close)
    prev_date = candles[-2]["date"] if len(candles) >= 2 else None
    if history_only:
        pass                       # the previous session is a day of its own in the pull
    elif prev_date:
        path = f"/stock/{ticker}/greek-exposure/strike"
        params = {"date": prev_date}
        filename = "greek_exposure_strike_prev.json"
        payload = _call("strike_exposure_prev", path, params, filename)
        if payload is not None:
            raw["strike_exposure_prev"] = payload
    else:
        warnings.append("strike_exposure_prev skipped: no prior session candle")

    if history_only:
        transient = [r["name"] for r in requests
                     if r["status"] is None or r["status"] in _TRANSIENT or r["status"] >= 500]
        if transient:
            raise SnapshotError(f"{asof_date} not kept: transient failure on {', '.join(transient)} "
                                f"(a later run pulls the day again)")

    def _rows(payload) -> int | None:
        data = payload.get("data") if isinstance(payload, dict) else None
        return len(data) if isinstance(data, list) else None

    files: dict[str, dict] = {}
    live_files = frozenset() if history_only else LIVE_FILES   # history mode asks nothing undated
    for fn, payload in sorted(written.items()):
        files[fn] = {"as_of": fn not in live_files, "rows": _rows(payload),
                     "limit_hit": fn in limit_hits}
    for fn, sides in limit_hits.items():
        files[fn]["limit_hit_sides"] = sides

    manifest = {
        "ticker": ticker,
        "requested_date": report_date,
        "effective_date": asof_date,
        "asof_date": asof_date,
        "first_session_date": candles[0]["date"] if candles else None,
        "last_session_date": candles[-1]["date"] if candles else None,
        "api_snapshot_time": _utc_now(),
        "timezone": "America/New_York",
        "candle_source": candle_source,
        "uw_ohlc_attempted": uw_ohlc_attempted,
        "uw_ohlc_status": uw_ohlc_status,
        "uw_ohlc_fallback_reason": None,
        "spot_exposure_mode": mode,
        "spot_exposure_search_bounds": [int(spot * (1 - SPOT_EXPOSURE_WINDOW_PCT)), int(spot * (1 + SPOT_EXPOSURE_WINDOW_PCT))] if spot else None,
        # The spot the bounds above were built from, so a Refresh or a re-render reads the same level this
        # Fetch used. Without it load_raw_day rebuilt spot from the stored window's last close, and a live
        # Fetch and a later Refresh of the same day disagreed by a whole session (#293 review MUST-1).
        "spot": raw.get("spot") or None,
        "zero_dte_expiry": asof_date if zero_dte else None,
        "zero_dte_expiry_present": bool(zero_dte),
        "methodology_version": "1.0",
        "source": "Unusual Whales API",
        "requests": requests,
        "input_sources": input_sources,
        "sha256": hashes,
        "files": files,
        "warnings": warnings,
    }
    if history_only:
        manifest["origin"] = HISTORY_ORIGIN
    raw["_meta"] = {"requested_date": report_date, "effective_date": asof_date,
                    "api_snapshot_time": manifest["api_snapshot_time"]}
    (raw_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # Promote: the snapshot is complete, so it may now replace the cached day.
    # ``asof_date`` may have been reassigned to the fallback session above, so
    # the destination is resolved here rather than up-front.
    final = root / asof_date
    if asof_date != report_date and (final / "manifest.json").exists():
        # Fallback run: this staging dir mixes the previous session's gex with
        # the report day's chain and candles. Never replace that session's
        # complete, non-regenerable raw day with it (S-2) — keep the cached day
        # and discard the staging copy (the caller's finally removes it).
        logger.warning("spx snapshot: %s rejected, fell back to %s; kept the complete "
                       "raw/%s and did not overwrite it", report_date, asof_date, asof_date)
        # The caller builds from what it is handed: hand it the stored day, not the staging raw
        # (today's live IV, chain and VIX), or the stored session is rebuilt from mixed inputs (M4).
        from .raw_day import load_raw_day
        return load_raw_day(final)
    if history_only and (final / "manifest.json").exists():
        # A Fetch completed this day while the pull was staging it: a complete day is never replaced.
        logger.warning("spx history pull: raw/%s completed meanwhile; kept it and discarded the pull", asof_date)
        return None
    if final.exists():
        shutil.rmtree(final)
    raw_dir.replace(final)
    return raw
