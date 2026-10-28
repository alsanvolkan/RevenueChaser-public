"""The ownership store's test sources, in each source's documented shape.

FINRA's answers follow what its API answered on 2026-10-28 (field names, the ``record-total`` header); the values are
chosen for the tests. ``make_spine`` writes a small live ``spine.duckdb`` (the tables the ownership build reads) for
tests that need tickers named without running the whole spine.
"""
from __future__ import annotations

import json

import duckdb

from RevenueChaser.rcdata import serving

SPINE_TICKERS = ("MSFT", "BF-A", "BF-B", "BRK-B", "AAPL", "GME")
# The issuers' real SEC CIKs (BF-A and BF-B share one, as do a company's share classes) and CUSIPs.
SPINE_CIKS = {"MSFT": "789019", "BF-A": "14693", "BF-B": "14693", "BRK-B": "1067983", "AAPL": "320193",
              "GME": "1326380"}
SPINE_CUSIPS = {"MSFT": "594918104", "BF-A": "115637100", "BF-B": "115637209", "BRK-B": "084670702",
                "AAPL": "037833100", "GME": "36467W109"}


def make_spine(tickers=SPINE_TICKERS, ciks=SPINE_CIKS) -> None:
    """A live ``spine.duckdb`` whose ``security`` table holds ``tickers`` (the columns the ownership build reads)."""
    path = serving.live_path("spine")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE security (ticker TEXT, cik TEXT, gvkey TEXT, iid TEXT, cusip TEXT, tier TEXT)")
        con.executemany("INSERT INTO security VALUES (?, ?, NULL, NULL, ?, 'A')",
                        [[t, (ciks or {}).get(t), SPINE_CUSIPS.get(t)] for t in tickers])
    finally:
        con.close()


def finra_row(symbol: str, day: str, short: int, adv: int = 1_000_000, dtc=1.5, name: str = "SOME ISSUE") -> dict:
    return {"stockSplitFlag": None, "previousShortPositionQuantity": short - 100, "averageDailyVolumeQuantity": adv,
            "issueName": name, "currentShortPositionQuantity": short, "changePreviousNumber": 100,
            "accountingYearMonthNumber": int(day[:7].replace("-", "")), "settlementDate": day,
            "marketClassCode": "NNM", "symbolCode": symbol, "daysToCoverQuantity": dtc,
            "issuerServicesGroupExchangeCode": "R", "revisionFlag": None, "changePercent": 0.01}


class Finra:
    """FINRA's two routes: the partitions list and the paged data answer, by settlement date and offset."""

    def __init__(self, dates: dict[str, list[dict]], total_override: dict[str, int] | None = None):
        self.dates, self.total_override = dates, total_override or {}

    def partitions(self, request):
        body = {"availablePartitions": [{"partitions": [d]} for d in sorted(self.dates, reverse=True)]}
        return 200, json.dumps(body).encode(), {"content-type": "application/json"}

    def data(self, request):
        body = json.loads(request.content)
        day = body["compareFilters"][0]["fieldValue"]
        rows = self.dates.get(day, [])
        page = rows[body["offset"]:body["offset"] + body["limit"]]
        total = self.total_override.get(day, len(rows))
        return 200, json.dumps(page).encode(), {"record-total": str(total), "record-max-limit": "5000"}

    def install(self, http_mock) -> None:
        http_mock.add("finra.test/partitions/group/otcMarket/name/consolidatedShortInterest", self.partitions)
        http_mock.add("finra.test/data/group/otcMarket/name/consolidatedShortInterest", self.data)


# ── Reg SHO threshold lists (the shapes Nasdaq Trader and NYSE answered on 2026-10-28) ──────────────────────────

NASDAQ_HEADER = "Symbol|Security Name|Market Category|Reg SHO Threshold Flag|Rule 3210|Filler"
NYSE_HEADER = "Symbol|Security Name|Market Category|Reg SHO Threshold Flag|Filler|Filler"


def nasdaq_list(rows, stamp="20261027230025") -> bytes:
    return "\r\n".join([NASDAQ_HEADER, *rows, stamp, ""]).encode()


def nyse_list(rows, stamp="20261027220001") -> bytes:
    return "\n".join([NYSE_HEADER, *rows, stamp, ""]).encode()


