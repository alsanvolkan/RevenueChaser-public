"""The spine's test sources: the build plan's 10 example names in each source's documented shape.

**Not recorded from the real sources** (no WRDS, OpenFIGI or fund-file call is made from here, and the one sec.gov
request this thread tried was refused for its User-Agent). The shapes follow each source's documentation; the
values are chosen for the tests. The SEC CIKs and the CUSIPs are the issuers' real ones as far as known here; the
Compustat gvkeys and iids are illustrative.

The names and what each one tests:

| Name | Case |
|---|---|
| MSFT | the plain case: Nasdaq, linked by ticker, CUSIP, FIGI, GICS, in the S&P 500 and the Nasdaq-100 |
| HEI, HEI-A | two share classes of one CIK and one gvkey, told apart by Compustat's ``tic`` (``HEI.A``) |
| ETN | a CINS CUSIP (Ireland): no US ISIN is derived |
| TM | an ADR (OpenFIGI ``ADR``), NYSE |
| CNA | a CIK with two Compustat companies: only the ticker match decides |
| FDX | a fund file writes it with a CUSIP only (matched by CUSIP) |
| NVR | OpenFIGI has no FIGI for its CUSIP (``tier_a_no_figi``) |
| WFC-PC | a preferred (the ``-P`` ticker rule): Tier B, no Compustat link |
| VYLR | a new listing Compustat's snapshot does not hold yet (the new-name query); an old, delisted company that
left the S&P 500 in 2009 still carries Compustat tic VYLR (a reused ticker: its history must not become VYLR's) |

Plus WFC (common, so WFC-PC's CIK has two tickers), an OTC name (Tier C), a row with no exchange (Tier D) and an ETF
(SPY: OpenFIGI ``ETP``, Tier B).
"""
from __future__ import annotations

import io
import json
import re

SEC_FILE = {
    "fields": ["cik", "name", "ticker", "exchange"],
    "data": [
        [789019, "MICROSOFT CORP", "MSFT", "Nasdaq"],
        [46619, "HEICO CORP", "HEI", "NYSE"],
        [46619, "HEICO CORP", "HEI.A", "NYSE"],
        [1551182, "Eaton Corp plc", "ETN", "NYSE"],
        [1094517, "TOYOTA MOTOR CORP/", "TM", "NYSE"],
        [21175, "CNA FINANCIAL CORP", "CNA", "NYSE"],
        [1048911, "FEDEX CORP", "FDX", "NYSE"],
        [906163, "NVR INC", "NVR", "NYSE"],
        [72971, "WELLS FARGO & COMPANY/MN", "WFC", "NYSE"],
        [72971, "WELLS FARGO & COMPANY/MN", "WFC-PC", "NYSE"],
        [2000001, "VYLR HOLDINGS INC", "VYLR", "Nasdaq"],
        [884394, "SPDR S&P 500 ETF TRUST", "SPY", "NYSE"],
        [3000001, "SOME OTC CO", "OTCX", "OTC"],
        [3000002, "NO EXCHANGE CO", "NOEX", None],
    ],
}
NAMES = ("MSFT", "HEI", "HEI-A", "ETN", "TM", "CNA", "FDX", "NVR", "WFC-PC", "VYLR")

# ── Compustat ────────────────────────────────────────────────────────────────────────────────────────────────

