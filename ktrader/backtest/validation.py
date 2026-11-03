"""과최적화(overfitting) 방지 검증: 워크포워드 최적화와 파라미터 안정성 점검.

과최적화: 과거 데이터에만 우연히 잘 맞도록 파라미터를 고른 결과, 실전에서 성과가 무너지는 현상.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Callable

import pandas as pd

from .engine import Backtester, BacktestConfig
from .metrics import compute_metrics


@dataclass
class WalkForwardWindow:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_end: pd.Timestamp
    best_params: dict
    train_sharpe: float
    test_sharpe: float
    test_return: float


def grid(param_grid: dict[str, list]) -> list[dict]:
    keys = list(param_grid)
    return [dict(zip(keys, vals)) for vals in itertools.product(*param_grid.values())]


def walk_forward_optimize(panel, make_strategy: Callable[..., object], param_grid: dict[str, list],
                          cfg: BacktestConfig, fundamentals=None, train_days: int = 252,
                          test_days: int = 126) -> list[WalkForwardWindow]:
    """학습 구간에서 샤프지수 최고 파라미터 선택 → 바로 다음 검증 구간에 적용(롤링 반복).
    검증 구간 성과의 연결만이 '실전 기대 성과'에 가깝다.
    """
    dates = panel["close"].index
    out = []
    start = cfg.warmup_days
    while start + train_days + test_days <= len(dates):
        tr_s, tr_e = dates[start], dates[start + train_days - 1]
        te_e = dates[start + train_days + test_days - 1]
        best, best_sh = None, -1e9
        for p in grid(param_grid):
            res = Backtester(cfg).run(panel, make_strategy(**p), fundamentals, start=str(tr_s.date()), end=str(tr_e.date()))
            sh = compute_metrics(res)["sharpe"]
            if pd.notna(sh) and sh > best_sh:
                best, best_sh = p, sh
        best = best or grid(param_grid)[0]
        test_cfg = BacktestConfig(**{**cfg.__dict__, "warmup_days": 0})
        res = Backtester(test_cfg).run(panel, make_strategy(**best), fundamentals,
                                       start=str(dates[start + train_days].date()), end=str(te_e.date()))
        m = compute_metrics(res)
        out.append(WalkForwardWindow(tr_s, tr_e, te_e, best, best_sh, m["sharpe"], m["total_return"]))
        start += test_days
    return out


def parameter_stability(panel, make_strategy, param_grid, cfg, fundamentals=None) -> pd.DataFrame:
    """파라미터 조합별 성과표. 최고점 주변 조합들도 비슷하게 좋아야(고원 형태) 견고한 전략이다."""
    rows = []
    for p in grid(param_grid):
        m = compute_metrics(Backtester(cfg).run(panel, make_strategy(**p), fundamentals))
        rows.append({**p, "sharpe": m["sharpe"], "cagr": m["cagr"], "mdd": m["mdd"]})
    return pd.DataFrame(rows).sort_values("sharpe", ascending=False)
