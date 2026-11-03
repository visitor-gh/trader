"""일 단위 포트폴리오 백테스트 엔진(국내·해외 다중 시장).

현실성 확보 규칙
  1) 미래 정보 차단: 전략은 t일 종가까지의 데이터만 받고, 주문은 t+1일 시가에 체결된다.
     - 시장 간 시차: 날짜 기준으로 정렬하면 미국 t일 종가(한국시간 t+1일 새벽)는
       한국 t+1일 시가(09:00)보다 먼저 확정되므로 'd일 신호 → d+1일 시가 체결'은 모든 시장에서 유효하다.
  2) 비용: 시장별 비용 모델(수수료, 거래세·인지세, 환전 스프레드)과 슬리피지.
  3) 체결 제약: 해당 시장 휴장일·거래정지(거래량 0) 미체결, 가격제한폭이 있는 시장은 시가 상한가 매수·
     하한가 매도 불가, 일 거래량의 max_participation 비율까지만 체결, 매매단위(일본 100주 등) 내림.
  4) 환율: 현금은 원화 단일 계좌(통합증거금 가정). 해외 종목은 체결일 환율로 원화 환산하며,
     평가는 매일 종가 × 당일 환율로 한다(panel["fx"]가 있으면 사용, 없으면 전 종목 원화로 간주).
  5) 리스크 오버레이: 매일 종가 기준 손절/추적손절 점검 → 다음날 시가 청산.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
import pandas as pd

from ..config import MARKET_COSTS, CostModel
from ..markets import MARKETS, parse_symbol


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
    cost: CostModel = field(default_factory=CostModel)       # 국내(KR) 비용
    market_costs: dict = field(default_factory=lambda: dict(MARKET_COSTS))  # 해외 시장별 비용
    max_participation: float = 0.05
    stop_loss_pct: float | None = None
    trailing_stop_pct: float | None = None
    warmup_days: int = 252          # 지표 계산에 필요한 초기 구간(거래 안 함)

    def cost_for(self, market: str) -> CostModel:
        return self.cost if market == "KR" else self.market_costs.get(market, self.cost)


@dataclass
class BacktestResult:
    equity: pd.Series
    cash: pd.Series
    positions: pd.DataFrame          # 날짜×종목 보유 수량
    trades: pd.DataFrame             # date, ticker, side, qty, price(현지통화), cost(원화), reason, market, fx
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


def _market(t: str) -> str:
    try:
        return parse_symbol(t).market
    except ValueError:
        return "KR"


def _lot(t: str) -> int:
    try:
        return parse_symbol(t).lot
    except ValueError:
        return 1


class Backtester:
    def __init__(self, cfg: BacktestConfig | None = None):
        self.cfg = cfg or BacktestConfig()

    def run(self, panel: dict[str, pd.DataFrame], strategy: Strategy,
            fundamentals: dict[str, pd.DataFrame] | None = None,
            start: str | None = None, end: str | None = None) -> BacktestResult:
        cfg = self.cfg
        o, v = panel["open"], panel["volume"]
        c_raw = panel["close"]
        c = c_raw.ffill()                                  # 휴장일 평가는 직전 종가
        fxp = panel.get("fx")
        fxp = fxp.ffill().reindex_like(c).fillna(1.0) if fxp is not None else pd.DataFrame(1.0, index=c.index, columns=c.columns)
        mkt = {t: _market(t) for t in c.columns}
        lots = {t: _lot(t) for t in c.columns}
        costs = {t: cfg.cost_for(mkt[t]) for t in c.columns}

        dates = c.index
        if start:
            dates = dates[dates >= pd.Timestamp(start)]
        if end:
            dates = dates[dates <= pd.Timestamp(end)]
        all_dates = c.index
        first_idx = max(all_dates.get_loc(dates[0]), cfg.warmup_days, 1)
        last_idx = all_dates.get_loc(dates[-1])
        trade_dates = all_dates[first_idx:last_idx + 1]
        rebal = rebalance_dates(trade_dates, cfg.rebalance)

        cash = cfg.initial_cash
        pos: dict[str, int] = {}
        avg_cost: dict[str, float] = {}   # 현지통화 평균단가
        peak: dict[str, float] = {}
        pending: pd.Series | None = None     # 다음 거래 가능일 시가에 실행할 목표 비중
        pending_scope: set | None = None     # 이월된 종목만 처리할 때의 대상 집합
        forced_exit: dict[str, str] = {}     # 다음 거래 가능일 시가 청산 대상과 사유
        eq_hist, cash_hist, pos_hist, trades, targets = {}, {}, {}, [], {}

        for i, d in enumerate(trade_dates):
            prev_close = c.iloc[all_dates.get_loc(d) - 1]
            opens, vols, fx_today = o.loc[d], v.loc[d], fxp.loc[d]

            def can_trade(t, side):
                if not np.isfinite(opens.get(t, np.nan)) or not (vols.get(t, 0) > 0):
                    return False   # 휴장일·거래정지
                lo, hi = MARKETS[mkt[t]].price_limits(prev_close[t])
                return not ((side == "buy" and opens[t] >= hi) or (side == "sell" and opens[t] <= lo))

            def execute(t, qty, reason) -> bool:
                nonlocal cash
                side = "buy" if qty > 0 else "sell"
                if not can_trade(t, side):
                    return False
                cap = int(vols[t] * cfg.max_participation) // lots[t] * lots[t]
                qty = int(np.sign(qty) * min(abs(qty), cap))
                if qty == 0 or not can_trade(t, side):
                    return False
                cm = costs[t]
                slip = cm.slippage_bps / 10_000
                fx = float(fx_today[t])
                px = opens[t] * (1 + slip if qty > 0 else 1 - slip)
                notional = abs(qty) * px * fx                # 원화
                if qty > 0:
                    fee = cm.buy_cost(notional)
                    if notional + fee > cash:  # 현금 부족 → 가능한 수량으로 축소(미수 금지)
                        unit = px * fx * (1 + cm.commission_rate + cm.buy_tax_rate + cm.fx_spread)
                        qty = int(cash / unit) // lots[t] * lots[t]
                        if qty <= 0:
                            return False
                        notional = qty * px * fx
                        fee = cm.buy_cost(notional)
                    cash -= notional + fee
                    held = pos.get(t, 0)
                    avg_cost[t] = (avg_cost.get(t, 0) * held + px * qty) / (held + qty)
                    peak[t] = max(peak.get(t, 0), px)
                else:
                    fee = cm.sell_cost(notional)
                    cash += notional - fee
                pos[t] = pos.get(t, 0) + qty
                if pos[t] == 0:
                    pos.pop(t)
                    avg_cost.pop(t, None)
                    peak.pop(t, None)
                trades.append({"date": d, "ticker": t, "side": side, "qty": abs(qty), "price": px,
                               "cost": fee, "reason": reason, "market": mkt[t], "fx": fx})
                return True

            # 1) 전일 신호에 따른 강제 청산(손절). 휴장 시장은 다음 거래일로 이월
            for t, why in list(forced_exit.items()):
                if t not in pos or execute(t, -pos[t], why):
                    forced_exit.pop(t)

            # 2) 리밸런싱 실행: 매도 먼저(현금 확보) → 매수.
            #    휴장 시장 종목은 이월(carry)하여 그 시장의 다음 거래일에 해당 종목만 다시 처리
            if pending is not None:
                px_open = opens.where(np.isfinite(opens), prev_close)
                equity_open = cash + sum(q * px_open[t] * fx_today[t] for t, q in pos.items())
                scope = pending_scope if pending_scope is not None else set(pending.index) | set(pos)
                deltas, carry = {}, set()
                for t in scope:
                    w = float(pending.get(t, 0.0))
                    tgt = 0
                    if w > 0 and np.isfinite(px_open.get(t, np.nan)) and px_open[t] > 0:
                        tgt = int(equity_open * w / (px_open[t] * fx_today[t])) // lots[t] * lots[t]
                    dq = tgt - pos.get(t, 0)
                    if dq == 0:
                        continue
                    if not np.isfinite(opens.get(t, np.nan)):
                        carry.add(t)
                    else:
                        deltas[t] = dq
                for t, dq in sorted(deltas.items(), key=lambda x: x[1]):
                    execute(t, dq, "리밸런싱")
                if carry:
                    pending_scope = carry
                else:
                    pending, pending_scope = None, None

            # 3) 종가 평가(원화)
            closes = c.loc[d]
            equity = cash + sum(q * closes[t] * fx_today[t] for t, q in pos.items())
            eq_hist[d], cash_hist[d], pos_hist[d] = equity, cash, dict(pos)

            # 4) 종가 기준 손절 점검(현지통화 기준, 다음 거래 가능일 시가 실행)
            traded_today = c_raw.loc[d].notna()
            for t in pos:
                if not traded_today.get(t, False):
                    continue
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
                pending, pending_scope, targets[d] = w, None, w

        return BacktestResult(
            equity=pd.Series(eq_hist, name="equity"),
            cash=pd.Series(cash_hist, name="cash"),
            positions=pd.DataFrame(pos_hist).T.fillna(0).astype(int),
            trades=pd.DataFrame(trades, columns=["date", "ticker", "side", "qty", "price", "cost", "reason", "market", "fx"]),
            target_weights=targets,
            strategy_name=getattr(strategy, "name", type(strategy).__name__),
        )