class RegSho:
    """Nasdaq's daily file by name, NYSE's download by ``selectedDate`` and ``market``; a day or market not set
    answers 404 (Nasdaq) or the header alone (NYSE, as it does for a market with no threshold security)."""

    def __init__(self):
        self.nasdaq: dict[str, bytes] = {}
        self.nyse: dict[tuple[str, str], bytes] = {}

    def nasdaq_answer(self, request):
        name = str(request.url).rsplit("/", 1)[1]
        day = name[len("nasdaqth"):-len(".txt")]
        body = self.nasdaq.get(day)
        return (200, body, {}) if body is not None else (404, b"<html>Not Found</html>", {})

    def nyse_answer(self, request):
        key = (request.url.params["selectedDate"], request.url.params["market"])
        return 200, self.nyse.get(key, nyse_list([], stamp="")), {}

    def install(self, http_mock) -> None:
        http_mock.add("/regsho/nasdaqth", self.nasdaq_answer)
        http_mock.add("nyse.test/api/regulatory/threshold-securities/download", self.nyse_answer)


# ── IBKR's public short-stock file (format as IBKR documents it; not read from the cloud) ───────────────────────

IBKR_HEADER = "#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|"
IBKR_ROWS = ["AAPL|USD|APPLE INC|265598|US0378331005|3.3383|0.25|>10000000|BBG000B9XRY4|",
             "BRK B|USD|BERKSHIRE HATHAWAY INC-CL B|72063691|US0846707026|3.1|0.25|3500000|BBG000DWG505|",
             "GME|USD|GAMESTOP CORP-CLASS A|36285627|US36467W1099|-12.5|16.0|150000|BBG000BB5BF6|",
             "NOAV|USD|NOTHING AVAILABLE CO|99|US0000000001|NA|NA|0||"]


def ibkr_file(rows=IBKR_ROWS, header=IBKR_HEADER, eof=True) -> bytes:
    lines = ["#BOF|2026.10.28|09:45:02", header, *rows] + ([f"#EOF|{len(rows)}"] if eof else [])
    return ("\n".join(lines) + "\n").encode()


class FakeFtp:
    """``ftplib.FTP`` as the dataset uses it: ``login``, ``retrbinary`` and ``quit``; records every session.
    ``fail`` errors are raised by the next sessions' ``retrbinary``, one each, before the file is answered."""

    sessions: list = []

    def __init__(self, body: bytes, fail=()):
        self.body, self.fail = body, list(fail)

    def __call__(self, host, timeout=None):
        outer = self

        class Session:
            def __init__(self):
                self.host, self.login_args, self.commands = host, None, []
                outer.sessions.append(self)

            def login(self, user="", passwd=""):
                self.login_args = (user, passwd)

            def retrbinary(self, cmd, callback, blocksize=8192):
                self.commands.append(cmd)
                if outer.fail:
                    raise outer.fail.pop(0)
                callback(outer.body)

            def quit(self):
                pass

            def close(self):
                pass

        return Session()


# ── The SEC's fails-to-deliver halves (format as the SEC documents it; sec.gov refused the cloud) ───────────────

FTD_HEADER = "SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE"


