"""The identity refresh (issue #85, Part 3; sector and industry reworked in #265, S&P list and SIC fallback in #272):
fetch the deltas, build the master, record the run.

``refresh()`` wires the pieces together and **never raises**: any exception becomes a recorded run
with status ``error``. It writes the identity tables (``security_master``, ``identity_conflicts``,
``identity_source_cache``, ``identity_runs``), and, after a successful master build (status ``ok`` or
``partial``, never after an error), the master's sector and industry (plus UW's next earnings date) are
applied to ``equity_universe`` (issue #86, R3; #265): see ``core.identity.apply`` -- those columns only,
tickers without a master row untouched. The same apply step copies ``cusip`` / ``isin`` onto ``security_master``
from the CUSIP map already in the store (#127); it does not call a provider. The apply counts are in the run record (``counts["applied"]`` /
``unchanged`` / ``not_in_master``).

Fetch plan (per run):

* SEC exchange file: no fetch. The nightly RC-Daily-Bars step pulls ``company_tickers_exchange.json`` and
  stores its change history (#301 D21); this job reads the stored rows (the retired ``sec_company_tickers``
  is not written any more).
* Nasdaq directory, the S&P 1500 snapshot and UW sectors: every run.
* SEC submissions: tickers with no cached row, or one older than 30 days (per-CIK; a CIK shared
  by several tickers is fetched once and cached for each of them), plus a stagger: the oldest 25% of the
  cached rows that are at least 7 days old, so a universe fetched in one day does not expire together.
* OpenFIGI: tickers with no cached row, or one older than 90 days; a ``no_match`` row after 7 days
  (new listings reach OpenFIGI late, and a spelling fix must reach old misses).
* Yahoo sector: only tickers UW gave no usable sector for (one of the 11 names; largest caps first, optionally
  capped).
* FDIC: only the candidates (no SEC periodic filings, OpenFIGI says common stock).
* SIC: no fetch of its own. The raw SIC group on the master (``sic``, ``sic_description``, ``sic_group2``) and the
  SIC sector fallback read the SEC submissions cache, then ``sec_company_sic``.
* SPDR: a *check*, every run. The 11 Select Sector fund holdings are compared with the master sector (just
  built) of each S&P 500 member (``index_constituents`` slug ``sp500``) and a mismatch becomes a ``spdr``
  conflict (``counts.spdr_checked`` / ``spdr_agree``). SPDR never sets a value, is never cached, is not in
  "sources ok n/m", and a failed check leaves the run ``ok``.

The S&P snapshot goes through ``sp1500.diff_gate`` against the last accepted snapshot: a rejected snapshot is not
cached (the previous one keeps being used), a ``source`` conflict is recorded and the source is marked ``gated``. On
a run with no fresh accepted snapshot (failed, gated or not selected) the last accepted snapshot answers; a name that
has since left the list keeps its old cache row but is not in that snapshot, so it is not listed (#272).

Inputs for ``master.build`` come from the cache (see ``master.py``): a cached row wins; a healthy
source without a row gives ``{}``; a source that failed or was skipped (and has no row) gives ``None``.
"""
from __future__ import annotations

import math
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Callable

from .. import index_registry, job_lock, rawstore, sec_files
from . import apply as universe_apply
from . import cache, gics, master
from . import types as T
from .sources import SourceResult, fail, norm_ticker, now_iso, redact

SEC_DAYS = 30
# The whole universe was first fetched on one day, so without a stagger every cached SEC submissions row
# would expire together and one run would re-fetch the lot. Each run therefore also refreshes the oldest
# SEC_STAGGER of the cached rows that are at least SEC_STAGGER_MIN_DAYS old (issue #89, amends K5).
SEC_STAGGER = 0.25
SEC_STAGGER_MIN_DAYS = 7
FIGI_DAYS = 90
FIGI_MISS_DAYS = 7
YAHOO_DAYS = 90
FDIC_DAYS = 90
# Yahoo .info is per-ticker and rate-limited (docs/data_health.md: never the whole
# universe in one go). Each run fills at most this many sector gaps, largest caps
# first; later runs continue where it stopped (the 90-day window skips done ones).
YAHOO_PER_RUN = 200

SOURCES = ("sec_exchange", "sec_submissions", "openfigi", "nasdaq", "sp1500", "uw_sector", "yahoo_sector", "fdic",
           "spdr")
