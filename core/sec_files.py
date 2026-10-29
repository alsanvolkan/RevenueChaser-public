"""SEC exchange and fund files (#301, D21 + D8): the one writer and reader of ``sec_exchange_file`` and
``sec_mf_file`` (raw).

A best-effort first step of the nightly daily-bars run (``scripts/daily_bars.py --nightly --sec-files``, task
RC-Daily-Bars): one GET of ``company_tickers_exchange.json`` (``sec.exchange_map``) and one of
``company_tickers_mf.json`` (``sec.fund_map``) per weekday night. Every other reader now reuses the stored rows
through :func:`latest` (the universe and identity jobs, the CIK lookups), so the file is pulled once a night and
nowhere else; ``sec_company_tickers`` is retired.

- **At most 2 SEC requests a night.** Only on a New York weekday; each file is asked once per New York session (rule
  B: a run before the next open still belongs to the session that closed), with no retry (``attempts=1``). Before its
  request a file takes a marker (``<session>_<file>`` under ``config.OUTPUT_DIR/sec_files``, created with
  ``O_EXCL``), so a rerun or a second process the same night asks nothing. No marker, no request.
- **Change history (rule A).** A row is appended only when ``cik``, ``name`` or ``exchange`` (fund file: ``cik``,
  ``series_id`` or ``symbol``) differ from the key's newest row, or that row is 'absent'; ``valid_from`` is the run's
  New York session (``calendar.session_for``; NULL outside the holiday table), ``status`` 'present'. A key gone from a
  healthy file gets one 'absent' row (key and stamps only). An unchanged file appends nothing.
- **Floors.** An exchange file with fewer Nasdaq / NYSE / CBOE rows than ``MIN_MEMBERS`` (the universe's own floor,
  ``core/universe_fetch.MIN_FULL_UNIVERSE``) or a fund file with fewer classes than ``MIN_FUND_CLASSES`` is a failed
  pull: nothing is written, so a short file can never mark the rest 'absent'.
- **Never raises.** Every failure is a ``failed`` status with its error; the stored rows stay. The daily-bars exit
  code does not depend on this step, because RC-Screeners-Daily and RC-Earnings-Inputs wait on
  that exit 0.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from RevenueChaser.core import calendar, config, ledger, rawstore
from RevenueChaser.core import datahealth
from RevenueChaser.core.datahealth import Health
from RevenueChaser.core.identity.sources import SourceResult, fail, now_iso, sec
from RevenueChaser.core.universe_fetch import MIN_FULL_UNIVERSE, SEC_LIST_EXCHANGES

PRESENT, ABSENT = "present", "absent"
# Healthy-file floors. Exchange file: Nasdaq / NYSE / CBOE rows (7,719 of 10,434 on 2026-10-04), the universe's floor.
# Fund file: classes (28,580 on 2026-10-04); 10,000 is inferred, not measured.
MIN_MEMBERS = MIN_FULL_UNIVERSE
MIN_FUND_CLASSES = 10_000
MARKER_KEEP_DAYS = 7


@dataclass(frozen=True)
class SecFile:
    table: str
    key: str
    tracked: tuple[str, ...]
    source: str
    fetch: str            # the ``core.identity.sources.sec`` function, looked up at call time


FILES: dict[str, SecFile] = {
    "exchange": SecFile("sec_exchange_file", "ticker", ("cik", "name", "exchange"), sec.EXCHANGE_SOURCE,
                        "exchange_map"),
    "mf": SecFile("sec_mf_file", "class_id", ("cik", "series_id", "symbol"), sec.FUND_SOURCE, "fund_map"),
}


def _stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _ny_date(run_at: float) -> date:
    return datetime.fromtimestamp(run_at, tz=calendar.NY).date()


def _session(run_at: float) -> date | None:
    try:
        return calendar.session_for(run_at).session_date
    except ValueError:
        return None


# --- the request budget ---------------------------------------------------------------------------------------------

def _reserve(marker_dir: Path, night: date, name: str, log) -> Path | None:
    """Take the marker of file ``name`` for session ``night``; None when it was taken before (asked tonight).

    The marker doubles as the night's receipt (see :func:`health_summary`). Raises ``OSError`` when it cannot be
    written: the caller then makes no request."""
    marker_dir.mkdir(parents=True, exist_ok=True)
    path = marker_dir / f"{night.isoformat()}_{name}"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    os.close(fd)
    _prune(marker_dir, night, log)
    return path


def _prune(marker_dir: Path, night: date, log) -> None:
    """Drop this step's own markers older than ``MARKER_KEEP_DAYS``; bookkeeping only, never a failure."""
    cutoff = night - timedelta(days=MARKER_KEEP_DAYS)
    for f in marker_dir.glob("????-??-??_*"):
        try:
            if date.fromisoformat(f.name[:10]) < cutoff and f.name[11:] in FILES:
                f.unlink()
        except (OSError, ValueError) as exc:
            log(f"SEC files: old marker {f.name} kept ({type(exc).__name__})")


