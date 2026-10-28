"""The UW calls of the options datasets: one GET, a paged GET, the failure policy, and the observed window.

Every call goes through the run's one ``UWClient`` (``RunContext.uw_client``: its retry ladder, 429 handling and
breaker, and the observer that counts each answer against the ``uw`` day cap), after ``check_cap``.

What a failed call does, by UW's answer after the client's retries:

- 401 or 403: the key is refused, so the run fails (``SourceError``); nothing more is sent.
- the breaker opened (five provider failures in a row): the run stops (``SourceError``).
- 429, 5xx or a transport error: the name fails this run (``NameFailed``) and is asked again by the next one; no
  raw file is written for it, so no partial name is kept.
- any other 4xx (an unknown ticker, a parameter UW refuses for this name): that part of the name is stored empty
  and named in the raw file's ``errors``; the name is kept and the build issue says which part.

The chain, the flow per expiry, the prints and the option volume take no ``date`` (UW answers the current or last
session), so the options datasets fetch only in the **observed window**: for the latest complete session, between
its close plus 30 minutes and the next open (``core/calendar.py``, the SPX slice's ``observed`` rule). A run outside
it sends no call.
"""
from __future__ import annotations

from datetime import date

from ....core import calendar
from ....core.providers.unusual_whales import _seg
from .. import base

PAGE_LIMIT = 500        # UW's documented maximum for the paged endpoints (docs/openapi.yaml, "Default 500 Max 500")
# Pages before a part fails instead of asking on: 20 pages are 10,000 rows, past any one expiry's contracts or any
# ticker's strikes, and only stop an answer that never ends.
MAX_PAGES = 20


class NameFailed(base.SourceError):
    """One name could not be fetched whole this run (a 429, 5xx or transport failure, or a paged answer that moved);
    the next run asks again."""


def stock(ticker: str, path: str) -> str:
    return f"/stock/{_seg(ticker)}/{path}"


def observed(as_of: date) -> bool:
    """True when ``as_of`` is the latest complete session and the next session has not opened."""
    return as_of == calendar.latest_complete_session() and not calendar.in_us_session_window()


def get(ctx: base.RunContext, ep: str, params: dict | None, part: str, errors: dict) -> object | None:
    """One UW GET; the body, or None for a part stored empty (its error in ``errors[part]``). See the module doc."""
    ctx.check_cap("uw")
    res = ctx.uw_client().get_result(ep, params)
    if res.status == "ok":
        return res.data
    err = res.error or {}
    status, what = err.get("status"), err.get("error") or "failed"
    if what == "breaker_open":
        raise base.SourceError(f"UW {ep}: breaker_open: the client stopped after repeated provider failures")
    if status in (401, 403):
        raise base.SourceError(f"UW {ep}: HTTP {status}: the key was refused")
    if isinstance(status, int) and 400 <= status < 500 and status != 429:
        errors[part] = f"HTTP {status}"
        return None
    raise NameFailed(f"UW {ep}: {what}")


def rows_of(body) -> list:
    """The ``data`` list of a body (or a bare list); anything else is no rows."""
    items = body.get("data") if isinstance(body, dict) else body
    return items if isinstance(items, list) else []


def paged(ctx: base.RunContext, ep: str, params: dict, part: str, errors: dict, key) -> dict | None:
    """Every page of a paged UW answer: ``{"data": rows, "pages": [rows per page]}``, or None for a part stored
    empty. Page 0 is sent without ``page``, then ``page=1, 2, ...`` (0-based) until a page is shorter than the
    limit. A key (``key(row)``) on two pages means the answer moved while paging, and a row may have been skipped
    too, so the name fails this run (``NameFailed``); so does the ``MAX_PAGES`` ceiling."""
    params = dict(params, limit=PAGE_LIMIT)
    body = get(ctx, ep, params, part, errors)
    if body is None:
        return None
    pages = [rows_of(body)]
    while len(pages[-1]) >= PAGE_LIMIT:
        if len(pages) >= MAX_PAGES:
            raise NameFailed(f"UW {ep}: {MAX_PAGES} full pages of {PAGE_LIMIT} rows reach the page ceiling")
        more = get(ctx, ep, dict(params, page=len(pages)), part, errors)
        if more is None:
            raise NameFailed(f"UW {ep} page {len(pages)}: {errors.pop(part)}")
        # Only a ``data`` list (or a bare list) is a page: a 200 carrying ``{}`` read as an empty page would end the
        # part early and keep its first pages as the whole of it.
        items = more.get("data") if isinstance(more, dict) else more
        if not isinstance(items, list):
            raise NameFailed(f"UW {ep} page {len(pages)}: no list of rows in the answer")
        pages.append(items)
    seen: set = set()
    for page in pages:
        keys = set()
        for row in page:
            try:
                keys.add(key(row))
            except (KeyError, TypeError, ValueError):
                continue                    # typing reports a row without a usable key
        if keys & seen:
            raise NameFailed(f"UW {ep}: {len(keys & seen)} rows repeated across pages; the answer moved while paging")
        seen |= keys
    return {"data": [r for page in pages for r in page], "pages": [len(p) for p in pages]}