# checks only compare: their failure does not make a run ``partial``, and they are not in "sources ok n/m"
CHECKS = ("spdr",)
# CLI / `sources=` names -> the source keys they switch on
GROUPS = {"sec": ("sec_exchange", "sec_submissions"), "sec_exchange": ("sec_exchange",),
          "sec_submissions": ("sec_submissions",), "openfigi": ("openfigi",), "nasdaq": ("nasdaq",),
          "sp1500": ("sp1500",), "uw": ("uw_sector",), "uw_sector": ("uw_sector",),
          "yahoo": ("yahoo_sector",), "yahoo_sector": ("yahoo_sector",), "fdic": ("fdic",), "spdr": ("spdr",)}
# the "sources ok n/m" summary counts these groups
_SUMMARY_GROUPS = (("sec_exchange", "sec_submissions"), ("openfigi",), ("nasdaq",), ("sp1500",), ("uw_sector",),
                   ("yahoo_sector",), ("fdic",))
_TS = "%Y-%m-%dT%H:%M:%SZ"


def _default_clients() -> dict[str, Callable]:
    from .. import sec_files
    from .sources import fdic, nasdaq, openfigi, sec, sp1500, spdr, uw_sector, yahoo_sector
    # The exchange file is pulled once a night by RC-Daily-Bars (#301 D21); this job reuses its stored rows.
    return {"sec_exchange": sec_files.exchange_source, "sec_submissions": sec.submissions,
            "openfigi": openfigi.security_types, "nasdaq": nasdaq.symbol_directory, "sp1500": sp1500.snapshot,
            "uw": uw_sector.sectors, "yahoo": yahoo_sector.sectors, "fdic": fdic.match_banks,
            "spdr": spdr.holdings}


def _parse(ts) -> datetime | None:
    try:
        return datetime.strptime(str(ts), _TS).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _stale(fetched_at, now: datetime, days: int) -> bool:
    t = _parse(fetched_at)
    return t is None or (now - t).days >= days


def _fetched(source: str) -> dict[str, str]:
    return {r["ticker"]: r["fetched_at"] for r in rawstore.query(
        "SELECT ticker, fetched_at FROM identity_source_cache WHERE source = ?", [source])}


def _misses(source: str) -> set[str]:
    return {r["ticker"] for r in rawstore.query(
        "SELECT ticker FROM identity_source_cache WHERE source = ? AND status = 'no_match'", [source])}


def _by_ticker(source: str) -> dict[str, dict]:
    return {r["ticker"]: r for r in cache.last_good(source)}


def _last_snapshot(source: str) -> list[dict]:
    """The rows of the last accepted snapshot of a list source, not every row the cache has ever held.

    The cache never deletes, so a name that left the list keeps its old row. The rows of one saved snapshot share
    their ``fetched_at``; the newest such stamp is the last accepted snapshot (a failed fetch carries no rows and a
    rejected one is not cached). Used for the S&P 1500 list, both as the gate's baseline and as the answer on a run
    that has no fresh snapshot (#272).
    """
    rows = cache.last_good(source)
    newest = max((r["fetched_at"] for r in rows if r.get("fetched_at")), default=None)
    return [r for r in rows if r.get("fetched_at") == newest] if newest else rows


# ── row -> master input dict ──────────────────────────────────────────────────────────────────
def _conv_sec(r):
    return {"status": r.get("status") or "ok", "name": r.get("name"), "sic": r.get("sic"),
            "sic_description": r.get("sic_description"), "forms": r.get("forms") or [],
            "shell_closed": bool(r.get("shell_closed"))}


def _conv_openfigi(r):
    if r.get("status") == "no_match" or not r.get("security_type"):
        return {}
    return {"security_type": r.get("security_type"), "security_type2": r.get("security_type2"), "name": r.get("name"),
            "figi": r.get("figi"), "composite_figi": r.get("composite_figi"),
            "share_class_figi": r.get("share_class_figi"), "exch_code": r.get("exch_code"),
            "security_description": r.get("security_description")}


def _conv_nasdaq(r):
    return {"is_etf": r.get("is_etf"), "security_name": r.get("security_name")}


def _conv_sp1500(r):
    return {"sector": r.get("sector"), "sub_industry": r.get("sub_industry"), "index": r.get("index")} \
        if r.get("sector") else {}


