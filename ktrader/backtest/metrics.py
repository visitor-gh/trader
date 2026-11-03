"""성과 지표. 모든 지표에 한글 설명을 붙여 리포트에서 그대로 쓴다."""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252

METRIC_LABELS = {
    "total_return": ("누적수익률", "기간 전체 수익률"),
    "cagr": ("연환산수익률(CAGR)", "매년 같은 비율로 성장했다고 가정한 연평균 수익률"),
    "ann_vol": ("연변동성", "수익률의 출렁임 크기(연 기준). 클수록 위험"),
    "sharpe": ("샤프지수", "위험 1단위당 초과수익. 1 이상이면 양호, 2 이상이면 우수"),
    "sortino": ("소르티노지수", "하락 변동성만 위험으로 본 샤프지수"),
    "mdd": ("최대낙폭(MDD)", "고점 대비 가장 크게 떨어졌던 비율. 실제로 견뎌야 할 최악의 손실"),
    "mdd_days": ("최대낙폭 회복기간(일)", "고점을 회복하기까지 걸린 가장 긴 거래일 수"),
    "calmar": ("칼마지수", "CAGR / |MDD|. 손실 대비 수익 효율"),
    "win_rate": ("승률", "청산된 거래 중 이익 거래 비율"),
    "profit_factor": ("손익비(PF)", "총이익 / 총손실. 1보다 커야 수익 구조"),
    "turnover": ("연회전율", "1년 동안 포트폴리오를 몇 번 교체했는지. 높을수록 비용 증가"),
    "total_cost": ("총거래비용(원)", "수수료+세금 합계(슬리피지는 체결가에 반영)"),
    "n_trades": ("거래횟수", "체결된 주문 수"),
    "alpha": ("알파(연)", "벤치마크로 설명되지 않는 초과수익"),
    "beta": ("베타", "벤치마크 대비 민감도. 1이면 시장과 같은 정도로 움직임"),
}


def drawdown(equity: pd.Series) -> pd.Series:
    return equity / equity.cummax() - 1


def max_drawdown_duration(equity: pd.Series) -> int:
    underwater = equity < equity.cummax()
    runs = underwater.groupby((~underwater).cumsum()).cumsum()
    return int(runs.max()) if len(runs) else 0


def trade_pnl(trades: pd.DataFrame) -> pd.Series:
    """선입선출(FIFO) 아닌 평균단가 방식으로 매도 거래별 실현손익 계산."""
    pnl, held, avg = [], {}, {}
    for r in trades.sort_values("date").itertuples():
        if r.side == "buy":
            q0 = held.get(r.ticker, 0)
            avg[r.ticker] = (avg.get(r.ticker, 0) * q0 + r.price * r.qty + r.cost) / (q0 + r.qty)
            held[r.ticker] = q0 + r.qty
        else:
            pnl.append((r.price - avg.get(r.ticker, r.price)) * r.qty - r.cost)
            held[r.ticker] = held.get(r.ticker, 0) - r.qty
    return pd.Series(pnl, dtype=float)


