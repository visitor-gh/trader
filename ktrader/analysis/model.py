"""머신러닝 기반 종목 순위 모델과 '알고리즘 선정' 절차.

핵심 원칙
  - 예측 대상: 향후 h일 수익률의 '횡단면 순위'(종목 간 상대 순위). 절대 수익률보다 잡음이 적다.
  - 검증: 시간 순서를 지키는 워크포워드(walk-forward) 방식. 학습 구간과 검증 구간 사이에
    h일의 공백(embargo, 엠바고)을 두어 라벨 기간이 겹치며 생기는 미래 정보 누수를 막는다.
  - 선정 기준: 표본 외(out-of-sample) IC 평균과 IC 정보비율(ICIR = 평균/표준편차).
    단순 규칙(동일가중 팩터) 대비 ICIR이 확실히 높지 않으면 복잡한 모델을 채택하지 않는다.

외부 의존성 없이 numpy 릿지(ridge) 회귀로 구현했다. LightGBM·XGBoost로 교체할 때는
fit/predict 인터페이스만 맞추면 동일한 검증 절차를 그대로 쓸 수 있다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

import numpy as np
import pandas as pd

from .factors import compute_factor_table


class RankModel(Protocol):
    def fit(self, X: np.ndarray, y: np.ndarray) -> "RankModel": ...
    def predict(self, X: np.ndarray) -> np.ndarray: ...


class RidgeRank:
    def __init__(self, alpha: float = 1.0):
        self.alpha = alpha
        self.coef_: np.ndarray | None = None

    def fit(self, X, y):
        Xb = np.c_[np.ones(len(X)), X]
        reg = self.alpha * np.eye(Xb.shape[1])
        reg[0, 0] = 0
        self.coef_ = np.linalg.solve(Xb.T @ Xb + reg, Xb.T @ y)
        return self

    def predict(self, X):
        return np.c_[np.ones(len(X)), X] @ self.coef_


class EqualWeight:
    """비교 기준(baseline): 모든 팩터 순위의 단순 평균."""

    def fit(self, X, y):
        return self

    def predict(self, X):
        return X.mean(axis=1)


def _cs_rank(df: pd.DataFrame) -> pd.DataFrame:
    """횡단면 순위를 -0.5~0.5로 정규화(날짜별로 종목 간 비교 가능하게)."""
    return df.rank(pct=True) - 0.5


def build_dataset(panel, fundamentals, dates, features: list[str], horizon: int = 21) -> pd.DataFrame:
    """출력: MultiIndex(date, ticker) 데이터셋. 컬럼 = 피처 순위 + 'target'(향후 수익률 순위)"""
    close = panel["close"]
    fwd = close.shift(-horizon) / close - 1
    rows = []
    for d in dates:
        t = compute_factor_table(panel, fundamentals, d)
        X = _cs_rank(t[features]).fillna(0)
        X["target"] = _cs_rank(fwd.loc[d].to_frame()).iloc[:, 0]
        X["date"] = d
        rows.append(X.dropna(subset=["target"]))
    return pd.concat(rows).set_index("date", append=True).swaplevel()


@dataclass
class WalkForwardReport:
    ic_by_date: pd.Series

    @property
    def mean_ic(self) -> float:
        return float(self.ic_by_date.mean())

    @property
    def icir(self) -> float:
        sd = self.ic_by_date.std()
        return float(self.mean_ic / sd) if sd and np.isfinite(sd) else float("nan")

    @property
    def hit_ratio(self) -> float:
        return float((self.ic_by_date > 0).mean())


def walk_forward(data: pd.DataFrame, model_factory: Callable[[], RankModel], train_periods: int = 12,
                 embargo_periods: int = 1) -> WalkForwardReport:
    """날짜 단위 확장형(expanding) 워크포워드. 각 시점 d에서 [과거 ~ d-embargo-1]로 학습 → d 예측."""
    dates = data.index.get_level_values(0).unique().sort_values()
    feats = [c for c in data.columns if c != "target"]
    ics = {}
    for i in range(train_periods + embargo_periods, len(dates)):
        train_dates = dates[: i - embargo_periods]
        tr = data.loc[train_dates]
        te = data.loc[dates[i]]
        m = model_factory().fit(tr[feats].values, tr["target"].values)
        pred = pd.Series(m.predict(te[feats].values), index=te.index)
        ics[dates[i]] = pred.rank().corr(te["target"].rank())
    return WalkForwardReport(pd.Series(ics))


def select_model(data: pd.DataFrame, candidates: dict[str, Callable[[], RankModel]], **kw) -> pd.DataFrame:
    """후보 모델별 표본 외 성과표. ICIR 기준 내림차순."""
    rows = []
    for name, factory in candidates.items():
        r = walk_forward(data, factory, **kw)
        rows.append({"model": name, "mean_ic": r.mean_ic, "icir": r.icir, "hit_ratio": r.hit_ratio, "n": len(r.ic_by_date)})
    return pd.DataFrame(rows).sort_values("icir", ascending=False).reset_index(drop=True)
