"""합법적 거래 준수 장치(ComplianceGuard).

자본시장법상 불공정거래(시세조종 등)로 오인될 수 있는 주문 패턴을 '시스템이 스스로' 차단한다.
이 모듈은 법률 자문을 대체하지 않으며, 실제 운영 전 증권사 약관·거래소 규정을 확인해야 한다.

차단·경고 항목
  - 자전성 주문: 같은 종목에 매수·매도 미체결 주문이 동시에 존재(가장매매·통정매매 오인 소지)
  - 과다 정정·취소: 일정 시간 내 취소 비율 과다(허수성 호가 오인 소지)
  - 단일가 시간대 대량 주문: 시가·종가 결정에 과도하게 관여(종가 관여 오인 소지)
  - 실계좌 모드 진입 시 이중 확인
"""
from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from ..broker.base import Order, Side
from ..config import TradingMode
from ..krx import now_kst
from ..markets import parse_symbol

REAL_MODE_CONFIRM = "I_UNDERSTAND_REAL_MONEY_RISK"


def assert_mode_allowed(mode: TradingMode) -> None:
    """실계좌 주문은 환경변수로 명시적 확인 문구를 설정해야만 허용."""
    if mode == TradingMode.REAL and os.environ.get("TRADER_REAL_CONFIRM") != REAL_MODE_CONFIRM:
        raise PermissionError(
            "실계좌 모드는 TRADER_REAL_CONFIRM 환경변수에 확인 문구를 설정해야 실행됩니다. "
            "반드시 모의투자에서 충분히 검증한 뒤 진행하십시오."
        )


@dataclass
class ComplianceGuard:
    max_cancel_ratio: float = 0.5          # 최근 주문 대비 취소 비율 상한
    cancel_window: int = 50                # 최근 N건 기준
    auction_max_participation: float = 0.01  # 단일가 시간대 주문 수량 / 종목 거래량 상한
    _history: deque = field(default_factory=lambda: deque(maxlen=200))

    def check(self, order: Order, open_orders: list[Order], volume: float, now: datetime | None = None) -> list[str]:
        """위반 사유 목록(빈 목록이면 통과)."""
        now = now or now_kst()
        issues = []
        opposite = Side.SELL if order.side == Side.BUY else Side.BUY
        if any(o.ticker == order.ticker and o.side == opposite for o in open_orders):
            issues.append("동일 종목 반대방향 미체결 주문 존재(자전성 거래 오인 소지) → 기존 주문 취소 후 재시도")
        recent = list(self._history)[-self.cancel_window:]
        if len(recent) >= 10:
            ratio = sum(1 for e in recent if e == "cancel") / len(recent)
            if ratio > self.max_cancel_ratio:
                issues.append(f"최근 취소 비율 {ratio:.0%} 과다(허수성 호가 오인 소지) → 주문 일시 중단")
        try:
            phase = parse_symbol(order.ticker).spec.session_phase(now)  # 종목 시장의 현지 시각 기준
        except ValueError:
            phase = "closed"
        if phase in ("pre_auction", "close_auction") and volume > 0 and order.qty > volume * self.auction_max_participation:
            issues.append("단일가 시간대 대량 주문(시가·종가 관여 오인 소지) → 정규 접속매매 시간에 분할 주문")
        return issues

    def record(self, event: str) -> None:
        """event: 'order' | 'cancel'"""
        self._history.append(event)