def ftd_zip(rows, member="cnsfails202609b.txt", trailer=True) -> bytes:
    import io
    import zipfile
    lines = [FTD_HEADER, *rows]
    if trailer:
        lines += [f"Trailer record count {len(rows)}", "Trailer total quantity of shares 1"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(member, "\r\n".join(lines) + "\r\n")
    return buf.getvalue()


class Ftd:
    """``files/data/fails-deliver-data/cnsfails<YYYYMM><a|b>.zip``; a half not set answers 404."""

    def __init__(self):
        self.halves: dict[str, bytes] = {}

    def answer(self, request):
        name = str(request.url).rsplit("/", 1)[1]
        body = self.halves.get(name[len("cnsfails"):-len(".zip")])
        return (200, body, {"content-type": "application/zip"}) if body is not None else (404, b"Not Found", {})

    def install(self, http_mock) -> None:
        http_mock.add("/files/data/fails-deliver-data/cnsfails", self.answer)


# ── EDGAR: the daily index and full submissions (the SEC's documented shapes; sec.gov refused the cloud) ──────────

INDEX_HEADER = "CIK|Company Name|Form Type|Date Filed|File Name"


def daily_index(rows) -> bytes:
    """``master.<YYYYMMDD>.idx``: the preamble, the header, a rule, then ``CIK|Company|Form|YYYYMMDD|File Name``."""
    head = ["Description:           Daily Index of EDGAR Dissemination Feed by Company Name",
            "Last Data Received:    October 27, 2026", "Comments:              webmaster@sec.gov",
            "Anonymous FTP:         ftp://ftp.sec.gov/edgar/", "", "", "", "", INDEX_HEADER, "-" * 80]
    return ("\n".join(head + ["|".join(str(c) for c in r) for r in rows]) + "\n").encode("latin-1")


def idx_row(cik: int, name: str, form: str, day: str, accession: str) -> tuple:
    return (cik, name, form, day.replace("-", ""), f"edgar/data/{cik}/{accession}.txt")


def submission(accession: str, form: str, filed: str, roles: list[tuple[str, int, str]], docs: list[tuple],
               period: str | None = None) -> bytes:
    """A full submission ``.txt``: the SGML header (``roles``: ``(role, cik, name)``, e.g. ``FILER``) and one
    ``<DOCUMENT>`` per ``(type, filename, xml)``."""
    ymd = filed.replace("-", "")
    head = [f"<SEC-DOCUMENT>{accession}.txt : {ymd}", f"<SEC-HEADER>{accession}.hdr.sgml : {ymd}",
            f"<ACCEPTANCE-DATETIME>{ymd}160512", f"ACCESSION NUMBER:\t\t{accession}",
            f"CONFORMED SUBMISSION TYPE:\t{form}", f"PUBLIC DOCUMENT COUNT:\t\t{len(docs)}"]
    if period:
        head.append(f"CONFORMED PERIOD OF REPORT:\t{period.replace('-', '')}")
    head += [f"FILED AS OF DATE:\t\t{ymd}", f"DATE AS OF CHANGE:\t\t{ymd}", ""]
    for role, cik, name in roles:
        data = "OWNER DATA" if role == "REPORTING-OWNER" else "COMPANY DATA"
        head += [f"{role}:", "", f"\t{data}:\t", f"\t\tCOMPANY CONFORMED NAME:\t\t\t{name}",
                 f"\t\tCENTRAL INDEX KEY:\t\t\t{cik:010d}", ""]
    head.append("</SEC-HEADER>")
    body = [f"<DOCUMENT>\n<TYPE>{t}\n<SEQUENCE>{i}\n<FILENAME>{f}\n<TEXT>\n<XML>\n{x}\n</XML>\n</TEXT>\n</DOCUMENT>"
            if x is not None else f"<DOCUMENT>\n<TYPE>{t}\n<SEQUENCE>{i}\n<FILENAME>{f}\n<TEXT>\nplain text\n"
                                  f"</TEXT>\n</DOCUMENT>"
            for i, (t, f, x) in enumerate(docs, 1)]
    return ("\n".join(head + body + ["</SEC-DOCUMENT>", ""])).encode()


def f13_primary(period="09-30-2026", report_type="13F HOLDINGS REPORT", amendment_type=None, amendment_no=None,
                name="SOME CAPITAL LLC", entries=0, value_total=0) -> str:
    amend = ""
    if amendment_type:
        amend = (f"<isAmendment>true</isAmendment><amendmentNo>{amendment_no or 1}</amendmentNo>"
                 f"<amendmentInfo><amendmentType>{amendment_type}</amendmentType></amendmentInfo>")
    else:
        amend = "<isAmendment>false</isAmendment>"
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler" '
            'xmlns:com="http://www.sec.gov/edgar/common">'
            '<headerData><submissionType>13F-HR</submissionType></headerData>'
            f'<formData><coverPage><reportCalendarOrQuarter>{period}</reportCalendarOrQuarter>{amend}'
            f'<filingManager><name>{name}</name></filingManager><reportType>{report_type}</reportType></coverPage>'
            f'<summaryPage><otherIncludedManagersCount>0</otherIncludedManagersCount>'
            f'<tableEntryTotal>{entries}</tableEntryTotal><tableValueTotal>{value_total}</tableValueTotal>'
            '</summaryPage></formData></edgarSubmission>')


def f13_holding(issuer, cusip, value, shares, sh_type="SH", put_call=None, figi=None) -> dict:
    return {"issuer": issuer, "cusip": cusip, "value": value, "shares": shares, "sh_type": sh_type,
            "put_call": put_call, "figi": figi}


