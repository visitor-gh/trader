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


# 시장별 합성 데이터 설정: (종목 접미사 생성 함수, 시작가 후보, 거래량 로그평균, 밸류에이션 수준 보정)
_GLOBAL_SPECS = {
    "KR": (lambda i: f"{200000 + i * 10:06d}", [5_000, 30_000, 80_000, 250_000], 11.0, 0.0),
    "US": (lambda i: f"ZUS{i:02d}", [15, 60, 150, 400], 13.5, 0.8),      # 미국: 고PBR
    "JP": (lambda i: f"{9900 + i}.T", [800, 2_500, 6_000], 13.0, -0.5),  # 일본: 저PBR
    "HK": (lambda i: f"{9800 + i}.HK", [3, 25, 90, 350], 14.0, -0.3),
}
_FX_START = {"USD": 1_350.0, "JPY": 9.2, "HKD": 173.0}


def make_global_sample(per_market: int = 12, n_days: int = 780, seed: int = 11, start: str = "2023-01-02"):
    """다중 시장 합성 데이터. 시장마다 다른 휴장일(NaN 행)과 환율 경로를 갖는다.

    출력: (panel(+fx, value_base), fundamentals, names, FxRates)
    종목 코드는 실제 상장 종목과 겹치지 않도록 가상 코드(ZUS01, 9901.T, 9801.HK 등)를 쓴다.
    """
    from ..fx import FxRates, attach_fx

    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n_days)
    fields = {k: {} for k in ("open", "high", "low", "close", "volume")}
    fund = {"PBR": {}, "PER": {}, "ROE": {}}
    names = {}
    for m, (code, px0, lv, val_shift) in _GLOBAL_SPECS.items():
        mkt_ret = rng.normal(0.0004 if m == "US" else 0.0002, 0.011, n_days)
        holidays = rng.choice(n_days, size=12, replace=False)       # 시장별 고유 휴장일
        for i in range(per_market):
            t = code(i + 1)
            drift = rng.normal(0.0002, 0.0006)
            r = np.clip(mkt_ret * rng.uniform(0.7, 1.3) + drift + rng.normal(0, rng.uniform(0.01, 0.025), n_days), -0.25, 0.25)
            close = rng.choice(px0) * np.exp(np.cumsum(np.log1p(r)))
            open_ = np.r_[close[:1], close[:-1]] * (1 + rng.normal(0, 0.004, n_days))
            spread = np.abs(rng.normal(0, 0.008, n_days))
            vol = rng.lognormal(lv, 0.5, n_days).round()
            arrs = {"open": open_, "high": np.maximum(open_, close) * (1 + spread),
                    "low": np.minimum(open_, close) * (1 - spread), "close": close, "volume": vol}
            for k, a in arrs.items():
                a = a.astype(float).copy()
                a[holidays] = np.nan
                fields[k][t] = a
            pbr0 = np.exp(rng.normal(val_shift, 0.4) - drift * 400)
            roe = float(np.clip(rng.normal(0.10, 0.05) + drift * 80, -0.1, 0.35))
            pbr = close / (close[0] / pbr0)
            fund["PBR"][t] = pbr
            fund["PER"][t] = np.where(roe > 0, pbr / roe, 0.0)
            fund["ROE"][t] = np.full(n_days, roe)
            names[t] = f"가상{m}{i + 1:02d}"
    panel = {k: pd.DataFrame(v, index=dates) for k, v in fields.items()}
    panel["value"] = panel["close"] * panel["volume"]
    fundamentals = {k: pd.DataFrame(v, index=dates) for k, v in fund.items()}
    fx = FxRates({c: pd.Series(s0 * np.exp(np.cumsum(rng.normal(0.00005, 0.004, n_days))), index=dates)
                  for c, s0 in _FX_START.items()})
    return attach_fx(panel, fx), fundamentals, names, fx
