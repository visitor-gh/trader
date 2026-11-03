"""브로커(증권사) 공통 인터페이스와 주문·체결 자료구조.

실거래(KIS), 모의투자(KIS 모의서버), 로컬 가상체결(PaperBroker)이 같은 인터페이스를 구현하므로
전략·리스크·실행 코드는 한 줄도 바꾸지 않고 모드만 전환할 수 있다.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..krx import now_kst


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"     # 지정가
    MARKET = "market"   # 시장가


class OrderStatus(str, Enum):
    NEW = "new"                  # 생성(미전송)
    REJECTED_RISK = "rejected_risk"
    SUBMITTED = "submitted"      # 증권사 접수
    PARTIAL = "partial"          # 일부 체결
    FILLED = "filled"            # 전량 체결
    CANCELLED = "cancelled"
    REJECTED = "rejected"        # 증권사 거부
    ERROR = "error"


@dataclass
class Order:
    ticker: str
    side: Side
    qty: int
    order_type: OrderType = OrderType.LIMIT
    price: float = 0  # 지정가(현지통화). 원화 종목은 정수, 미국 종목은 센트 단위 소수
    reason: str = ""
    client_order_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])  # 멱등성 키
    broker_order_id: str | None = None
    status: OrderStatus = OrderStatus.NEW
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    created_at: datetime = field(default_factory=now_kst)
    message: str = ""

    @property
    def notional(self) -> float:
        return self.qty * self.price


@dataclass
class Fill:
    client_order_id: str
    ticker: str
    side: Side
    qty: int
    price: float
    fee: float
    tax: float
    filled_at: datetime = field(default_factory=now_kst)
    currency: str = "KRW"
    fx_rate: float = 1.0      # 체결 시점 환율(외화 1단위당 원화) — 양도소득 원화 환산에 사용


@dataclass
class Position:
    ticker: str
    qty: int
    avg_price: float
    last_price: float = 0.0
    name: str = ""
    currency: str = "KRW"
    fx_rate: float = 1.0      # 현재 환율. avg_price·last_price는 현지통화

    @property
    def market_value(self) -> float:
        """원화 환산 평가금액."""
        return self.qty * self.last_price * self.fx_rate


@dataclass
class Quote:
    ticker: str
    price: float          # 현재가
    base_price: float     # 기준가(전일 종가) → 상·하한가 계산
    upper_limit: float
    lower_limit: float
    volume: float = 0.0
    status_code: str = "" # 종목 상태(관리·경고·정지 등)
    halted: bool = False
    currency: str = "KRW"
    fx_rate: float = 1.0  # 외화 1단위당 원화
    lot: int = 1          # 매매단위


@dataclass
class AccountSnapshot:
    cash: float                    # 주문 가능 현금(원화 환산 합계)
    positions: dict[str, Position]
    total_equity: float            # 원화 환산 총평가금액
    as_of: datetime = field(default_factory=now_kst)
    cash_by_ccy: dict[str, float] = field(default_factory=dict)  # 통화별 현금(현지통화 금액)

    def exposure(self, key=lambda p: p.currency) -> dict[str, float]:
        """그룹별(기본: 통화별) 원화 평가금액 합계."""
        out: dict[str, float] = {}
        for t, p in self.positions.items():
            k = key(p) if callable(key) else key
            out[k] = out.get(k, 0.0) + p.market_value
        return out


class Broker(ABC):
    @abstractmethod
    def get_quote(self, ticker: str) -> Quote: ...

    @abstractmethod
    def get_account(self) -> AccountSnapshot: ...

    @abstractmethod
    def place_order(self, order: Order) -> Order: ...

    @abstractmethod
    def cancel_order(self, order: Order) -> Order: ...

    @abstractmethod
    def poll_fills(self) -> list[Fill]:
        """마지막 호출 이후 새로 발생한 체결 내역."""
        ...
