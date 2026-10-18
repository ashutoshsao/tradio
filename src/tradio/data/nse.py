"""NSE archives: bhavcopy (UDiFF format) and the daily PR zip's corporate actions file.

See docs/v1-spec.md §2 and §4.
"""

import csv
import io
import logging
import re
import time
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import httpx

from tradio.data.base import Bar, CorporateAction

log = logging.getLogger(__name__)

BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"
)
PR_URL = "https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR{day:%d%m%y}.zip"
ACTIONS_API_URL = "https://www.nseindia.com/api/corporates-corporateActions"
HEADERS = {
    # NSE rejects requests without a browser-like User-Agent.
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "*/*",
}
SERIES = "EQ"


class NseArchiveSource:
    def __init__(self, client: httpx.Client | None = None, retries: int = 3, backoff: float = 2.0):
        self.client = client or httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True)
        self.retries = retries
        self.backoff = backoff

    def fetch_day(self, day: date) -> list[Bar] | None:
        content = self._download(BHAVCOPY_URL.format(day=day))
        return None if content is None else parse_bhavcopy_zip(content)

    def fetch_actions(self, day: date) -> list[CorporateAction] | None:
        content = self._download(PR_URL.format(day=day))
        return None if content is None else parse_pr_zip(content, day)

    def fetch_actions_between(self, start: date, end: date) -> list[CorporateAction] | None:
        # www.nseindia.com is bot-protected and less reliable than the archives;
        # callers treat None as "try again next run".
        params = {"index": "equities", "from_date": f"{start:%d-%m-%Y}", "to_date": f"{end:%d-%m-%Y}"}
        headers = {"Referer": "https://www.nseindia.com/companies-listing/corporate-filings-actions"}
        try:
            resp = self.client.get(ACTIONS_API_URL, params=params, headers=headers)
            resp.raise_for_status()
            return parse_actions_api(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("corporate actions API failed: %s", exc)
            return None

    def _download(self, url: str) -> bytes | None:
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.client.get(url)
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                return resp.content
            except httpx.HTTPError as exc:
                if attempt == self.retries:
                    raise
                log.warning("download failed (%s), retry %d/%d", exc, attempt, self.retries)
                time.sleep(self.backoff * attempt)
        raise AssertionError("unreachable")


def parse_bhavcopy_zip(content: bytes) -> list[Bar]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        (name,) = [n for n in zf.namelist() if n.endswith(".csv")]
        text = zf.read(name).decode("utf-8")
    return parse_bhavcopy_csv(text)


def parse_bhavcopy_csv(text: str) -> list[Bar]:
    bars, skipped = [], 0
    for row in csv.DictReader(io.StringIO(text)):
        if row["SctySrs"] != SERIES:
            continue
        try:
            bars.append(
                Bar(
                    symbol=row["TckrSymb"].strip(),
                    isin=row["ISIN"].strip(),
                    name=row["FinInstrmNm"].strip(),
                    series=row["SctySrs"],
                    trade_date=date.fromisoformat(row["TradDt"]),
                    open=Decimal(row["OpnPric"]),
                    high=Decimal(row["HghPric"]),
                    low=Decimal(row["LwPric"]),
                    close=Decimal(row["ClsPric"]),
                    prev_close=Decimal(row["PrvsClsgPric"]),
                    volume=int(row["TtlTradgVol"]),
                )
            )
        except (InvalidOperation, ValueError):
            skipped += 1
    if skipped:
        log.warning("skipped %d malformed EQ rows", skipped)
    return bars


# --- Corporate actions -------------------------------------------------------
# Two NSE sources, both incomplete on their own (checked over Oct 2024 - Oct 2026):
#  - the PR zip's bc file (archives host, reliable) misses some splits/bonuses
#    and all demergers;
#  - the corporate actions API (bot-protected host) has those but no ETFs.
# Wording varies: "FV SPLT FRM RS 2 TO RE 1", "FVSPLT FRMRS 100 TO RE 1",
# "FV SPLT FRM RS 10 TO 1", "Face Value Split (Sub-Division) - From Rs 10/- Per
# Share To Re 1/- Per Share", "BONUS 4:1", "Demerger".

_BC_NAME = re.compile(r"^bc\d+\.csv$", re.IGNORECASE)
_BONUS = re.compile(r"\bBONUS\s*(\d+)\s*:\s*(\d+)")
_FROM_TO = re.compile(
    r"FR(?:O)?M\s*(?:R[SE]\.?)?\s*([\d.]+)\s*(?:/-)?\s*(?:PER\s+SHARE)?\s*TO\s*(?:R[SE]\.?)?\s*([\d.]+)"
)
_SPLIT = re.compile(r"F\.?\s*V\.?\s*SPLT|SPLIT|SUB-?DIV")
_CONSOLIDATION = re.compile(r"CONSOL")
_DEMERGER = re.compile(r"\bDE-?MERGER\b")
# Bonus preference shares or debentures don't change the equity share count.
_NOT_EQUITY = re.compile(r"PREF|NCRPS|DEB")


def _num(value: str) -> str:
    return format(Decimal(value).normalize(), "f")


def parse_purpose(purpose: str) -> list[tuple[str, Decimal | None]]:
    """(action key, factor) pairs for the share-count / value changes in one PURPOSE text."""
    text = purpose.upper()
    found: list[tuple[str, Decimal | None]] = []
    if (m := _BONUS.search(text)) and not _NOT_EQUITY.search(text):
        new, held = Decimal(m[1]), Decimal(m[2])
        found.append((f"bonus {m[1]}:{m[2]}", held / (new + held)))
    if _SPLIT.search(text) or _CONSOLIDATION.search(text):
        if m := _FROM_TO.search(text):
            kind = "split" if _SPLIT.search(text) else "consolidation"
            found.append((f"{kind} {_num(m[1])}>{_num(m[2])}", Decimal(m[2]) / Decimal(m[1])))
        else:
            log.warning("unrecognised share-change purpose: %r", purpose)
    if _DEMERGER.search(text):
        found.append(("demerger", None))
    return found


def _parse_date(value: str) -> date | None:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def _actions(symbol: str, ex_date: date, purpose: str, source: str) -> list[CorporateAction]:
    purpose = " ".join(purpose.split())
    return [CorporateAction(symbol, ex_date, key, factor, purpose, source) for key, factor in parse_purpose(purpose)]


def parse_pr_zip(content: bytes, day: date) -> list[CorporateAction]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = [n for n in zf.namelist() if _BC_NAME.match(n.rsplit("/", 1)[-1])]
        if not names:
            log.warning("%s: no corporate actions file in PR zip", day)
            return []
        text = zf.read(names[0]).decode("utf-8", errors="replace")
    return parse_bc_csv(text, day)


def parse_bc_csv(text: str, day: date) -> list[CorporateAction]:
    """Actions from a bc file, keeping only those already ex on `day`.

    The file also lists upcoming actions; those are picked up from their own
    day's file, so a rescheduled action is never applied early.
    """
    actions = []
    for row in csv.DictReader(io.StringIO(text)):
        if row["SERIES"].strip() != SERIES:
            continue
        ex_date = _parse_date(row["EX_DT"])
        if ex_date is None or ex_date > day:
            continue
        actions.extend(_actions(row["SYMBOL"].strip(), ex_date, row["PURPOSE"], "bc"))
    return actions


def parse_actions_api(rows: list[dict]) -> list[CorporateAction]:
    actions = []
    for row in rows:
        if row.get("series") != SERIES or not (ex_date := _parse_date(row.get("exDate") or "")):
            continue
        actions.extend(_actions(row["symbol"].strip(), ex_date, row.get("subject") or "", "api"))
    return actions
