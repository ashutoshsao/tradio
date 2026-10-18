"""Builds bhavcopy files in NSE's real UDiFF layout for tests."""

import io
import zipfile
from datetime import date

HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,"
    "StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,"
    "SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,"
    "Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)


def isin(symbol: str) -> str:
    """A made-up ISIN, unique per symbol."""
    return ("INE" + symbol.replace("&", "").replace("-", "").ljust(7, "0"))[:10] + "01"


def row(day: date, symbol: str, close: str, prev_close: str | None = None, series: str = "EQ") -> str:
    prev_close = prev_close or close
    return (
        f"{day},{day},CM,NSE,STK,1,{isin(symbol)},{symbol},{series},,,,,{symbol} LTD,"
        f"{close},{close},{close},{close},{close},{prev_close},,{close},,,1000,1000.00,10,F1,1,,,,,"
    )


def csv_text(*rows: str) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


def zip_bytes(text: str, day: date) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv", text)
    return buf.getvalue()
