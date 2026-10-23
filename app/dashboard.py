"""RevenueChaser interactive dashboard — Flask app with sidebar tabs + iframes.

Run:  python -m RevenueChaser.app.dashboard   ->  http://localhost:8000

Tabs (grouped): Research (Earnings Radar, SPX Gamma,
Financials, UW Interaction), Screeners (Sector Rotation, Asymmetric Growth,
Swing Potential, Fundamentals), Ownership (SEC 13F, LEAPS), TradeWhisperer.
Each tab is an iframe serving the module's latest generated HTML
(regenerated on demand via /run/* routes).

This is a LOCAL, on-demand tool.
"""
import gzip
import json
import logging
import os
import re
import shutil
import signal as _signal
import subprocess
import sys
import webbrowser
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, Response, g, jsonify, render_template, request, send_from_directory
from markupsafe import Markup

from RevenueChaser.core import config, html_safe, open_issues, rawstore, reporting_common, templating
from RevenueChaser.core.calendar import today_ny
from RevenueChaser.core.providers.unusual_whales import UWClient
from RevenueChaser.core.rawstore import StoreLockedError
from RevenueChaser.core.reporting_common import payload_path
from . import runners
from . import jobs
from . import logview

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dashboard")
# The Logs tab polls /api/logs every few seconds; keep those requests out of the log it shows.
logview.quiet_polling()

app = Flask(__name__, template_folder=str(Path(__file__).resolve().parent.parent / "templates"))
# Local tool served with debug=False, which makes Jinja compile the template once
# and cache it for the life of the process — so an edit to dashboard.html silently
# does not appear until the server is restarted. Re-check the file instead; the
# cost is one stat() per render.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
@app.errorhandler(StoreLockedError)
def _store_locked(exc: StoreLockedError):
    """Render the one failure that has a concrete, one-step fix.

    Without this every board answers a bare 500 and the actual cause — DuckDB
    naming the process that holds the file — stays buried in the server log.
    JSON for /api callers so the dashboard's own polling stays parseable; a
    readable page for anything in an iframe. 503: the service is unavailable
    right now, not broken.
    """
    logger.error("store locked: %s", exc)
    if request.path.startswith("/api/"):
        return jsonify({
            "ok": False, "error": "store_locked", "message": exc.headline(),
            "remedy": exc.remedy(), "holder": exc.holder_label(),
            "pid": exc.holder_pid,
        }), 503
    # core.templating, not render_template: Flask autoescapes only .html/.xml, so a
    # .html.j2 page would render the driver's text raw (F7, #144).
    return templating.render(
        "store_locked.html.j2", holder=exc.holder_label(), pid=exc.holder_pid,
        holder_path=exc.holder_path, remedy=exc.remedy(), detail=exc.detail,
        db_path=exc.path), 503


# ── local-only guard (B1) ────────────────────────────────────────────────────
# The dashboard binds to 127.0.0.1 but has no login, so any web page the user
# visits could POST to it (start a 2 h crawl, burn UW quota, edit the
# watchlist) or, via DNS rebinding, read its APIs. Two cheap checks close that:
#   - Host must be a loopback name (a rebinding page arrives with its own
#     hostname in Host);
#   - a state-changing request must not come from another site (browsers send
#     Origin and Sec-Fetch-Site on cross-site requests; same-origin fetches from
#     the dashboard's own pages and its iframes pass).
# A page on another localhost port is same-site and its Origin host is localhost, so
# those two checks let its simple form/fetch POST through (#231). So every non-safe
# method also needs a custom header: a custom header forces a CORS preflight, which
# this server never answers, so another origin cannot send it. Every page the
# dashboard serves sends it (value = the sender: ``dashboard``, a board key,
# ``e2e``, ``cli``); the automations route also wants its own exact value (#130).
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_PANEL_HEADER = "X-RC-Panel"


