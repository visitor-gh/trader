"""해외 주식·환율 데이터 수집.

| 데이터 | 1순위 | 폴백 | 비고 |
| 해외 일봉(수정주가) | yfinance(auto_adjust) | FinanceDataReader | 비공식. 상장폐지 종목 누락 → 생존편향 주의 |
| 미국 재무제표 | SEC EDGAR XBRL companyfacts(공식·무료) | — | 'filed'(제출일) 포함 → 시점정합 가능 |
| 일본 재무제표 | EDINET API(금융청, 공식, 키 필요) | yfinance info(스냅샷) | 스냅샷은 백테스트 금지(미래정보) |
| 홍콩 재무제표 | 유료 데이터(벤더) 권장 | yfinance info(스냅샷) | |
| 환율 | 한국은행 ECOS(공식) | yfinance / FinanceDataReader | 외화 1단위당 원화로 통일 |
| 실시간 시세·주문 | 한국투자증권 해외주식 API | — | broker/kis_overseas.py |

SEC 이용 규칙: 요청 헤더에 운영자 식별 User-Agent(이름·연락처)를 반드시 넣고 초당 10회 이하로 호출한다.
"""
from __future__ import annotations

import logging
import os

import pandas as pd
import requests

from ..fx import FX_SYMBOLS, FxRates
from ..markets import parse_symbol
from .collector import OHLCV_COLUMNS, MarketDataCollector, RateLimiter, retry

log = logging.getLogger(__name__)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# 주요 US-GAAP 태그(기업마다 다른 태그를 쓰는 경우가 있어 후보를 순서대로 탐색)
US_GAAP_TAGS = {
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "liabilities": ["Liabilities"],
    "eps_diluted": ["EarningsPerShareDiluted"],
}