# --- the write ------------------------------------------------------------------------------------------------------

def record(name: str, rows: list[dict], *, run_id: str, run_at: float, fetched_at: float) -> dict:
    """Append the changes of one healthy file in one transaction: ``{"appended", "absent", "unchanged"}``.

    ``appended`` counts the new 'present' rows, ``absent`` the keys gone from the file. Raises on a store error."""
    f = FILES[name]
    valid_from = _session(run_at)
    stamps = {"valid_from": valid_from, "source": f.source, "fetched_at": fetched_at, "run_id": run_id,
              "run_at": run_at}

    def _write(tx):
        tx.ensure_table(f.table)
        cur = tx.execute(f"SELECT {f.key}, status, {', '.join(f.tracked)} FROM {f.table} "
                         f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {f.key} ORDER BY run_at DESC) = 1")
        cols = [d[0] for d in cur.description]
        latest = {r[f.key]: r for r in (dict(zip(cols, v)) for v in cur.fetchall())}
        out, seen, unchanged, absent = [], set(), 0, 0
        for row in rows:
            k = row[f.key]
            if k in seen:
                continue
            seen.add(k)
            prev = latest.get(k)
            if prev and prev["status"] == PRESENT and all(prev[c] == row.get(c) for c in f.tracked):
                unchanged += 1
                continue
            out.append({f.key: k, **{c: row.get(c) for c in f.tracked}, "status": PRESENT, **stamps})
        for k, prev in latest.items():
            if k not in seen and prev["status"] == PRESENT:
                out.append({f.key: k, "status": ABSENT, **stamps})
                absent += 1
        tx.write_rows(f.table, out)
        return {"appended": len(out) - absent, "absent": absent, "unchanged": unchanged}

    return rawstore.atomic(_write, db=rawstore._db_of(f.table))


# --- reading the stored rows ----------------------------------------------------------------------------------------

def latest(name: str, keys: list[str] | None = None) -> list[dict]:
    """The newest row per key of one stored file, keeping the keys whose latest row is 'present'.

    This is the one reader of the two tables: the universe job, the identity job and the CIK lookups all reuse
    what the nightly step stored instead of pulling the file again. A key whose newest row is 'absent' is gone
    from the file and is not returned. Rows carry the file's key and tracked columns, ordered by key.
    """
    f = FILES[name]
    cols = ", ".join((f.key, *f.tracked))
    where = ""
    params: list = []
    if keys:
        where = f" WHERE {f.key} IN ({', '.join('?' for _ in keys)})"
        params = list(keys)
    rows = rawstore.query(
        f"SELECT {cols}, status FROM (SELECT {cols}, status, ROW_NUMBER() OVER "
        f"(PARTITION BY {f.key} ORDER BY run_at DESC) AS rn FROM {f.table}{where}) "
        f"WHERE rn = 1 AND status = '{PRESENT}' ORDER BY {f.key}", params or None)
    return [{c: r.get(c) for c in (f.key, *f.tracked)} for r in rows]


def exchange_source() -> SourceResult:
    """The identity job's ``sec_exchange`` source: the stored rows, never a pull of the file.

    An empty store is an ``error``, like an empty file: the identity master would otherwise treat "nothing
    stored yet" as "no company has a CIK".
    """
    rows = latest("exchange")
    if not rows:
        return fail(sec.EXCHANGE_SOURCE, "no SEC exchange file stored yet (the nightly RC-Daily-Bars step writes it)")
    return SourceResult(sec.EXCHANGE_SOURCE, "ok", rows, None, now_iso())


def _short(name: str, rows: list[dict]) -> tuple[dict, str | None]:
    """``(size fields, error)``: the error names the floor a short file missed."""
    if name == "exchange":
        members = sum(1 for r in rows if str(r.get("exchange") or "").strip().casefold() in SEC_LIST_EXCHANGES)
        size = {"file_rows": len(rows), "members": members}
        if members < MIN_MEMBERS:
            return size, (f"{members} Nasdaq / NYSE / CBOE rows, floor {MIN_MEMBERS}: a short file, nothing written")
        return size, None
    size = {"file_rows": len(rows)}
    if len(rows) < MIN_FUND_CLASSES:
        return size, f"{len(rows)} fund classes, floor {MIN_FUND_CLASSES}: a short file, nothing written"
    return size, None


def _header(headers: dict, name: str) -> str | None:
    """One header by name, whatever the case: httpx lowercases what the server sent."""
    if not headers:
        return None
    want = name.lower()
    return next((v for k, v in headers.items() if k.lower() == want), None)


