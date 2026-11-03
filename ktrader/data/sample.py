"""오프라인 데모·테스트용 합성 데이터 생성기.

실제 시장 데이터가 아니다. 네트워크 없이 전체 파이프라인을 검증하기 위한 용도로만 사용한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def make_sample_panel(n_tickers: int = 40, n_days: int = 756, seed: int = 7, start: str = "2023-01-02"):
    """출력: (panel dict[field → DataFrame(날짜×종목)], fundamentals DataFrame(날짜×종목) dict, names)"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n_days)
    tickers = [f"{100000 + i * 10:06d}" for i in range(n_tickers)]

    market = rng.normal(0.0003, 0.011, n_days)
    drift = rng.normal(0.0002, 0.0006, n_tickers)          # 종목 고유 추세(모멘텀 원천)
    beta = rng.uniform(0.6, 1.4, n_tickers)
    idio_vol = rng.uniform(0.01, 0.03, n_tickers)
    rets = market[:, None] * beta + drift + rng.normal(0, 1, (n_days, n_tickers)) * idio_vol
    rets = np.clip(rets, -0.29, 0.29)

    start_px = rng.choice([3_000, 12_000, 45_000, 80_000, 250_000], n_tickers)
    close = start_px * np.exp(np.cumsum(np.log1p(rets), axis=0))
    gap = rng.normal(0, 0.004, (n_days, n_tickers))
    open_ = np.r_[close[:1], close[:-1]] * (1 + gap)
    spread = np.abs(rng.normal(0, 0.008, (n_days, n_tickers)))
    high = np.maximum(open_, close) * (1 + spread)
    low = np.minimum(open_, close) * (1 - spread)
    volume = (rng.lognormal(11, 0.6, (n_days, n_tickers)) * (1 + 3 * np.abs(rets))).round()

    mk = lambda a: pd.DataFrame(a, index=dates, columns=tickers)  # noqa: E731
    panel = {
        "open": mk(open_), "high": mk(high), "low": mk(low), "close": mk(close),
        "volume": mk(volume),
    }
    panel["value"] = panel["close"] * panel["volume"]

    # 밸류에이션: 추세와 약하게 반대(가치·모멘텀 상충 재현)
    pbr0 = np.exp(rng.normal(0, 0.5, n_tickers) - drift * 500)
    roe = np.clip(rng.normal(0.09, 0.06, n_tickers) + drift * 100, -0.2, 0.4)
    bps = start_px / pbr0
    pbr = mk(close / bps)
    per = pbr / np.where(roe > 0, roe, np.nan)
    fundamentals = {"PBR": pbr, "PER": per.fillna(0), "ROE": mk(np.tile(roe, (n_days, 1)))}
    names = {t: f"샘플종목{i:02d}" for i, t in enumerate(tickers)}
    return panel, fundamentals, names


def inject_errors(df: pd.DataFrame) -> pd.DataFrame:
    """검증 로직 시연용: 대표적 데이터 오류를 의도적으로 주입."""
    bad = df.copy()
    bad.iloc[10, bad.columns.get_loc("high")] = bad.iloc[10]["low"] * 0.9   # OHLC 불일치
    px = [bad.columns.get_loc(c) for c in ("open", "high", "low", "close")]
    bad.iloc[20:, px] *= 0.5                                                  # 수정주가 미반영(2:1 분할)
    bad.iloc[30, bad.columns.get_loc("volume")] = 0                           # 거래정지 의심
    bad = pd.concat([bad, bad.iloc[[5]]]).sort_index()                         # 중복 날짜
    return bad