def _conv_uw(r):
    if not (r.get("sector") or r.get("security_type")):
        return {}
    return {"sector": r.get("sector"), "industry": r.get("industry"), "security_type": r.get("security_type"),
            "market_cap": r.get("market_cap")}


def _conv_yahoo(r):
    return {"sector": r.get("sector"), "industry": r.get("industry")} if r.get("sector") else {}


def _conv_fdic(r):
    return {"match": r} if r.get("status") == "ok" else {"match": None}


def _pick(ticker, rows: dict, healthy: bool, failed: set, conv):
    """A cached row wins; a healthy source with no row answers ``{}``; otherwise ``None`` (no answer)."""
    row = rows.get(ticker)
    if row is not None:
        return conv(row)
    return {} if healthy and ticker not in failed else None


def _rows_of(res: SourceResult) -> dict[str, dict]:
    return {r["ticker"]: r for r in res.rows if r.get("ticker")}


def _universe(limit):
    sql = "SELECT ticker, name, market_cap FROM equity_universe WHERE ticker IS NOT NULL ORDER BY ticker"
    rows = rawstore.query(sql)
    return rows[:limit] if limit else rows


_LOCK = "identity"


def _busy_record(run_id: str, started: str, holder_pid: int) -> dict:
    """The run record of a refresh another holder kept out: a status, the holder's PID, and no counts.

    ``counts`` is there and empty, never missing: a reader that walks it has nothing to special-case.
    """
    return {"run_id": run_id, "started_at": started, "finished_at": now_iso(), "status": "busy", "error": None,
            "holder_pid": holder_pid, "counts": {},
            "sources": {s: {"status": "skipped", "selected": False, "rows": 0, "fetched": 0, "seconds": 0.0,
                            "error": None} for s in SOURCES}}


def refresh(limit: int | None = None, sources=None, now: str | None = None, clients: dict | None = None,
            yahoo_limit: int | None = None, progress: Callable[[str], None] | None = None) -> dict:
    """Run one identity refresh. Returns the run record (``run_id``, ``status``, ``sources``, ``counts`` ...).

    ``sources`` restricts the run to the named sources (``sec, openfigi, nasdaq, uw, yahoo, fdic, spdr``);
    the rest are ``skipped`` and their cached answers are used. ``clients`` replaces source
    functions (tests). Never raises.

    One run at a time (``job_lock`` name ``"identity"``, #26): the lock is taken here, so it covers the dashboard
    job and the CLI alike. While another refresh holds it, this writes nothing at all — not even an
    ``identity_runs`` row — and returns a record with ``status`` ``"busy"`` and the holder's ``holder_pid``
    (``0`` when the lock is held but its owner cannot be read).
    """
    started = now or now_iso()
    run_id = f"{started.replace('-', '').replace(':', '')}-{uuid.uuid4().hex[:6]}"
    with job_lock.acquire(_LOCK, wait=0) as got:
        if not got:
            return _busy_record(run_id, started, dict(job_lock.holders()).get(_LOCK, 0))
        rec: dict = {"run_id": run_id, "started_at": started, "finished_at": None, "status": "error", "error": None,
                     "sources": {s: {"status": "skipped", "selected": False, "rows": 0, "fetched": 0, "seconds": 0.0,
                                     "error": None} for s in SOURCES}, "counts": {}}
        try:
            _run(rec, limit, sources, started, clients, yahoo_limit, progress or (lambda m: None))
        except Exception as exc:  # noqa: BLE001 - refresh never raises
            rec["status"], rec["error"] = "error", redact(f"{type(exc).__name__}: {exc}")[:500]
        rec["finished_at"] = now_iso()
        try:
            rawstore.write_rows("identity_runs", [{
                "run_id": run_id, "started_at": started, "finished_at": rec["finished_at"],
                "status": rec["status"],
                "sources": rec["sources"],
                "counts": dict(rec["counts"], **({"error": rec["error"]} if rec["error"] else {}))}])
        except Exception as exc:  # noqa: BLE001
            rec["record_error"] = redact(f"{type(exc).__name__}: {exc}")[:300]
        return rec


