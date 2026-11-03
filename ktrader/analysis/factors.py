"""팩터(종목 선별 기준) 정의와 계산.

팩터(factor): 미래 수익률과 통계적으로 관련이 있다고 알려진 종목 특성(예: 저평가, 추세, 수익성).
각 팩터는 방향(direction)을 가진다. +1 이면 값이 클수록 좋고, -1 이면 작을수록 좋다.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..markets import parse_symbol
from . import indicators as ind


def _market(t: str) -> str:
    try:
        return parse_symbol(t).market
    except ValueError:
        return "KR"


@dataclass(frozen=True)
class FactorSpec:
    name: str
    label_ko: str
    direction: int
    category: str  # technical | fundamental | risk | liquidity
    desc_ko: str


FACTOR_SPECS: dict[str, FactorSpec] = {
    "mom_12_1": FactorSpec("mom_12_1", "12-1개월 모멘텀", +1, "technical",
                           "최근 1개월을 제외한 12개월 수익률. 높을수록 중기 상승 추세가 강함"),
    "mom_3": FactorSpec("mom_3", "3개월 모멘텀", +1, "technical", "최근 3개월 수익률"),
    "trend_200": FactorSpec("trend_200", "200일선 대비 위치", +1, "technical",
                            "종가/200일 이동평균 - 1. 양수면 장기 상승 추세 위에 있음"),
    "rsi_14": FactorSpec("rsi_14", "RSI(14)", -1, "technical",
                         "단기 과열 정도. 너무 높으면 단기 조정 가능성이 있어 감점"),
    "vol_60": FactorSpec("vol_60", "60일 변동성", -1, "risk", "가격 출렁임. 낮을수록 안정적"),
    "earnings_yield": FactorSpec("earnings_yield", "이익수익률(1/PER)", +1, "fundamental",
                                 "주가 대비 이익. 높을수록 이익 대비 저평가"),
    "book_yield": FactorSpec("book_yield", "순자산수익률(1/PBR)", +1, "fundamental",
                             "주가 대비 순자산. 높을수록 자산 대비 저평가"),
    "roe": FactorSpec("roe", "ROE(자기자본이익률)", +1, "fundamental",
                      "자기자본으로 낸 순이익 비율. 높을수록 수익성이 좋음"),
    "liquidity": FactorSpec("liquidity", "20일 평균 거래대금(원화 환산)", +1, "liquidity",
                            "거래가 활발할수록 원하는 가격에 사고팔기 쉬움(필터 용도)"),
}


def compute_factor_table(
    panel: dict[str, pd.DataFrame],
    fundamentals: dict[str, pd.DataFrame] | None,
    asof: pd.Timestamp,
) -> pd.DataFrame:
    """특정 날짜(asof) 기준 종목별 팩터 테이블.

    입력: 패널(날짜×종목), 시점정합된 재무 패널, 기준일
    출력: DataFrame(index=ticker, columns=팩터명 + price, suspended)
    asof 이후 데이터는 잘라내고 계산하므로 미래 정보가 섞이지 않는다.
    """
    close = panel["close"].loc[:asof].ffill()          # 다중 시장: 휴장일은 직전 종가
    value = panel.get("value_base", panel.get("value", panel["close"] * panel["volume"])).loc[:asof]
    last = close.iloc[-1]
    t = pd.DataFrame(index=close.columns)
    t["price"] = last                                   # 현지통화
    fx = panel["fx"].loc[:asof].ffill().iloc[-1] if "fx" in panel else 1.0
    t["price_base"] = last * fx                         # 원화 환산(필터·비교용)
    t["market"] = [_market(x) for x in close.columns]
    t["mom_12_1"] = ind.momentum(close, 252, 21).iloc[-1]
    t["mom_3"] = (close.iloc[-1] / close.shift(63).iloc[-1]) - 1
    t["trend_200"] = last / ind.sma(close, 200).iloc[-1] - 1
    t["rsi_14"] = ind.rsi(close, 14).iloc[-1]
    t["vol_60"] = ind.volatility(close, 60).iloc[-1]
    t["liquidity"] = value.rolling(20, min_periods=10).mean().iloc[-1]
    vol = panel["volume"].loc[:asof]
    last_trading = vol.apply(lambda s: s.dropna().iloc[-1] if s.notna().any() else 0)
    t["suspended"] = last_trading == 0                  # 마지막 거래일 거래량 0(휴장일과 구분)

    if fundamentals:
        # 종목 순서를 팩터 테이블에 맞춰 정렬(위치 기반 대입 오류 방지), 휴장일 결측은 직전 값
        f = {k: (v.loc[:asof].ffill().iloc[-1] if len(v.loc[:asof]) else pd.Series(dtype=float)).reindex(t.index)
             for k, v in fundamentals.items()}
        if "PER" in f:
            per = f["PER"]
            t["earnings_yield"] = np.where(per > 0, 1 / per, np.nan)  # 적자(PER≤0) → 결측
        if "PBR" in f:
            pbr = f["PBR"]
            t["book_yield"] = np.where(pbr > 0, 1 / pbr, np.nan)
        if "ROE" in f:
            t["roe"] = f["ROE"]
    return t
