"""실거래 실행 엔진.

1회 사이클(run_cycle) 순서
  ① 장 상태 확인(정규 접속매매 시간에만 신규 주문)
  ② 계좌 동기화 + 원장 대사(reconcile): 증권사 잔고 ≠ 내부 기록이면 즉시 중단
  ③ 평가금액 갱신 → 일일손실·최대낙폭 한도 점검(킬 스위치)
  ④ 손절 신호 → 매도 주문
  ⑤ 목표 비중(target weights) → 현재 보유와의 차이 → 주문 목록(매도 먼저)
  ⑥ 각 주문: 호가단위 가격 산정 → 컴플라이언스 점검 → 리스크 점검 → 전송 → 원장 기록
  ⑦ 체결 수집 → 원장 기록, 오래된 미체결 취소
"""
from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

import pandas as pd

from ..broker.base import AccountSnapshot, Broker, Order, OrderStatus, OrderType, Side
from ..krx import now_kst, price_limits, round_to_tick, session_phase
from ..ledger.store import Ledger
from ..report.explain import explain_order
from ..risk.manager import RiskManager
from ..security.compliance import ComplianceGuard

log = logging.getLogger(__name__)


@dataclass
class ExecutionConfig:
    limit_offset: float = 0.002          # 지정가 = 현재가 ± 0.2% (체결 가능성 높인 지정가)
    min_trade_value: float = 100_000     # 이 금액 미만 차이는 주문하지 않음(불필요한 잔거래 방지)
    order_timeout_sec: int = 300         # 미체결 주문 자동 취소
    rebalance_tolerance: float = 0.02    # 목표 비중과 2%p 이내 차이는 무시
    allow_outside_session: bool = False  # 테스트용(장외 시간 주문 허용)


class ReconcileError(RuntimeError):
    pass


