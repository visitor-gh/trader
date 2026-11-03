"""로컬 가상 브로커(PaperBroker). 외부로 어떤 주문도 보내지 않는다.

시세는 set_quote()로 주입한다(백테스트 데이터, 실시간 시세 어느 쪽이든 가능).
지정가 매수는 현재가 ≤ 지정가, 매도는 현재가 ≥ 지정가일 때 체결되며 미체결은 대기 상태로 남는다.
"""
from __future__ import annotations

from ..config import CostModel
from ..krx import price_limits
from .base import AccountSnapshot, Broker, Fill, Order, OrderStatus, OrderType, Position, Quote, Side


class PaperBroker(Broker):
    def __init__(self, cash: float = 100_000_000, cost: CostModel | None = None):
        self.cash = cash
        self.cost = cost or CostModel()
        self.positions: dict[str, Position] = {}
        self.quotes: dict[str, Quote] = {}
        self.open_orders: dict[str, Order] = {}
        self._new_fills: list[Fill] = []
        self._seq = 0

    def set_quote(self, ticker: str, price: float, base_price: float | None = None, volume: float = 1e6,
                  status_code: str = "", halted: bool = False):
        base = base_price or price
        lo, hi = price_limits(base)
        self.quotes[ticker] = Quote(ticker, price, base, hi, lo, volume, status_code, halted)
        if ticker in self.positions:
            self.positions[ticker].last_price = price
        self._match(ticker)

    def get_quote(self, ticker: str) -> Quote:
        if ticker not in self.quotes:
            raise KeyError(f"시세 없음: {ticker}")
        return self.quotes[ticker]

    def get_account(self) -> AccountSnapshot:
        mv = sum(p.qty * p.last_price for p in self.positions.values())
        return AccountSnapshot(self.cash, {k: Position(**vars(v)) for k, v in self.positions.items()}, self.cash + mv)

    def place_order(self, order: Order) -> Order:
        q = self.quotes.get(order.ticker)
        if q is None or q.halted:
            order.status, order.message = OrderStatus.REJECTED, "거래불가(시세 없음/거래정지)"
            return order
        if order.order_type == OrderType.LIMIT and not (q.lower_limit <= order.price <= q.upper_limit):
            order.status, order.message = OrderStatus.REJECTED, "가격제한폭 이탈"
            return order
        if order.side == Side.SELL and self.positions.get(order.ticker, Position(order.ticker, 0, 0)).qty < order.qty:
            order.status, order.message = OrderStatus.REJECTED, "매도가능수량 부족(공매도 금지)"
            return order
        self._seq += 1
        order.broker_order_id = f"P{self._seq:08d}"
        order.status = OrderStatus.SUBMITTED
        self.open_orders[order.client_order_id] = order
        self._match(order.ticker)
        return order

    def cancel_order(self, order: Order) -> Order:
        o = self.open_orders.pop(order.client_order_id, None)
        if o is not None:
            o.status = OrderStatus.CANCELLED
            return o
        return order

    def poll_fills(self) -> list[Fill]:
        out, self._new_fills = self._new_fills, []
        return out

    # ------------------------------------------------------------- 내부 체결
    def _match(self, ticker: str):
        q = self.quotes.get(ticker)
        if q is None:
            return
        slip = self.cost.slippage_bps / 10_000
        for cid, o in list(self.open_orders.items()):
            if o.ticker != ticker:
                continue
            if o.order_type == OrderType.MARKET:
                px = q.price * (1 + slip if o.side == Side.BUY else 1 - slip)
            elif o.side == Side.BUY and q.price <= o.price:
                px = q.price
            elif o.side == Side.SELL and q.price >= o.price:
                px = q.price
            else:
                continue
            notional = px * o.qty
            fee = notional * self.cost.commission_rate
            tax = notional * self.cost.sell_tax_rate if o.side == Side.SELL else 0.0
            if o.side == Side.BUY:
                if notional + fee > self.cash:
                    o.status, o.message = OrderStatus.REJECTED, "주문가능현금 부족(미수 금지)"
                    self.open_orders.pop(cid)
                    continue
                self.cash -= notional + fee
                p = self.positions.setdefault(ticker, Position(ticker, 0, 0.0))
                p.avg_price = (p.avg_price * p.qty + notional) / (p.qty + o.qty)
                p.qty += o.qty
                p.last_price = q.price
            else:
                self.cash += notional - fee - tax
                p = self.positions[ticker]
                p.qty -= o.qty
                if p.qty == 0:
                    self.positions.pop(ticker)
            o.filled_qty, o.avg_fill_price, o.status = o.qty, px, OrderStatus.FILLED
            self.open_orders.pop(cid)
            self._new_fills.append(Fill(cid, ticker, o.side, o.qty, px, fee, tax))