def _run(rec, limit, sources, started, clients, yahoo_limit, say):
    now_dt = _parse(started) or datetime.now(timezone.utc)
    fns = {**_default_clients(), **(clients or {})}
    selected = set(SOURCES)
    if sources:
        selected = set()
        for name in sources:
            if name not in GROUPS:
                raise ValueError(f"unknown identity source {name!r}; choose from {sorted(GROUPS)}")
            selected.update(GROUPS[name])
    srec = rec["sources"]
    for s in selected:
        srec[s]["selected"] = True
        srec[s]["status"] = "ok"

    universe = _universe(limit)
    if not universe:
        raise RuntimeError("equity_universe is empty: nothing to build the identity master from")
    tickers = [u["ticker"] for u in universe]
    uni = {u["ticker"]: u for u in universe}
    healthy: dict[str, bool] = {}
    failed: dict[str, set] = {s: set() for s in SOURCES}
    extra_conflicts: list[dict] = []

    def call(name, key, *args, **kw) -> SourceResult:
        t0 = time.monotonic()
        try:
            res = fns[key](*args, **kw)
        except Exception as exc:  # noqa: BLE001 - a raising source is an error source
            res = fail(name, exc)
        srec[name].update(status=res.status, rows=len(res.rows), error=res.error,
                          seconds=round(time.monotonic() - t0, 1))
        healthy[name] = res.ok
        say(f"{name}: {res.status} ({len(res.rows)} rows, {srec[name]['seconds']}s)"
            + (f" - {res.error}" if res.error else ""))
        return res

    def note_failed_rows(name, res):
        for r in res.rows:
            if r.get("status") == "error" and r.get("ticker"):
                failed[name].add(r["ticker"])

    # ── SEC exchange file (the stored rows; the nightly step pulls the file, #301 D21) ──
    if "sec_exchange" in selected:
        res = call("sec_exchange", "sec_exchange")
        cache.save_results([res])
    else:
        healthy["sec_exchange"] = False
    emap = _by_ticker("sec_exchange")
    store_map = {r["ticker"]: r for r in sec_files.latest("exchange")}

    def cik_of(t):
        r = emap.get(t) or store_map.get(t)
        return str(int(r["cik"])) if r and str(r.get("cik") or "").isdigit() else None

    # ── SEC submissions (delta: new or > 30 days, plus the oldest quarter of the cache) ──
    if "sec_submissions" in selected:
        fetched = _fetched("sec_submissions")
        by_cik: dict[int, list[str]] = {}
        for t in tickers:
            c = cik_of(t)
            if c:
                by_cik.setdefault(int(c), []).append(t)
        with_cik = [t for ts in by_cik.values() for t in ts]
        stale = {t for t in with_cik if _stale(fetched.get(t), now_dt, SEC_DAYS)}
        cached = [t for t in with_cik if _parse(fetched.get(t)) is not None]
        quota = math.ceil(len(cached) * SEC_STAGGER)
        pool = sorted((t for t in cached if _stale(fetched.get(t), now_dt, SEC_STAGGER_MIN_DAYS)),
                      key=lambda t: (fetched[t], t))
        stagger = set(pool[:quota])
        refresh_set = stale | stagger
        need = {c: ts for c, ts in by_cik.items() if any(t in refresh_set for t in ts)}
        srec["sec_submissions"]["fetched"] = len(need)
        srec["sec_submissions"]["staggered"] = len(stagger - stale)
        if need:
            res = call("sec_submissions", "sec_submissions", sorted(need),
                       ticker_by_cik={c: ts[0] for c, ts in need.items()})
            rows = []
            for r in res.rows:
                c = int(r["cik"]) if str(r.get("cik") or "").isdigit() else None
                for t in (need.get(c) or ([r["ticker"]] if r.get("ticker") else [])):
                    rows.append(dict(r, ticker=t))
            res = SourceResult(res.source, res.status, rows, res.error, res.fetched_at)
            note_failed_rows("sec_submissions", res)
            cache.save_results([res])
        else:
            healthy["sec_submissions"] = True
    else:
        healthy["sec_submissions"] = False
    sec_rows = _by_ticker("sec_submissions")

    # ── OpenFIGI (delta: new, > 90 days, or a miss > 7 days) ──
    if "openfigi" in selected:
        fetched = _fetched("openfigi")
        misses = _misses("openfigi")
        need_f = [t for t in tickers if _stale(fetched.get(t), now_dt, FIGI_DAYS)
                  or (t in misses and _stale(fetched.get(t), now_dt, FIGI_MISS_DAYS))]
        srec["openfigi"]["fetched"] = len(need_f)
        if need_f:
            res = call("openfigi", "openfigi", need_f)
            note_failed_rows("openfigi", res)
            cache.save_results([res])
        else:
            healthy["openfigi"] = True
    else:
        healthy["openfigi"] = False
    figi_rows = _by_ticker("openfigi")

    # ── Nasdaq / UW (every run) ──
    # A healthy full snapshot is authoritative for its own run: a ticker it no longer lists must read
    # as "not listed" ({}), not as the stale cached row (the cache itself never deletes).
    if "nasdaq" in selected:
        res = call("nasdaq", "nasdaq")
        cache.save_results([res])
        nasdaq_rows = _rows_of(res) if res.ok else _by_ticker("nasdaq")
    else:
        healthy["nasdaq"] = False
        nasdaq_rows = _by_ticker("nasdaq")

    sp_rows = None
    if "sp1500" in selected:
        res = call("sp1500", "sp1500")
        if res.ok:
            accept, share, changed = _gate(res)
            if not accept:
                srec["sp1500"].update(status="gated", error=f"snapshot rejected: {share:.1%} of rows changed sector")
                say(f"sp1500: gated ({share:.1%} changed)")
                extra_conflicts.append({
                    "ticker": "*", "kind": "source", "field": "sp1500_snapshot", "side_a": "previous snapshot",
                    "side_b": f"new snapshot ({len(res.rows)} rows)",
                    "note": f"{share:.1%} of rows changed sector (limit {cache_gate_limit():.0%}); kept the "
                            f"previous snapshot. e.g. " + ", ".join(
                                f"{c['ticker']} {c['old_sector']}->{c['new_sector']}" for c in changed[:5])})
                res = SourceResult(res.source, "ok", [], None, res.fetched_at)        # nothing to cache
            else:
                sp_rows = _rows_of(res)
        cache.save_results([res])
    else:
        healthy["sp1500"] = False
    # no fresh accepted snapshot (failed, gated or not selected): the last accepted one answers, never the names
    # that have since left the list
    sp_rows = sp_rows if sp_rows is not None else {r["ticker"]: r for r in _last_snapshot("sp1500")}

    # ── SPDR sector check: fetched here, compared below with the master the run builds (never a value) ──
    spdr_res = call("spdr", "spdr") if "spdr" in selected else None      # not cached, not read by the master

    if "uw_sector" in selected:
        res = call("uw_sector", "uw")
        cache.save_results([res])
        uw_rows = _rows_of(res) if res.ok else _by_ticker("uw_sector")
    else:
        healthy["uw_sector"] = False
        uw_rows = _by_ticker("uw_sector")

    # ── Yahoo sector: only tickers UW has no usable sector for ──
    if "yahoo_sector" in selected:
        fetched = _fetched("yahoo_sector")
        if not (healthy.get("uw_sector") or uw_rows):
            srec["yahoo_sector"].update(status="skipped", error="no UW sector data: every ticker would look sector-less")
            healthy["yahoo_sector"] = False
        else:
            cand = [t for t in tickers if not gics.provider_sector(uw_rows.get(t))
                    and _stale(fetched.get(t), now_dt, YAHOO_DAYS)]
            cand.sort(key=lambda t: -(uni[t]["market_cap"] or 0))
            cap = YAHOO_PER_RUN if yahoo_limit is None else yahoo_limit
            if cap:
                cand = cand[:cap]
            srec["yahoo_sector"]["fetched"] = len(cand)
            if cand:
                res = call("yahoo_sector", "yahoo", cand)
                note_failed_rows("yahoo_sector", res)
                cache.save_results([res])
            else:
                healthy["yahoo_sector"] = True
    else:
        healthy["yahoo_sector"] = False
    yahoo_rows = _by_ticker("yahoo_sector")

    # ── inputs, then FDIC for its candidates ──
    sic_rows = {r["ticker"]: r for r in rawstore.query("SELECT ticker, sic, sic_description FROM sec_company_sic")}
    inputs: dict[str, dict] = {}
    for t in tickers:
        c = cik_of(t)
        inputs[t] = {
            "name": uni[t]["name"], "market_cap": uni[t]["market_cap"], "cik": c,
            "cik_source": "sec_exchange",
            "exchange": (emap.get(t) or {}).get("exchange"), "sic_store": (sic_rows.get(t) or {}).get("sic"),
            "sic_store_description": (sic_rows.get(t) or {}).get("sic_description"),
            "sec": _pick(t, sec_rows, healthy.get("sec_submissions", False), failed["sec_submissions"], _conv_sec),
            "openfigi": _pick(t, figi_rows, healthy.get("openfigi", False), failed["openfigi"], _conv_openfigi),
            "nasdaq": _pick(t, nasdaq_rows, healthy.get("nasdaq", False), failed["nasdaq"], _conv_nasdaq),
            "sp1500": _pick(t, sp_rows, healthy.get("sp1500", False), failed["sp1500"], _conv_sp1500),
            "uw": _pick(t, uw_rows, healthy.get("uw_sector", False), failed["uw_sector"], _conv_uw),
            "yahoo": _pick(t, yahoo_rows, healthy.get("yahoo_sector", False), failed["yahoo_sector"], _conv_yahoo),
        }

    def fdic_candidate(t) -> bool | None:
        i = inputs[t]
        if i["sec"] is None or i["openfigi"] is None:
            return None
        ft = T.openfigi_type(i["openfigi"].get("security_type"), i["openfigi"].get("security_type2"))
        return T.filer_class(i) in ("no_periodic_filings", "not_in_sec") and ft == "common"

    fdic_cached = _by_ticker("fdic")
    if "fdic" in selected:
        fetched = _fetched("fdic")
        need_d = {t: (uni[t]["name"] or (inputs[t]["openfigi"] or {}).get("name") or t) for t in tickers
                  if fdic_candidate(t) and _stale(fetched.get(t), now_dt, FDIC_DAYS)}
        srec["fdic"]["fetched"] = len(need_d)
        if need_d:
            res = call("fdic", "fdic", need_d)
            note_failed_rows("fdic", res)
            cache.save_results([res])
            fdic_cached = _by_ticker("fdic")
        else:
            healthy["fdic"] = True
    else:
        healthy["fdic"] = False
    for t in tickers:
        if t in fdic_cached:
            inputs[t]["fdic"] = _conv_fdic(fdic_cached[t])
        elif fdic_candidate(t) is None or (fdic_candidate(t) and (not healthy.get("fdic") or t in failed["fdic"])):
            inputs[t]["fdic"] = None
        else:
            inputs[t]["fdic"] = {}

    # ── build and store ──
    previous = {r["ticker"]: r for r in rawstore.query("SELECT * FROM security_master")}
    rows, conflicts = master.build(inputs, previous, now=started)
    spdr_counts: dict = {}
    if spdr_res is not None and spdr_res.ok:
        spdr_conflicts, spdr_counts = _spdr_check(spdr_res, rows)
        extra_conflicts.extend(spdr_conflicts)
    conflicts = conflicts + extra_conflicts
    _write_conflicts(conflicts, started)
    # #127: CUSIP/ISIN from the map we already hold, on the rows about to be written (one history row, not a
    # NULL-then-fill). ``apply_identifiers`` below covers spine tickers this run did not rebuild.
    id_map = universe_apply.load_map()       # once per identity run, shared with apply_identifiers below
    rows = universe_apply.stamp_rows(rows, id_map)
    rawstore.write_rows("security_master", rows)

    # ── run record ──
    by_src = Counter(r["sector_source"] for r in rows)
    rec["counts"] = {
        "tickers": len(rows), "by_type": dict(Counter(r["security_type"] for r in rows)),
        "by_sector_source": dict(by_src),
        "unclassified": sum(1 for r in rows if r["sector"] in (None, "Unclassified")),
        "conflicts": len(conflicts), "conflicts_by_kind": dict(Counter(c["kind"] for c in conflicts)),
    }
    rec["counts"].update(spdr_counts)
    bad = [s for s in selected if s not in CHECKS and srec[s]["status"] != "ok"]
    rec["status"] = "partial" if bad else "ok"

    # ── apply the master's sector and industry to equity_universe (R3, #86; #265): only after a successful build ──
    # An exception here is a recorded error (the generic handler in ``refresh``): the master above stays.
    rec["counts"].update(universe_apply.apply_to_universe())
    rec["counts"].update(universe_apply.apply_identifiers(id_map))
    say(f"apply: {rec['counts'].get('applied', 0)} universe rows updated, "
        f"{rec['counts'].get('not_in_master', 0)} without a master row")