COMPANY = [
    {"gvkey": "012141", "conm": "MICROSOFT CORP", "cik": "0000789019"},
    {"gvkey": "005567", "conm": "HEICO CORP", "cik": "0000046619"},
    {"gvkey": "016478", "conm": "EATON CORP PLC", "cik": "0001551182"},
    {"gvkey": "010581", "conm": "TOYOTA MOTOR CORP", "cik": "0001094517"},
    {"gvkey": "002968", "conm": "CNA FINANCIAL CORP", "cik": "0000021175"},
    {"gvkey": "099999", "conm": "CNA SURETY CORP (OLD)", "cik": "0000021175"},
    {"gvkey": "004598", "conm": "FEDEX CORP", "cik": "0001048911"},
    {"gvkey": "025648", "conm": "NVR INC", "cik": "0000906163"},
    {"gvkey": "008007", "conm": "WELLS FARGO & CO", "cik": "0000072971"},
    {"gvkey": "001000", "conm": "SPDR S&P 500 ETF TRUST", "cik": "0000884394"},
    # A company that left the S&P 500 in 2009 and was delisted, still carrying tic VYLR: today VYLR is another CIK.
    {"gvkey": "123456", "conm": "OLD VAYLOR CORP", "cik": "0000999999"},
]
NEW_COMPANY = [{"gvkey": "040001", "conm": "VYLR HOLDINGS INC", "cik": "0002000001"}]
SECURITY = [
    {"gvkey": "012141", "iid": "01", "tic": "MSFT", "cusip": "594918104", "exchg": 14, "excntry": "USA",
     "secstat": "A", "tpci": "0", "dldtei": None},
    {"gvkey": "005567", "iid": "01", "tic": "HEI", "cusip": "422806109", "exchg": 11, "excntry": "USA",
     "secstat": "A", "tpci": "0", "dldtei": None},
    {"gvkey": "005567", "iid": "02", "tic": "HEI.A", "cusip": "422806208", "exchg": 11, "excntry": "USA",
     "secstat": "A", "tpci": "0", "dldtei": None},
    {"gvkey": "016478", "iid": "01", "tic": "ETN", "cusip": "G29183103", "exchg": 11, "excntry": "USA",
     "secstat": "A", "tpci": "0", "dldtei": None},
    {"gvkey": "010581", "iid": "90", "tic": "TM", "cusip": "892331307", "exchg": 11, "excntry": "USA",
     "secstat": "A", "tpci": "F", "dldtei": None},
    {"gvkey": "002968", "iid": "01", "tic": "CNA", "cusip": "126117100", "exchg": 11, "excntry": "USA",
     "secstat": "A", "tpci": "0", "dldtei": None},
    {"gvkey": "099999", "iid": "01", "tic": "SUR", "cusip": "12612L108", "exchg": 11, "excntry": "USA",
     "secstat": "I", "tpci": "0", "dldtei": "2011-06-01"},
    {"gvkey": "004598", "iid": "01", "tic": "FDX", "cusip": "31428X106", "exchg": 11, "excntry": "USA",
     "secstat": "A", "tpci": "0", "dldtei": None},
    {"gvkey": "025648", "iid": "01", "tic": "NVR", "cusip": "62944T105", "exchg": 11, "excntry": "USA",
     "secstat": "A", "tpci": "0", "dldtei": None},
    {"gvkey": "008007", "iid": "01", "tic": "WFC", "cusip": "949746101", "exchg": 11, "excntry": "USA",
     "secstat": "A", "tpci": "0", "dldtei": None},
    {"gvkey": "001000", "iid": "01", "tic": "SPY", "cusip": "78462F103", "exchg": 11, "excntry": "USA",
     "secstat": "A", "tpci": "%", "dldtei": None},
    {"gvkey": "123456", "iid": "01", "tic": "VYLR", "cusip": "92921Q100", "exchg": 11, "excntry": "USA",
     "secstat": "I", "tpci": "0", "dldtei": "2009-07-15"},
]
NEW_SECURITY = [{"gvkey": "040001", "iid": "01", "tic": "VYLR", "cusip": "92921V109", "exchg": 14, "excntry": "USA",
                 "secstat": "A", "tpci": "0", "dldtei": None}]
IDHIST = [
    {"gvkey": "012141", "iid": "01", "item": "TIC", "itemvalue": "MSFT", "efffrom": "1986-03-13", "effthru": None},
    # MSFT's CUSIP before a (made-up) change: a fund file still writing it is MSFT, not a conflict.
    {"gvkey": "012141", "iid": "01", "item": "CUSIP", "itemvalue": "594918100", "efffrom": "1986-03-13",
     "effthru": "2001-12-31"},
    {"gvkey": "005567", "iid": "02", "item": "TIC", "itemvalue": "HEI.A", "efffrom": "1998-04-01", "effthru": None},
    {"gvkey": "005567", "iid": "02", "item": "CUSIP", "itemvalue": "422806208", "efffrom": "1998-04-01",
     "effthru": None},
    {"gvkey": "002968", "iid": "01", "item": "EXCHG", "itemvalue": "11", "efffrom": "1970-01-01", "effthru": None},
    {"gvkey": "777777", "iid": "01", "item": "TIC", "itemvalue": "NOTOURS", "efffrom": "2000-01-01", "effthru": None},
]
NEW_IDHIST = [{"gvkey": "040001", "iid": "01", "item": "TIC", "itemvalue": "VYLR", "efffrom": "2026-10-01",
               "effthru": None}]
