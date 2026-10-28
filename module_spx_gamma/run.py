"""End-to-end pipeline: fetch -> normalize -> chart -> render (SHA-256 audit)."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import date
from functools import partial
from pathlib import Path

from RevenueChaser.core.calendar import is_trading_day, today_ny
from RevenueChaser.core.providers.unusual_whales import UWClient

from .charts import position_map
from .charts.tornado import build_tornado_fig
from .brier import BRIER_FILENAME
from .config import DATA_ROOT, HISTORY_FILENAME, TICKER, UW_API_TOKEN
from . import safe_json
from .brier import record_probs
from .normalize import normalize_snapshot
from .render import render_html, render_markdown
from .scenarios import build_scenarios
from .raw_day import load_raw_day
from .snapshot import fetch_snapshot
from . import store_reads

logger = logging.getLogger(__name__)

HISTORY_PATH = DATA_ROOT / HISTORY_FILENAME
HISTORY_LOOKBACK_DAYS = 10  # keep ~2 trading weeks of daily Net GEX records


def _load_history(path: Path | None = None) -> dict:
    """History for display (render, API): a corrupt file reads as empty here but
    is left untouched — only the writer quarantines it (S-1)."""
    p = path or HISTORY_PATH
    try:
        data = safe_json.load_json(p, {})
    except (safe_json.CorruptJsonError, OSError) as exc:
        logger.error("spx history unreadable, shown as empty: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _load_history_for_update(path: Path | None = None) -> dict:
    """History for a write: a corrupt file is moved aside and the write refused,
    so a torn file never becomes a one-record history (S-1)."""
    p = path or HISTORY_PATH
    data = safe_json.load_for_update(p, {})
    if not isinstance(data, dict):
        safe_json.quarantine(p)
        raise safe_json.CorruptJsonError(p, f"expected an object, got {type(data).__name__}")
    return data


def _save_history(history: dict, path: Path | None = None) -> None:
    safe_json.write_json_atomic(path or HISTORY_PATH, history)


def _record_history(snapshot: dict, asof_date: str, path: Path | None = None) -> None:
    """Append today's EOD GEX record, keeping only the last ~2 weeks."""
    from datetime import date as _date
    vix = (snapshot.get("vix") or {}).get("vix")
    try:
        is_friday = _date.fromisoformat(asof_date).weekday() == 4
    except ValueError:
        is_friday = False
    history = _load_history_for_update(path)
    history[asof_date] = {
        "is_friday_close": is_friday,
        "spot": snapshot.get("current_level"),
        "net_dex": snapshot.get("net_dex"),
        "net_gex_oi": snapshot.get("net_gamma_total"),
        "net_vanna": snapshot.get("net_vanna"),
        "live_charm": snapshot.get("live_charm"),
        "vanna_at_spot": snapshot.get("vanna_at_spot"),
        "charm_live_sum": snapshot.get("charm_live_sum"),
        "matrix_gex": snapshot.get("matrix_gex"),
        "source_chip": snapshot.get("source_chip"),
        "regime": snapshot.get("regime"),
        "vix": vix,
    }
    # prune to the newest N records (sorted by date)
    kept = dict(sorted(history.items()))
    if len(kept) > HISTORY_LOOKBACK_DAYS:
        kept = dict(list(kept.items())[-HISTORY_LOOKBACK_DAYS:])
    _save_history(kept, path)


def _is_session(d) -> bool:
    """NYSE session test; outside the calendar table (2025-2028) only weekends are known."""
    try:
        return is_trading_day(d)
    except ValueError:
        return d.weekday() < 5


def _trading_days_before(today, days: int) -> list[str]:
    """The ``days`` NYSE sessions strictly before ``today``, oldest first."""
    from datetime import timedelta
    out: list[str] = []
    d = today - timedelta(days=1)
    while len(out) < days:
        if _is_session(d):
            out.append(d.isoformat())
        d -= timedelta(days=1)
    out.reverse()
    return out


