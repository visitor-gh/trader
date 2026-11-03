"""한국투자증권 해외주식 Open API 어댑터(미국·일본·홍콩).

국내 어댑터(KISBroker)와 같은 토큰·요청·재시도 로직을 상속하고, 경로·TR_ID·필드만 해외용으로 바꾼다.

주의(운영 전 KIS 개발자센터 최신 문서로 반드시 확인)
  - 거래소 코드가 주문(OVRS_EXCG_CD: NASD/NYSE/AMEX/SEHK/TKSE)과 시세(EXCD: NAS/NYS/AMS/HKS/TSE)에서 다르다.
  - 미국 정규장 주문은 지정가가 기본이며, 시장가 대신 LOO·LOC 등 별도 주문구분을 쓴다.
  - 모의투자 서버는 해외주식 일부 기능·시장만 지원한다.
  - 미국 주간거래(한국 낮 시간대 거래)는 별도 TR_ID·경로를 쓰므로 이 어댑터 범위 밖이다.
  - 체결 통화 예수금이 없으면 통합증거금 신청 여부에 따라 원화 자동 환전 또는 주문 거부가 된다.
"""
from __future__ import annotations

from ..config import MARKET_COSTS, CostModel, KISConfig
from ..markets import MARKETS, parse_symbol
from .base import AccountSnapshot, Fill, Order, OrderStatus, OrderType, Position, Quote, Side
from .kis import KISBroker, KISError

# 시장별 (실전, 모의) 주문 TR_ID
OVERSEAS_TR_IDS = {
    "US": {"buy": ("TTTT1002U", "VTTT1002U"), "sell": ("TTTT1006U", "VTTT1001U")},
    "JP": {"buy": ("TTTS0308U", "VTTS0308U"), "sell": ("TTTS0307U", "VTTS0307U")},
    "HK": {"buy": ("TTTS1002U", "VTTS1002U"), "sell": ("TTTS1001U", "VTTS1001U")},
}
OVERSEAS_COMMON_TR = {
    "cancel": ("TTTT1004U", "VTTT1004U"),
    "balance": ("TTTS3012R", "VTTS3012R"),
    "ccnl": ("TTTS3035R", "VTTS3035R"),
    "price": ("HHDFS00000300", "HHDFS00000300"),
}
# 주문 거래소 → 시세 거래소 코드
QUOTE_EXCD = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS", "SEHK": "HKS", "TKSE": "TSE"}
CCY_OF_EXCHANGE = {"NASD": "USD", "NYSE": "USD", "AMEX": "USD", "SEHK": "HKD", "TKSE": "JPY"}