def f13_infotable(holdings) -> str:
    rows = []
    for h in holdings:
        rows.append(
            "<ns1:infoTable>"
            f"<ns1:nameOfIssuer>{h['issuer']}</ns1:nameOfIssuer><ns1:titleOfClass>COM</ns1:titleOfClass>"
            f"<ns1:cusip>{h['cusip']}</ns1:cusip>" + (f"<ns1:figi>{h['figi']}</ns1:figi>" if h["figi"] else "") +
            f"<ns1:value>{h['value']}</ns1:value><ns1:shrsOrPrnAmt><ns1:sshPrnamt>{h['shares']}</ns1:sshPrnamt>"
            f"<ns1:sshPrnamtType>{h['sh_type']}</ns1:sshPrnamtType></ns1:shrsOrPrnAmt>"
            + (f"<ns1:putCall>{h['put_call']}</ns1:putCall>" if h["put_call"] else "") +
            "<ns1:investmentDiscretion>SOLE</ns1:investmentDiscretion><ns1:votingAuthority><ns1:Sole>"
            f"{h['shares']}</ns1:Sole><ns1:Shared>0</ns1:Shared><ns1:None>0</ns1:None></ns1:votingAuthority>"
            "</ns1:infoTable>")
    return ('<?xml version="1.0" encoding="UTF-8"?><ns1:informationTable '
            'xmlns:ns1="http://www.sec.gov/edgar/document/thirteenf/informationtable">' + "".join(rows) +
            "</ns1:informationTable>")


def f13_filing(accession, cik, filed, holdings, period="09-30-2026", form="13F-HR", amendment_type=None,
               amendment_no=None, report_type="13F HOLDINGS REPORT", name=None) -> bytes:
    name = name or f"MANAGER {cik}"
    docs = [(form, "primary_doc.xml", f13_primary(period, report_type, amendment_type, amendment_no, name,
                                                  len(holdings), sum(h["value"] for h in holdings)))]
    if holdings is not None and report_type != "13F NOTICE":
        docs.append(("INFORMATION TABLE", "infotable.xml", f13_infotable(holdings)))
    m, d, y = period.split("-")
    return submission(accession, form, filed, [("FILER", cik, name)], docs, period=f"{y}-{m}-{d}")


class Edgar:
    """The daily indexes by day and the submissions by accession; anything else answers 404."""

    def __init__(self):
        self.indexes: dict[str, bytes] = {}
        self.filings: dict[str, bytes] = {}

    def index(self, request):
        day = str(request.url).rsplit("master.", 1)[1][:8]
        body = self.indexes.get(day)
        return (200, body, {}) if body is not None else (404, b"Not Found", {})

    def filing(self, request):
        acc = str(request.url).rsplit("/", 1)[1][:-len(".txt")]
        body = self.filings.get(acc)
        return (200, body, {}) if body is not None else (404, b"Not Found", {})

    def install(self, http_mock) -> None:
        http_mock.add("/Archives/edgar/daily-index/", self.index)
        http_mock.add("/Archives/edgar/data/", self.filing)

    def add_day(self, day: str, entries: list[tuple]) -> None:
        """``entries``: ``(index row, submission bytes or None)``; the same accession may be listed twice."""
        self.indexes[day.replace("-", "")] = daily_index([r for r, _ in entries])
        for r, body in entries:
            if body is not None:
                self.filings[r[4].rsplit("/", 1)[1][:-4]] = body


# ── Form 4 (the ownershipDocument schema as the SEC documents it) ──────────────────────────────────────────────

def f4_transaction(code, shares, price, ad="D", after=None, date_="2026-10-23", title="Common Stock",
                   derivative=False, exercise=None, underlying=None, expires=None, price_footnote=False) -> str:
    price_xml = ('<transactionPricePerShare><footnoteId id="F1"/></transactionPricePerShare>' if price_footnote
                 else f"<transactionPricePerShare><value>{price}</value></transactionPricePerShare>")
    tag = "derivativeTransaction" if derivative else "nonDerivativeTransaction"
    extra = ""
    if derivative:
        extra = (f"<conversionOrExercisePrice><value>{exercise}</value></conversionOrExercisePrice>"
                 f"<expirationDate><value>{expires}</value></expirationDate>"
                 "<underlyingSecurity><underlyingSecurityTitle><value>Common Stock</value></underlyingSecurityTitle>"
                 f"<underlyingSecurityShares><value>{underlying}</value></underlyingSecurityShares>"
                 "</underlyingSecurity>")
    return (f"<{tag}><securityTitle><value>{title}</value></securityTitle>{extra}"
            f"<transactionDate><value>{date_}</value></transactionDate>"
            f"<transactionCoding><transactionFormType>4</transactionFormType><transactionCode>{code}"
            "</transactionCode><equitySwapInvolved>0</equitySwapInvolved></transactionCoding>"
            f"<transactionAmounts><transactionShares><value>{shares}</value></transactionShares>{price_xml}"
            f"<transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode>"
            "</transactionAmounts>"
            f"<postTransactionAmounts><sharesOwnedFollowingTransaction><value>{after}</value>"
            "</sharesOwnedFollowingTransaction></postTransactionAmounts>"
            "<ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>"
            f"</ownershipNature></{tag}>")