def cache_gate_limit() -> float:
    from .sources import sp1500
    return sp1500.DIFF_THRESHOLD


def _gate(res: SourceResult):
    from .sources import sp1500
    return sp1500.diff_gate(_last_snapshot("sp1500"), res.rows)


def _spdr_check(res: SourceResult, master_rows: list[dict]) -> tuple[list[dict], dict]:
    """``(conflicts, counts)`` of the SPDR check: each S&P 500 member (``index_constituents`` slug ``sp500``, UW's
    spelling normalised) that a fund holds and the master has is compared with the master sector. Flag only."""
    hold = _rows_of(res)
    members = {norm_ticker(t) for t in index_registry.load_index("sp500") or ()}
    sector = {r["ticker"]: r.get("sector") for r in master_rows}
    checked = sorted(t for t in members if t in hold and t in sector)
    mismatch = [t for t in checked if sector[t] != hold[t].get("sector")]
    conflicts = []
    for t in mismatch:
        fund = hold[t].get("fund")
        conflicts.append({
            "ticker": t, "kind": "spdr", "field": "sector", "side_a": sector[t], "side_b": hold[t].get("sector"),
            "note": f"SPDR {fund} holdings" if fund else "SPDR holdings"})
    return conflicts, {"spdr_checked": len(checked), "spdr_agree": len(checked) - len(mismatch)}