class KISOverseasBroker(KISBroker):
    def __init__(self, cfg: KISConfig, fx_provider=None, market_costs: dict[str, CostModel] | None = None, **kw):
        """fx_provider(ccy) → 외화 1단위당 원화. 없으면 잔고 응답의 기준환율을 사용."""
        super().__init__(cfg, **kw)
        self.fx_provider = fx_provider
        self.market_costs = market_costs or MARKET_COSTS
        self._fx_cache: dict[str, float] = {}

    def _otr(self, key: str) -> str:
        return OVERSEAS_COMMON_TR[key][1 if self.cfg.is_mock else 0]

    def _fx(self, ccy: str) -> float:
        if self.fx_provider is not None:
            return float(self.fx_provider(ccy))
        if ccy in self._fx_cache:
            return self._fx_cache[ccy]
        raise KeyError(f"{ccy} 환율 정보 없음: fx_provider를 지정하거나 get_account()를 먼저 호출하십시오.")

    # ---------------------------------------------------------------- 시세
    def get_quote(self, ticker: str) -> Quote:
        inst = parse_symbol(ticker)
        if inst.market == "KR":
            raise ValueError("국내 종목은 KISBroker를 사용하십시오(RoutingBroker 권장).")
        js = self._request("GET", "/uapi/overseas-price/v1/quotations/price", self._otr("price"), params={
            "AUTH": "", "EXCD": QUOTE_EXCD[inst.exchange], "SYMB": inst.local_code})
        o = js["output"]
        price = float(o.get("last") or 0)
        base = float(o.get("base") or price)
        lo, hi = inst.spec.price_limits(base)
        return Quote(ticker, price, base, hi, lo, float(o.get("tvol") or 0), "", price <= 0,
                     inst.currency, self._fx(inst.currency), inst.lot)

    # ---------------------------------------------------------------- 계좌
    def get_account(self) -> AccountSnapshot:
        """시장별 잔고를 조회해 원화 환산으로 합산. (거래소별 1회 호출, 50종목 초과 시 연속조회 필요)"""
        positions: dict[str, Position] = {}
        cash_by_ccy: dict[str, float] = {}
        for exch in ("NASD", "SEHK", "TKSE"):   # NASD 조회 시 미국 전체 거래소 잔고가 함께 조회됨
            ccy = CCY_OF_EXCHANGE[exch]
            try:
                js = self._request("GET", "/uapi/overseas-stock/v1/trading/inquire-balance", self._otr("balance"), params={
                    "CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "OVRS_EXCG_CD": exch, "TR_CRCY_CD": ccy,
                    "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""})
            except KISError as e:
                if self.cfg.is_mock:   # 모의서버 미지원 시장은 건너뜀
                    continue
                raise e
            for r in js.get("output1", []):
                qty = int(float(r.get("ovrs_cblc_qty", 0)))
                if qty <= 0:
                    continue
                sym = self._to_symbol(r["ovrs_pdno"], exch)
                rate = float(r.get("bass_exrt") or 0) or self._fx_cache.get(ccy, 0)
                if rate:
                    self._fx_cache[ccy] = rate
                positions[sym] = Position(sym, qty, float(r.get("pchs_avg_pric", 0)), float(r.get("now_pric2", 0)),
                                          r.get("ovrs_item_name", ""), ccy, rate or self._fx(ccy))
            o2 = js.get("output2") or {}
            o2 = o2[0] if isinstance(o2, list) and o2 else o2
            cash_by_ccy[ccy] = cash_by_ccy.get(ccy, 0.0) + float(o2.get("frcr_dncl_amt_2", 0) or 0)
        cash_krw = sum(v * self._fx(c) for c, v in cash_by_ccy.items() if v)
        mv = sum(p.market_value for p in positions.values())
        return AccountSnapshot(cash_krw, positions, cash_krw + mv, cash_by_ccy=cash_by_ccy)

    @staticmethod
    def _to_symbol(code: str, exch: str) -> str:
        if exch == "TKSE":
            return f"{code}.T"
        if exch == "SEHK":
            return f"{int(code):04d}.HK"
        return code

    # ---------------------------------------------------------------- 주문
    def place_order(self, order: Order) -> Order:
        inst = parse_symbol(order.ticker)
        if order.order_type != OrderType.LIMIT:
            order.status, order.message = OrderStatus.REJECTED, "해외주식은 지정가 주문만 허용(시스템 정책)"
            return order
        tr = OVERSEAS_TR_IDS[inst.market]["buy" if order.side == Side.BUY else "sell"][1 if self.cfg.is_mock else 0]
        body = {
            "CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "OVRS_EXCG_CD": inst.exchange, "PDNO": inst.local_code,
            "ORD_QTY": str(int(order.qty)), "OVRS_ORD_UNPR": f"{order.price:.4f}".rstrip("0").rstrip("."),
            "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00",   # 00 지정가
        }
        try:
            js = self._request("POST", "/uapi/overseas-stock/v1/trading/order", tr, body=body)
        except KISError as e:
            order.status, order.message = OrderStatus.REJECTED, str(e)
            return order
        except Exception as e:
            order.status, order.message = OrderStatus.ERROR, f"접수 불명: {e}"
            return order
        out = js["output"]
        order.broker_order_id = out["ODNO"]
        order.message = f"{out.get('KRX_FWDG_ORD_ORGNO', '')}|{js.get('msg1', '')}"
        order.status = OrderStatus.SUBMITTED
        return order

    def cancel_order(self, order: Order) -> Order:
        inst = parse_symbol(order.ticker)
        body = {
            "CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "OVRS_EXCG_CD": inst.exchange, "PDNO": inst.local_code,
            "ORGN_ODNO": order.broker_order_id, "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": str(order.qty - order.filled_qty),
            "OVRS_ORD_UNPR": "0", "ORD_SVR_DVSN_CD": "0",
        }
        try:
            self._request("POST", "/uapi/overseas-stock/v1/trading/order-rvsecncl", self._otr("cancel"), body=body)
            order.status = OrderStatus.CANCELLED
        except KISError as e:
            order.message += f" / 취소실패 {e}"
        return order

    def poll_fills(self) -> list[Fill]:
        from datetime import datetime, timedelta

        today = datetime.now()
        js = self._request("GET", "/uapi/overseas-stock/v1/trading/inquire-ccnl", self._otr("ccnl"), params={
            "CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "PDNO": "%",
            "ORD_STRT_DT": (today - timedelta(days=1)).strftime("%Y%m%d"),   # 미국장은 한국 날짜로 이틀에 걸침
            "ORD_END_DT": today.strftime("%Y%m%d"), "SLL_BUY_DVSN": "00", "CCLD_NCCS_DVSN": "01",
            "OVRS_EXCG_CD": "%", "SORT_SQN": "DS", "ORD_DT": "", "ORD_GNO_BRNO": "", "ODNO": "",
            "CTX_AREA_NK200": "", "CTX_AREA_FK200": ""})
        fills = []
        for r in js.get("output", []):
            cum = int(float(r.get("ft_ccld_qty", 0)))
            new = cum - self._seen_fills.get(r["odno"], 0)
            if new <= 0:
                continue
            self._seen_fills[r["odno"]] = cum
            exch = r.get("ovrs_excg_cd", "NASD")
            sym = self._to_symbol(r["pdno"], exch)
            inst = parse_symbol(sym)
            side = Side.SELL if r.get("sll_buy_dvsn_cd") == "01" else Side.BUY
            px = float(r.get("ft_ccld_unpr3", 0))
            fx = self._fx(inst.currency)
            cm = self.market_costs.get(inst.market, self.cost)
            notional = new * px * fx
            tax = notional * (cm.sell_tax_rate if side == Side.SELL else cm.buy_tax_rate)
            fee = (cm.sell_cost(notional) if side == Side.SELL else cm.buy_cost(notional)) - tax
            fills.append(Fill(r["odno"], sym, side, new, px, fee, tax, currency=inst.currency, fx_rate=fx))
        return fills


__all__ = ["KISOverseasBroker", "OVERSEAS_TR_IDS", "MARKETS"]
