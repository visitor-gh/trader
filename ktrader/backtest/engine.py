"""일 단위 포트폴리오 백테스트 엔진.

현실성 확보 규칙
  1) 미래 정보 차단: 전략은 t일 종가까지의 데이터만 받고, 주문은 t+1일 시가에 체결된다.
  2) 비용: 매수 수수료, 매도 수수료+거래세, 슬리피지(불리한 방향으로 체결가 보정).
  3) 체결 제약: 거래정지(거래량 0) 미체결, 시가 상한가 매수 불가, 시가 하한가 매도 불가,
     일 거래량의 max_participation 비율까지만 체결, 정수 주식 단위.
  4) 리스크 오버레이: 매일 종가 기준 손절/추적손절 점검 → 다음날 시가 청산.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
import pandas as pd

from ..config import CostModel
from ..krx import price_limits


class Strategy(Protocol):
    name: str

    def generate_weights(self, asof: pd.Timestamp, panel: dict[str, pd.DataFrame],
                         fundamentals: dict[str, pd.DataFrame] | None) -> pd.Series:
        """asof 종가까지의 데이터로 목표 비중(합 ≤ 1)을 반환."""
        ...


@dataclass
class BacktestConfig:
    initial_cash: float = 100_000_000
    rebalance: str = "M"            # D(매일) / W(매주 첫 거래일) / M(매월 첫 거래일)
    cost: CostModel = field(default_factory=CostModel)
    max_participation: float = 0.05
    stop_loss_pct: float | None = None
    trailing_stop_pct: float | None = None
    warmup_days: int = 252          # 지표 계산에 필요한 초기 구간(거래 안 함)


@dataclass
class BacktestResult:
    equity: pd.Series
    cash: pd.Series
    positions: pd.DataFrame          # 날짜×종목 보유 수량
    trades: pd.DataFrame             # date, ticker, side, qty, price, cost, reason
    target_weights: dict = field(default_factory=dict)
    strategy_name: str = ""

    @property
    def returns(self) -> pd.Series:
        return self.equity.pct_change().fillna(0.0)


def rebalance_dates(index: pd.DatetimeIndex, freq: str) -> set:
    if freq == "D":
        return set(index)
    key = index.to_period("M" if freq == "M" else "W")
    s = pd.Series(index, index=index)
    return set(s.groupby(key).first())


class Backtester:
    def __init__(self, cfg: BacktestConfig | None = None):
        self.cfg = cfg or BacktestConfig()

    def run(self, panel: dict[str, pd.DataFrame], strategy: Strategy,
            fundamentals: dict[str, pd.DataFrame] | None = None,
            start: str | None = None, end: str | None = None) -> BacktestResult:
        cfg = self.cfg
        o, c, v = panel["open"], panel["close"], panel["volume"]
        dates = c.index
        if start:
            dates = dates[dates >= pd.Timestamp(start)]
        if end:
            dates = dates[dates <= pd.Timestamp(end)]
        all_dates = c.index
        first_idx = max(all_dates.get_loc(dates[0]), cfg.warmup_days)
        trade_dates = all_dates[first_idx:]
        rebal = rebalance_dates(trade_dates, cfg.rebalance)
        slip = cfg.cost.slippage_bps / 10_000

        cash = cfg.initial_cash
        pos: dict[str, int] = {}
        avg_cost: dict[str, float] = {}
        peak: dict[str, float] = {}
        pending: pd.Series | None = None     # 다음날 시가에 실행할 목표 비중
        forced_exit: dict[str, str] = {}     # 다음날 시가 청산 대상과 사유
        eq_hist, cash_hist, pos_hist, trades, targets = {}, {}, {}, [], {}

        for i, d in enumerate(trade_dates):
            prev_close = c.iloc[all_dates.get_loc(d) - 1]
            opens, vols = o.loc[d], v.loc[d]

            def can_trade(t, side):
                if not np.isfinite(opens.get(t, np.nan)) or vols.get(t, 0) <= 0:
                    return False
                lo, hi = price_limits(prev_close[t])
                return not ((side == "buy" and opens[t] >= hi) or (side == "sell" and opens[t] <= lo))

            def execute(t, qty, reason):
                nonlocal cash
                side = "buy" if qty > 0 else "sell"
                cap = int(vols[t] * cfg.max_participation)
                qty = int(np.sign(qty) * min(abs(qty), cap))
                if qty == 0 or not can_trade(t, side):
                    return
                px = opens[t] * (1 + slip if qty > 0 else 1 - slip)
                notional = abs(qty) * px
                if qty > 0:
                    fee = cfg.cost.buy_cost(notional)
                    if notional + fee > cash:  # 현금 부족 → 가능한 수량으로 축소(미수 금지)
                        qty = int(cash / (px * (1 + cfg.cost.commission_rate)))
                        if qty <= 0:
                            return
                        notional, fee = qty * px, cfg.cost.buy_cost(qty * px)
                    cash -= notional + fee
                    held = pos.get(t, 0)
                    avg_cost[t] = (avg_cost.get(t, 0) * held + notional) / (held + qty)
                    peak[t] = max(peak.get(t, 0), px)
                else:
                    fee = cfg.cost.sell_cost(notional)
                    cash += notional - fee
                pos[t] = pos.get(t, 0) + qty
                if pos[t] == 0:
                    pos.pop(t)
                    avg_cost.pop(t, None)
                    peak.pop(t, None)
                trades.append({"date": d, "ticker": t, "side": side, "qty": abs(qty),
                               "price": px, "cost": fee, "reason": reason})

            # 1) 전일 신호에 따른 강제 청산(손절)
            for t, why in list(forced_exit.items()):
                if t in pos:
                    execute(t, -pos[t], why)
            forced_exit.clear()

            # 2) 전일 결정된 리밸런싱 실행: 매도 먼저(현금 확보) → 매수
            if pending is not None:
                equity_open = cash + sum(q * opens.get(t, prev_close[t]) for t, q in pos.items())
                tgt_qty = {t: int(equity_open * w / opens[t]) for t, w in pending.items()
                           if w > 0 and np.isfinite(opens.get(t, np.nan)) and opens[t] > 0}
                deltas = {t: tgt_qty.get(t, 0) - pos.get(t, 0) for t in set(tgt_qty) | set(pos)}
                for t, dq in sorted(deltas.items(), key=lambda x: x[1]):
                    if dq != 0:
                        execute(t, dq, "리밸런싱")
                pending = None

            # 3) 종가 평가
            closes = c.loc[d]
            equity = cash + sum(q * closes[t] for t, q in pos.items())
            eq_hist[d], cash_hist[d], pos_hist[d] = equity, cash, dict(pos)

            # 4) 종가 기준 손절 점검(다음날 시가 실행)
            for t in pos:
                peak[t] = max(peak.get(t, 0), closes[t])
                if cfg.stop_loss_pct and closes[t] <= avg_cost[t] * (1 - cfg.stop_loss_pct):
                    forced_exit[t] = f"손절(-{cfg.stop_loss_pct:.0%})"
                elif cfg.trailing_stop_pct and closes[t] <= peak[t] * (1 - cfg.trailing_stop_pct):
                    forced_exit[t] = f"추적손절(고점대비 -{cfg.trailing_stop_pct:.0%})"

            # 5) 리밸런싱일: 오늘 종가까지의 정보로 목표 비중 산출(실행은 내일)
            if d in rebal and i < len(trade_dates) - 1:
                view = {k: df.loc[:d] for k, df in panel.items()}
                fview = {k: df.loc[:d] for k, df in fundamentals.items()} if fundamentals else None
                w = strategy.generate_weights(d, view, fview).fillna(0)
                w = w[w > 0]
                if w.sum() > 1.0 + 1e-9:
                    w = w / w.sum()
                w = w.drop(list(forced_exit), errors="ignore")  # 손절 종목 즉시 재매수 방지
                pending, targets[d] = w, w

        return BacktestResult(
            equity=pd.Series(eq_hist, name="equity"),
            cash=pd.Series(cash_hist, name="cash"),
            positions=pd.DataFrame(pos_hist).T.fillna(0).astype(int),
            trades=pd.DataFrame(trades, columns=["date", "ticker", "side", "qty", "price", "cost", "reason"]),
            target_weights=targets,
            strategy_name=getattr(strategy, "name", type(strategy).__name__),
        )
