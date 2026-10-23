"""New-filing sync — detect a newer 10-K/20-F and refresh the raw doc + corpus.

For each classification-universe CIK, compare the stored accession in
``chain_filings`` against the newest 10-K/20-F in ``submissions.recent``. Only a
newer accession triggers a download → ``edgar_docs/`` file + ``edgar_documents``
metadata → re-slice → ``chain_filings`` upsert. Idempotent and resumable: a
re-run with no new filing writes nothing.

The same run is the daily SEC submissions sweep (#299, D15): it asks
``submissions/CIK*.json`` once for every CIK of the whole universe
(``equity_universe``, all caps), not only the classification universe, and stores
each answer through ``core/sec_submissions.py`` (filings once per accession, the
header on change). Only classification-universe tickers go on to the document
step. A CIK shared by two tickers is asked once.

A new filing whose slice is worse than the stored ``ok`` one is rejected
(``engine.store_slices``); its raw doc stays in ``edgar_docs/``, so the next run
re-slices it locally and never downloads it again (#116 C6). A CIK with no
10-K / 20-F, or with a 40-F, is recorded as a miss and skipped until its CIK
changes or ``config.MISS_RETRY_DAYS`` pass; a missing CIK is counted (C26).

Entry point is mechanism-agnostic (manual / dashboard job / scheduler):
``python -m RevenueChaser.module_value_chain --sync-filings``.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import CancelledError, FIRST_COMPLETED, ThreadPoolExecutor, wait

import httpx

from RevenueChaser.core import job_control, job_lock, ledger, rawstore, sec_submissions
from RevenueChaser.core.providers import sec_edgar

from . import edgar_text
from . import engine as vc_engine

logger = logging.getLogger(__name__)


def _universe_split(tickers: list[str] | None = None) -> tuple[dict[str, str], list[str]]:
    """``({ticker: cik}, [tickers with no CIK])`` for the classification universe."""
    from RevenueChaser.core.universe import classification_universe
    want = {t.upper() for t in tickers} if tickers else set(classification_universe())
    cmap = sec_edgar._cik_map()
    ciks = {t: str((cmap.get(t) or {}).get("cik"))
            for t in sorted(want) if (cmap.get(t) or {}).get("cik")}
    return ciks, sorted(want - set(ciks))


def _sweep_ciks(doc_ciks: list[str]) -> dict[str, list]:
    """``{cik: []}`` for every CIK of the whole universe (#299, D15), in request order: ``doc_ciks`` first
    (the classification universe, by ticker), then the CIKs of the other universe tickers."""
    out: dict[str, list] = {sec_submissions.norm_cik(c): [] for c in doc_ciks}
    cmap = sec_edgar._cik_map()
    for r in rawstore.query("SELECT ticker FROM equity_universe ORDER BY market_cap DESC NULLS LAST, ticker"):
        cik = (cmap.get(str(r["ticker"]).upper()) or {}).get("cik")
        if cik:
            out.setdefault(sec_submissions.norm_cik(cik), [])
    return out


def universe_ciks(tickers: list[str] | None = None) -> dict[str, str]:
    """``{ticker: cik}`` for the classification universe (via the SEC ticker map)."""
    return _universe_split(tickers)[0]


def _stored_accession(ticker: str) -> str | None:
    row = rawstore.get("chain_filings", ticker.upper())
    return (row or {}).get("accession") or None


def sync_one(ticker: str, cik, client=None, force: bool = False) -> dict:
    """Sync one issuer. Returns ``{ticker, status, accession?}``."""
    payload = sec_edgar.get_json(edgar_text.submissions_url(cik), client)
    return _sync_ticker(ticker, cik, payload, client, force)


def _sync_ticker(ticker: str, cik, payload: dict, client=None, force: bool = False,
                 run_at: float | None = None) -> dict:
    """The document step of one issuer, from a submissions answer already in hand."""
    filing = edgar_text.pick_filing(payload)
    if not filing or filing["form"] == edgar_text.FORM_40F:
        reason = "form_40f" if filing else "no_filing"
        vc_engine.record_misses([vc_engine.miss_row(ticker, reason, cik)])
        return {"ticker": ticker, "status": reason}
    acc = filing["accession"]
    if not force and _stored_accession(ticker) == acc:
        return {"ticker": ticker, "status": "current"}

    # A stored doc (a rejected filing, or the other share class of a dual-class
    # issuer) is re-sliced, never downloaded or stored again (D16: first write wins).
    row = edgar_text.document_slice(ticker, cik, filing, client, run_at=run_at)
    _written, kept = vc_engine.store_slices([row])
    return {"ticker": ticker, "status": "kept" if kept else "updated", "accession": acc}


def _status_code(exc: Exception) -> int | None:
    return getattr(getattr(exc, "response", None), "status_code", None)


def _sync_cik(cik: str, tickers: list[tuple[str, str]], client, force: bool, capture: sec_submissions.Capture,
              run_at: float) -> list[dict]:
    """One submissions request for ``cik``: the answer goes to ``capture``, then each of ``tickers`` (the
    classification ``(ticker, cik as the ticker map spells it)`` pairs of the CIK still due) takes the document
    step, with its own CIK spelling as before. A 404 is recorded as such and raises."""
    try:
        payload = sec_edgar.get_json(edgar_text.submissions_url(cik), client)
    except httpx.HTTPStatusError as exc:
        if _status_code(exc) == 404:
            capture.add_missing(cik)
        raise
    capture.add(cik, payload)
    out = []
    for t, ticker_cik in tickers:
        try:
            out.append(_sync_ticker(t, ticker_cik, payload, client, force, run_at=run_at))
        except sec_edgar.SECUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - one ticker's document step; the answer is already captured
            logger.warning("sync %s failed: %s", t, exc)
            out.append({"ticker": t, "status": "failed"})
    return out


FLUSH_EVERY = 250        # CIKs per submissions flush (one edgar transaction each)


def sync_filings(tickers: list[str] | None = None, limit: int | None = None,
                 no_fetch: bool = False, force: bool = False,
                 workers: int = 8, run_id: str | None = None) -> dict:
    """Sync the universe; safe to re-run. ``no_fetch`` never touches the network.

    Without ``tickers`` or ``limit`` the submissions sweep covers every universe CIK (#299); with either it
    covers only the CIKs of the tickers synced. ``run_id`` stamps the stored submissions (the dashboard job's
    id; a CLI run gets a fresh one). The result's ``submissions`` holds that sweep's counts.
    """
    ciks, no_cik = _universe_split(tickers)
    items = list(ciks.items())
    if limit:
        items = items[:limit]
    # A ticker with no CIK costs no request; it is counted, not silently dropped.
    requested = len(items) + len(no_cik)
    if no_fetch:
        return {"requested": requested, "updated": 0, "current": 0,
                "failed": 0, "no_cik": len(no_cik), "no_fetch": True}

    # Host-level mutex: one EDGAR crawl at a time (crawler / 13F / guidance).
    with job_lock.acquire("edgar_sync", wait=0) as got:
        if not got:
            return {"requested": requested, "updated": 0, "current": 0,
                    "failed": 0, "locked": True}

        rid = run_id or ledger.new_run_id()
        run_at = time.time()
        misses = vc_engine.stored_misses()
        todo = [(t, c) for t, c in items
                if force or not vc_engine.miss_is_current(misses.get(t), c)]
        counts = {"updated": 0, "current": 0, "no_filing": 0, "failed": 0,
                  "no_cik": len(no_cik), "known_miss": len(items) - len(todo)}
        # One request per CIK: the due classification tickers ride on it; a known miss and a
        # ticker outside the classification universe get the submissions answer only.
        doc_ciks = [c for _t, c in items]
        work = _sweep_ciks(doc_ciks) if not tickers and not limit else {
            sec_submissions.norm_cik(c): [] for c in doc_ciks}
        for t, c in todo:
            work[sec_submissions.norm_cik(c)].append((t, c))
        capture = sec_submissions.Capture()
        subs = {**sec_submissions.EMPTY_RESULT, "requested": len(work), "failed": 0, "failed_ciks": []}
        errors: list[str] = []

        def _flush():
            res = capture.flush(run_id=rid)
            if res.get("error"):
                errors.append(res["error"])
            for k in sec_submissions.EMPTY_RESULT:
                subs[k] += res.get(k, 0)

        sec_edgar.reset_breaker()  # every run tries SEC again
        stopped = False
        done = 0  # every ticker that came back with a status, whatever it is
        lost = 0  # tickers whose CIK request failed (no status)
        answered = 0
        # One filer is submitted only after a checkpoint, and only while the pool has a free worker. A cancel or a
        # passed deadline therefore stops before the next filer starts, not inside its submissions write (#26 PL4).
        pending = iter(list(work.items()))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {}

            def _submit_until_full():
                while len(futs) < workers:
                    try:
                        c, ts = next(pending)
                    except StopIteration:
                        return
                    # Name the filer first, so a deadline already past says where it stopped, before any submit.
                    job_control.checkpoint(f"EDGAR filer {c}")
                    if job_control.stop_requested():
                        job_control.stop_pool(futs)
                    futs[ex.submit(_sync_cik, c, ts, None, force, capture, capture.run_at)] = (c, ts)

            _submit_until_full()
            while futs:
                finished, _ = wait(set(futs), return_when=FIRST_COMPLETED)
                for f in finished:
                    c, ts = futs.pop(f)
                    try:
                        results = f.result()
                    except sec_edgar.SECUnavailable:
                        # SEC is down for this process: stop queueing, let running calls finish.
                        stopped = True
                        for other in futs:
                            other.cancel()
                        continue
                    except CancelledError:
                        continue
                    except Exception as exc:  # noqa: BLE001 - per-CIK failure
                        counts["failed"] += len(ts)
                        lost += len(ts)
                        if _status_code(exc) == 404:   # answered: the SEC does not serve this CIK
                            logger.info("sync CIK %s (%s): not served by SEC", c, ",".join(t for t, _c in ts) or "-")
                            answered += 1
                        else:
                            logger.warning("sync CIK %s (%s) failed: %s", c, ",".join(t for t, _c in ts) or "-", exc)
                            subs["failed"] += 1
                            subs["failed_ciks"].append(c)
                        continue
                    done += len(results)
                    for res in results:
                        counts[res["status"]] = counts.get(res["status"], 0) + 1
                    answered += 1
                    if len(capture) >= FLUSH_EVERY:
                        _flush()
                if job_control.stop_requested():
                    job_control.stop_pool(futs)
                if not stopped:
                    _submit_until_full()
        _flush()
        if stopped:
            counts["sec_unavailable"] = True
            counts["pending"] = len(todo) - done - lost
            subs["pending"] = len(work) - answered - subs["failed"]
            logger.warning("SEC unavailable: %d of %d done, rest pending", done, len(todo))
        subs["failed_ciks"].sort()
        if errors:
            subs["error"] = errors[0]

    # Refresh the derived edges from the (possibly updated) corpus, and record them in result history (#387).
    job_control.checkpoint("EDGAR edges")
    info: dict = {}
    edges = vc_engine.build_edges(info)
    vc_engine.record_history(edges, info, run_id=rid, run_at=run_at, counts=counts)
    return {"requested": requested, **counts, "edges": len(edges), "run_id": rid, "submissions": subs}