class GlobalDataCollector(MarketDataCollector):
    """국내는 상위 클래스(pykrx) 경로, 해외는 yfinance → FinanceDataReader 경로로 수집."""

    @retry(times=3)
    def get_ohlcv(self, ticker: str, start: str, end: str, adjusted: bool = True) -> pd.DataFrame:
        inst = parse_symbol(ticker)
        if inst.market == "KR":
            return super().get_ohlcv(inst.local_code, start, end, adjusted)

        def load():
            self._limiter.wait()
            s, e = pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=1)  # yfinance end는 미포함
            try:
                import yfinance as yf

                df = yf.download(ticker, start=s, end=e, auto_adjust=adjusted, progress=False, threads=False)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                if df.empty:
                    raise ValueError("빈 응답")
            except Exception as ex:
                log.warning("yfinance 실패(%s): %s → FinanceDataReader 폴백", ticker, ex)
                import FinanceDataReader as fdr

                df = fdr.DataReader(ticker, s, e)
            df = df.rename(columns=str.lower)
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df.index.name = "date"
            df["value"] = df["close"] * df["volume"]          # 현지통화 거래대금
            return df[OHLCV_COLUMNS].astype(float)

        tag = "adj" if adjusted else "raw"
        return self._cached("ohlcv", f"{ticker.replace('.', '_')}_{start}_{end}_{tag}", load)

    def get_ohlcv_panel(self, tickers: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
        """다중 시장 패널. 날짜는 모든 시장 거래일의 합집합 — 휴장 시장 칸은 NaN(엔진이 미체결 처리)."""
        return super().get_ohlcv_panel(tickers, start, end)

    @retry(times=3)
    def get_fx(self, ccy: str, start: str, end: str) -> pd.Series:
        """외화 1단위당 원화 환율 일별 시계열."""
        meta = FX_SYMBOLS[ccy]

        def load():
            self._limiter.wait()
            try:
                import yfinance as yf

                df = yf.download(meta["yfinance"], start=pd.Timestamp(start), end=pd.Timestamp(end) + pd.Timedelta(days=1),
                                 progress=False, threads=False)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                s = df["Close"]
            except Exception as ex:
                log.warning("yfinance 환율 실패(%s): %s → FinanceDataReader 폴백", ccy, ex)
                import FinanceDataReader as fdr

                s = fdr.DataReader(meta["fdr"], pd.Timestamp(start), pd.Timestamp(end))["Close"]
            s = (s / meta["per"]).astype(float)
            s.index = pd.to_datetime(s.index).tz_localize(None)
            return s.to_frame("rate")

        return self._cached("fx", f"{ccy}_{start}_{end}", load)["rate"]

    def get_fx_rates(self, currencies: set[str], start: str, end: str) -> FxRates:
        return FxRates({c: self.get_fx(c, start, end) for c in currencies if c != "KRW"})

    def get_snapshot_fundamentals(self, tickers: list[str]) -> pd.DataFrame:
        """현재 시점 밸류에이션 스냅샷(PER·PBR·ROE). 실시간 종목 선별 전용 — 과거 백테스트에 쓰면 미래정보 누수."""
        import yfinance as yf

        rows = {}
        for t in tickers:
            self._limiter.wait()
            try:
                info = yf.Ticker(t).info
                rows[t] = {"PER": info.get("trailingPE"), "PBR": info.get("priceToBook"), "ROE": info.get("returnOnEquity")}
            except Exception as ex:
                log.warning("스냅샷 실패 %s: %s", t, ex)
        return pd.DataFrame(rows).T.astype(float)


class SecEdgarClient:
    """미국 SEC EDGAR XBRL 재무 데이터(공식, 무료). 제출일(filed) 기준 시점정합 레코드를 만든다."""

    def __init__(self, user_agent: str | None = None, cache_dir: str = "./var/data", session=None):
        ua = user_agent or os.environ.get("SEC_USER_AGENT", "")
        if not ua:
            raise ValueError("SEC_USER_AGENT 환경변수가 필요합니다(예: 'MyCompany research contact@mycompany.com').")
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
        self.limiter = RateLimiter(0.15)   # 초당 10회 제한보다 보수적
        self.cache_dir = cache_dir
        self._cik: dict[str, int] | None = None

    def cik_map(self) -> dict[str, int]:
        if self._cik is None:
            self.limiter.wait()
            r = self.s.get(SEC_TICKERS_URL, timeout=30)
            r.raise_for_status()
            self._cik = {v["ticker"].upper(): int(v["cik_str"]) for v in r.json().values()}
        return self._cik

    @retry(times=3)
    def company_facts(self, ticker: str) -> dict:
        cik = self.cik_map().get(ticker.upper().replace(".", "-"))
        if cik is None:
            raise KeyError(f"SEC CIK를 찾을 수 없음: {ticker}")
        self.limiter.wait()
        r = self.s.get(SEC_FACTS_URL.format(cik=cik), timeout=30)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def fact_frame(facts: dict, metric: str, annual_only: bool = True) -> pd.DataFrame:
        """출력: DataFrame[period_end, filed, value, form, fy, fp]
        같은 기간 값이 정정공시로 여러 번 나오면 '가장 먼저 제출된 값'을 남긴다
        (당시 시장이 실제로 알 수 있었던 값 → 시점정합).
        """
        gaap = facts.get("facts", {}).get("us-gaap", {})
        for tag in US_GAAP_TAGS[metric]:
            if tag not in gaap:
                continue
            units = gaap[tag]["units"]
            unit = "USD" if "USD" in units else ("USD/shares" if "USD/shares" in units else next(iter(units)))
            df = pd.DataFrame(units[unit])
            if df.empty:
                continue
            if annual_only:
                df = df[df["form"].isin(["10-K", "20-F", "40-F"]) & (df.get("fp", "FY") == "FY")]
                if "start" in df:   # 손익 등 기간 항목은 약 1년 길이만 사용
                    dur = (pd.to_datetime(df["end"]) - pd.to_datetime(df["start"])).dt.days
                    df = df[dur.isna() | dur.between(350, 380)]
            df = df.rename(columns={"end": "period_end", "val": "value"})
            df["filed"] = pd.to_datetime(df["filed"])
            df = df.sort_values("filed").drop_duplicates("period_end", keep="first")
            return df[["period_end", "filed", "value", "form", "fy", "fp"]].reset_index(drop=True)
        return pd.DataFrame(columns=["period_end", "filed", "value", "form", "fy", "fp"])

    def pit_records(self, ticker: str) -> pd.DataFrame:
        """align_fundamentals_pit() 입력 형식의 레코드: [ticker, period_end, report, disclosed_at, roe, net_income, equity]"""
        facts = self.company_facts(ticker)
        ni = self.fact_frame(facts, "net_income")
        eq = self.fact_frame(facts, "equity")
        if ni.empty or eq.empty:
            return pd.DataFrame()
        m = ni.merge(eq[["period_end", "value"]], on="period_end", suffixes=("_ni", "_eq"))
        out = pd.DataFrame({
            "ticker": ticker, "period_end": m["period_end"], "report": "FY",
            "disclosed_at": m["filed"],                      # 실제 제출일 = 공개일
            "net_income": m["value_ni"], "equity": m["value_eq"],
        })
        out["roe"] = out["net_income"] / out["equity"].where(out["equity"] > 0)
        return out