HGIC = [
    {"gvkey": "012141", "indfrom": "1999-01-01", "indthru": None, "gsector": "45", "ggroup": "4510", "gind": "451030",
     "gsubind": "45103020"},
    {"gvkey": "005567", "indfrom": "1999-01-01", "indthru": "2015-12-31", "gsector": "20", "ggroup": "2010",
     "gind": "201010", "gsubind": "20101010"},
    {"gvkey": "005567", "indfrom": "2016-01-01", "indthru": None, "gsector": "20", "ggroup": "2010", "gind": "201010",
     "gsubind": "20101010"},
    {"gvkey": "016478", "indfrom": "2012-11-30", "indthru": None, "gsector": "20", "ggroup": "2010", "gind": "201040",
     "gsubind": "20104010"},
    {"gvkey": "010581", "indfrom": "1999-01-01", "indthru": None, "gsector": "25", "ggroup": "2510", "gind": "251020",
     "gsubind": "25102010"},
    {"gvkey": "002968", "indfrom": "1999-01-01", "indthru": None, "gsector": "40", "ggroup": "4030", "gind": "403010",
     "gsubind": "40301040"},
    {"gvkey": "004598", "indfrom": "1999-01-01", "indthru": None, "gsector": "20", "ggroup": "2030", "gind": "203020",
     "gsubind": "20302010"},
    {"gvkey": "025648", "indfrom": "1999-01-01", "indthru": None, "gsector": "25", "ggroup": "2520", "gind": "252010",
     "gsubind": "25201030"},
    {"gvkey": "008007", "indfrom": "1999-01-01", "indthru": None, "gsector": "40", "ggroup": "4010", "gind": "401010",
     "gsubind": "40101010"},
]
IDX = [
    {"gvkey": "012141", "iid": "01", "gvkeyx": "000003", "valid_from": "1994-06-01", "thru": None},
    {"gvkey": "016478", "iid": "01", "gvkeyx": "000003", "valid_from": "1957-03-04", "thru": None},
    {"gvkey": "004598", "iid": "01", "gvkeyx": "000003", "valid_from": "1980-12-31", "thru": None},
    {"gvkey": "025648", "iid": "01", "gvkeyx": "000003", "valid_from": "2023-09-18", "thru": None},
    {"gvkey": "008007", "iid": "01", "gvkeyx": "000003", "valid_from": "1976-06-30", "thru": None},
    {"gvkey": "005567", "iid": "01", "gvkeyx": "024248", "valid_from": "2010-01-04", "thru": "2026-10-02"},
    {"gvkey": "005567", "iid": "02", "gvkeyx": "024248", "valid_from": "2010-01-04", "thru": None},
    {"gvkey": "123456", "iid": "01", "gvkeyx": "000003", "valid_from": "2001-03-01", "thru": "2009-06-30"},
]


class FakeWrds:
    """A WRDS connection that answers the five Compustat tables from the rows above and records every query."""

    def __init__(self, with_new=True):
        self.queries: list[str] = []
        self.closed = False
        self.with_new = with_new

    def raw_sql(self, sql: str):
        self.queries.append(sql)
        table = re.search(r"FROM comp\.(\w+)", sql).group(1)
        rows = {"company": COMPANY + (NEW_COMPANY if self.with_new else []),
                "security": SECURITY + (NEW_SECURITY if self.with_new else []),
                "sec_idhist": IDHIST + (NEW_IDHIST if self.with_new else []), "co_hgic": HGIC,
                "idxcst_his": IDX}[table]
        ciks = re.search(r"cik IN \(([^)]*)\)", sql)
        if ciks:
            want = set(re.findall(r"'(\d+)'", ciks.group(1)))
            rows = [r for r in rows if r["cik"] in want]
        gvkeys = re.search(r"gvkey IN \(([^)]*)\)", sql)
        if gvkeys:
            want = set(re.findall(r"'(\d+)'", gvkeys.group(1)))
            rows = [r for r in rows if r["gvkey"] in want]
        return [dict(r) for r in rows]

    def query(self, sql: str, out) -> int:
        """The worker's side (``wrds.Worker.query``): the answer as gzip JSON lines of text values, as
        ``wrds_worker.py`` writes them."""
        rows = self.raw_sql(sql)
        write_jsonl(out, rows)
        return len(rows)

    def close(self):
        self.closed = True