def _one(name: str, night: date, *, run_id: str, run_at: float, marker_dir: Path, log) -> tuple[dict, int]:
    """``(result, requests)`` for one file. Never raises."""
    try:
        marker = _reserve(marker_dir, night, name, log)
        if marker is None:
            return {"status": "skipped", "reason": f"already asked for session {night.isoformat()}"}, 0
    except OSError as exc:
        return {"status": "failed", "error": f"marker not writable, no request made ({type(exc).__name__})"}, 0
    def failed(error: str, **extra) -> tuple[dict, int]:
        """A failed pull leaves a receipt too, so Data Health can tell it from a file that was never written
        (avo 2026-10-22: the row must separate "pulled, unchanged", "never written" and "pull failed")."""
        res = {"status": "failed", **extra, "error": error}
        _write_receipt(marker, night, res)
        return res, 1

    try:
        headers: dict = {}
        got = getattr(sec, FILES[name].fetch)(attempts=1, headers_out=headers)
        fetched_at = time.time()
        if not got.ok:
            return failed(got.error or "SEC file failed")
        size, short = _short(name, got.rows)
        if short:
            return failed(short, **size)
        out = record(name, got.rows, run_id=run_id, run_at=run_at, fetched_at=fetched_at)
        _write_receipt(marker, night, {"status": "ok", **size, **out,
                                       "last_modified": _header(headers, "Last-Modified")})
        return {"status": "ok", **size, **out}, 1
    except Exception as exc:  # noqa: BLE001 - the step never raises
        return failed(f"{type(exc).__name__}: {exc}"[:300])


# --- what the last good pull said (Data Health) ---------------------------------------------------------------------

def _write_receipt(marker: Path, night: date, data: dict) -> None:
    """Write the night's receipt into its marker: the file's own size and Last-Modified, and what was appended.

    Bookkeeping only: a receipt that cannot be written never fails the pull (the rows are already stored)."""
    try:
        tmp = marker.with_suffix(".tmp")
        tmp.write_text(json.dumps({"session": night.isoformat(), **data}), encoding="utf-8")
        tmp.replace(marker)
    except OSError:
        pass


