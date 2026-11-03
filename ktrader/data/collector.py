"""국내 주식 데이터 자동 수집.

데이터 소스(우선순위 순서로 폴백):
  1) pykrx            : KRX 정보데이터시스템 기반 일봉·시가총액·PER/PBR (무료, 비공식 스크래핑)
  2) FinanceDataReader: 종목 목록·일봉 (무료, 비공식)
  3) 한국투자증권 API  : 일봉/현재가(공식, 계좌 필요) → broker.kis.KISBroker.get_daily_ohlcv
  4) OpenDART API     : 재무제표(공식, 금융감독원, 인증키 필요)

pykrx·FinanceDataReader는 외부 웹페이지 구조나 이용 정책이 바뀌면 동작이 멈출 수 있다.
따라서 모든 함수는 (a) 재시도, (b) 요청 간격 제한, (c) 로컬 캐시, (d) 다음 소스로 폴백 구조를 갖는다.
"""
from __future__ import annotations

import io
import logging
import os
import time
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import wraps
from typing import Callable

import pandas as pd
import requests

log = logging.getLogger(__name__)

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume", "value"]

_PYKRX_RENAME = {
    "시가": "open", "고가": "high", "저가": "low", "종가": "close",
    "거래량": "volume", "거래대금": "value", "등락률": "change_pct",
    "시가총액": "market_cap", "상장주식수": "shares",
}

DART_URL = "https://opendart.fss.or.kr/api"
# 보고서 코드: 1분기 11013, 반기 11012, 3분기 11014, 사업보고서 11011
REPORT_CODES = {"Q1": "11013", "H1": "11012", "Q3": "11014", "FY": "11011"}


def retry(times: int = 3, backoff: float = 1.0, exceptions=(Exception,)):
    """지수 백오프 재시도(1s, 2s, 4s ...)."""

    def deco(fn: Callable):
        @wraps(fn)
        def wrapper(*a, **kw):
            for i in range(times):
                try:
                    return fn(*a, **kw)
                except exceptions as e:  # noqa: PERF203
                    if i == times - 1:
                        raise
                    wait = backoff * (2**i)
                    log.warning("%s 실패(%s) → %.1fs 후 재시도", fn.__name__, e, wait)
                    time.sleep(wait)

        return wrapper

    return deco


