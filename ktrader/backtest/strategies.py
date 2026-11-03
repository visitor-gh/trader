"""대표 전략 구현. 모두 Strategy 프로토콜(generate_weights)을 따른다."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..analysis import indicators as ind
from ..analysis.factors import compute_factor_table
from ..analysis.screener import DEFAULT_WEIGHTS, ScreenConfig, score


def _liquid(panel, asof, min_value: float) -> pd.Index:
    val = panel.get("value_base", panel.get("value", panel["close"] * panel["volume"]))  # 원화 환산 우선
    avg = val.loc[:asof].tail(20).mean()
    vol = panel["volume"].loc[:asof].tail(5)
    alive = vol.max() > 0          # 최근 5거래일 중 거래 있음(시장별 휴장일과 거래정지 구분)
    return avg[(avg >= min_value) & alive].index


@dataclass
class MomentumStrategy:
    """횡단면 모멘텀: 12-1개월 수익률 상위 N종목 동일가중.
    시장 필터(절대 모멘텀): 유니버스 평균 지수가 200일선 아래면 현금 보유 → 하락장 손실 완화.
    """

    lookback: int = 252
    skip: int = 21
    top_n: int = 10
    market_filter: bool = True
    min_value: float = 1e9
    name: str = "모멘텀"

    def generate_weights(self, asof, panel, fundamentals=None) -> pd.Series:
        close = panel["close"].ffill()
        if self.market_filter:
            idx = (close / close.iloc[0]).mean(axis=1)
            if idx.iloc[-1] < ind.sma(idx, 200).iloc[-1]:
                return pd.Series(dtype=float)
        mom = ind.momentum(close, self.lookback, self.skip).iloc[-1]
        mom = mom.reindex(_liquid(panel, asof, self.min_value)).dropna()
        picks = mom[mom > 0].nlargest(self.top_n).index
        return pd.Series(1 / self.top_n, index=picks)


@dataclass
class ValueStrategy:
    """가치 전략: 1/PBR + 1/PER 순위 합산 상위 N종목. 적자 기업(PER ≤ 0) 제외."""

    top_n: int = 10
    min_value: float = 1e9
    name: str = "가치"

    def generate_weights(self, asof, panel, fundamentals=None) -> pd.Series:
        if not fundamentals:
            raise ValueError("가치 전략에는 시점정합된 PER/PBR 데이터가 필요합니다.")
        pbr, per = fundamentals["PBR"].iloc[-1], fundamentals["PER"].iloc[-1]
        df = pd.DataFrame({"by": 1 / pbr.where(pbr > 0), "ey": 1 / per.where(per > 0)})
        df = df.reindex(_liquid(panel, asof, self.min_value)).dropna()
        rank = df.rank().sum(axis=1)
        picks = rank.nlargest(self.top_n).index
        return pd.Series(1 / self.top_n, index=picks)


@dataclass
class RsiMeanReversion:
    """단기 역추세(트레이딩) 전략: 장기 상승 추세(200일선 위) 종목 중 RSI(2)가 과매도인 종목 매수.
    매일 리밸런싱으로 사용하며, 반등(RSI 회복) 시 목표 비중에서 빠지며 자동 청산된다.
    """

    rsi_n: int = 2
    entry: float = 10.0
    max_names: int = 10
    min_value: float = 1e9
    name: str = "RSI 역추세"

    def generate_weights(self, asof, panel, fundamentals=None) -> pd.Series:
        close = panel["close"].ffill()
        r = ind.rsi(close, self.rsi_n).iloc[-1]
        up = close.iloc[-1] > ind.sma(close, 200).iloc[-1]
        cand = r[(r < self.entry) & up].reindex(_liquid(panel, asof, self.min_value)).dropna()
        picks = cand.nsmallest(self.max_names).index
        return pd.Series(1 / self.max_names, index=picks)


@dataclass
class MultiFactorStrategy:
    """다중 팩터 전략: screener.score 종합점수 상위 N종목."""

    top_n: int = 15
    weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    screen: ScreenConfig = field(default_factory=ScreenConfig)
    name: str = "멀티팩터"

    def generate_weights(self, asof, panel, fundamentals=None) -> pd.Series:
        table = compute_factor_table(panel, fundamentals, asof)
        res = score(table, self.weights, self.screen)
        picks = res.top(self.top_n).index
        return pd.Series(1 / self.top_n, index=picks)


@dataclass
class BuyAndHoldEqual:
    """비교 기준: 유니버스 전체 동일가중 매수 보유(월 리밸런싱)."""

    name: str = "동일가중 벤치마크"

    def generate_weights(self, asof, panel, fundamentals=None) -> pd.Series:
        alive = panel["close"].loc[:asof].ffill().iloc[-1].dropna().index
        return pd.Series(1 / len(alive), index=alive)