def _host_name(value: str) -> str:
    """``"localhost:8000"`` -> ``"localhost"``; ``"[::1]:8000"`` -> ``"[::1]"``."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def _from_another_site() -> bool:
    """True when the browser says the request came from a page of another site.

    ``same-origin`` (the shell's iframes) and ``none`` (the address bar) are ours; so is a client that
    sends no ``Sec-Fetch-Site`` (curl, the test client). Another localhost port is ``same-site``, which
    is why that value counts as another site here (#231).
    """
    return (request.headers.get("Sec-Fetch-Site") or "").lower() in ("cross-site", "same-site")


@app.before_request
def _local_only():
    if _host_name(request.host) not in _LOCAL_HOSTS:
        return jsonify({"ok": False, "error": "forbidden host"}), 403
    if request.method in _SAFE_METHODS:
        return None
    if (request.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
        return jsonify({"ok": False, "error": "cross-site request refused"}), 403
    origin = request.headers.get("Origin")
    if origin and origin != "null":
        from urllib.parse import urlsplit
        if _host_name(urlsplit(origin).netloc) not in _LOCAL_HOSTS:
            return jsonify({"ok": False, "error": "cross-site request refused"}), 403
    elif origin == "null":
        return jsonify({"ok": False, "error": "cross-site request refused"}), 403
    if not (request.headers.get(_PANEL_HEADER) or "").strip():
        return jsonify({"ok": False, "error": "panel header required: reload this board"}), 403
    return None


@app.before_request
def _open_store_session():
    """One store handle per request instead of one per query.

    A board render or /api call runs many small store reads; opening the
    500 MB file costs ~100 ms each time, re-using an open one < 1 ms. The
    handle is opened lazily (a request that never touches the store never
    locks it) and closed in ``_close_store_session`` — never held between
    requests, so scheduled jobs still get the file. Static files skip it.
    """
    if request.endpoint == "static":
        return
    g._store_session = rawstore.session()
    g._store_session.__enter__()


@app.teardown_request
def _close_store_session(exc):
    sess = g.pop("_store_session", None)
    if sess is not None:
        sess.__exit__(None, None, None)


@app.after_request
def _no_cache(resp):
    """Never cache dashboard HTML/views — the running app window must always
    pick up the latest generated report + template after a restart."""
    if request.endpoint == "static":
        return resp  # vendor JS / icons are immutable: let the browser cache them
    ct = (resp.headers.get("Content-Type") or "")
    if "text/html" in ct or resp.status_code in (200, 302):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


# Register module runners with the job queue. ``produces`` lists which iframes
# to reload when the job completes.
jobs.register("earnings", runners.run_earnings_range, ["module_earnings_radar"])
jobs.register("module_leaps", runners.run_leaps_latest, ["module_leaps"])  # live UW scan (Fetch)
# 13F Fetch = download the new SEC Form 13F data set(s), ingest, derive the totals, render (backend D4).
# The SEC form-13f zips are the only trusted 13F source. The Fetch needs no UW key: the names/sectors
# step uses the UW screener best-effort and opens its own client (#206).
jobs.register("module_sec_13f", runners.build_sec13f, ["module_sec_13f"], requires_uw=False, lane="long")
# Store-only: re-derive sec13f_totals from the ingested holdings. Zero network, no UW key.
jobs.register("sec13f_rebuild", runners.run_sec13f_rebuild, ["module_sec_13f"], requires_uw=False,
              lane="long")
# The CUSIP map (#207): OpenFIGI only (at most 400 requests), no SEC and no UW; then the derive with the new map.
jobs.register("sec13f_map", runners.run_sec13f_map, ["module_sec_13f"], requires_uw=False, lane="long")
jobs.register("rotation", runners.run_rotation_latest, ["module_sector_rotation"])
jobs.register("moneyflow", runners.run_money_flow_latest, ["module_sector_rotation"])
jobs.register("holdings", runners.run_etf_holdings_sync, ["module_sector_rotation"], lane="long")
jobs.register("spxgamma", runners.run_spxgamma, ["module_spx_gamma"])
jobs.register("spxgamma_stored", runners.run_spxgamma_stored, ["module_spx_gamma"], requires_uw=False)  # #179 D5
jobs.register("quick", runners.run_quick, ["module_financials"], requires_uw=False)
# The single-board Fetches are refused while a universe run holds the universe lock (#244), and a universe run is
# refused while one of them runs (#356).
# These four reach job_control checkpoints, so a running one can be cancelled (#243). Swing and Fundamentals score
# the whole screenable universe since #383 R3 C12 (card C: ~3,520 names), so they run on the long lane.
jobs.register("module_asymmetric_growth", runners.refused_during_universe(runners.run_asymmetric_growth),
              ["module_asymmetric_growth"], requires_uw=False, cancellable=True)
jobs.register("swing", runners.refused_during_universe(runners.run_swing), ["module_swing_potential"],
              requires_uw=False, lane="long", cancellable=True)
jobs.register("module_fundamentals", runners.refused_during_universe(runners.run_fundamentals),
              ["module_fundamentals"], requires_uw=False, lane="long", cancellable=True)
jobs.register("universe", runners.run_universe_job,   # + UW quote rows when a key verifies (#294)
              ["module_asymmetric_growth", "module_swing_potential", "module_fundamentals"], requires_uw=False,
              lane="long", cancellable=True)
jobs.register("sec13f_check", runners.run_sec13f_check, [], requires_uw=False)
jobs.register("uw", runners.run_uw_interaction, ["module_uw_interaction"])
jobs.register("module_comovement", runners.run_comovement, ["module_comovement"], requires_uw=False)
# Settings save / reset (#40): store-only recompute of the board, then the Settings card.
jobs.register("comovement_recompute", runners.run_comovement_recompute,
              ["module_comovement", "module_settings"], requires_uw=False)
# The Swing board scores only on a Fetch (a Refresh serves the stored payload, K4), so a saved threshold
# re-derives the Settings card alone (#384 SW-S4).
jobs.register("settings_refresh", runners.run_settings, ["module_settings"], requires_uw=False)
jobs.register("module_value_chain", runners.run_value_chain, ["module_value_chain"], requires_uw=False)
jobs.register("edgar_sync", runners.run_edgar_sync, ["module_value_chain"], requires_uw=False, lane="long")
jobs.register("universe_stats", runners.run_universe_stats, ["module_settings"], requires_uw=False, lane="long")
jobs.register("identity", runners.run_identity, ["module_settings"], requires_uw=False, lane="long")
jobs.register("daily_bars", runners.run_daily_bars, ["module_settings"], lane="long")
# Index lists (#302): three UW requests plus the SmallCap 600 list from the identity cache.
jobs.register("index_lists", runners.run_index_lists, ["module_settings"], lane="long")
jobs.register("module_oipd", runners.run_oipd, ["module_oipd"], requires_uw=True)
jobs.register("module_discord", runners.run_discord, ["module_discord"], requires_uw=False, lane="long",
              requires=("discord", "discord_cli"))   # update() needs the token and the CLI, not the guild id
jobs.register("module_discord_discover", runners.run_discord_discover, ["module_discord"], requires_uw=False,
              lane="long", requires=("discord", "discord_cli", "discord_guild"))   # discover() also reads the id

# Which store-only job a board's Save / Reset queues (``api_settings_board``, #40 / #384 SW-S4).
SETTINGS_JOBS = {"module_comovement": "comovement_recompute", "module_swing_potential": "settings_refresh"}

TABS = [
    {"type": "header", "label": "Research"},
    {"type": "tab", "key": "module_earnings_radar", "label": "Earnings Radar", "url": "/view/module_earnings_radar", "sub": True},
    {"type": "tab", "key": "module_spx_gamma", "label": "SPX Gamma", "url": "/view/module_spx_gamma", "sub": True},
    {"type": "tab", "key": "module_oipd", "label": "Implied Probability", "url": "/view/module_oipd", "sub": True},
    {"type": "tab", "key": "module_financials", "label": "Financials", "url": "/view/module_financials", "sub": True},
    {"type": "tab", "key": "module_uw_interaction", "label": "UW Interaction (lab)", "url": "/view/module_uw_interaction", "sub": True},
    {"type": "tab", "key": "module_discord", "label": "Discord", "url": "/view/module_discord", "sub": True},
    {"type": "header", "label": "Classification"},
    {"type": "tab", "key": "module_comovement", "label": "Co-movement", "url": "/view/module_comovement", "sub": True},
    {"type": "tab", "key": "module_value_chain", "label": "Value Chain", "url": "/view/module_value_chain", "sub": True},
    {"type": "tab", "key": "module_relatedness", "label": "Relatedness", "url": "/view/module_relatedness", "sub": True},
    {"type": "header", "label": "Screeners"},
    {"type": "tab", "key": "module_sector_rotation", "label": "Sector Rotation", "url": "/view/module_sector_rotation", "sub": True},
    {"type": "tab", "key": "module_asymmetric_growth", "label": "Asymmetric Growth", "url": "/view/module_asymmetric_growth", "sub": True},
    {"type": "tab", "key": "module_swing_potential", "label": "Swing Potential", "url": "/view/module_swing_potential", "sub": True},
    {"type": "tab", "key": "module_fundamentals", "label": "Fundamentals", "url": "/view/module_fundamentals", "sub": True},
    {"type": "header", "label": "Ownership"},
    {"type": "tab", "key": "module_sec_13f", "label": "SEC 13F", "url": "/view/module_sec_13f", "sub": True},
    {"type": "tab", "key": "module_leaps", "label": "LEAPS", "url": "/view/module_leaps", "sub": True},
    {"type": "header", "label": "Ops"},
    {"type": "tab", "key": "module_settings", "label": "Settings / Data health", "url": "/view/module_settings", "sub": True},
    # Read-only: the dashboard's own log files (app/logview.py). No Fetch, no Refresh, no data date.
    {"type": "tab", "key": "logs", "label": "Logs", "url": "/view/logs", "sub": True},
]


def _uw() -> UWClient:
    return UWClient()


def _module_file(mod: str) -> Path:
    return {
        "module_earnings_radar": runners.EARNINGS_LATEST,
        "module_leaps": runners.LEAPS_LATEST,
        "module_sector_rotation": runners.ROTATION_LATEST,
        "module_sec_13f": runners.SEC_13F_LATEST,
        "module_financials": runners.QUICK_LATEST,
        "module_asymmetric_growth": runners.ASYMMETRIC_GROWTH_LATEST,
        "module_swing_potential": runners.SWING_LATEST,
        "module_fundamentals": runners.FUNDAMENTALS_LATEST,
        "module_comovement": runners.COMOVEMENT_LATEST,
        "module_value_chain": runners.VALUE_CHAIN_LATEST,
        "module_relatedness": runners.RELATEDNESS_LATEST,
        "module_oipd": runners.OIPD_LATEST,
        "module_settings": runners.SETTINGS_LATEST,
    }.get(mod)


def _nav_groups(tabs: list[dict]) -> list[dict]:
    """Fold the flat ``TABS`` list into single-level collapsible groups.

    The child key is ``tabs`` (not ``items``) so Jinja does not resolve it to the
    dict ``.items`` method.
    """
    groups: list[dict] = []
    for t in tabs:
        if t["type"] == "header":
            groups.append({"label": t["label"], "tabs": []})
        elif groups:
            groups[-1]["tabs"].append(t)
        else:
            groups.append({"label": "", "tabs": [t]})
    return groups


NAV_GROUPS = _nav_groups(TABS)


# ── views ──
@app.route("/")
def index():
    issues, issues_error = _open_issues_for_shell()
    return render_template("dashboard.html", tabs=TABS, nav_groups=NAV_GROUPS,
                           quick_ticker=runners._last_quick_ticker(),
                           # One JSON block for all boards, script-safe (#383): ``script_json`` neutralises
                           # ``<``, so a hostile issue title cannot close the block. Markup only because the
                           # block is already escaped JSON and Jinja would otherwise turn ``&`` into ``&amp;``.
                           open_issues_json=Markup(html_safe.script_json(issues)),
                           open_issues_error=issues_error,
                           rc_esc_js=Markup(html_safe.ESC_JS))


def _open_issues_for_shell() -> tuple[dict, str]:
    """The "Open issues" payload for every data board (``{}`` + a message when the registry cannot be read).

    Not for ``logs``: it is not a data board, so it gets no panel
    (``core/tests/test_help_every_tab.py``). A bad registry never fails the page — the group shows the reason.
    """
    keys = [t["key"] for t in TABS if t["type"] == "tab" and t["key"] not in open_issues.NOT_A_DATA_BOARD]
    try:
        return {**reporting_common.open_issues_payload(keys), "data_boards": keys}, ""
    except open_issues.RegistryError as exc:
        # ``data_boards`` even on this path: without it the shell cannot tell a data board whose entries
        # failed to load from ``logs``, which has no group at all, and would show the error on both.
        return {"data_boards": keys}, f"Open issues are unavailable: {exc}"


@app.route("/view/<mod>/payload.json")
def view_payload(mod: str):
    """A board's data outside its HTML (K10, #383 C6a): the stored gzip file, sent as it is.

    ``Content-Encoding: gzip`` when the client accepts it (every browser does), so nothing is compressed per
    request; plain JSON otherwise. 404 for an unknown board or one with no payload file.
    """
    f = _module_file(mod)
    if f is None:
        return "Unknown module", 404
    try:
        data = payload_path(f).read_bytes()
    except FileNotFoundError:
        return "no payload", 404, {"Content-Type": "text/plain; charset=utf-8"}
    if request.accept_encodings["gzip"]:
        resp = Response(data, mimetype="application/json")
        resp.headers["Content-Encoding"] = "gzip"
    else:
        resp = Response(gzip.decompress(data), mimetype="application/json")
    resp.headers["Vary"] = "Accept-Encoding"  # Cache-Control: no-store comes from _no_cache
    return resp


@app.route("/view/<mod>")
def view(mod: str):
    if mod == "module_spx_gamma":
        return _view_spxgamma()
    if mod == "module_uw_interaction":
        return _view_uw_interaction()
    if mod == "module_discord":
        return _view_discord()
    if mod == "logs":
        return logview.render_page(logview.page_payload())
    f = _module_file(mod)
    if f is None:
        return "Unknown module", 404
    # General rule: Refresh (?refresh=1) drops the cached html and recomputes the
    # board FROM THE STORE (.store/raw.duckdb) — no network — for every board
    # backed by downloaded data. A plain load (no param) serves the existing
    # artifact, or self-heals from the store when it is missing.
    is_refresh = request.args.get("refresh") is not None
    if is_refresh and _from_another_site():
        # A GET carries no Origin and no X-RC-Panel, so an <img src=".../view/x?refresh=1"> on any
        # page the user visits would drop the cache and rebuild the board (#175 review).
        return "refresh refused: not requested from this dashboard", 403, {"Content-Type": "text/plain; charset=utf-8"}
    if mod == "module_financials" and is_refresh:
        # Financials is per-ticker: the ticker in the box drives the store-only
        # re-render (run_quick_cached drops the stale artifact when it must).
        ticker = (request.args.get("ticker") or "").strip().upper() or None
        if ticker is not None and not _valid_ticker(ticker):
            return "bad ticker", 400, {"Content-Type": "text/plain; charset=utf-8"}
        payload_path(f).unlink(missing_ok=True)      # K10: the rebuild writes a new payload or none
        path = runners.run_quick_cached(ticker=ticker)
        if path is not None and path.exists():
            return send_from_directory(path.parent, path.name)
        return _empty_view(mod)
    if mod == "module_oipd" and is_refresh:
        # Implied Probability is per-ticker too: re-fit the stored chain for the
        # ticker in the box, with zero network.
        ticker = (request.args.get("ticker") or "").strip().upper() or None
        if ticker is not None and not _valid_ticker(ticker):
            return "bad ticker", 400, {"Content-Type": "text/plain; charset=utf-8"}
        payload_path(f).unlink(missing_ok=True)      # K10: the rebuild writes a new payload or none
        path = runners.run_oipd_cached(ticker=ticker)
        if path is not None and path.exists():
            return send_from_directory(path.parent, path.name)
        return _empty_view(mod)
    if mod == "module_comovement" and is_refresh:
        # Changing Remove market / Horizon reloads the iframe with
        # ?refresh=1&horizon=..&benchmark=..; re-cluster the stored residuals at
        # those settings with zero network. This runs BEFORE the generic refresh
        # so the query args are not dropped.
        horizon = request.args.get("horizon", type=int)
        benchmark = (request.args.get("benchmark") or "").strip().lower() or None
        window = request.args.get("window", type=int)
        if request.args.get("window") not in (None, "") and window not in _comovement_windows():
            return "bad window", 400, {"Content-Type": "text/plain; charset=utf-8"}
        payload_path(f).unlink(missing_ok=True)      # K10: the rebuild writes a new payload or none
        path = runners.run_comovement(None, no_fetch=True, horizon=horizon,
                                      benchmark=benchmark, window=window)
        if path is not None and path.exists():
            return send_from_directory(path.parent, path.name)
        return _empty_view(mod)
    if _universe_loading(mod):
        return _universe_loading_view(mod)
    if is_refresh and mod in _STORE_REFRESHABLE:
        # Drop the cache, then fall through to the same self-heal path the board
        # already uses when its artifact is missing (recompute from raw.duckdb).
        # The payload file (K10) goes with it: the rebuild writes a new one or none.
        f.unlink(missing_ok=True)
        payload_path(f).unlink(missing_ok=True)
    if not f.exists():
        if mod == "module_sec_13f":
            # Write the re-rendered board back to its artifact (it used to be
            # returned as a string only, so every later load recomputed it).
            # The last Fetch's notes (names not refreshed, CUSIPs left out) come back from its history (C24, #383).
            try:
                path = runners.run_sec_13f_latest(
                    None, notice=runners.last_fetch_notice("module_sec_13f", partial=True))
            except jobs.JobMessage:
                path = runners.SEC_13F_LATEST  # the empty board was written
            return send_from_directory(path.parent, path.name)
        if mod == "module_sector_rotation":
            html_path = _render_rotation_dashboard()
            if html_path is not None:
                return send_from_directory(html_path.parent, html_path.name)
        if mod in _MODEL_ONLY_RENDERERS:
            # Re-run the module's model-only (no_fetch) path from raw.duckdb
            # caches. No network; the regenerated file lands in .cache/ so later
            # page loads serve it directly.
            path = _MODEL_ONLY_RENDERERS[mod]()
            if path is not None and path.exists():
                return send_from_directory(path.parent, path.name)
        return _empty_view(mod)
    return send_from_directory(f.parent, f.name)


def _render_rotation_dashboard():
    """Render the RRG map from stored snapshots (no API calls).

    Returns the written HTML path, or None when no snapshots exist yet. The
    rendered artifact lands in .cache/ so later page loads serve the file.
    """
    from RevenueChaser.module_sector_rotation import store as rotation_store
    from RevenueChaser.module_sector_rotation.report import (
        export_rotation_dashboard, _rows_to_snapshots)
    dates = rotation_store.list_snapshot_dates()
    if not dates:
        return None
    snap = rotation_store.load_snapshot(date.fromisoformat(dates[-1])) or {}
    rows = snap.get("etfs", [])
    if not rows:
        return None
    snaps = _rows_to_snapshots(rows)
    return export_rotation_dashboard(snaps, filename="sector_rotation_latest.html", uw=None)


# Module -> zero-network renderer (model-only run over raw.duckdb caches), used
# when the board's .cache artifact is missing. Each returns the written Path.
_MODEL_ONLY_RENDERERS = {
    "module_swing_potential": lambda: _import_runners().run_swing(None, no_fetch=True),
    "module_fundamentals": lambda: _import_runners().run_fundamentals(None, no_fetch=True),
    "module_asymmetric_growth": lambda: _import_runners().run_asymmetric_growth(None, no_fetch=True),
    "module_leaps": lambda: _import_runners().run_leaps_cached(),
    "module_financials": lambda: _import_runners().run_quick_cached(),
    "module_comovement": lambda: _import_runners().run_comovement(None, no_fetch=True),
    "module_value_chain": lambda: _import_runners().run_value_chain(None, no_fetch=True),
    "module_relatedness": lambda: _import_runners().run_relatedness(None, no_fetch=True),
    "module_oipd": lambda: _import_runners().run_oipd_cached(),
    "module_settings": lambda: _import_runners().run_settings(None),
}

# Boards that can be re-derived from .store with zero network calls. "Refresh
# Tab" on any of them drops the cached html and recomputes from raw.duckdb;
# boards absent here (live-only Earnings Radar) keep a fetch-only empty state.
_STORE_REFRESHABLE = frozenset(_MODEL_ONLY_RENDERERS) | {
    "module_sec_13f",
    "module_sector_rotation",
    "module_discord",          # rendered in memory by _view_discord, no cached file
}


def _import_runners():
    from . import runners
    return runners


# The boards that read the shared universe, and the jobs that load it when the store has none (#145 D1).
_UNIVERSE_BOARDS = ("module_asymmetric_growth", "module_swing_potential", "module_fundamentals")
_UNIVERSE_JOBS = ("universe", "module_asymmetric_growth", "swing", "module_fundamentals")


def _universe_loading(mod: str) -> bool:
    """True while a job that loads the universe is queued or running and the store has none yet.

    A Refresh or a plain load never starts that job (AGENTS.md §2); it only reports that one is under way.
    """
    if mod not in _UNIVERSE_BOARDS or not jobs.has_active(_UNIVERSE_JOBS):
        return False
    return not rawstore.query("SELECT 1 FROM equity_universe LIMIT 1")


def _universe_loading_view(mod: str) -> str:
    """The "Loading universe…" state, rendered in memory: a transient state is never written to ``.cache``."""
    from datetime import datetime, timezone
    from RevenueChaser.core.scan_quality import no_data_message
    return templating.render(
        "empty_board.html.j2", title=_TAB_LABELS.get(mod, mod), message=no_data_message("universe_loading"),
        hint="", stamp=datetime.now(timezone.utc).isoformat(timespec="seconds"))


def _view_discord():
    """The Discord board is filtered per request, so it is rendered from the store on every load.

    Store only (``discord.duckdb``): a plain load and Refresh behave the same and never reach DCE or Discord.
    """
    from RevenueChaser.module_discord import board, report

    try:
        payload = board.build_payload(
            request.args.get("channel") or None, request.args.get("q") or "",
            request.args.get("date_from") or "", request.args.get("date_to") or "",
            request.args.get("page", type=int) or 1)
    except board.BoardInputError as exc:
        return str(exc), 400, {"Content-Type": "text/plain; charset=utf-8"}
    # Rendered in memory (no cached file), so concurrent filtered views can never be served each other's page.
    return report.render_discord_board(payload)


def _view_spxgamma() -> str:
    """Serve the SPX Option Position Map section HTML.

    The day the last Fetch or calendar click built, while this process runs: a stored past day
    is rebuilt from disk without moving ``latest.json`` (#179 D5, SPX-3). Otherwise, and after a
    restart, the newest day (``latest.json``). Refresh (``?refresh=1``) rebuilds the newest stored
    raw day from disk: zero network, no backfill (#177). It needs a Fetch first; without a stored
    day the board is served as it is.
    """
    if request.args.get("refresh") is not None:
        if _from_another_site():
            return "refresh refused: not requested from this dashboard", 403, {"Content-Type": "text/plain; charset=utf-8"}
        path = runners.run_spxgamma_cached()
        if path is not None and Path(path).exists():
            return send_from_directory(Path(path).parent, Path(path).name)
    shown = runners.spxgamma_view_report()
    if shown is not None and Path(shown).exists():
        return send_from_directory(Path(shown).parent, Path(shown).name)
    latest = _spxgamma_latest_payload()
    if latest and latest.get("report_html"):
        html = Path(latest["report_html"])
        if html.exists():
            return send_from_directory(html.parent, html.name)

    newest = _spxgamma_latest_report_file()
    if newest is not None:
        return send_from_directory(newest.parent, newest.name)
    return _empty_view("spxgamma")

def _spxgamma_latest_report_file() -> Path | None:
    from RevenueChaser.module_spx_gamma.config import REPORT_ROOT
    if not REPORT_ROOT.exists():
        return None
    candidates = list(REPORT_ROOT.glob("*/section.html"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _spxgamma_latest_payload() -> dict | None:
    path = runners.spxgamma_latest_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _view_uw_interaction():
    """Serve the UW Interaction (lab) board from its stored payload.

    The board is its own console: it renders the tool form plus the last run's
    rows directly from the regenerable JSON under ``.cache/`` — zero network and
    no credentials needed to view. Running a tool is a separate async job
    (``/run/uw``); the page polls and reloads itself on completion.
    """
    from RevenueChaser.module_uw_interaction.config import load_state
    from RevenueChaser.module_uw_interaction.report import render_board
    return render_board(load_state())


_TAB_LABELS = {t["key"]: t["label"] for t in TABS if t["type"] == "tab"}


# Starts alphanumeric: a punctuation-only "." / ".." / "-" is never a symbol and
# would reach file names and the UW client's /stock/{ticker}/ paths (#144).
_TICKER_RE = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,9}")


def _valid_ticker(ticker: str) -> bool:
    """An upper-cased symbol such as ``BRK.B``; anything else is rejected with 400."""
    return bool(_TICKER_RE.fullmatch(ticker))


def _empty_view(mod: str) -> str:
    label = _TAB_LABELS.get(mod, mod.replace("_", " ").title())
    return templating.render("empty_view.html.j2", label=label)


# ── run routes (non-blocking: enqueue a job, return its id) ──
def _submit(module: str, params: dict):
    """Enqueue a module run, refusing up front when a credential it declared is missing or UW's won't work.

    Two gates, in this order and for different reasons:

    - **Presence** (`jobs.credential_problem`): a module declares the credentials it cannot run without
      (`register(..., requires=...)`); reading env vars costs nothing, so an unmet one is refused before the
      HTTPS pool the UW probe builds.
    - **Validity** (UW): a real authenticated probe, not "is a key set": an expired token used to sail past the
      old ``uw.active`` gate and fail minutes later inside a module with an opaque message.
    """
    missing = jobs.credential_problem(module)
    if missing:
        return jsonify({"ok": False, "error": missing}), 400
    if jobs._REQUIRES_UW.get(module, True):
        uw = _uw()
        try:
            ok, message = uw.verify()
        finally:
            getattr(uw, "close", lambda: None)()
        if not ok:
            return jsonify({"ok": False, "error": message}), 400
    job_id = jobs.submit(module, params)
    return jsonify({"ok": True, "job_id": job_id}), 202


def _earnings_job_params():
    """Parse ``dates=`` (comma-separated ISO days) or contiguous ``start``/``end``."""
    raw = (request.form.get("dates") or "").strip()
    if raw:
        try:
            days = [date.fromisoformat(p.strip()) for p in raw.split(",") if p.strip()]
        except ValueError:
            return None, (jsonify({"ok": False, "error": "bad date"}), 400)
        if not days:
            return None, (jsonify({"ok": False, "error": "bad date"}), 400)
        return {"dates": days}, None
    start = request.form.get("start") or request.form.get("date") or date.today().isoformat()
    end = request.form.get("end") or start
    try:
        s = date.fromisoformat(start)
        e = date.fromisoformat(end)
    except ValueError:
        return None, (jsonify({"ok": False, "error": "bad date"}), 400)
    return {"start": s, "end": e}, None


@app.route("/run/earnings", methods=["POST"])
def run_earnings():
    params, err = _earnings_job_params()
    if err:
        return err
    return _submit("earnings", params)


@app.route("/run/module_leaps", methods=["POST"])
def run_leaps():
    return _submit("module_leaps", {})


@app.route("/run/module_sec_13f", methods=["POST"])
def run_module_sec_13f():
    refused = _busy(("module_sec_13f", "sec13f_rebuild", "sec13f_map"),
                    "A 13F job is already queued or running.")
    if refused:
        return refused
    return _submit("module_sec_13f", {})


@app.route("/run/uw", methods=["POST"])
def run_uw():
    """Run a single UW Interaction (lab) tool with its query params.

    The form fields are the catalog tool key (``tool``) plus one entry per
    declared param; the engine validates them against the schema before any
    network call. Standard async job: returns 202 + a job id to poll.
    """
    from RevenueChaser.module_uw_interaction.catalog import get_tool

    tool = (request.form.get("tool") or "").strip()
    if not tool:
        return jsonify({"ok": False, "error": "tool required"}), 400
    # Only the tool's declared params: every forwarded key becomes a keyword of
    # ``runner(uw, **params)``, so a form key ``uw`` was a TypeError (M12).
    names = {p["name"] for p in (get_tool(tool) or {}).get("params", [])}
    params = {k: v for k, v in request.form.items() if k in names}
    params["tool"] = tool
    return _submit("uw", params)


@app.route("/run/module_sec_13f-check", methods=["POST"])
def run_module_sec_13f_check():
    """Ask the SEC whether a new Form 13F data set is out (downloads nothing).

    A network call, so it is a job, not a GET view; the result comes back as the
    job's message ("New set … — press Fetch" / "Up to date").
    """
    return _submit("sec13f_check", {})


@app.route("/run/module_sec_13f-rebuild", methods=["POST"])
def run_module_sec_13f_rebuild():
    """Re-derive the 13F totals from the ingested holdings (store only, zero network)."""
    refused = _busy(("module_sec_13f", "sec13f_rebuild", "sec13f_map"),
                    "A 13F job is already queued or running.")
    if refused:
        return refused
    return _submit("sec13f_rebuild", {})


@app.route("/run/module_sec_13f-map", methods=["POST"])
def run_module_sec_13f_map():
    """Map the held CUSIPs through OpenFIGI, then re-derive the 13F totals (#207). No button: the PC starts it."""
    refused = _busy(("module_sec_13f", "sec13f_rebuild", "sec13f_map"),
                    "A 13F job is already queued or running.")
    if refused:
        return refused
    return _submit("sec13f_map", {})


@app.route("/run/rotation", methods=["POST"])
def run_rotation():
    params = {"benchmark": "SPY"}
    for key in ("start", "end"):
        raw = (request.form.get(key) or "").strip()
        if not raw:
            continue
        try:
            date.fromisoformat(raw)  # validate before the UW key probe
        except ValueError:
            return jsonify({"ok": False, "error": "bad date"}), 400
        params[key] = raw
    resp = _submit("rotation", params)
    if resp[1] == 202:
        # Money flow (21-day net dollar flow of sector/theme ETFs) is refreshed
        # right after every Rotation Fetch so its panel is never stale (backend D3).
        jobs.submit("moneyflow", {})
    return resp


@app.route("/run/moneyflow", methods=["POST"])
def run_moneyflow():
    return _submit("moneyflow", {})


@app.route("/run/holdings", methods=["POST"])
def run_holdings():
    return _submit("holdings", {})


@app.route("/run/spxgamma", methods=["POST"])
def run_spxgamma():
    asof = request.form.get("date") or request.form.get("asof") or today_ny().isoformat()
    try:
        date.fromisoformat(asof)
    except ValueError:
        return jsonify({"ok": False, "error": "bad date"}), 400
    from RevenueChaser.module_spx_gamma.run import serves_from_disk

    params = {"asof_date": asof}
    # A stored past day is served from disk and never refetched (#179 D5); only an explicit
    # force field refetches it. Its job needs no UW client, so no auth probe is sent either.
    if (request.form.get("force") or "").strip().lower() in ("1", "true"):
        params["force"] = True
    elif serves_from_disk(asof):
        return _submit("spxgamma_stored", params)
    return _submit("spxgamma", params)


@app.route("/run/quick", methods=["POST"])
def run_quick():
    ticker = (request.form.get("ticker") or "").strip().upper()
    if not ticker:
        return jsonify({"ok": False, "error": "ticker required"}), 400
    if not _valid_ticker(ticker):
        # M8: the ticker becomes a file name, a store key and page text.
        return jsonify({"ok": False, "error": "bad ticker"}), 400
    job_id = jobs.submit("quick", {"ticker": ticker})
    return jsonify({"ok": True, "job_id": job_id}), 202


@app.route("/run/module_oipd", methods=["POST"])
def run_module_oipd():
    """Fetch the Implied Probability board for the ticker selected in the iframe.

    The board owns its ticker picker (an in-board combobox); the parent reads the
    iframe's ``window.OIPD_TICKER`` and passes it here, not a header text box.
    """
    ticker = (request.form.get("ticker") or "").strip().upper()
    if ticker and not _valid_ticker(ticker):
        return jsonify({"ok": False, "error": "bad ticker"}), 400
    return _submit("module_oipd", {"ticker": ticker or None})


def _universe_params() -> dict:
    """Form flags for the universe boards.

    - ``refresh_universe`` — 'Refresh Universe': re-fetch the shared ticker universe.
    - ``no_fetch`` — 'Refresh Tab': model-only, run purely from cached data (no network).
    """
    p: dict = {}
    if request.form.get("refresh_universe"):
        p["refresh_universe"] = True
    if request.form.get("no_fetch"):
        p["no_fetch"] = True
    return p


@app.route("/run/module_asymmetric_growth", methods=["POST"])
def run_module_asymmetric_growth():
    return _submit("module_asymmetric_growth", _universe_params())


@app.route("/run/swing", methods=["POST"])
def run_swing():
    return _submit("swing", _universe_params())


@app.route("/run/module_fundamentals", methods=["POST"])
def run_module_fundamentals():
    return _submit("module_fundamentals", _universe_params())


@app.route("/run/module_comovement", methods=["POST"])
def run_module_comovement():
    return _submit("module_comovement", {})


def _busy(modules: tuple[str, ...], label: str):
    """A 409 while a job of one of ``modules`` is already queued or running; ``None`` when the way is clear.

    Repeated clicks on a long-lane Fetch must not queue repeated multi-minute ingests; ``modules`` names one writer
    family, so the three 13F jobs count as one (#26, PL6-a). The job's own ``job_lock`` covers a run beside the
    dashboard (a CLI or a scheduled task); this covers the queue in front of it. The body names that job (``job_id``),
    so a board can follow the run already going instead of starting another (#244).
    """
    active = jobs.active_id(modules)
    if active:
        return jsonify({"ok": False, "error": label, "job_id": active}), 409
    return None


def _discord_busy():
    """A 409 while a Discord job is already queued or running (repeated clicks must not queue repeated exports)."""
    return _busy(("module_discord", "module_discord_discover"), "A Discord job is already queued or running.")


@app.route("/run/module_discord", methods=["POST"])
def run_module_discord():
    channel = (request.form.get("channel") or "").strip()
    if channel and not channel.isdigit():
        return jsonify({"ok": False, "error": "bad channel"}), 400
    refused = _discord_busy()      # credentials are the submit gate's job (#26, jobs.credential_problem)
    if refused:
        return refused
    if channel:
        from RevenueChaser.module_discord import store as discord_store
        if channel not in {c["channel_id"] for c in discord_store.channels()}:
            return jsonify({"ok": False, "error": "unknown channel"}), 404
    # "Download attachments" is the epic's decision: images and files land under DISCORD_MEDIA_DIR.
    return _submit("module_discord", {"media": True, **({"channel_id": channel} if channel else {})})


@app.route("/run/module_discord_discover", methods=["POST"])
def run_module_discord_discover():
    refused = _discord_busy()
    if refused:
        return refused
    return _submit("module_discord_discover", {})


@app.route("/api/discord/follow", methods=["POST"])
def discord_follow():
    from RevenueChaser.module_discord import store as discord_store

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "expected a JSON object"}), 400
    channel_id, enabled = body.get("channel_id"), body.get("enabled")
    if not isinstance(channel_id, str) or not channel_id.isdigit() or not isinstance(enabled, bool):
        return jsonify({"ok": False, "error": "need channel_id (digits) and enabled (true/false)"}), 400
    if channel_id not in {c["channel_id"] for c in discord_store.channels()}:
        return jsonify({"ok": False, "error": "unknown channel"}), 404
    discord_store.set_enabled([channel_id], enabled)
    return jsonify({"ok": True, "channel_id": channel_id, "enabled": enabled})


@app.route("/discord/media/<path:rel>")
def discord_media(rel: str):
    """Serve a downloaded Discord attachment from inside DISCORD_MEDIA_DIR only (404 for anything else).

    Images show inline; every other type is forced to download so a hostile .html/.svg never runs on this origin.
    """
    from RevenueChaser.module_discord import board

    # A cross-site GET (a page the user visits) must not reach the disk at all, and a URL path with a
    # backslash, colon or drive is refused by pure string checks BEFORE any filesystem call: on Windows
    # resolve() on a UNC path opens an SMB connection to that host.
    if (request.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
        return "Not found", 404
    safe = board.media_rel(rel) if board.url_rel_is_safe(rel) else None
    if safe is None:
        return "Not found", 404
    resp = send_from_directory(config.DISCORD_MEDIA_DIR, safe, as_attachment=not board.is_image(safe))
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "sandbox"
    return resp


@app.route("/run/module_value_chain", methods=["POST"])
def run_module_value_chain():
    return _submit("module_value_chain", {})


@app.route("/run/edgar-sync", methods=["POST"])
def run_edgar_sync():
    """Sync the 10-K/20-F corpus against SEC `submissions.recent` (new-filing check).

    No ``force`` field (#185): a sync never re-slices an already-stored filing; a
    forced re-slice of the stored corpus is a separate read-only dry run
    (``module_value_chain --reslice-dry-run``).
    """
    params: dict = {}
    limit_raw = (request.form.get("limit") or "").strip()
    if limit_raw:
        try:
            limit = int(limit_raw)
        except ValueError:
            return jsonify({"ok": False, "error": "bad limit"}), 400
        if limit <= 0:
            return jsonify({"ok": False, "error": "bad limit"}), 400
        params["limit"] = limit
    refused = _busy(("edgar_sync",), "An EDGAR sync is already queued or running.")
    if refused:
        return refused
    return _submit("edgar_sync", params)


@app.route("/run/daily-bars", methods=["POST"])
def run_daily_bars():
    """Daily price bars (#156): the latest complete session per scope ticker. Optional form field
    ``tickers`` (comma list, at most 200) for a partial run."""
    params: dict = {}
    raw = (request.form.get("tickers") or "").strip()
    if raw:
        items = [p.strip().upper() for p in raw.split(",") if p.strip()]
        if len(items) > 200:
            return jsonify({"ok": False, "error": "at most 200 tickers"}), 400
        for t in items:
            if not re.fullmatch(r"[A-Z0-9.\-]{1,12}", t):     # no ^: index levels are not bars
                return jsonify({"ok": False, "error": "bad ticker"}), 400
        params["tickers"] = list(dict.fromkeys(items))
    refused = _busy(("daily_bars",), "A daily-bars run is already queued or running.")
    if refused:
        return refused
    return _submit("daily_bars", params)


@app.route("/run/index-lists", methods=["POST"])
def run_index_lists():
    """Index lists (#302): every stored slug once. Optional form field ``first_fill=sp600``, the attended first
    SmallCap 600 fill (it needs the user's OK for that run)."""
    params: dict = {}
    raw = (request.form.get("first_fill") or "").strip()
    if raw:
        if raw != "sp600":
            return jsonify({"ok": False, "error": "first_fill takes sp600 only"}), 400
        params["first_fill"] = [raw]
    refused = _busy(("index_lists",), "An index-list run is already queued or running.")
    if refused:
        return refused
    return _submit("index_lists", params)


@app.route("/run/universe", methods=["POST"])
def run_universe():
    """One-stop refresh: fetch the shared universe + all per-ticker data the three
    scanners need, then run all three boards.

    A universe job already queued or running is not queued again (#244): 409 with that job's id.
    """
    refused = _busy(("universe",), "A universe run is already queued or running.")
    if refused:
        return refused
    return _submit("universe", {})


@app.route("/run/universe-stats", methods=["POST"])
def run_universe_stats():
    """Fill only the NULL ``equity_universe`` market caps from Yahoo.

    Standalone Phase B: no screener, no scanners. Optional positive ``limit``
    caps how many null rows are checked in one run (same validation as
    ``/run/edgar-sync``).
    """
    params: dict = {}
    limit_raw = (request.form.get("limit") or "").strip()
    if limit_raw:
        try:
            limit = int(limit_raw)
        except ValueError:
            return jsonify({"ok": False, "error": "bad limit"}), 400
        if limit <= 0:
            return jsonify({"ok": False, "error": "bad limit"}), 400
        params["limit"] = limit
    refused = _busy(("universe_stats",), "A universe-stats run is already queued or running.")
    if refused:
        return refused
    return _submit("universe_stats", params)


@app.route("/run/identity", methods=["POST"])
def run_identity():
    """Refresh ``security_master`` (issue #85): fetch the identity deltas, apply the type/GICS rules, then
    apply the master's GICS to ``equity_universe`` (issue #86).

    Optional positive ``limit`` caps how many universe tickers are built (same validation as
    ``/run/universe-stats``). Long lane; manual for now (the Settings button is R6).
    """
    params: dict = {}
    limit_raw = (request.form.get("limit") or "").strip()
    if limit_raw:
        try:
            limit = int(limit_raw)
        except ValueError:
            return jsonify({"ok": False, "error": "bad limit"}), 400
        if limit <= 0:
            return jsonify({"ok": False, "error": "bad limit"}), 400
        params["limit"] = limit
    refused = _busy(("identity",), "An identity refresh is already queued or running.")
    if refused:
        return refused
    return _submit("identity", params)


def _comovement_windows() -> tuple[int, ...]:
    from RevenueChaser.module_comovement import config as comov_config
    return tuple(comov_config.WINDOW_OPTIONS)


@app.route("/api/settings/board/<board>", methods=["POST"])
def api_settings_board(board: str):
    """Save or reset a board's editable settings, then re-derive the board store-only (#40).

    Body: ``{"changes": {KEY: value, ...}}`` or ``{"reset": true | [KEY, ...]}``.
    Invalid -> 400 with one message per broken rule and nothing written; valid
    -> the change is stored (reset deletes the rows), the board's store-only
    job is queued, and the route answers 202 with the job id and the effective
    values. Boards with settings: ``module_comovement`` and
    ``module_swing_potential`` (#384 SW-S4).
    """
    from RevenueChaser.core import board_settings

    if board not in board_settings.boards():
        return jsonify({"ok": False, "error": "unknown board"}), 404
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "JSON body required",
                        "messages": ["JSON body required"]}), 400
    try:
        if "reset" in body:
            reset = body["reset"]
            keys = None if reset is True else reset
            if not (reset is True or (isinstance(reset, list) and reset
                                      and all(isinstance(k, str) for k in reset))):
                msg = "reset must be true or a non-empty list of setting keys"
                return jsonify({"ok": False, "error": msg, "messages": [msg]}), 400
            unknown = [k for k in (keys or []) if k not in board_settings.defaults(board)]
            if unknown:
                msg = f"unknown setting {unknown[0]!r}"
                return jsonify({"ok": False, "error": msg, "messages": [msg]}), 400
            values = board_settings.reset(board, keys)
        else:
            values = board_settings.save(board, body.get("changes"))
    except board_settings.SettingsError as exc:
        return jsonify({"ok": False, "error": "; ".join(exc.messages),
                        "messages": exc.messages}), 400
    job_id = jobs.submit(SETTINGS_JOBS[board], {})
    return jsonify({"ok": True, "job_id": job_id, "values": values}), 202


@app.route("/api/automations")
def api_automations():
    """Live Task Scheduler state of the registry's tasks (#130).

    The Settings board is a cached artifact, so the panel polls this instead of
    baking scheduler state into the page. Each call starts PowerShell on Windows.
    """
    from RevenueChaser.core import automations

    try:
        return jsonify({"ok": True, **automations.live_state()})
    except automations.AutomationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.http_status


@app.route("/api/automations/<name>/<action>", methods=["POST"])
def api_automations_action(name: str, action: str):
    """Install / enable / disable / remove / run one registry task. No arguments (#130).

    Only an exact registry name is accepted and the body, query string and form
    must be empty, so nothing a page could send selects another task or command.
    """
    from RevenueChaser.core import automations

    # _local_only wants any panel header on every write; this route wants its own value.
    if request.headers.get(_PANEL_HEADER) != "automations":
        return jsonify({"ok": False, "error": "panel header required"}), 403
    if (request.args or request.form or request.files or request.content_length
            or request.get_data()):
        return jsonify({"ok": False, "error": "this route takes no arguments"}), 400
    try:
        return jsonify({"ok": True, "row": automations.apply(name, action)})
    except automations.AutomationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.http_status


@app.route("/api/settings/ticker/<ticker>")
def api_settings_ticker(ticker: str):
    """Store-only per-ticker drill for the Settings board.

    Reads ``raw.duckdb`` / ``edgar.duckdb`` and nothing else: no job, no network,
    no credentials. An unknown ticker is a plain 404 ``{"ok": false}``; one that is
    not ticker-shaped is a 400; a failed store read is a 500 with ``error: "store read failed"``.
    """
    from RevenueChaser.module_settings import inventory

    ticker = ticker.strip().upper()
    if not _valid_ticker(ticker):
        return jsonify({"ok": False, "error": "bad ticker"}), 400
    try:
        detail = inventory.ticker_detail(ticker)
    except Exception as exc:  # noqa: BLE001 - a failed store read must not look like an unknown ticker (DF98)
        logger.exception("settings ticker drill failed for %s", ticker)
        return jsonify({"ok": False, "error": "store read failed", "detail": str(exc)[:200]}), 500
    if detail is None:
        return jsonify({"ok": False}), 404
    return jsonify({"ok": True, **detail})


@app.route("/api/module_fundamentals/latest")
def api_module_fundamentals_latest():
    """Last Fundamental Analysis scan generated_at (for the 'Last run' label)."""
    p = runners.FUNDAMENTALS_JSON
    if not p.exists():
        return jsonify({"generated_at": None})
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return jsonify({"generated_at": None})
    return jsonify({"generated_at": data.get("generated_at")})


@app.route("/api/swing/latest")
def api_swing_latest():
    """Last Swing Screener scan generated_at (for the 'Last run' label)."""
    p = runners.SWING_JSON
    if not p.exists():
        return jsonify({"generated_at": None})
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return jsonify({"generated_at": None})
    return jsonify({"generated_at": data.get("generated_at")})


@app.route("/api/data_asof/<mod>")
def api_data_asof(mod: str):
    """Newest date the board's data is computed against.

    "Last run" is when a scan executed; it can be minutes old over data that is
    days old, because a cached re-render touches no network. Every board backed
    by downloaded data reports its own data date here.
    """
    from RevenueChaser.core import provenance
    return jsonify({"module": mod, "data_as_of": provenance.data_as_of(mod)})


@app.route("/api/universe")
def api_universe():
    """Shared-universe provenance: which board last refreshed it + when + count."""
    from RevenueChaser.core.universe import (
        load_cached_universe,
        load_universe_meta,
    )
    rows = load_cached_universe()
    meta = load_universe_meta()
    return jsonify({
        "count": len(rows),
        "generated_at": meta.get("generated_at"),
        "refreshed_by": meta.get("refreshed_by"),
    })


@app.route("/api/module_asymmetric_growth/latest")
def api_module_asymmetric_growth_latest():
    p = runners.ASYMMETRIC_GROWTH_JSON
    if not p.exists():
        return jsonify({"asof": None, "generated_at": None})
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return jsonify({"asof": None, "generated_at": None})
    return jsonify({"asof": data.get("asof"), "generated_at": data.get("asof")})


@app.route("/api/ticker")
def api_ticker_list():
    """Tüm ticker'ların hafif özeti (paylaşılan ticker store'dan)."""
    from RevenueChaser.core import ticker_store
    return jsonify({
        "asof": ticker_store.asof(),
        "count": ticker_store.count(),
        "tickers": ticker_store.list_tickers(),
    })


@app.route("/api/ticker/<ticker>")
def api_ticker(ticker: str):
    """Tek ticker'ın tüm zengin verisi (Asymmetric Growth Hunter skorları + 13F + temalar...)."""
    from RevenueChaser.core import ticker_store
    ticker = ticker.strip().upper()   # normalise first: lower-case ``aapl`` is a valid request
    if not _valid_ticker(ticker):
        return jsonify({"ok": False, "error": "bad ticker"}), 400
    data = ticker_store.get(ticker)
    if data is None:
        return jsonify({"ok": False, "error": "unknown ticker", "ticker": ticker}), 404
    return jsonify({"ok": True, "asof": ticker_store.asof(), "data": data})


@app.route("/api/spxgamma/latest")
def api_spxgamma_latest():
    payload = _spxgamma_latest_payload()
    if payload is None:
        return jsonify({"status": None, "generated_at": None})
    return jsonify(payload)


@app.route("/api/spxgamma/history")
def api_spxgamma_history():
    """Daily Net GEX history (for the calendar heatmap coloring)."""
    from RevenueChaser.module_spx_gamma.run import _load_history
    return jsonify({"days": _load_history()})


@app.route("/api/jobs")
def api_jobs():
    return jsonify({"jobs": jobs.list_jobs()})


@app.route("/api/logs")
def api_logs():
    """The Logs tab's poll: the tail of one of the dashboard's own log files, named by a source key."""
    try:
        data = logview.read(request.args.get("src", ""), request.args.get("run", "current"),
                            request.args.get("tail", logview.DEFAULT_TAIL))
    except ValueError:
        return jsonify({"ok": False, "error": "unknown log source or run"}), 400
    return jsonify(data)


@app.route("/api/job/<job_id>/cancel", methods=["POST"])
def api_job_cancel(job_id):
    """Cancel a queued job, or ask a running one that can stop early to stop at its next checkpoint (#243).

    409 for a running job that cannot stop early, and for a finished one.
    """
    job = jobs.get(job_id)
    if job is None:
        return jsonify({"ok": False, "error": "unknown job"}), 404
    if not jobs.cancel(job_id):
        if job["status"] == "running" and not job.get("cancellable"):
            return jsonify({"ok": False, "error": "this job cannot stop early; it runs to the end"}), 409
        return jsonify({"ok": False, "error": "job already finished"}), 409
    return jsonify({"ok": True, "job": jobs.get(job_id)})


@app.route("/api/job/<job_id>")
def api_job(job_id):
    job = jobs.get(job_id)
    if job is None:
        return jsonify({"ok": False, "error": "unknown job"}), 404
    return jsonify({"ok": True, "job": job})


@app.route("/api/rotation/dates")
def api_rotation_dates():
    from RevenueChaser.module_sector_rotation import store as rotation_store
    return jsonify({"dates": rotation_store.list_snapshot_dates()})


@app.route("/api/rotation/latest")
def api_rotation_latest():
    """Newest snapshot's as_of_date + generated_at (for last-run display + auto-run)."""
    from RevenueChaser.module_sector_rotation import store as rotation_store
    dates = rotation_store.list_snapshot_dates()
    if not dates:
        return jsonify({"as_of_date": None, "generated_at": None})
    latest = dates[-1]
    snap = rotation_store.load_snapshot(date.fromisoformat(latest)) or {}
    return jsonify({"as_of_date": latest, "generated_at": snap.get("generated_at")})


@app.route("/api/moneyflow")
def api_moneyflow():
    """Cached money-flow leaderboard (inflows / outflows)."""
    path = runners.MONEY_FLOW_LATEST
    if not path.exists():
        return jsonify({"inflows": [], "outflows": [], "generated_at": None})
    try:
        return jsonify(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return jsonify({"inflows": [], "outflows": [], "generated_at": None})


ROTATION_PICKER_WEEKS = 26  # how far back "Fetch Data Range" can reach


@app.route("/api/rotation/weeks")
def api_rotation_weeks():
    """Selectable ISO weeks for the rotation range picker.

    Returns the last ``ROTATION_PICKER_WEEKS`` weeks plus any older week that
    already has a snapshot, each flagged ``has_data``. Listing only the weeks
    that already had snapshots (the old behaviour) made "Fetch Data Range"
    unable to reach a week you had not already fetched — the one thing it is for.
    """
    from RevenueChaser.module_sector_rotation import store as rotation_store
    from RevenueChaser.module_sector_rotation.report import _weekly_sample, _week_label
    dates = rotation_store.list_snapshot_dates()
    # ISO Monday -> the stored snapshot date that represents that week
    stored: dict[str, str] = {}
    for d in _weekly_sample(dates):
        dt = date.fromisoformat(d)
        stored[(dt - timedelta(days=dt.weekday())).isoformat()] = d

    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    mondays = {(this_monday - timedelta(weeks=i)).isoformat()
               for i in range(ROTATION_PICKER_WEEKS)}
    mondays.update(stored)

    weeks = []
    for iso_monday in sorted(mondays):
        monday = date.fromisoformat(iso_monday)
        snap_date = stored.get(iso_monday)
        weeks.append({
            "label": _week_label(iso_monday),
            "start": iso_monday,
            "end": (monday + timedelta(days=6)).isoformat(),
            # ``date`` is the snapshot the week resolves to; for a week with no
            # data yet it is the Monday, so the client can still address it.
            "date": snap_date or iso_monday,
            "has_data": snap_date is not None,
        })
    return jsonify({"weeks": weeks})


@app.route("/view/module_sector_rotation/holdings")
@app.route("/view/rotation/holdings")  # legacy slug (pre-Phase-2) — keep working
def view_rotation_holdings():
    """Holdings detail row, as an HTML fragment.

    Sector ETFs (XLK, XLE, ...) expand to the sector -> industry tree built from
    the shared universe (cap-weighted, no network). Theme/region/bond ETFs keep
    their ETF top-10 with SPY-relative strength.
    """
    ticker = request.args.get("ticker", "").upper().strip()
    if not ticker:
        return "Missing ticker", 404
    if not _valid_ticker(ticker):   # only the sector and theme rows expand, and those are plain symbols
        return "Bad ticker", 400
    from RevenueChaser.module_sector_rotation import store as rotation_store
    from RevenueChaser.module_sector_rotation.engine import sector_etf_trees
    from RevenueChaser.module_sector_rotation.report import (
        render_holdings_fragment, render_industry_fragment,
    )

    try:
        tree = sector_etf_trees().get(ticker)
    except Exception:  # store unavailable/locked — fall through to ETF holdings
        logger.warning("industry tree unavailable for %s", ticker, exc_info=True)
        tree = None
    if tree is not None:
        return render_industry_fragment(tree)

    # Store-only (backend B9): a GET view never calls the network and never
    # hides stored data behind an API key. RS-enriched rows come from the
    # "holdings" job (Rotation → Fetch holdings); without them the bulk holdings
    # list is shown with RS as "—".
    rows = rotation_store.load_holdings_cache(ticker, any_age=True)
    if rows is None:
        rows = _load_holdings_bulk_cache(ticker)
    if not rows:
        return ('<td colspan="11" style="color:#8b949e;padding:10px 14px">'
                'No stored holdings for this ETF yet — use <b>Fetch holdings</b> on the '
                'Rotation board.</td>')
    return render_holdings_fragment(rows)


def _load_holdings_bulk_cache(ticker: str):
    """A ticker's raw holdings from the bulk ``etf_holdings`` sync (no RS, no network)."""
    from RevenueChaser.core import rawstore
    row = rawstore.get("etf_holdings", ticker.upper())
    holdings = (row or {}).get("entry")
    if not holdings:
        return None
    return [{"ticker": h.get("ticker"), "name": h.get("name") or "",
             "weight": h.get("weight"), "close": None,
             "rs_1w": None, "rs_1m": None, "rs_3m": None}
            for h in holdings if h.get("ticker")][:10]

@app.route("/view/module_sector_rotation/trail")
@app.route("/view/rotation/trail")  # legacy slug (pre-Phase-2) — keep working
def view_rotation_trail():
    from RevenueChaser.module_sector_rotation.report import export_rotation_trail
    def _d(key, default):
        raw = request.args.get(key)
        if not raw:
            return default
        raw = raw.split("?")[0].split("&")[0]  # tolerate stray cache-buster tokens
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return default
    start = _d("start", date.today() - timedelta(days=30))
    end = _d("end", date.today())
    f = export_rotation_trail(start, end)
    if f is None:
        return "No trail data", 404
    return send_from_directory(f.parent, f.name)


# ── API ──
@app.route("/api/sec_13f/<ticker>")
def api_sec13f(ticker: str):
    """Plotly figure for a ticker's SEC 13F total-ownership chart."""
    from RevenueChaser.module_sec_13f import engine as module_sec_13f
    from RevenueChaser.module_sec_13f.enrich import load_inputs
    from RevenueChaser.module_sec_13f.report import build_chart_fig
    t = ticker.strip().upper()
    if not _valid_ticker(t):
        return jsonify({"ok": False, "error": "bad ticker"}), 400
    totals = module_sec_13f.load_totals()
    periods = totals.get(t)
    if not periods:
        return jsonify({"ok": False, "error": "no series", "ticker": t}), 404
    enrich = load_inputs({t: periods}).get(t, {})       # the board's inputs (#375 F2): D01 closes, store only
    return jsonify({"ok": True, "ticker": t, "fig": build_chart_fig(t, periods, enrich)})


@app.route("/api/watchlist/toggle/<ticker>", methods=["POST"])
def api_watchlist_toggle(ticker: str):
    """Add/remove a ticker from the watchlist (used by the LEAPS pin)."""
    from RevenueChaser.core import rawstore
    t = (ticker or "").strip().upper()
    if not _valid_ticker(t):
        return jsonify({"ok": False, "error": "bad ticker"}), 400
    in_watchlist = rawstore.toggle_watchlist(t)     # this ticker only, one transaction (#306)
    return jsonify({"ok": True, "ticker": t, "in_watchlist": in_watchlist})


@app.route("/manifest.json")
def manifest():
    """PWA manifest — installs RevenueChaser as the 'REVENUE CHASER' app window."""
    return jsonify({
        "name": "REVENUE CHASER - ONLY THE GOOD ONES",
        "short_name": "REVENUE CHASER",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#05111F",
        "theme_color": "#05111F",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    })


def _find_chrome() -> str | None:
    """Locate a Chromium browser binary (Chrome). Returns path or None."""
    if sys.platform != "win32":
        return None
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    exe = shutil.which("chrome") or shutil.which("chromium") or shutil.which("chrome.exe")
    return exe


def _is_revenuechaser(port: int) -> bool:
    """True when the program listening on ``port`` is a RevenueChaser dashboard.

    Asks it for ``/manifest.json`` (every dashboard serves one) and checks the
    app name. Anything else — another program, no answer — is not ours.
    """
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/manifest.json", timeout=2) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - not answering like a dashboard = not ours
        return False
    return str(data.get("short_name") or "").upper() == "REVENUE CHASER"


class PortInUseError(RuntimeError):
    pass


def _kill_stale_server(port: int) -> None:
    """Kill a stale RevenueChaser dashboard still listening on ``port``.

    A stale server keeps serving cached templates (``debug=False``), so every
    restart must clear it first. It only ever kills a process that answers as a
    RevenueChaser dashboard (``_is_revenuechaser``); if some other program owns
    the port it raises ``PortInUseError`` instead of killing it. Works on Windows
    (``netstat -ano``) and macOS/Linux (``lsof -ti``) — no psutil dependency.
    """
    try:
        if sys.platform == "win32":
            out = subprocess.check_output(
                ["netstat", "-ano", "-p", "TCP"], text=True, errors="ignore")
            pids: set[int] = set()
            for line in out.splitlines():
                parts = line.split()
                if (len(parts) >= 5 and parts[0] == "TCP"
                        and parts[1].endswith(f":{port}")
                        and parts[3] == "LISTENING" and parts[4].isdigit()):
                    pids.add(int(parts[4]))
        else:  # macOS / Linux
            out = subprocess.check_output(
                ["lsof", "-ti", f"tcp:{port}"], text=True, errors="ignore")
            pids = {int(pid) for pid in out.split() if pid.isdigit()}
    except (subprocess.CalledProcessError, OSError):
        return  # no listener found (or tool missing) — nothing to do

    pids.discard(os.getpid())
    if not pids:
        return
    if not _is_revenuechaser(port):
        raise PortInUseError(
            f"Port {port} is used by another program (PID {', '.join(map(str, sorted(pids)))}); "
            f"it is not a RevenueChaser dashboard, so it was left running. Stop it or "
            f"start the dashboard on another port.")

    for pid in pids:
        if pid == os.getpid():
            continue  # never kill ourselves
        try:
            if sys.platform == "win32":
                os.kill(pid, 9)  # TerminateProcess (SIGKILL is not defined on Windows)
            else:
                os.kill(pid, _signal.SIGKILL)
            logger.info("Killed stale dashboard server PID %s (port %s)", pid, port)
        except OSError:
            pass


def main(port: int = 8000, open_browser: bool = True):
    # Clear any stale server still listening on the port so the fresh process
    # starts with current templates (Flask caches templates when debug=False).
    try:
        _kill_stale_server(port)
    except PortInUseError as exc:
        logger.error("%s", exc)
        print(f"\n  {exc}\n", file=sys.stderr)
        raise SystemExit(2) from exc
    # After the stale server is gone, so a row it left ``running`` can be closed (#82).
    try:
        jobs.reconcile_lost_runs()
    except Exception:  # noqa: BLE001 - the run log must not stop the dashboard from starting
        logger.warning("job_runs: could not reconcile stuck runs", exc_info=True)
    if open_browser:
        chrome = _find_chrome()
        if chrome:
            # Chrome --app: standalone app window (no tabs), sized to fit the
            # DPI-aware work area so the dashboard's bottom scrollbar never
            # lands under the taskbar. OverlayScrollbar off => horizontal
            # scrollbars stay visible instead of auto-hiding until hover.
            win_w, win_h = _app_window_size()
            subprocess.Popen([chrome, f"--app=http://localhost:{port}",
                              f"--window-size={win_w},{win_h}", "--window-position=0,0",
                              "--disable-features=OverlayScrollbar"],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        else:
            webbrowser.open(f"http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)


def _app_window_size() -> tuple[int, int]:
    """Preferred app-window size (logical px) that fits on the primary monitor.

    Chrome interprets ``--window-size`` in logical (DPI-aware) pixels, so on a
    high-DPI display (e.g. 200% = 3840x2160 physical) a hardcoded 1600x950 can
    overflow the visible area and push the page's bottom scrollbar under the
    taskbar. Query the DPI-aware work area (SPI_GETWORKAREA) and cap the window
    height to stay above the taskbar. Width stays 1600 on common monitors.
    """
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system DPI aware
            except Exception:  # pragma: no cover - older Windows
                ctypes.windll.user32.SetProcessDPIAware()
            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(
                    0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
                wa_w = rect.right - rect.left
                wa_h = rect.bottom - rect.top
                dpi = max(96, ctypes.windll.user32.GetDpiForSystem() or 96)
                scale = dpi / 96.0
                # work area from SystemParametersInfoW is in physical pixels for
                # a DPI-aware process; convert to logical for --window-size.
                log_w = int(wa_w / scale)
                log_h = int(wa_h / scale)
                win_w = min(1600, log_w)
                win_h = min(950, log_h - 40)  # leave room for the title bar
                if win_w >= 800 and win_h >= 500:
                    return win_w, win_h
    except Exception:  # pragma: no cover - measurement is best-effort
        pass
    return 1600, 950


if __name__ == "__main__":
    main()
