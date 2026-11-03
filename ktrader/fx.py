"""환율(FX) 처리.

규칙
  - 기준통화(base)는 원화(KRW). 모든 평가금액·손익·리스크 한도는 원화로 환산해 비교한다.
  - 환율은 '외화 1단위당 원화'(예: USD 1,380.5, JPY 9.21, HKD 177.4)로 저장한다.
    ※ 일부 소스는 엔화를 100엔당으로 고시하므로 수집 시 단위를 반드시 맞춘다.
  - t일 평가에는 t일(또는 그 이전 최신) 환율만 사용한다(미래 환율 사용 금지).
  - 원화 기준 수익률 = (1 + 현지통화 수익률) × (1 + 환율 변화율) − 1
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .markets import parse_symbol

BASE = "KRW"

# 수집용 심볼(야후 파이낸스 / FinanceDataReader)
FX_SYMBOLS = {
    "USD": {"yfinance": "KRW=X", "fdr": "USD/KRW", "per": 1},
    "JPY": {"yfinance": "JPYKRW=X", "fdr": "JPY/KRW", "per": 1},
    "HKD": {"yfinance": "HKDKRW=X", "fdr": "HKD/KRW", "per": 1},
}


@dataclass
class FxRates:
    rates: dict[str, pd.Series] = field(default_factory=dict)   # 통화 → 날짜별 원화 환율
    base: str = BASE

    def __post_init__(self):
        for c, s in self.rates.items():
            s = s.dropna().sort_index()
            if (s <= 0).any():
                raise ValueError(f"{c} 환율에 0 이하 값이 있습니다.")
            self.rates[c] = s

    def rate(self, ccy: str, asof=None) -> float:
        if ccy == self.base:
            return 1.0
        if ccy not in self.rates:
            raise KeyError(f"{ccy}/{self.base} 환율이 없습니다.")
        s = self.rates[ccy]
        if asof is not None:
            s = s.loc[: pd.Timestamp(asof)]
            if s.empty:
                raise KeyError(f"{asof} 이전 {ccy} 환율이 없습니다.")
        return float(s.iloc[-1])

    def to_base(self, amount: float, ccy: str, asof=None) -> float:
        return amount * self.rate(ccy, asof)

    def series(self, ccy: str, index: pd.DatetimeIndex) -> pd.Series:
        """거래일 index에 맞춘 환율. 환율 휴일은 직전 값(과거 방향으로만 채움)."""
        if ccy == self.base:
            return pd.Series(1.0, index=index)
        s = self.rates[ccy]
        return s.reindex(s.index.union(index)).ffill().reindex(index)

    def multiplier_panel(self, tickers, index: pd.DatetimeIndex) -> pd.DataFrame:
        """날짜×종목 환산계수(해당 종목 통화의 원화 환율)."""
        cache = {}
        cols = {}
        for t in tickers:
            ccy = parse_symbol(t).currency
            if ccy not in cache:
                cache[ccy] = self.series(ccy, index)
            cols[t] = cache[ccy]
        return pd.DataFrame(cols, index=index)


def attach_fx(panel: dict[str, pd.DataFrame], fx: FxRates) -> dict[str, pd.DataFrame]:
    """패널에 'fx'(환산계수)와 'value_base'(원화 환산 거래대금)를 추가.
    이후 팩터·스크리너·백테스트·리스크는 이 두 필드로 시장 간 금액을 비교한다.
    """
    close = panel["close"]
    out = dict(panel)
    out["fx"] = fx.multiplier_panel(close.columns, close.index)
    value = panel.get("value", close * panel["volume"])
    out["value_base"] = value * out["fx"]
    return out


def decompose_return(local_price: pd.Series, fx_rate: pd.Series) -> pd.DataFrame:
    """해외 자산의 원화 수익률을 '주가 효과'와 '환율 효과'로 분해.
    출력: 누적 local / fx / cross(교차항) / total
    """
    idx = local_price.dropna().index
    fxr = fx_rate.reindex(fx_rate.index.union(idx)).ffill().reindex(idx)
    r_l = local_price.loc[idx].iloc[-1] / local_price.loc[idx].iloc[0] - 1
    r_f = fxr.iloc[-1] / fxr.iloc[0] - 1
    return pd.DataFrame({"주가효과": [r_l], "환율효과": [r_f], "교차항": [r_l * r_f],
                         "원화기준합계": [(1 + r_l) * (1 + r_f) - 1]})