def _write_conflicts(conflicts: list[dict], now: str) -> None:
    if not conflicts:
        return
    first = {(r["ticker"], r["kind"], r["field"]): r["first_seen"] for r in rawstore.query(
        "SELECT ticker, kind, field, first_seen FROM identity_conflicts")}
    rawstore.write_rows("identity_conflicts", [
        dict(c, first_seen=first.get((c["ticker"], c["kind"], c["field"])) or now, last_seen=now)
        for c in conflicts])


def summary(rec: dict) -> str:
    """One line: ``7,366 tickers · 213 funds/ETFs · 116 SPACs · sectors: S&P 1,501 / UW 4,500 / Yahoo 900 / SIC 120 / unclassified 345 · 266 conflicts · universe 7,000 updated / 3 without a master row · sources ok 7/7``.

    A run another holder kept out has no counts: it says so — ``identity refresh skipped: another identity refresh
    (pid 1234) is running; nothing was written.`` — rather than printing a row of zeros.
    """
    if rec.get("status") == "busy":
        pid = rec.get("holder_pid") or 0
        who = f" (pid {pid})" if pid else ""
        return f"identity refresh skipped: another identity refresh{who} is running; nothing was written."
    if rec.get("status") == "error" and not rec.get("counts"):
        return f"identity refresh failed: {rec.get('error')}"
    c = rec.get("counts") or {}
    bt, bs = c.get("by_type", {}), c.get("by_sector_source", {})
    parts = [f"{c.get('tickers', 0):,} tickers", f"{bt.get('etp', 0) + bt.get('cef', 0):,} funds/ETFs",
             f"{bt.get('spac', 0):,} SPACs",
             f"sectors: S&P {bs.get('sp1500', 0):,} / UW {bs.get('provider_uw', 0):,} / "
             f"Yahoo {bs.get('provider_yf', 0):,} / SIC {bs.get('sic_explicit', 0):,} "
             f"/ unclassified {c.get('unclassified', 0):,}",
             f"{c.get('conflicts', 0):,} conflicts"]
    if "applied" in c:
        parts.append(f"universe {c['applied']:,} updated / {c.get('not_in_master', 0):,} without a master row")
    sel = [g for g in _SUMMARY_GROUPS if any(rec["sources"][s]["selected"] for s in g)]
    good = sum(1 for g in sel if all(rec["sources"][s]["status"] == "ok" for s in g if rec["sources"][s]["selected"]))
    # a checks-only run (``sources=["spdr"]``) has no source group to count: say so, not "ok 0/0" (#142c)
    parts.append(f"sources ok {good}/{len(sel)}" if sel else "checks only")
    if rec.get("status") == "error":
        parts.append(f"ERROR: {rec.get('error')}")
    return " · ".join(parts)
