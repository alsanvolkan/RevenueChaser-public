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


def make_spine(tickers=SPINE_TICKERS) -> None:
    """A live ``spine.duckdb`` whose ``security`` table holds ``tickers`` (the columns the ownership build reads)."""
    path = serving.live_path("spine")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE security (ticker TEXT, cik TEXT, gvkey TEXT, iid TEXT, cusip TEXT, tier TEXT)")
        con.executemany("INSERT INTO security VALUES (?, NULL, NULL, NULL, NULL, 'A')", [[t] for t in tickers])
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
