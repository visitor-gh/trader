"""로컬 가상 브로커(PaperBroker). 외부로 어떤 주문도 보내지 않는다.

시세는 set_quote()로 주입한다(백테스트 데이터, 실시간 시세 어느 쪽이든 가능).
지정가 매수는 현재가 ≤ 지정가, 매도는 현재가 ≥ 지정가일 때 체결되며 미체결은 대기 상태로 남는다.

다중 시장: 현금은 원화 한 계좌로 관리하고(통합증거금 방식), 해외 종목 매수·매도 시
체결 시점 환율로 자동 환전하며 시장별 비용 모델(MARKET_COSTS)의 환전 스프레드를 부과한다.
"""
from __future__ import annotations

from ..config import MARKET_COSTS, CostModel
from ..markets import parse_symbol
from .base import AccountSnapshot, Broker, Fill, Order, OrderStatus, OrderType, Position, Quote, Side


class PaperBroker(Broker):
    def __init__(self, cash: float = 100_000_000, cost: CostModel | None = None,
                 market_costs: dict[str, CostModel] | None = None):
        self.cash = cash  # 원화
        self.costs = dict(market_costs or MARKET_COSTS)
        if cost is not None:
            self.costs["KR"] = cost
        self.positions: dict[str, Position] = {}
        self.quotes: dict[str, Quote] = {}
        self.open_orders: dict[str, Order] = {}
        self._new_fills: list[Fill] = []
        self._seq = 0

    @property
    def cost(self) -> CostModel:  # 하위 호환
        return self.costs["KR"]

    def set_quote(self, ticker: str, price: float, base_price: float | None = None, volume: float = 1e6,
                  status_code: str = "", halted: bool = False, fx_rate: float = 1.0):
        inst = parse_symbol(ticker)
        base = base_price or price
        lo, hi = inst.spec.price_limits(base)
        self.quotes[ticker] = Quote(ticker, price, base, hi, lo, volume, status_code, halted,
                                    inst.currency, fx_rate, inst.lot)
        if ticker in self.positions:
            self.positions[ticker].last_price = price
            self.positions[ticker].fx_rate = fx_rate
        self._match(ticker)

    def get_quote(self, ticker: str) -> Quote:
        if ticker not in self.quotes:
            raise KeyError(f"시세 없음: {ticker}")
        return self.quotes[ticker]

    def get_account(self) -> AccountSnapshot:
        mv = sum(p.market_value for p in self.positions.values())
        return AccountSnapshot(self.cash, {k: Position(**vars(v)) for k, v in self.positions.items()},
                               self.cash + mv, cash_by_ccy={"KRW": self.cash})

    def place_order(self, order: Order) -> Order:
        q = self.quotes.get(order.ticker)
        if q is None or q.halted:
            order.status, order.message = OrderStatus.REJECTED, "거래불가(시세 없음/거래정지)"
            return order
        if order.order_type == OrderType.LIMIT and not (q.lower_limit <= order.price <= q.upper_limit):
            order.status, order.message = OrderStatus.REJECTED, "가격제한폭 이탈"
            return order
        if order.qty % q.lot:
            order.status, order.message = OrderStatus.REJECTED, f"매매단위({q.lot}주) 불일치"
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
        cost = self.costs.get(parse_symbol(ticker).market, self.cost)
        slip = cost.slippage_bps / 10_000
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
            notional = px * o.qty * q.fx_rate          # 원화 환산
            if o.side == Side.BUY:
                fee = cost.buy_cost(notional)
                tax = notional * cost.buy_tax_rate
                fee -= tax
                if notional + fee + tax > self.cash:
                    o.status, o.message = OrderStatus.REJECTED, "주문가능현금 부족(미수 금지)"
                    self.open_orders.pop(cid)
                    continue
                self.cash -= notional + fee + tax
                p = self.positions.setdefault(ticker, Position(ticker, 0, 0.0, currency=q.currency))
                p.avg_price = (p.avg_price * p.qty + px * o.qty) / (p.qty + o.qty)  # 현지통화 평균단가
                p.qty += o.qty
                p.last_price, p.fx_rate = q.price, q.fx_rate
            else:
                tax = notional * cost.sell_tax_rate
                fee = cost.sell_cost(notional) - tax
                self.cash += notional - fee - tax
                p = self.positions[ticker]
                p.qty -= o.qty
                if p.qty == 0:
                    self.positions.pop(ticker)
            o.filled_qty, o.avg_fill_price, o.status = o.qty, px, OrderStatus.FILLED
            self.open_orders.pop(cid)
            # fee·tax는 원화 금액으로 기록
            self._new_fills.append(Fill(cid, ticker, o.side, o.qty, px, fee, tax, currency=q.currency, fx_rate=q.fx_rate))