def f4_document(issuer_cik, issuer_name, symbol, owners, non_derivative=(), derivative=(), form="4") -> str:
    owner_xml = "".join(
        f"<reportingOwner><reportingOwnerId><rptOwnerCik>{cik:010d}</rptOwnerCik><rptOwnerName>{name}</rptOwnerName>"
        f"</reportingOwnerId><reportingOwnerRelationship><isDirector>{int('dir' in rel)}</isDirector>"
        f"<isOfficer>{int('off' in rel)}</isOfficer>" + (f"<officerTitle>{rel['off']}</officerTitle>" if 'off' in rel
                                                         else "") +
        f"<isTenPercentOwner>{int('ten' in rel)}</isTenPercentOwner><isOther>0</isOther>"
        "</reportingOwnerRelationship></reportingOwner>" for cik, name, rel in owners)
    return ('<?xml version="1.0"?><ownershipDocument><schemaVersion>X0508</schemaVersion>'
            f"<documentType>{form}</documentType><periodOfReport>2026-10-23</periodOfReport>"
            f"<issuer><issuerCik>{issuer_cik:010d}</issuerCik><issuerName>{issuer_name}</issuerName>"
            f"<issuerTradingSymbol>{symbol}</issuerTradingSymbol></issuer>{owner_xml}<aff10b5One>1</aff10b5One>"
            f"<nonDerivativeTable>{''.join(non_derivative)}</nonDerivativeTable>"
            f"<derivativeTable>{''.join(derivative)}</derivativeTable></ownershipDocument>")


def f4_filing(accession, filed, issuer_cik, issuer_name, symbol, owners, non_derivative=(), derivative=(),
              form="4") -> bytes:
    roles = [("ISSUER", issuer_cik, issuer_name)] + [("REPORTING-OWNER", c, n) for c, n, _ in owners]
    return submission(accession, form, filed, roles,
                      [(form, "form4.xml", f4_document(issuer_cik, issuer_name, symbol, owners, non_derivative,
                                                       derivative, form))], period=filed)


# ── Schedule 13D/G (XML since 2024-12-18; tags as the SEC's technical specification names them, read tolerantly) ──

def sched_xml(kind, issuer_cik, issuer_name, cusip, persons, event="2026-10-20") -> str:
    """A Schedule 13D/G ``edgarSubmission``; ``persons``: ``(name, amount, percent, sole_voting)``."""
    ns = f"http://www.sec.gov/edgar/schedule{kind.lower()}"
    people = "".join(
        "<coverPageHeaderReportingPersonDetails>"
        f"<reportingPersonName>{n}</reportingPersonName><typeOfReportingPerson>IA</typeOfReportingPerson>"
        f"<reportingPersonBeneficiallyOwnedNumberOfShares><soleVotingPower>{sv}</soleVotingPower>"
        "<sharedVotingPower>0</sharedVotingPower></reportingPersonBeneficiallyOwnedNumberOfShares>"
        f"<reportingPersonBeneficiallyOwnedAggregateNumberOfShares>{amt}"
        "</reportingPersonBeneficiallyOwnedAggregateNumberOfShares>"
        f"<classPercent>{pct}</classPercent></coverPageHeaderReportingPersonDetails>" for n, amt, pct, sv in persons)
    return (f'<?xml version="1.0"?><edgarSubmission xmlns="{ns}"><headerData><submissionType>SCHEDULE {kind}'
            "</submissionType></headerData><formData><coverPageHeader>"
            f"<securitiesClassTitle>Common Stock</securitiesClassTitle><dateOfEvent>{event}</dateOfEvent>"
            f"<issuerInfo><issuerCik>{issuer_cik:010d}</issuerCik><issuerName>{issuer_name}</issuerName>"
            f"<issuerCusip>{cusip}</issuerCusip></issuerInfo></coverPageHeader>{people}</formData></edgarSubmission>")


def sched_filing(accession, form, filed, issuer, filer, xml=None) -> bytes:
    """``issuer`` and ``filer``: ``(cik, name)``; no ``xml`` is a text filing of the years before the XML form."""
    roles = [("SUBJECT COMPANY", *issuer), ("FILED BY", *filer)]
    doc = (form, "primary_doc.xml", xml) if xml is not None else (form, "sc13g.htm", None)
    return submission(accession, form, filed, roles, [doc])