class TradingEngine:
    def __init__(self, broker: Broker, risk: RiskManager, ledger: Ledger,
                 target_fn: Callable[[AccountSnapshot], tuple[pd.Series, dict[str, str]]],
                 cfg: ExecutionConfig | None = None, compliance: ComplianceGuard | None = None,
                 clock=now_kst, reconcile: bool = True):
        """target_fn(account) → (목표비중 Series, {종목: 선정 사유})"""
        self.broker, self.risk, self.ledger = broker, risk, ledger
        self.target_fn = target_fn
        self.cfg = cfg or ExecutionConfig()
        self.compliance = compliance or ComplianceGuard()
        self.clock = clock
        self.reconcile_enabled = reconcile
        self.open_orders: dict[str, Order] = {}
        self._by_broker_id: dict[str, str] = {}
        self._stop = False

    # ------------------------------------------------------------ 가격·수량
    def limit_price(self, side: Side, price: float, base_price: float) -> int:
        """시장성 지정가: 매수는 현재가보다 약간 높게(올림), 매도는 약간 낮게(내림) + 상·하한가 제한."""
        lo, hi = price_limits(base_price)
        if side == Side.BUY:
            return int(min(round_to_tick(price * (1 + self.cfg.limit_offset), "sell"), hi))
        return int(max(round_to_tick(price * (1 - self.cfg.limit_offset), "buy"), lo))

    def build_orders(self, targets: pd.Series, account: AccountSnapshot, reasons: dict[str, str]) -> list[Order]:
        eq = account.total_equity
        orders = []
        tickers = set(targets.index) | set(account.positions)
        for t in tickers:
            w = float(targets.get(t, 0.0))
            q = self.broker.get_quote(t)
            cur_qty = account.positions[t].qty if t in account.positions else 0
            cur_w = cur_qty * q.price / eq if eq else 0
            if abs(w - cur_w) < self.cfg.rebalance_tolerance and w > 0:
                continue
            tgt_qty = self.risk.size_position(eq, q.price, target_weight=w) if w > 0 else 0
            dq = tgt_qty - cur_qty
            if dq == 0 or abs(dq) * q.price < self.cfg.min_trade_value and tgt_qty != 0:
                continue
            side = Side.BUY if dq > 0 else Side.SELL
            why = reasons.get(t, "목표 비중 조정") if dq > 0 else ("편출(목표에서 제외)" if w == 0 else "비중 축소")
            orders.append(Order(t, side, abs(dq), OrderType.LIMIT, self.limit_price(side, q.price, q.base_price), why))
        orders.sort(key=lambda o: 0 if o.side == Side.SELL else 1)  # 매도 먼저 → 현금 확보
        return orders

    # ------------------------------------------------------------ 대사
    def reconcile(self, account: AccountSnapshot) -> None:
        """증권사 잔고와 원장(체결 기록) 비교. 불일치 = 체결 누락·수동 매매·오류 → 자동 매매 중단."""
        if not self.reconcile_enabled:
            return
        book = self.ledger.positions_from_fills()
        broker = {t: p.qty for t, p in account.positions.items()}
        diff = {t: (book.get(t, 0), broker.get(t, 0)) for t in set(book) | set(broker) if book.get(t, 0) != broker.get(t, 0)}
        if diff:
            self.ledger.log_event("reconcile_mismatch", {"diff": diff})
            self.risk.trigger_kill(f"잔고 불일치 {diff}")
            raise ReconcileError(f"원장(내부기록)과 증권사 잔고 불일치: {diff} → 확인 전까지 매매 중단")

    def bootstrap_from_account(self, account: AccountSnapshot) -> None:
        """최초 가동 시 기존 보유 종목을 '기초 잔고' 체결로 원장에 등록(이후 대사의 기준점)."""
        from ..broker.base import Fill

        if self.ledger.positions_from_fills():
            return
        for t, p in account.positions.items():
            self.ledger.record_fill(Fill("OPENING_BALANCE", t, Side.BUY, p.qty, p.avg_price, 0.0, 0.0))
        self.ledger.log_event("bootstrap", {"positions": {t: p.qty for t, p in account.positions.items()}})

    # ------------------------------------------------------------ 주문 실행
    def submit(self, order: Order, account: AccountSnapshot) -> Order:
        q = self.broker.get_quote(order.ticker)
        issues = self.compliance.check(order, list(self.open_orders.values()), q.volume, self.clock())
        if issues:
            order.status, order.message = OrderStatus.REJECTED_RISK, "; ".join(issues)
            self.ledger.upsert_order(order, "컴플라이언스: " + order.message)
            log.warning(explain_order(order))
            return order
        dec = self.risk.validate_order(order, account, q)
        if not dec.approved:
            order.status = OrderStatus.REJECTED_RISK
            self.ledger.upsert_order(order, dec.text())
            log.warning(explain_order(order, dec))
            return order
        self.ledger.upsert_order(order, dec.text())  # 전송 전 기록(장애 시 추적 가능)
        order = self.broker.place_order(order)
        self.compliance.record("order")
        self.ledger.upsert_order(order)
        if order.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIAL, OrderStatus.FILLED):
            self.open_orders[order.client_order_id] = order
            if order.broker_order_id:
                self._by_broker_id[order.broker_order_id] = order.client_order_id
        log.info(explain_order(order, dec))
        return order

    def collect_fills(self) -> int:
        n = 0
        for f in self.broker.poll_fills():
            f.client_order_id = self._by_broker_id.get(f.client_order_id, f.client_order_id)
            self.ledger.record_fill(f)
            o = self.open_orders.get(f.client_order_id)
            if o:
                o.filled_qty += f.qty
                o.avg_fill_price = f.price
                o.status = OrderStatus.FILLED if o.filled_qty >= o.qty else OrderStatus.PARTIAL
                self.ledger.upsert_order(o)
                if o.status == OrderStatus.FILLED:
                    self.open_orders.pop(o.client_order_id)
            n += 1
        return n

    def cancel_stale(self) -> None:
        now = self.clock()
        for o in list(self.open_orders.values()):
            if now - o.created_at > timedelta(seconds=self.cfg.order_timeout_sec):
                self.broker.cancel_order(o)
                self.compliance.record("cancel")
                self.ledger.upsert_order(o)
                self.open_orders.pop(o.client_order_id, None)

    # ------------------------------------------------------------ 사이클
    def run_cycle(self) -> dict:
        phase = session_phase(self.clock())
        if phase != "continuous" and not self.cfg.allow_outside_session:
            return {"status": "skip", "reason": f"장 구분 {phase}: 신규 주문 없음"}

        self.collect_fills()
        acct = self.broker.get_account()
        self.reconcile(acct)
        self.risk.update_equity(acct.total_equity)
        mv = acct.total_equity - acct.cash
        self.ledger.record_equity(acct.cash, mv, len(acct.positions))
        if self.risk.kill_switch:
            self.ledger.log_event("kill_switch", {"reason": self.risk.kill_reason})
            return {"status": "halted", "reason": self.risk.kill_reason}

        submitted = []
        # 손절 우선
        prices = {t: self.broker.get_quote(t).price for t in acct.positions}
        exits = self.risk.check_exits(acct, prices)
        exit_tickers = set()
        for ex in exits:
            q = self.broker.get_quote(ex.ticker)
            o = Order(ex.ticker, Side.SELL, ex.qty, OrderType.LIMIT, self.limit_price(Side.SELL, q.price, q.base_price), ex.reason)
            submitted.append(self.submit(o, acct))
            exit_tickers.add(ex.ticker)

        targets, reasons = self.target_fn(acct)
        targets = targets.drop(list(exit_tickers), errors="ignore")
        for t, r in reasons.items():
            if t in targets.index:
                self.ledger.record_signal(t, float(targets[t]), 0.0, r)
        for o in self.build_orders(targets, acct, reasons):
            if o.ticker in exit_tickers:
                continue
            submitted.append(self.submit(o, self.broker.get_account()))
        self.collect_fills()
        self.cancel_stale()
        return {"status": "ok", "orders": submitted, "exits": exits}

    def run_forever(self, interval_sec: int = 60) -> None:
        """운영 루프. SIGINT/SIGTERM 시 현재 사이클을 마치고 안전하게 종료."""
        def _stop(*_):
            self._stop = True
            log.info("종료 신호 수신 → 현재 사이클 후 종료")
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        while not self._stop:
            try:
                res = self.run_cycle()
                log.info("cycle: %s", res.get("status"))
                if res.get("status") == "halted":
                    break
            except ReconcileError as e:
                log.critical(str(e))
                break
            except Exception as e:  # 예기치 못한 오류: 기록 후 다음 사이클(연속 실패 시 중단은 운영 정책으로)
                log.exception("사이클 오류: %s", e)
                self.ledger.log_event("cycle_error", {"error": repr(e)})
            time.sleep(interval_sec)