def _missing_backfill_days(history: dict, today, days: int) -> list[str]:
    """Recent sessions (never ``today``) that have no history record yet."""
    return [day for day in _trading_days_before(today, days) if day not in history]


def _backfill_candidates(days: int) -> list[str]:
    """The last ``days`` sessions before today in New York, oldest first."""
    return _trading_days_before(today_ny(), days)


def _backfill_history(root: Path, uw, ticker: str,
                      asof_date: str, days: int = HISTORY_LOOKBACK_DAYS) -> None:
    """Backfill missing recent GEX days (idempotent, ~2 weeks).

    For each of the last ``days`` New York sessions before today without a history
    record, fetch a full snapshot (gex-levels + greek-exposure/strike + expiry + VIX)
    and record Net GEX. A day whose raw files are already complete is left alone.
    Short sleeps keep UW rate limits happy.
    """
    import time

    history_path = root / HISTORY_FILENAME
    history = _load_history_for_update(history_path)
    missing = _missing_backfill_days(history, today_ny(), days)
    if not missing:
        return

    from .snapshot import fetch_snapshot as _fs
    for day in missing:
        try:
            raw = _fs(uw, day, ticker=ticker, data_root=root / "raw",
                      mode="open_interest", fetch_all_expiries=True, overwrite=False)
            if raw is None:
                # A complete raw day is never overwritten, but its history row may be missing (a
                # quarantined history file, a run that died after the promote): rebuild it from
                # disk, no network.
                raw = load_raw_day(root / "raw" / day)
            effective = (raw.get("_meta") or {}).get("effective_date") or day
            snap = normalize_snapshot(with_store_inputs(raw, effective), effective, ticker=ticker,
                                      mode="open_interest")
            _record_history(snap, effective, history_path)
            time.sleep(0.2)
        except Exception as exc:  # noqa: BLE001 — best effort per day
            logger.warning("spx backfill %s skipped: %s", day, exc)
            continue


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_prev_snapshot(root: Path, asof_date: str) -> dict | None:
    """Load the prior session's normalized snapshot for week-over-week deltas."""
    prev_dir = root / "parsed"
    if not prev_dir.exists():
        return None
    candidates = [d for d in prev_dir.iterdir() if d.is_dir() and d.name < asof_date]
    if not candidates:
        return None
    newest = max(candidates, key=lambda d: d.name)
    snap_file = newest / "snapshot.json"
    if not snap_file.exists():
        return None
    try:
        return json.loads(snap_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _export_chart(fig, chart_dir: Path, name: str, png: bool = True):
    """Write the chart's HTML (always) and PNG (``png=True``, a Fetch). Returns (html, png, fragment).

    The PNG export is the slow step (kaleido, ~3.5 s each); a Refresh passes ``png=False`` (#376).
    """
    if fig is None:
        return None, None, None
    html_path = chart_dir / f"{name}.html"
    fig.write_html(str(html_path), include_plotlyjs=False, full_html=True)
    fragment = fig.to_html(full_html=False, include_plotlyjs=False, config={"responsive": True})
    png_path = None
    if not png:
        return html_path, None, fragment
    try:
        png_path = chart_dir / f"{name}.png"
        fig.write_image(str(png_path))
    except Exception:  # noqa: BLE001 — kaleido may be unavailable
        png_path = None
    return html_path, png_path, fragment


def _historical_live_files(day_dir: Path) -> list[str]:
    """The live files (manifest ``as_of`` false) of a day fetched after a later session opened.

    Live files carry the value at fetch time whatever date was asked for (snapshot.LIVE_FILES),
    so in a backfilled or re-fetched past day they belong to a later session and the board shows
    them as "n/a (historical)" (SPX-7 option B). A later session counts from its 09:30 New York
    open: a day fetched in its own session, before the next open (a pre-open fallback to the
    previous session) or over a weekend keeps them. Unreadable manifests mark nothing.
    """
    from datetime import datetime, time as _time, timedelta, timezone
    from zoneinfo import ZoneInfo
    try:
        manifest = json.loads((Path(day_dir) / "manifest.json").read_text(encoding="utf-8"))
        effective = date.fromisoformat(manifest.get("effective_date") or manifest.get("asof_date"))
        fetched = datetime.fromisoformat(str(manifest["api_snapshot_time"]).replace("Z", "+00:00"))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        logger.debug("spx manifest gives no fetch time for %s: %s", day_dir, exc)
        return []
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)   # the snapshot stamps UTC (snapshot._utc_now)
    fetched = fetched.astimezone(ZoneInfo("America/New_York"))
    d = effective + timedelta(days=1)
    later_session = False
    while d <= fetched.date():
        if _is_session(d) and (d < fetched.date() or fetched.time() >= _time(9, 30)):
            later_session = True
            break
        d += timedelta(days=1)
    if not later_session:
        return []
    files = manifest.get("files") or {}
    return sorted(fn for fn, meta in files.items() if isinstance(meta, dict) and meta.get("as_of") is False)


