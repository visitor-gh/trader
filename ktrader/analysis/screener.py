"""종목 선별(스크리닝)과 다중 팩터 점수화.

절차
  1) 투자 부적격 종목 제외(필터) — 사유를 기록해 사용자에게 설명
  2) 팩터별 극단값 제거(윈저라이징) → 표준화(z-score) → 방향 반영
  3) 가중합 = 종합점수, 팩터별 기여도(가중치×z) 보존 → 설명 가능성 확보
  4) 상위 N개 추천
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..data.preprocess import winsorize, zscore
from .factors import FACTOR_SPECS

DEFAULT_WEIGHTS = {
    "mom_12_1": 0.25, "trend_200": 0.10, "rsi_14": 0.05, "vol_60": 0.10,
    "earnings_yield": 0.20, "book_yield": 0.15, "roe": 0.15,
}


@dataclass
class ScreenConfig:
    min_liquidity: float = 1_000_000_000   # 20일 평균 거래대금 10억원 이상
    min_price: float = 1_000               # 동전주 제외
    exclude_suspended: bool = True
    restricted: set[str] = field(default_factory=set)  # 관리종목·투자경고/위험·정리매매 등
    min_factor_coverage: float = 0.7       # 팩터 값의 70% 이상 존재해야 점수 산출


@dataclass
class ScreenResult:
    scores: pd.DataFrame               # index=ticker: composite, rank, z_*, contrib_*
    excluded: pd.Series                # ticker → 제외 사유
    weights: dict[str, float]

    def top(self, n: int = 10) -> pd.DataFrame:
        return self.scores.sort_values("composite", ascending=False).head(n)


def apply_filters(table: pd.DataFrame, cfg: ScreenConfig) -> tuple[pd.DataFrame, pd.Series]:
    reasons = pd.Series("", index=table.index, dtype=object)

    def mark(mask, why):
        mask = mask.fillna(False) & (reasons == "")
        reasons[mask] = why

    mark(table.index.to_series().isin(cfg.restricted), "투자유의·관리 지정 종목")
    if cfg.exclude_suspended and "suspended" in table:
        mark(table["suspended"].astype(bool), "거래정지(최근 거래량 0)")
    mark(table["price"] < cfg.min_price, f"주가 {cfg.min_price:,.0f}원 미만")
    if "liquidity" in table:
        mark(table["liquidity"] < cfg.min_liquidity, f"평균 거래대금 {cfg.min_liquidity / 1e8:,.0f}억원 미만")
    return table[reasons == ""], reasons[reasons != ""]


def score(table: pd.DataFrame, weights: dict[str, float] | None = None, cfg: ScreenConfig | None = None) -> ScreenResult:
    """입력: compute_factor_table 결과
    출력: ScreenResult(composite 내림차순 rank 포함)
    """
    cfg = cfg or ScreenConfig()
    weights = {k: v for k, v in (weights or DEFAULT_WEIGHTS).items() if k in table.columns}
    if not weights:
        raise ValueError("사용 가능한 팩터가 없습니다.")
    passed, excluded = apply_filters(table, cfg)

    cols = list(weights)
    coverage = passed[cols].notna().mean(axis=1)
    low_cov = coverage < cfg.min_factor_coverage
    excluded = pd.concat([excluded, pd.Series("팩터 데이터 부족", index=passed.index[low_cov])])
    passed = passed[~low_cov]

    out = pd.DataFrame(index=passed.index)
    total_w = sum(abs(w) for w in weights.values())
    for f, w in weights.items():
        spec = FACTOR_SPECS[f]
        z = zscore(winsorize(passed[f].astype(float))) * spec.direction
        out[f"z_{f}"] = z
        # 결측 팩터는 0점(평균) 처리 → 결측이 유리하거나 불리하게 작용하지 않음
        out[f"contrib_{f}"] = z.fillna(0) * (w / total_w)
    out["composite"] = out[[c for c in out if c.startswith("contrib_")]].sum(axis=1)
    out["rank"] = out["composite"].rank(ascending=False, method="first").astype(int)
    for c in ("price", "liquidity"):
        if c in passed:
            out[c] = passed[c]
    return ScreenResult(out.sort_values("rank"), excluded, weights)


def information_coefficient(factor: pd.Series, fwd_return: pd.Series) -> float:
    """정보계수(IC): 팩터 순위와 이후 수익률 순위의 상관계수(스피어만).
    0.02~0.05 이상이 꾸준히 유지되면 실용적으로 유의미한 팩터로 본다.
    """
    df = pd.concat([factor, fwd_return], axis=1).dropna()
    if len(df) < 5:
        return np.nan
    return float(df.iloc[:, 0].rank().corr(df.iloc[:, 1].rank()))
