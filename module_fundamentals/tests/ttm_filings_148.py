"""Synthetic filings through the real extractor (core.providers.sec_edgar.extract_*) for the Fundamentals TTM
consumer (#148, spec plans/2026-10-08_ttm_consumer_spec.md). No store, no network.

Builders copied from the spec's evidence ``synthetic_cases.py`` (cases C1-C8)."""
from __future__ import annotations

from datetime import date, timedelta

REV, NI = "Revenues", "NetIncomeLoss"
OCF, CAPEX = "NetCashProvidedByUsedInOperatingActivities", "PaymentsToAcquirePropertyPlantAndEquipment"


def fact(start, end, val, filed, accn, form, fp):
    return {"start": start, "end": end, "val": val, "filed": filed, "accn": accn, "form": form, "fp": fp}


def _iso(d):
    return d.isoformat()


def fiscal_year_facts(start: date, q_days: list[int], vals: list[float], *, style: str, quarters: int = 4,
                      tag: str = "", skip_months: tuple = (), override: dict | None = None):
    """Facts of one fiscal year starting on ``start`` with quarters of ``q_days`` days and values ``vals``.

    style "is" (income statement): 3M facts for Q1-Q3 plus 6M / 9M YTD, the FY in the 10-K.
    style "cf" (cash flow): Q1 3M, 6M and 9M YTD only, the FY in the 10-K.
    ``quarters`` < 4 stops after that many 10-Qs (no 10-K yet). ``skip_months`` drops YTD facts (a missing link);
    ``override`` {months: value} replaces a YTD value (a restatement or a sign error)."""
    override = override or {}
    ends, s = [], start
    for d in q_days:
        e = s + timedelta(days=d - 1)
        ends.append(e)
        s = e + timedelta(days=1)
    out = []
    for k in range(min(quarters, 3)):
        e = ends[k]
        filed, accn, fp = _iso(e + timedelta(days=35)), f"{tag}{start.year}-q{k + 1}", f"Q{k + 1}"
        q_start = start if k == 0 else ends[k - 1] + timedelta(days=1)
        months = 3 * (k + 1)
        if style == "is" or k == 0:
            if not (k == 0 and 3 in skip_months):
                out.append(fact(_iso(q_start), _iso(e), vals[k], filed, accn, "10-Q", fp))
        if k > 0 and months not in skip_months:
            out.append(fact(_iso(start), _iso(e), override.get(months, sum(vals[:k + 1])), filed, accn, "10-Q", fp))
    if quarters >= 4:
        e = ends[3]
        out.append(fact(_iso(start), _iso(e), override.get(12, sum(vals)), _iso(e + timedelta(days=60)),
                        f"{tag}{start.year}-k", "10-K", "FY"))
    return out, ends[-1] + timedelta(days=1)


CAL = [90, 91, 92, 92]          # a calendar year (2025: 90 + 91 + 92 + 92 = 365)


def company(years: list[dict]):
    """``years``: one dict per fiscal year, {start, q_days, rev, ni, ocf, capex, quarters, skip, override}."""
    g = {REV: [], NI: [], OCF: [], CAPEX: []}
    for y in years:
        for concept, key, style in ((REV, "rev", "is"), (NI, "ni", "is"), (OCF, "ocf", "cf"), (CAPEX, "capex", "cf")):
            rows, _ = fiscal_year_facts(y["start"], y.get("q_days", CAL), y[key], style=style,
                                        quarters=y.get("quarters", 4), skip_months=y.get("skip", {}).get(key, ()),
                                        override=y.get("override", {}).get(key))
            g[concept] += rows + y.get("extra", {}).get(key, [])
    return {"entityName": "Synthetic", "facts": {"us-gaap": {c: {"units": {"USD": r}} for c, r in g.items()}}}


def dec(y):
    return date(y, 1, 1)


def base_years(first=2023, last_full=2025, cur_quarters=2, **cur):
    """A December filer: full years ``first``..``last_full`` growing 10% a year, then ``cur_quarters`` 10-Qs of the next."""
    ys = []
    for n, y in enumerate(range(first, last_full + 2)):
        k = 1.1 ** n
        row = {"start": dec(y), "rev": [100 * k] * 4, "ni": [10 * k] * 4, "ocf": [30 * k] * 4, "capex": [5 * k] * 4}
        if y == last_full + 1:
            row.update(quarters=cur_quarters, **cur)
        ys.append(row)
    return ys