def with_store_inputs(raw: dict, day: str) -> dict:
    """``raw`` with the inputs the market store holds for ``day`` (PR 3): the VIX family, the CFTC COT, the SPX
    closes of the window and the 0-1 DTE dollar gamma, each with its "not stored" marker (``store_reads.read_day``).

    Read at render time for the day rendered, so a Fetch and a later Refresh show the same thing; a raw day of an
    older Fetch that still holds vix_family.json, cftc_tff.json or ohlc_8wk.json is overridden by the store.
    """
    return {**raw, **store_reads.read_day(day)}


def build_day(raw: dict, requested_date: str, root: Path, ticker: str = TICKER,
              mode: str = "open_interest", update_latest: bool = True, record: bool = True,
              write_png: bool = True, builder_request: dict | None = None) -> dict:
    """Everything after the fetch: one raw day -> snapshot, charts, report, latest.json, history.

    Pure of the network, so Refresh runs it on a stored day. The day is filed under the raw day's
    effective date, never the requested one (a fallback session is not the requested day, G3).
    ``update_latest=False`` leaves the ``latest.json`` pointer alone (rebuilding an older day).
    ``record=False`` leaves the Brier record alone: only a Fetch records the day's probabilities,
    a Refresh never rewrites the audit file (SPX-4).
    ``write_png=False`` skips the PNG export (a Refresh, #376): PNGs a Fetch wrote stay on disk and
    keep their report reference, but the manifest records no sha256 for a PNG this run did not write.
    ``builder_request`` is what a Fetch queued for the builder (PR 3, A4); the board says so in one line.
    """
    asof_date = (raw.get("_meta") or {}).get("effective_date") or requested_date
    history_path = root / HISTORY_FILENAME
    brier_path = root / BRIER_FILENAME

    snapshot = normalize_snapshot(with_store_inputs(raw, asof_date), asof_date, ticker=ticker, mode=mode)
    snapshot["historical_live_files"] = _historical_live_files(root / "raw" / asof_date)
    snapshot["builder_request"] = builder_request

    parsed_dir = root / "parsed" / asof_date
    parsed_dir.mkdir(parents=True, exist_ok=True)
    (parsed_dir / "snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, default=str, indent=2), encoding="utf-8")

    # Predictive scenario & directional engine payload (cache-first: the dashboard
    # serves this without any UW API call on Refresh Tab).
    (parsed_dir / "prediction.json").write_text(
        json.dumps(snapshot.get("prediction", {}), ensure_ascii=False, default=str, indent=2),
        encoding="utf-8")

    chart_dir = root / "charts" / asof_date
    kept_png = None
    if chart_dir.exists():
        if write_png:
            shutil.rmtree(chart_dir)
        else:                                      # keep the Fetch's PNGs, replace the rest
            for old in chart_dir.iterdir():
                if old.suffix != ".png":
                    old.unlink() if old.is_file() else shutil.rmtree(old)
            if (chart_dir / "position_map.png").exists():
                kept_png = chart_dir / "position_map.png"
    chart_dir.mkdir(parents=True, exist_ok=True)
    export = _export_chart if write_png else partial(_export_chart, png=False)
    # Three standalone charts: position map (candles+levels), tornado (Net GEX),
    # cumulative envelope (dealer-flow liquidity layer).
    fig = position_map.build(snapshot, include_tornado=False)
    html_path, png_path, frag = export(fig, chart_dir, "position_map")
    tornado_fig = build_tornado_fig(snapshot)
    tornado_html, tornado_png, tornado_frag = export(tornado_fig, chart_dir, "tornado")
    env_fig = position_map.build_envelope_fig(snapshot)
    env_html, env_png, env_frag = export(env_fig, chart_dir, "cumulative_envelope")

    report_dir = root / "reports" / asof_date
    report_dir.mkdir(parents=True, exist_ok=True)
    png_ref = str(png_path or kept_png) if (png_path or kept_png) else None
    prev_snapshot = _load_prev_snapshot(root, asof_date)
    md = render_markdown(snapshot, chart_png=png_ref, prev_snapshot=prev_snapshot, data_root=root)
    html = render_html(snapshot, chart_png=png_ref, chart_html=frag, prev_snapshot=prev_snapshot,
                       tornado_html=tornado_frag, envelope_html=env_frag, data_root=root)
    (report_dir / "section.md").write_text(md, encoding="utf-8")
    (report_dir / "section.html").write_text(html, encoding="utf-8")

    # Brier scorecard: record this week's scenario probabilities (honest audit), Fetch only
    if record:
        try:
            scenarios = build_scenarios(snapshot).get("scenarios", [])
            probs = {s["name"]: s["p"] for s in scenarios}
            record_probs(asof_date, probs, path=brier_path)
        except safe_json.CorruptJsonError as exc:
            logger.error("spx brier not recorded (file kept aside): %s", exc)
        except Exception as exc:  # noqa: BLE001 — scorecard is best-effort
            logger.warning("spx brier record failed: %s", exc)

    # Final audit manifest with SHA-256 of raw inputs and rendered artifacts
    raw_dir = root / "raw" / asof_date
    hashes = {}
    for p in sorted(raw_dir.glob("*.json")):
        hashes[p.name] = _sha256(p)
    for p in (html_path, png_path):
        if p and p.exists():
            hashes[p.name] = _sha256(p)

    manifest = {
        "ticker": ticker,
        "requested_date": requested_date,
        "effective_date": asof_date,
        "asof_date": asof_date,
        "first_session_date": snapshot.get("first_session_date"),
        "last_session_date": snapshot.get("last_session_date"),
        "api_snapshot_time": snapshot.get("api_snapshot_time"),
        "timezone": "America/New_York",
        "candle_source": snapshot.get("candle_source"),
        "spot_exposure_mode": mode,
        "charm_qualification": snapshot.get("charm_qualification"),
        "vanna_peaks": snapshot.get("vanna_peaks"),
        "regime": snapshot.get("regime"),
        "current_level": snapshot.get("current_level"),
        "spx_spot": snapshot.get("current_level"),
        "put_wall_vol": (snapshot.get("levels") or {}).get("put_wall_vol"),
        "call_wall_vol": (snapshot.get("levels") or {}).get("call_wall_vol"),
        "report_html": str(report_dir / "section.html"),
        "methodology_version": "1.0",
        # The market-store inputs this render read (PR 3): source, source_as_of and status, or "not stored".
        "input_sources": snapshot.get("store_inputs"),
        "sha256": hashes,
    }
    if update_latest:
        safe_json.write_json_atomic(root / "latest.json", manifest)

    # EOD GEX history record (daily Net GEX for Δ + calendar coloring).
    try:
        _record_history(snapshot, asof_date, history_path)
    except safe_json.CorruptJsonError as exc:
        logger.error("spx history not recorded (file kept aside): %s", exc)
    except Exception as exc:  # noqa: BLE001 — history is best-effort
        logger.warning("spx history record failed: %s", exc)

    return {
        "snapshot": snapshot,
        "latest": manifest,
        "fig": fig,
        "paths": {
            "raw": raw_dir,
            "parsed": parsed_dir / "snapshot.json",
            "chart_html": html_path,
            "chart_png": png_path or kept_png,
            "report_md": report_dir / "section.md",
            "report_html": report_dir / "section.html",
        },
    }


def serves_from_disk(asof_date: str, data_root=None) -> bool:
    """True when :func:`run` rebuilds ``asof_date`` from disk instead of fetching it (#179 D5):
    a date before today in New York whose raw day is complete (it has its manifest)."""
    root = Path(data_root) if data_root else DATA_ROOT
    return (date.fromisoformat(asof_date) < today_ny()
            and (root / "raw" / asof_date / "manifest.json").exists())


def run(asof_date: str, data_root=None, token: str | None = None, mode: str = "open_interest",
        ticker: str = TICKER, fetch_all_expiries: bool = True, uw=None, force: bool = False,
        builder_request: dict | None = None) -> dict:
    """Run the full pipeline for one asof date; returns paths + snapshot.

    ``uw`` is the job's ``UWClient`` (the dashboard passes it, so its call accounting and breaker
    see these calls); without one a client is built from ``token`` or the configured key.

    A past date whose raw day is complete (it has its manifest) is rebuilt from disk with no
    network and its raw files are never rewritten (#179 D5, SPX-1): the day's live files can not
    be fetched again. ``force=True`` refetches it on purpose. Today, and a past date with no
    complete raw day, are fetched as before.
    """
    root = Path(data_root) if data_root else DATA_ROOT
    if not force and serves_from_disk(asof_date, root):
        return refresh(asof_date, data_root=root, ticker=ticker, mode=mode, builder_request=builder_request)
    if uw is None:
        tok = token if token is not None else UW_API_TOKEN
        if not tok:
            raise RuntimeError("UNUSUAL_WHALES_API_KEY not set")
        uw = UWClient(tok)
    elif not getattr(uw, "key", None):
        raise RuntimeError("UNUSUAL_WHALES_API_KEY not set")

    raw = fetch_snapshot(uw, asof_date, ticker=ticker, data_root=root / "raw", mode=mode,
                         fetch_all_expiries=fetch_all_expiries)
    # Only the newest stored day moves latest.json, which drives the global Regime badge (SPX-3).
    effective = (raw.get("_meta") or {}).get("effective_date") or asof_date
    newest = latest_stored_day(root)
    result = build_day(raw, asof_date, root, ticker=ticker, mode=mode,
                       update_latest=newest is None or effective >= newest, builder_request=builder_request)

    # Backfill of missing recent GEX history days (idempotent, ~2 weeks) comes last: it is
    # many UW calls and the requested report must not wait for, or fail on, them.
    try:
        _backfill_history(root, uw, ticker, result["latest"]["asof_date"])
    except Exception as exc:  # noqa: BLE001 — backfill is best-effort
        logger.warning("spx history backfill failed: %s", exc)
    return result


def latest_stored_day(root: Path) -> str | None:
    """The newest raw day that has its manifest (a complete fetch), or None."""
    raw_root = root / "raw"
    if not raw_root.exists():
        return None
    days = sorted(d.name for d in raw_root.iterdir()
                  if d.is_dir() and not d.name.startswith(".") and (d / "manifest.json").exists())
    return days[-1] if days else None


def refresh(day: str | None = None, data_root=None, ticker: str = TICKER,
            mode: str = "open_interest", builder_request: dict | None = None) -> dict | None:
    """Rebuild a stored day's snapshot, charts and report from disk with no network.

    ``day`` defaults to the newest stored raw day. Returns the same dict as :func:`run`, or None
    when there is no stored day to rebuild. The raw files and the Brier record are never written.
    """
    root = Path(data_root) if data_root else DATA_ROOT
    day = day or latest_stored_day(root)
    if day is None:
        return None
    raw = load_raw_day(root / "raw" / day)
    return build_day(raw, day, root, ticker=ticker, mode=mode,
                     update_latest=(day == latest_stored_day(root)), record=False, write_png=False,
                     builder_request=builder_request)