def write_jsonl(path, rows) -> None:
    import gzip
    import pathlib
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps({k: (None if v is None else str(v)) for k, v in r.items()}) + "\n")


def without_new(rows: list[dict], new: list[dict]) -> list[dict]:
    return [r for r in rows if r not in new]


# ── OpenFIGI ─────────────────────────────────────────────────────────────────────────────────────────────────

FIGI = {
    "594918104": {"figi": "BBG000BPH459", "ticker": "MSFT", "securityType": "Common Stock", "exchCode": "US",
                  "compositeFIGI": "BBG000BPH459", "shareClassFIGI": "BBG001S5TD05", "securityType2": "Common Stock",
                  "marketSector": "Equity", "name": "MICROSOFT CORP"},
    "422806109": {"figi": "BBG000BL3X00", "ticker": "HEI", "securityType": "Common Stock", "exchCode": "US"},
    "422806208": {"figi": "BBG000BL3Y00", "ticker": "HEI/A", "securityType": "Common Stock", "exchCode": "US"},
    "G29183103": {"figi": "BBG000BJ3PD2", "ticker": "ETN", "securityType": "Common Stock", "exchCode": "US"},
    "892331307": {"figi": "BBG000BGTHL6", "ticker": "TM", "securityType": "ADR", "exchCode": "US",
                  "securityType2": "Depositary Receipt"},
    "126117100": {"figi": "BBG000BFN3P1", "ticker": "CNA", "securityType": "Common Stock", "exchCode": "US"},
    "31428X106": {"figi": "BBG000BJF1Z8", "ticker": "FDX", "securityType": "Common Stock", "exchCode": "US"},
    "949746101": {"figi": "BBG000BWQFY7", "ticker": "WFC", "securityType": "Common Stock", "exchCode": "US"},
    "78462F103": {"figi": "BBG000BDTBL9", "ticker": "SPY", "securityType": "ETP", "exchCode": "US"},
    "92921V109": {"figi": "BBG0VYLR0000", "ticker": "VYLR", "securityType": "Common Stock", "exchCode": "US"},
}


def figi_answer(request):
    """The mapping endpoint: one entry per job, ``data`` for a known code asked with its own id type, the documented
    warning otherwise. A CINS (a letter first) is found only as ``ID_CINS``: asked as ``ID_CUSIP``, OpenFIGI found
    none of the 2,092 CINS the first live map held (homesrv, 2026-10-28); here a CUSIP is found only as ``ID_CUSIP``."""
    jobs = json.loads(request.content)

    def found(job) -> bool:
        own = "ID_CINS" if job["idValue"][:1].isalpha() else "ID_CUSIP"
        return job["idValue"] in FIGI and job.get("idType") == own
    out = [{"data": [FIGI[j["idValue"]]]} if found(j) else {"warning": "No identifier found."} for j in jobs]
    return 200, json.dumps(out).encode(), {"content-type": "application/json"}


# ── Fund files ───────────────────────────────────────────────────────────────────────────────────────────────

# (ticker as the file writes it, CUSIP or None, name)
SP500 = [("MSFT", "594918104", "MICROSOFT CORP"), ("ETN", "G29183103", "EATON CORP PLC"),
         ("FDX", "31428X106", "FEDEX CORP"), ("NVR", "62944T105", "NVR INC"), ("WFC", "949746101", "WELLS FARGO")]
SP400 = [("HEI", "422806109", "HEICO CORP"), ("HEI.A", "422806208", "HEICO CORP CLASS A")]


