"""리스크 관리: 주문 전 검증(pre-trade check), 포지션 크기 산정, 손절, 킬 스위치.

모든 주문은 브로커로 가기 전에 반드시 validate_order()를 통과해야 한다.
검증 실패 사유는 사람이 읽을 수 있는 한국어 문장으로 남겨 거래 기록·알림에 그대로 쓴다.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime

from ..broker.base import AccountSnapshot, Order, OrderType, Quote, Side
from ..config import RiskConfig
from ..krx import now_kst, tick_size


@dataclass
class RiskDecision:
    approved: bool
    order: Order
    reasons: list[str] = field(default_factory=list)
    adjusted: bool = False

    def text(self) -> str:
        if self.approved and not self.reasons:
            return "승인"
        return ("승인(수정): " if self.approved else "거부: ") + "; ".join(self.reasons)


@dataclass
class ExitSignal:
    ticker: str
    qty: int
    reason: str


class RiskManager:
    def __init__(self, cfg: RiskConfig | None = None, clock=now_kst):
        self.cfg = cfg or RiskConfig()
        self.clock = clock
        self.kill_switch = False
        self.kill_reason = ""
        self.buy_blocked_today = False
        self._day: date | None = None
        self._day_start_equity: float | None = None
        self._peak_equity = 0.0
        self._order_times: deque[datetime] = deque()
        self._recent: dict[tuple[str, Side], datetime] = {}
        self._high_water: dict[str, float] = {}   # 보유 중 최고가(추적손절용)
        self.restricted: dict[str, str] = {}      # ticker → 사유(관리종목 등)

    # ------------------------------------------------------------ 계좌 상태
    def update_equity(self, equity: float) -> None:
        """평가금액 갱신 → 일일 손실 한도·최대 낙폭 한도 점검."""
        today = self.clock().date()
        if self._day != today:
            self._day, self._day_start_equity, self.buy_blocked_today = today, equity, False
        self._peak_equity = max(self._peak_equity, equity)
        day_ret = equity / self._day_start_equity - 1 if self._day_start_equity else 0.0
        dd = equity / self._peak_equity - 1 if self._peak_equity else 0.0
        if day_ret <= -self.cfg.daily_loss_limit and not self.buy_blocked_today:
            self.buy_blocked_today = True
        if dd <= -self.cfg.max_drawdown_kill:
            self.trigger_kill(f"최대낙폭 {dd:.1%} ≤ 한도 -{self.cfg.max_drawdown_kill:.0%}")

    def trigger_kill(self, reason: str) -> None:
        self.kill_switch, self.kill_reason = True, reason

    def reset_kill(self, operator_confirm: str) -> None:
        """킬 스위치 해제는 사람이 원인을 확인하고 명시적으로 입력해야만 가능."""
        if operator_confirm != "I_REVIEWED_THE_INCIDENT":
            raise PermissionError("해제 확인 문구가 일치하지 않습니다.")
        self.kill_switch, self.kill_reason = False, ""

    # ------------------------------------------------------------ 사이징
    def size_position(self, equity: float, price: float, atr: float | None = None, target_weight: float | None = None) -> int:
        """포지션 크기(주식 수).
        - ATR 방식: 손절 시 손실이 equity × risk_per_trade가 되도록 수량 결정
                    수량 = (equity × risk_per_trade) / (ATR × atr_multiple)
        - 비중 방식: equity × target_weight / price
        두 방식 모두 종목당 최대 비중(max_position_weight)으로 상한을 둔다.
        """
        if price <= 0 or equity <= 0:
            return 0
        cap = equity * self.cfg.max_position_weight / price
        if atr and atr > 0:
            qty = equity * self.cfg.risk_per_trade / (atr * self.cfg.atr_multiple)
        elif target_weight is not None:
            qty = equity * target_weight / price
        else:
            qty = cap
        return int(math.floor(min(qty, cap)))

    # ------------------------------------------------------------ 주문 검증
    def validate_order(self, order: Order, account: AccountSnapshot, quote: Quote) -> RiskDecision:
        r: list[str] = []
        now = self.clock()
        cfg = self.cfg
        dec = RiskDecision(True, order)

        def reject(msg):
            r.append(msg)
            dec.approved = False

        # 1) 시스템 상태
        if self.kill_switch:
            reject(f"킬 스위치 작동 중({self.kill_reason})")
        if order.side == Side.BUY and self.buy_blocked_today:
            reject(f"일일 손실 한도(-{cfg.daily_loss_limit:.0%}) 도달로 당일 신규 매수 중단")

        # 2) 주문 형식
        if not (isinstance(order.qty, int) and order.qty > 0):
            reject(f"수량 오류({order.qty}): 1주 이상의 정수여야 함")
        if not (len(order.ticker) == 6 and order.ticker.isalnum()):
            reject(f"종목코드 형식 오류({order.ticker})")
        if order.order_type == OrderType.LIMIT:
            if order.price <= 0:
                reject("지정가 주문에 가격이 없음")
            elif order.price % tick_size(order.price):
                reject(f"호가단위 불일치({order.price:,}원, 단위 {tick_size(order.price)}원)")

        # 3) 종목 상태
        if quote.halted:
            reject("거래정지 종목")
        if order.side == Side.BUY and order.ticker in self.restricted:
            reject(f"매수 제한 종목({self.restricted[order.ticker]})")

        # 4) 가격 검증(팻 핑거 방지)
        px = order.price if order.order_type == OrderType.LIMIT and order.price > 0 else quote.price
        if order.order_type == OrderType.LIMIT and order.price > 0:
            if not (quote.lower_limit <= order.price <= quote.upper_limit):
                reject(f"가격제한폭 이탈({order.price:,} ∉ [{quote.lower_limit:,.0f}, {quote.upper_limit:,.0f}])")
            dev = abs(order.price / quote.price - 1)
            if dev > cfg.max_price_deviation:
                reject(f"현재가 대비 괴리 {dev:.1%} > 허용 {cfg.max_price_deviation:.0%}")
        notional = order.qty * px
        if notional > cfg.max_order_value:
            reject(f"1회 주문금액 {notional:,.0f}원 > 한도 {cfg.max_order_value:,.0f}원")

        # 5) 계좌·포지션 한도
        held = account.positions.get(order.ticker)
        if order.side == Side.SELL:
            if held is None or held.qty < order.qty:
                reject(f"보유수량({held.qty if held else 0}) 부족 — 공매도 금지")
        else:
            fee_buf = 1.001
            if notional * fee_buf > account.cash:
                affordable = int(account.cash / (px * fee_buf))
                if affordable <= 0:
                    reject(f"주문가능현금 부족(필요 {notional:,.0f} > 보유 {account.cash:,.0f})")
                else:
                    order.qty = affordable
                    notional = affordable * px
                    dec.adjusted = True
                    r.append(f"현금 범위로 수량 축소 → {affordable}주")
            new_weight = ((held.qty if held else 0) * px + notional) / max(account.total_equity, 1)
            if new_weight > cfg.max_position_weight * 1.01:  # 지정가 오프셋 오차 1% 허용
                reject(f"종목 비중 {new_weight:.1%} > 한도 {cfg.max_position_weight:.0%}")
            if held is None and len(account.positions) >= cfg.max_positions:
                reject(f"보유 종목 수 한도({cfg.max_positions}) 도달")
            if order.qty > 0 and quote.volume > 0 and order.qty > quote.volume * cfg.max_participation and quote.volume > 1000:
                reject(f"당일 거래량 대비 주문 과다({order.qty / quote.volume:.1%})")

        # 6) 주문 빈도·중복
        while self._order_times and (now - self._order_times[0]).total_seconds() > 60:
            self._order_times.popleft()
        if len(self._order_times) >= cfg.max_orders_per_minute:
            reject(f"분당 주문 한도({cfg.max_orders_per_minute}) 초과")
        last = self._recent.get((order.ticker, order.side))
        if last and (now - last).total_seconds() < cfg.duplicate_order_window_sec:
            reject(f"{cfg.duplicate_order_window_sec}초 내 동일 종목·방향 중복 주문")

        dec.reasons = r
        if dec.approved:
            self._order_times.append(now)
            self._recent[(order.ticker, order.side)] = now
        return dec

    # ------------------------------------------------------------ 손절
    def check_exits(self, account: AccountSnapshot, prices: dict[str, float]) -> list[ExitSignal]:
        """고정 손절(매입가 대비)·추적 손절(보유 중 최고가 대비) 점검."""
        out = []
        for t, p in account.positions.items():
            px = prices.get(t, p.last_price)
            if not px:
                continue
            hw = self._high_water[t] = max(self._high_water.get(t, p.avg_price), px)
            if px <= p.avg_price * (1 - self.cfg.stop_loss_pct):
                out.append(ExitSignal(t, p.qty, f"손절: 매입가 {p.avg_price:,.0f} 대비 {px / p.avg_price - 1:.1%}"))
            elif px <= hw * (1 - self.cfg.trailing_stop_pct):
                out.append(ExitSignal(t, p.qty, f"추적손절: 보유 중 최고가 {hw:,.0f} 대비 {px / hw - 1:.1%}"))
        for t in list(self._high_water):
            if t not in account.positions:
                self._high_water.pop(t)
        return out