def _receipts(marker_dir: Path, name: str) -> list[dict]:
    """Every readable receipt of file ``name``, newest session first."""
    out = []
    for f in sorted(marker_dir.glob(f"????-??-??_{name}"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("session"):
            out.append(data)
    return out


def _receipt(marker_dir: Path, name: str) -> dict:
    """The newest readable receipt of file ``name``; ``{}`` when none is left (pruned, or the cache was cleared)."""
    rs = _receipts(marker_dir, name)
    return rs[0] if rs else {}


def _last_good(marker_dir: Path, name: str) -> dict:
    """The newest receipt of a pull that actually read the file.

    A failed night must not erase what the last good pull knew: ``file_rows`` and ``Last-Modified`` describe the
    file itself, so they stay from the last successful read while the outcome below reports the failure.
    """
    return next((r for r in _receipts(marker_dir, name) if r.get("status") != "failed"), {})


@datahealth.scoped
def health_summary(marker_dir: Path | None = None) -> dict:
    """Read-only Data Health summary of both files, zero network (#301 D21 + D8).

    Per file: ``present`` (keys whose newest stored row is 'present') against ``file_rows`` (what the last good
    pull held), ``members`` against ``floor``, and the last pull's ``appended`` / ``absent`` / ``last_modified``
    (the HTTP ``Last-Modified`` of the file). The store always answers; the pull's own figures come from its
    receipt and are ``None`` when no receipt is left.
    """
    marker_dir = Path(marker_dir or (config.OUTPUT_DIR / "sec_files"))
    out: dict = {}
    for name, f in FILES.items():
        rows = latest(name)
        members = (len(rows) if name == "mf" else
                   sum(1 for r in rows if str(r.get("exchange") or "").strip().casefold() in SEC_LIST_EXCHANGES))
        receipt = _receipt(marker_dir, name)
        good = _last_good(marker_dir, name)
        out[name] = {"present": len(rows), "members": members,
                     "floor": MIN_FUND_CLASSES if name == "mf" else MIN_MEMBERS,
                     # the file's own figures survive a failed night; the outcome below reports the failure
                     "file_rows": good.get("file_rows"), "last_modified": good.get("last_modified"),
                     "appended": good.get("appended"), "absent": good.get("absent"),
                     "good_session": good.get("session"), "session": receipt.get("session"),
                     # the last pull's own outcome: "ok", "failed", or "unknown" when no receipt is left
                     "last_pull": receipt.get("status") or ("unknown" if not receipt else "ok"),
                     "error": receipt.get("error")}
    return out


def pull_outcome(entry: dict) -> str:
    """One file's last pull, in the Data Health note's own words (avo 2026-10-22).

    The tables append on change only, so "appended 0" cannot say whether the file was pulled and unchanged
    or never pulled: this is the sentence that separates ``pulled <session>, unchanged``, ``no pull recorded
    yet`` and ``pull FAILED <night> (<error>)``. It reads one :func:`health_summary` entry.
    """
    if entry.get("last_pull") == "failed":
        night = entry.get("session") or "—"
        good = entry.get("good_session")
        tail = f", last good pull {good}" if good else ""
        return f"pull FAILED {night} ({entry.get('error') or 'no reason recorded'}){tail}"
    if entry.get("last_pull") == "unknown":
        return "no pull recorded yet"
    session = entry.get("session") or "—"
    appended = entry.get("appended")
    if appended is None:
        return f"pulled {session}"
    if int(appended) == 0 and not int(entry.get("absent") or 0):
        return f"pulled {session}, unchanged"
    return f"pulled {session}, appended {int(appended):,} (+{int(entry.get('absent') or 0):,} absent)"


def health(marker_dir: Path | None = None) -> Health:
    """The numbers :func:`health_summary` reports, typed (#124 S4).

    Both files are summed: ``covered`` is what is stored as 'present', ``expected`` what the last good pull read
    (``None`` until a pull has recorded it, so coverage is ``n/a`` rather than a percentage of nothing). ``newest``
    is the newest last-good pull session and ``expected_as_of`` the newest attempt, both across the two files, so a
    night where both failed dates the store by the last good pull. ``problems`` carries each file's pull outcome,
    verbatim: it is the one fact the counts cannot show, per file.
    """
    n = health_summary(marker_dir)
    files = list(n.values())
    good = [f["good_session"] for f in files if f.get("good_session")]
    attempts = [f["session"] for f in files if f.get("session")]
    return Health(
        covered=sum(int(f["present"]) for f in files),
        expected=(None if any(f["file_rows"] is None for f in files)
                  else sum(int(f["file_rows"]) for f in files)),
        newest=max(good) if good else None,
        expected_as_of=max(attempts) if attempts else None,
        problems=tuple(f"{name}: {pull_outcome(f)}" for name, f in n.items()),
    )


def nightly(*, run_id: str | None = None, run_at: float | None = None, marker_dir: Path | None = None,
            log: Callable[[str], None] | None = None) -> dict:
    """Ask both SEC files once tonight and append their changes; see the module docstring. Never raises.

    ``{"status": ok|skipped|failed, "reason", "ny_date", "session_date", "requests", "exchange": {...}, "mf": {...}}``
    (``session_date``: the session the markers count against; the New York date outside the holiday table)."""
    log = log or _stderr
    res: dict = {"status": "failed", "reason": None, "ny_date": None, "session_date": None, "requests": 0,
                 **{n: {"status": "skipped", "reason": "not reached"} for n in FILES}}
    try:
        run_at = time.time() if run_at is None else float(run_at)
        run_id = run_id or ledger.new_run_id()
        d = _ny_date(run_at)
        res["ny_date"] = d.isoformat()
        if d.weekday() >= 5:
            res.update(status="skipped", reason="weekend", **{n: {"status": "skipped", "reason": "weekend"}
                                                             for n in FILES})
            return res
        night = _session(run_at) or d
        res["session_date"] = night.isoformat()
        marker_dir = Path(marker_dir or (config.OUTPUT_DIR / "sec_files"))
        for n in FILES:
            res[n], asked = _one(n, night, run_id=run_id, run_at=run_at, marker_dir=marker_dir, log=log)
            res["requests"] += asked
        states = {res[n]["status"] for n in FILES}
        res["status"] = "failed" if "failed" in states else "skipped" if states == {"skipped"} else "ok"
    except Exception as exc:  # noqa: BLE001 - the step never raises
        res.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:300])
    return res


def summary_line(res: dict) -> str:
    head = f"SEC files: {res.get('ny_date') or '-'}"
    if res.get("reason"):
        return f"{head}; skipped ({res['reason']})"
    if res.get("error"):
        return f"{head}; failed ({res['error']})"
    parts = []
    for n in FILES:
        r = res.get(n) or {}
        text = f"{n} {r.get('status')}"
        if r.get("status") == "ok":
            text += f" {r['appended']} appended ({r['absent']} absent, {r['unchanged']} unchanged)"
        elif r.get("error") or r.get("reason"):
            text += f" ({r.get('error') or r.get('reason')})"
        parts.append(text)
    return f"{head}; {'; '.join(parts)}; {res.get('requests', 0)} SEC requests"


__all__ = ["FILES", "MIN_MEMBERS", "MIN_FUND_CLASSES", "exchange_source", "health", "health_summary", "latest",
           "nightly", "pull_outcome", "record", "summary_line"]
