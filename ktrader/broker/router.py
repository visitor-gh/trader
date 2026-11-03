"""시장별 브로커 라우팅. 전략·실행 엔진은 RoutingBroker 하나만 보면 된다.

  RoutingBroker({"KR": KISBroker(...), "US": overseas, "JP": overseas, "HK": overseas})
    ├ get_quote / place_order / cancel_order → 종목의 시장에 맞는 브로커로 전달
    ├ get_account → 모든 브로커 잔고를 원화 환산으로 합산(같은 브로커는 1회만 조회)
    └ poll_fills  → 모든 브로커 체결을 합침
"""
from __future__ import annotations

from ..markets import parse_symbol
from .base import AccountSnapshot, Broker, Fill, Order, Quote


class RoutingBroker(Broker):
    def __init__(self, routes: dict[str, Broker]):
        if not routes:
            raise ValueError("최소 1개 시장의 브로커가 필요합니다.")
        self.routes = routes

    def _unique(self) -> list[Broker]:
        seen, out = set(), []
        for b in self.routes.values():
            if id(b) not in seen:
                seen.add(id(b))
                out.append(b)
        return out

    def broker_for(self, ticker: str) -> Broker:
        m = parse_symbol(ticker).market
        if m not in self.routes:
            raise KeyError(f"{m} 시장 브로커가 설정되지 않았습니다({ticker}).")
        return self.routes[m]

    def get_quote(self, ticker: str) -> Quote:
        return self.broker_for(ticker).get_quote(ticker)

    def place_order(self, order: Order) -> Order:
        return self.broker_for(order.ticker).place_order(order)

    def cancel_order(self, order: Order) -> Order:
        return self.broker_for(order.ticker).cancel_order(order)

    def get_account(self) -> AccountSnapshot:
        cash, positions, cash_by_ccy, equity = 0.0, {}, {}, 0.0
        for b in self._unique():
            a = b.get_account()
            cash += a.cash
            equity += a.total_equity
            positions.update(a.positions)
            for c, v in a.cash_by_ccy.items():
                cash_by_ccy[c] = cash_by_ccy.get(c, 0.0) + v
        return AccountSnapshot(cash, positions, equity, cash_by_ccy=cash_by_ccy)

    def poll_fills(self) -> list[Fill]:
        out: list[Fill] = []
        for b in self._unique():
            out.extend(b.poll_fills())
        return out