def compute_metrics(result, benchmark: pd.Series | None = None, risk_free: float = 0.03) -> dict:
    """입력: BacktestResult, 벤치마크 가격(선택), 무위험수익률(연, 예: CD91일물 금리)
    출력: dict[지표명 → 값]
    """
    eq = result.equity
    r = eq.pct_change().dropna()
    years = max(len(r) / TRADING_DAYS, 1e-9)
    rf_d = risk_free / TRADING_DAYS
    ex = r - rf_d
    downside = r[r < 0].std() * np.sqrt(TRADING_DAYS)
    dd = drawdown(eq)
    m = {
        "total_return": eq.iloc[-1] / eq.iloc[0] - 1,
        "cagr": (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1,
        "ann_vol": r.std() * np.sqrt(TRADING_DAYS),
        "sharpe": ex.mean() / r.std() * np.sqrt(TRADING_DAYS) if r.std() > 0 else np.nan,
        "sortino": (r.mean() * TRADING_DAYS - risk_free) / downside if downside > 0 else np.nan,
        "mdd": dd.min(),
        "mdd_days": max_drawdown_duration(eq),
    }
    m["calmar"] = m["cagr"] / abs(m["mdd"]) if m["mdd"] < 0 else np.nan
    t = result.trades
    pnl = trade_pnl(t) if len(t) else pd.Series(dtype=float)
    m["win_rate"] = float((pnl > 0).mean()) if len(pnl) else np.nan
    loss = -pnl[pnl < 0].sum()
    m["profit_factor"] = float(pnl[pnl > 0].sum() / loss) if loss > 0 else np.nan
    traded = (t["qty"] * t["price"]).sum() if len(t) else 0.0
    m["turnover"] = traded / 2 / eq.mean() / years
    m["total_cost"] = float(t["cost"].sum()) if len(t) else 0.0
    m["n_trades"] = len(t)
    if benchmark is not None:
        b = benchmark.reindex(eq.index).ffill().pct_change().dropna()
        a = r.reindex(b.index).dropna()
        b = b.reindex(a.index)
        if len(a) > 20 and b.var() > 0:
            beta = a.cov(b) / b.var()
            m["beta"] = beta
            m["alpha"] = (a.mean() - beta * b.mean()) * TRADING_DAYS
    return m


def format_metrics(m: dict) -> pd.DataFrame:
    pct = {"total_return", "cagr", "ann_vol", "mdd", "win_rate", "alpha"}
    rows = []
    for k, v in m.items():
        label, desc = METRIC_LABELS.get(k, (k, ""))
        if v is None or (isinstance(v, float) and not np.isfinite(v)):
            s = "-"
        elif k in pct:
            s = f"{v:.2%}"
        elif k in {"total_cost"}:
            s = f"{v:,.0f}"
        elif k in {"n_trades", "mdd_days"}:
            s = f"{int(v):,}"
        else:
            s = f"{v:.2f}"
        rows.append({"지표": label, "값": s, "설명": desc})
    return pd.DataFrame(rows)


def monthly_returns(equity: pd.Series) -> pd.DataFrame:
    m = equity.resample("ME").last().pct_change().dropna()
    return m.groupby([m.index.year, m.index.month]).first().unstack()


def pnl_attribution(result, panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """시장별 손익 기여도(원화)를 '주가 효과'와 '환율 효과'로 분해.

    보유수량 q(t-1)에 대해
      주가효과 = q × (P_t − P_{t−1}) × FX_{t−1}
      환율효과 = q × P_t × (FX_t − FX_{t−1})
    체결일 장중 손익과 비용은 포함하지 않는 근사치이며, 차이는 '거래·비용 등'으로 표시한다.
    """
    from ..markets import parse_symbol

    pos = result.positions
    if pos.empty:
        return pd.DataFrame()
    cols = pos.columns
    idx = pos.index
    px = panel["close"].ffill().reindex(idx)[cols]
    fx = panel["fx"].ffill().reindex(idx)[cols] if "fx" in panel else pd.DataFrame(1.0, index=idx, columns=cols)
    q = pos.shift(1).fillna(0)
    local = q * px.diff() * fx.shift(1)
    fxe = q * px * fx.diff()

    def mkt(t):
        try:
            return parse_symbol(t).market
        except ValueError:
            return "KR"

    m = {t: mkt(t) for t in cols}
    out = pd.DataFrame({
        "주가효과": local.sum().groupby(m).sum(),
        "환율효과": fxe.sum().groupby(m).sum(),
    })
    out["합계"] = out.sum(axis=1)
    total = result.equity.iloc[-1] - result.equity.iloc[0]
    out.loc["거래·비용 등"] = [np.nan, np.nan, total - out["합계"].sum()]
    return out