class RateLimiter:
    """최소 요청 간격을 보장한다(데이터 제공처 서버 부하·차단 방지)."""

    def __init__(self, min_interval_sec: float):
        self.min_interval = min_interval_sec
        self._last = 0.0

    def wait(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()


@dataclass
class MarketDataCollector:
    cache_dir: str = "./var/data"
    min_interval_sec: float = 0.5

    def __post_init__(self):
        os.makedirs(self.cache_dir, exist_ok=True)
        self._limiter = RateLimiter(self.min_interval_sec)

    # ------------------------------------------------------------------ 캐시
    def _cache_path(self, kind: str, key: str) -> str:
        return os.path.join(self.cache_dir, f"{kind}_{key}.csv")

    def _cached(self, kind: str, key: str, loader: Callable[[], pd.DataFrame]) -> pd.DataFrame:
        path = self._cache_path(kind, key)
        if os.path.exists(path):
            return pd.read_csv(path, index_col=0, parse_dates=True, dtype={"ticker": str})
        df = loader()
        if df is not None and not df.empty:
            df.to_csv(path)
        return df

    # ------------------------------------------------------------- 종목 목록
    def get_tickers(self, date: str, market: str = "KOSPI") -> pd.DataFrame:
        """입력: date "YYYYMMDD", market KOSPI|KOSDAQ
        출력: DataFrame[ticker, name, market]
        생존 편향(이미 상장폐지된 종목이 빠지는 문제)을 막기 위해 '해당 날짜 기준' 목록을 받는다.
        """

        def load():
            self._limiter.wait()
            try:
                from pykrx import stock

                codes = stock.get_market_ticker_list(date, market=market)
                names = [stock.get_market_ticker_name(c) for c in codes]
                return pd.DataFrame({"ticker": codes, "name": names, "market": market})
            except Exception as e:  # pykrx 실패 → FDR 폴백(현재 상장 목록만 제공)
                log.warning("pykrx 종목목록 실패: %s → FinanceDataReader 폴백", e)
                import FinanceDataReader as fdr

                df = fdr.StockListing(market)
                code_col = "Code" if "Code" in df.columns else "Symbol"
                return pd.DataFrame(
                    {"ticker": df[code_col].astype(str), "name": df["Name"], "market": market}
                )

        return self._cached("tickers", f"{market}_{date}", load)

    # ---------------------------------------------------------------- 일봉
    @retry(times=3)
    def get_ohlcv(self, ticker: str, start: str, end: str, adjusted: bool = True) -> pd.DataFrame:
        """입력: 종목코드 6자리, 기간 "YYYYMMDD", 수정주가 여부
        출력: DatetimeIndex, 컬럼 open/high/low/close/volume/value
        수정주가(adjusted): 액면분할·증자 등으로 끊긴 가격을 과거까지 소급 보정한 가격.
        백테스트에는 반드시 수정주가를 쓰고, 주문 가격 계산에는 실시간 원주가를 쓴다.
        """

        def load():
            self._limiter.wait()
            try:
                from pykrx import stock

                df = stock.get_market_ohlcv(start, end, ticker, adjusted=adjusted)
                df = df.rename(columns=_PYKRX_RENAME)
            except Exception as e:
                log.warning("pykrx 일봉 실패(%s): %s → FinanceDataReader 폴백", ticker, e)
                import FinanceDataReader as fdr

                df = fdr.DataReader(ticker, pd.Timestamp(start), pd.Timestamp(end))
                df = df.rename(columns=str.lower)
            df.index = pd.to_datetime(df.index)
            df.index.name = "date"
            if "value" not in df.columns:  # 거래대금이 없으면 종가×거래량으로 근사
                df["value"] = df["close"] * df["volume"]
            return df[OHLCV_COLUMNS].astype(float)

        tag = "adj" if adjusted else "raw"
        return self._cached("ohlcv", f"{ticker}_{start}_{end}_{tag}", load)

    def get_ohlcv_panel(self, tickers: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
        """여러 종목 일봉 → {필드: DataFrame(날짜×종목)} 패널 구조로 변환."""
        frames = {}
        for t in tickers:
            try:
                frames[t] = self.get_ohlcv(t, start, end)
            except Exception as e:
                log.error("일봉 수집 최종 실패 %s: %s (해당 종목 제외)", t, e)
        return {f: pd.DataFrame({t: df[f] for t, df in frames.items()}) for f in OHLCV_COLUMNS}

    # --------------------------------------------------- 밸류에이션·시가총액
    @retry(times=3)
    def get_fundamentals(self, date: str, market: str = "ALL") -> pd.DataFrame:
        """출력: index=ticker, 컬럼 BPS/PER/PBR/EPS/DIV/DPS + market_cap/shares/value
        PER(주가수익비율)=주가/주당순이익, PBR(주가순자산비율)=주가/주당순자산.
        """

        def load():
            self._limiter.wait()
            from pykrx import stock

            fund = stock.get_market_fundamental(date, market=market)
            cap = stock.get_market_cap(date, market=market).rename(columns=_PYKRX_RENAME)
            df = fund.join(cap[["market_cap", "shares", "value"]], how="left")
            df.index.name = "ticker"
            return df

        return self._cached("fundamental", f"{market}_{date}", load)


class DartClient:
    """금융감독원 OpenDART 재무제표 수집(공식 API, 일 20,000건 한도)."""

    def __init__(self, api_key: str, cache_dir: str = "./var/data", session=None):
        if not api_key:
            raise ValueError("DART_API_KEY 환경변수가 필요합니다(https://opendart.fss.or.kr 발급).")
        self.api_key = api_key
        self.cache_dir = cache_dir
        self.session = session or requests.Session()
        self._corp_map: dict[str, str] | None = None

    def corp_code_map(self) -> dict[str, str]:
        """종목코드(6자리) → DART 고유번호(8자리) 매핑. ZIP(XML) 1회 다운로드 후 캐시."""
        if self._corp_map is not None:
            return self._corp_map
        path = os.path.join(self.cache_dir, "dart_corpcode.csv")
        if os.path.exists(path):
            df = pd.read_csv(path, dtype=str)
        else:
            r = self.session.get(f"{DART_URL}/corpCode.xml", params={"crtfc_key": self.api_key}, timeout=30)
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                root = ET.fromstring(z.read(z.namelist()[0]))
            rows = [
                (e.findtext("stock_code", "").strip(), e.findtext("corp_code", "").strip())
                for e in root.iter("list")
            ]
            df = pd.DataFrame([x for x in rows if x[0]], columns=["stock_code", "corp_code"])
            os.makedirs(self.cache_dir, exist_ok=True)
            df.to_csv(path, index=False)
        self._corp_map = dict(zip(df["stock_code"], df["corp_code"]))
        return self._corp_map

    @retry(times=3)
    def financial_statements(self, ticker: str, year: int, report: str = "FY", consolidated: bool = True) -> pd.DataFrame:
        """입력: 종목코드, 사업연도, 보고서(Q1/H1/Q3/FY), 연결재무제표 여부
        출력: DataFrame[account_id, account_nm, sj_div, amount]
        sj_div: BS 재무상태표, IS 손익계산서, CIS 포괄손익계산서, CF 현금흐름표
        """
        corp = self.corp_code_map().get(ticker)
        if corp is None:
            raise KeyError(f"DART 고유번호를 찾을 수 없음: {ticker}")
        r = self.session.get(
            f"{DART_URL}/fnlttSinglAcntAll.json",
            params={
                "crtfc_key": self.api_key,
                "corp_code": corp,
                "bsns_year": str(year),
                "reprt_code": REPORT_CODES[report],
                "fs_div": "CFS" if consolidated else "OFS",
            },
            timeout=30,
        )
        r.raise_for_status()
        js = r.json()
        if js.get("status") != "000":  # 013: 데이터 없음, 020: 요청 한도 초과
            raise RuntimeError(f"DART 오류 {js.get('status')}: {js.get('message')}")
        df = pd.DataFrame(js["list"])
        df["amount"] = pd.to_numeric(df["thstrm_amount"].str.replace(",", ""), errors="coerce")
        return df[["account_id", "account_nm", "sj_div", "amount"]]

    @staticmethod
    def key_metrics(fs: pd.DataFrame) -> dict[str, float]:
        """재무제표 → 핵심 지표(ROE, 부채비율, 영업이익률). 계정 ID는 IFRS 표준 태그 사용."""

        def pick(*ids):
            for i in ids:
                v = fs.loc[fs["account_id"] == i, "amount"]
                if not v.empty and pd.notna(v.iloc[0]):
                    return float(v.iloc[0])
            return float("nan")

        equity = pick("ifrs-full_Equity")
        liabilities = pick("ifrs-full_Liabilities")
        revenue = pick("ifrs-full_Revenue", "ifrs-full_RevenueFromContractsWithCustomers")
        op_income = pick("dart_OperatingIncomeLoss")
        net_income = pick("ifrs-full_ProfitLoss")
        return {
            "roe": net_income / equity if equity else float("nan"),
            "debt_ratio": liabilities / equity if equity else float("nan"),
            "op_margin": op_income / revenue if revenue else float("nan"),
        }