CASES = {}
CASES["C1 Dec filer, Q2/2026 10-Q (cash flow YTD only)"] = company(base_years())
CASES["C2 Dec filer, latest filing = the FY2025 10-K"] = company(base_years(cur_quarters=0))
# C3: discontinued operations. The FY2025 10-K restates revenue to continuing ops (FY 360), the 9M of the 2025 10-Q
# stays total (400); the 2026 Q3 10-Q has no restated comparative yet. Q4/2025 revenue = 360 - 400 < 0 (a suspect).
c3 = base_years(last_full=2025, cur_quarters=3)
c3[2]["override"] = {"rev": {12: 360.0}}
c3[2]["rev"] = [133.1, 133.1, 133.8, 133.1]   # 9M = 400.0
CASES["C3a disc-ops: FY restated, prior-year 9M not (Q4 suspect)"] = company(c3)
# C3b: same, but the 2026 Q3 10-Q carries the restated 9M/2025 comparative (270), filed after the original
c3b = [dict(y) for y in c3]
c3b[3] = dict(c3b[3], extra={"rev": [fact("2025-01-01", "2025-09-30", 270.0, "2026-11-05", "2026-q3", "10-Q", "Q3")]})
CASES["C3b disc-ops: the current 10-Q restates the 9M comparative"] = company(c3b)
# C4: Costco-like 52/53-week year, fiscal year ends on a Sunday around 31 August, quarters of 12/12/12/16 weeks
cost = []
s = date(2022, 8, 29)
for n in range(4):
    q_days = [84, 84, 84, 112] if n != 2 else [84, 84, 84, 119]   # one 53-week year
    k = 1.1 ** n
    cost.append({"start": s, "q_days": q_days, "rev": [100 * k, 100 * k, 100 * k, 130 * k], "ni": [10 * k] * 4,
                 "ocf": [30 * k] * 4, "capex": [5 * k] * 4, **({"quarters": 2} if n == 3 else {})})
    s = s + timedelta(days=sum(q_days))
CASES["C4 Costco-like 52/53-week years, Q2 10-Q of the 4th year"] = company(cost)
# C5: June fiscal year, Q1 10-Q of FY2027 (ends 2026-09-30)
jun = [{"start": date(y, 7, 1), "q_days": [92, 92, 90, 91], "rev": [100 * 1.2 ** n] * 4, "ni": [10] * 4,
        "ocf": [30 * 1.2 ** n] * 4, "capex": [5] * 4, **({"quarters": 1} if y == 2026 else {})}
       for n, y in enumerate(range(2023, 2027))]
CASES["C5 June fiscal year, Q1 10-Q"] = company(jun)
# C6: missing link: the 2026 Q3 10-Q is filed but its 6M OCF is missing in the Q2 10-Q (only 3M and 9M exist)
c6 = base_years(cur_quarters=3, skip={"ocf": (6,)})
CASES["C6 missing link: no 6M OCF in 2026, latest = 9M"] = company(c6)
# C7: a sign error: the 2026 6M capex is filed as -11 (Q2 capex derived = -11 - 5.5 < 0 -> suspect, YTD not stored)
c7 = base_years(cur_quarters=2, override={"capex": {6: -11.0}})
CASES["C7 sign error in the current 6M capex"] = company(c7)
# C8: a 2024 IPO: FY2024 and FY2025 10-Ks, 10-Qs from 2025 only, Q2/2026 10-Q (no 6M of 2024 for the prior TTM)
c8 = base_years(first=2024)
c8[0]["quarters"] = 4
ipo = company(c8)
for c in ipo["facts"]["us-gaap"].values():   # drop the 2024 10-Q facts, keep the FY2024 10-K
    c["units"]["USD"] = [f for f in c["units"]["USD"] if not (f["end"] < "2025-01-01" and f["form"] == "10-Q")]
CASES["C8 2024 IPO: no 2024 10-Qs (prior-year TTM needs 6M/2024)"] = ipo