def ssga_xlsx(holdings) -> bytes:
    """SSGA's daily holdings workbook: four preamble rows, the header, the lines, a blank row, a footnote."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Fund Name:", "SPDR S&P 500 ETF Trust"])
    ws.append(["Ticker Symbol:", "SPY"])
    ws.append(["Holdings:", "As of 27-Oct-2026"])
    ws.append([])
    ws.append(["Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector", "Shares Held", "Local Currency"])
    for t, cusip, name in holdings:
        ws.append([name, t, cusip or "", "", 1.0, "Information Technology", 1000, "USD"])
    ws.append(["US DOLLAR", "CASH_USD", "", "", 0.1, "-", 5, "USD"])
    ws.append([])
    ws.append(["Past performance is not a guarantee of future results."])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def ishares_csv(holdings, drop_dash=False) -> bytes:
    """iShares' holdings CSV: nine preamble lines, the header, equity and cash lines, a blank line, a disclaimer.
    ``drop_dash`` writes class tickers without the dot (iShares' ``BRKB`` style)."""
    lines = ['iShares Core S&P 500 ETF', 'Fund Holdings as of,"Oct 27, 2026"', 'Inception Date,"May 15, 2000"',
             'Shares Outstanding,"1,000,000.00"', 'Stock,"-"', 'Bond,"-"', 'Cash,"-"', 'Other,"-"', '\xa0',
             'Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Quantity,Price,Location,'
             'Exchange,Currency,FX Rate,Market Currency,Accrual Date']
    for t, _cusip, name in holdings:
        t = t.replace(".", "") if drop_dash else t
        lines.append(f'{t},"{name}",Information Technology,Equity,"1,000.00",1.00,"1,000.00","10.00",100.00,'
                     'United States,NASDAQ,USD,1.00,USD,-')
    lines.append('USD,"USD CASH",Cash and/or Derivatives,Cash,"10.00",0.01,"10.00","10.00",1.00,United States,-,'
                 'USD,1.00,USD,-')
    lines += ["", '"The content contained herein is owned or licensed by BlackRock"']
    return ("\n".join(lines) + "\n").encode("utf-8")


def vanguard_json(holdings, cusip_only=()) -> bytes:
    """Vanguard's portfolio-holding answer; a name in ``cusip_only`` is written with ``-`` for its ticker, so only
    its CUSIP can match it."""
    entity = [{"ticker": "-" if t in cusip_only else t, "shortName": name, "cusip": cusip, "percentWeight": "1.00"}
              for t, cusip, name in holdings]
    entity.append({"ticker": "", "shortName": "CASH", "cusip": None, "percentWeight": "0.01"})
    return json.dumps({"size": len(entity), "fund": {"entity": entity}}).encode()


def nasdaq_json(symbols) -> bytes:
    return json.dumps({"data": {"data": {"headers": {}, "rows": [{"symbol": s, "companyName": s + " Inc."}
                                                                 for s in symbols]}},
                       "message": None, "status": {"rCode": 200}}).encode()


def add_fund_routes(http_mock, sp500=SP500, sp400=SP400, ndx=("MSFT",), fail=()) -> None:
    """Answer every fund file of ``funds.FILES``: S&P 500 and 400 from the lists above, the SmallCap 600 files 404."""
    def maybe(name, body):
        return (500, b"down", {}) if name in fail else (200, body, {})
    http_mock.add("holdings-daily-us-en-spy", lambda r: maybe("SPY", ssga_xlsx(sp500)))
    http_mock.add("holdings-daily-us-en-mdy", lambda r: maybe("MDY", ssga_xlsx(sp400)))
    http_mock.add("fileName=IVV_holdings", lambda r: maybe("IVV", ishares_csv(sp500)))
    http_mock.add("fileName=IJH_holdings", lambda r: maybe("IJH", ishares_csv(sp400, drop_dash=True)))
    http_mock.add("/VOO/portfolio-holding", lambda r: maybe("VOO", vanguard_json(sp500, cusip_only=("FDX",))))
    http_mock.add("/IVOO/portfolio-holding", lambda r: maybe("IVOO", vanguard_json(sp400)))
    http_mock.add("list-type/nasdaq100", lambda r: maybe("NDX", nasdaq_json(ndx)))
