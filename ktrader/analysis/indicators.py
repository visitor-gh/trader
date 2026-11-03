"""기술적 지표. 모든 함수는 Series 또는 DataFrame(날짜×종목)을 받아 같은 모양으로 돌려준다.
t일 값은 t일 종가까지의 정보만 사용한다(미래 정보 미사용).
"""
from __future__ import annotations

import pandas as pd


def sma(x, n: int):
    """단순이동평균: 최근 n일 종가의 산술평균."""
    return x.rolling(n, min_periods=n).mean()


def ema(x, n: int):
    """지수이동평균: 최근 값에 더 큰 가중치를 주는 평균."""
    return x.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close, n: int = 14):
    """상대강도지수(RSI, 0~100): 최근 상승폭 / (상승폭+하락폭).
    70 이상 과매수(단기 과열), 30 이하 과매도(단기 과도 하락)로 해석한다. Wilder 평활 방식.
    """
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / down
    out = 100 - 100 / (1 + rs)
    return out.where(down != 0, 100.0).where(up.notna())


def macd(close, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD(이동평균 수렴·확산): 단기 EMA - 장기 EMA. 시그널선 상향 돌파 = 추세 강화 신호."""
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig


def bollinger(close, n: int = 20, k: float = 2.0):
    """볼린저밴드: 이동평균 ± k×표준편차. %B = (종가-하단)/(상단-하단)."""
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std()
    upper, lower = mid + k * sd, mid - k * sd
    return upper, mid, lower, (close - lower) / (upper - lower)


def atr(high, low, close, n: int = 14):
    """평균진폭(ATR): 하루 가격 변동 폭의 평균. 손절 거리·포지션 크기 산정에 사용."""
    prev = close.shift(1)
    tr1, tr2, tr3 = high - low, (high - prev).abs(), (low - prev).abs()
    # 진폭(True Range) = max(고-저, |고-전일종가|, |저-전일종가|). NaN은 비교에서 제외
    tr = tr1.where(tr1 >= tr2.fillna(-1), tr2)
    tr = tr.where(tr >= tr3.fillna(-1), tr3)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def momentum(close, lookback: int = 252, skip: int = 21):
    """모멘텀: (skip일 전 가격 / lookback일 전 가격) - 1.
    최근 1개월(skip)을 제외하는 이유: 단기 반전 효과(최근 급등주 되돌림)를 피하기 위함.
    """
    return close.shift(skip) / close.shift(lookback) - 1


def volatility(close, n: int = 60, annualize: bool = True):
    """변동성: 일간 수익률의 표준편차(연율화 시 √252 곱)."""
    v = close.pct_change().rolling(n, min_periods=n).std()
    return v * (252**0.5) if annualize else v
