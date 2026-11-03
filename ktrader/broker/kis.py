"""한국투자증권(KIS) Open API REST 어댑터.

공식 문서: https://apiportal.koreainvestment.com  (샘플: github.com/koreainvestment/open-trading-api)

주의
  - TR_ID(거래 식별 코드)는 증권사 사정으로 변경될 수 있다(2024년 주문 TR_ID 신규 코드 전환 사례).
    운영 전 개발자센터 최신 문서와 TR_IDS 표를 반드시 대조한다.
  - 접근토큰(access token)은 유효기간 약 24시간이며, 발급 요청 빈도 제한(약 1분 1회)이 있다.
    → 메모리 캐시 + 만료 5분 전 재발급.
  - 초당 요청 수 제한(실전·모의 서버별로 다름)을 넘으면 오류가 나므로 RateLimiter로 간격을 둔다.
  - 모든 응답은 rt_cd == "0" 이 성공이다. 실패 시 msg_cd, msg1을 그대로 기록한다.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

import requests

from ..config import CostModel, KISConfig
from ..data.collector import RateLimiter
from .base import AccountSnapshot, Broker, Fill, Order, OrderStatus, OrderType, Position, Quote, Side

log = logging.getLogger(__name__)

# (실전, 모의) TR_ID
TR_IDS = {
    "buy": ("TTTC0012U", "VTTC0012U"),
    "sell": ("TTTC0011U", "VTTC0011U"),
    "cancel": ("TTTC0013U", "VTTC0013U"),
    "balance": ("TTTC8434R", "VTTC8434R"),
    "daily_ccld": ("TTTC0081R", "VTTC0081R"),
    "price": ("FHKST01010100", "FHKST01010100"),
    "daily_chart": ("FHKST03010100", "FHKST03010100"),
}

# 종목 상태 구분 코드(현재가 조회 iscd_stat_cls_code) — 신규 매수를 막을 코드
RESTRICTED_STATUS = {
    "51": "관리종목", "52": "투자위험", "53": "투자경고", "58": "거래정지", "59": "단기과열",
}


class KISError(RuntimeError):
    def __init__(self, code: str, msg: str):
        super().__init__(f"[{code}] {msg}")
        self.code, self.msg = code, msg


class KISBroker(Broker):
    def __init__(self, cfg: KISConfig, cost: CostModel | None = None, session: requests.Session | None = None,
                 min_interval_sec: float | None = None):
        if not (cfg.app_key and cfg.app_secret and cfg.account_no):
            raise ValueError("KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT_NO 환경변수가 필요합니다.")
        cano, _, prdt = cfg.account_no.partition("-")
        if len(cano) != 8 or len(prdt) != 2 or not (cano + prdt).isdigit():
            raise ValueError("계좌번호 형식은 '12345678-01' 이어야 합니다.")
        self.cfg, self.cano, self.prdt = cfg, cano, prdt
        self.cost = cost or CostModel()
        self.s = session or requests.Session()
        # 모의서버는 실전보다 허용 빈도가 낮으므로 더 보수적으로 둔다.
        self.limiter = RateLimiter(min_interval_sec if min_interval_sec is not None else (0.5 if cfg.is_mock else 0.07))
        self._token: str | None = None
        self._token_exp = datetime.min
        self._lock = threading.Lock()
        self._seen_fills: dict[str, int] = {}

    # ---------------------------------------------------------------- 공통
    def _tr(self, key: str) -> str:
        return TR_IDS[key][1 if self.cfg.is_mock else 0]

    def _access_token(self) -> str:
        with self._lock:
            if self._token and datetime.now() < self._token_exp - timedelta(minutes=5):
                return self._token
            r = self.s.post(f"{self.cfg.base_url}/oauth2/tokenP", json={
                "grant_type": "client_credentials", "appkey": self.cfg.app_key, "appsecret": self.cfg.app_secret,
            }, timeout=10)
            r.raise_for_status()
            js = r.json()
            self._token = js["access_token"]
            self._token_exp = datetime.now() + timedelta(seconds=int(js.get("expires_in", 86400)))
            log.info("KIS 접근토큰 발급(만료 %s)", self._token_exp)
            return self._token

    def _headers(self, tr_id: str) -> dict:
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._access_token()}",
            "appkey": self.cfg.app_key,
            "appsecret": self.cfg.app_secret,
            "tr_id": tr_id,
            "custtype": "P",  # 개인
        }

    def _request(self, method: str, path: str, tr_id: str, params=None, body=None, retries: int = 3) -> dict:
        url = f"{self.cfg.base_url}{path}"
        for i in range(retries):
            self.limiter.wait()
            try:
                r = self.s.request(method, url, headers=self._headers(tr_id), params=params, json=body, timeout=10)
                if r.status_code >= 500 or r.status_code == 429:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                js = r.json()
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
                # 주문(POST)은 네트워크 오류 시 '접수됐는지 모르는' 상태가 되므로 재전송하지 않는다.
                # → 호출자가 체결조회로 확인(reconcile) 후 재주문 여부를 판단한다.
                if method == "POST" or i == retries - 1:
                    raise
                time.sleep(2**i)
                log.warning("KIS 재시도 %s %s: %s", tr_id, path, e)
                continue
            if js.get("rt_cd") != "0":
                raise KISError(js.get("msg_cd", "?"), js.get("msg1", "알 수 없는 오류"))
            return js
        raise RuntimeError("unreachable")  # pragma: no cover

    # ---------------------------------------------------------------- 시세
    def get_quote(self, ticker: str) -> Quote:
        js = self._request("GET", "/uapi/domestic-stock/v1/quotations/inquire-price", self._tr("price"),
                           params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker})
        o = js["output"]
        status = o.get("iscd_stat_cls_code", "")
        return Quote(
            ticker=ticker, price=float(o["stck_prpr"]), base_price=float(o.get("stck_sdpr") or o["stck_prpr"]),
            upper_limit=float(o["stck_mxpr"]), lower_limit=float(o["stck_llam"]),
            volume=float(o.get("acml_vol", 0)), status_code=status,
            halted=o.get("temp_stop_yn") == "Y" or status == "58",
        )

    def get_daily_ohlcv(self, ticker: str, start: str, end: str, adjusted: bool = True) -> list[dict]:
        """일봉(최대 100건/회). 데이터 수집 폴백 소스로 사용."""
        js = self._request("GET", "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                           self._tr("daily_chart"), params={
                               "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker,
                               "FID_INPUT_DATE_1": start, "FID_INPUT_DATE_2": end,
                               "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0" if adjusted else "1",
                           })
        return [
            {"date": r["stck_bsop_date"], "open": float(r["stck_oprc"]), "high": float(r["stck_hgpr"]),
             "low": float(r["stck_lwpr"]), "close": float(r["stck_clpr"]), "volume": float(r["acml_vol"]),
             "value": float(r.get("acml_tr_pbmn", 0))}
            for r in js.get("output2", []) if r.get("stck_bsop_date")
        ]

    # ---------------------------------------------------------------- 계좌
    def get_account(self) -> AccountSnapshot:
        js = self._request("GET", "/uapi/domestic-stock/v1/trading/inquire-balance", self._tr("balance"), params={
            "CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "AFHR_FLPR_YN": "N", "OFL_YN": "",
            "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        })
        positions = {}
        for r in js.get("output1", []):
            qty = int(r["hldg_qty"])
            if qty > 0:
                positions[r["pdno"]] = Position(r["pdno"], qty, float(r["pchs_avg_pric"]), float(r["prpr"]), r.get("prdt_name", ""))
        s = js["output2"][0]
        # 주의: 연속조회(tr_cont, CTX_AREA_*)가 필요한 50종목 이상 계좌는 페이지 반복 구현 필요
        return AccountSnapshot(cash=float(s["dnca_tot_amt"]), positions=positions, total_equity=float(s["tot_evlu_amt"]))

    # ---------------------------------------------------------------- 주문
    def place_order(self, order: Order) -> Order:
        body = {
            "CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "PDNO": order.ticker,
            "ORD_DVSN": "00" if order.order_type == OrderType.LIMIT else "01",  # 00 지정가, 01 시장가
            "ORD_QTY": str(int(order.qty)),
            "ORD_UNPR": str(int(order.price)) if order.order_type == OrderType.LIMIT else "0",
        }
        try:
            js = self._request("POST", "/uapi/domestic-stock/v1/trading/order-cash",
                               self._tr("buy" if order.side == Side.BUY else "sell"), body=body)
        except KISError as e:
            order.status, order.message = OrderStatus.REJECTED, str(e)
            return order
        except Exception as e:  # 접수 여부 불명 → ERROR로 두고 상위에서 체결조회로 확인
            order.status, order.message = OrderStatus.ERROR, f"접수 불명: {e}"
            return order
        out = js["output"]
        order.broker_order_id = out["ODNO"]
        order.message = f"{out.get('KRX_FWDG_ORD_ORGNO', '')}|{js.get('msg1', '')}"
        order.status = OrderStatus.SUBMITTED
        return order

    def cancel_order(self, order: Order) -> Order:
        org_no = order.message.split("|", 1)[0]
        body = {
            "CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "KRX_FWDG_ORD_ORGNO": org_no,
            "ORGN_ODNO": order.broker_order_id, "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "02",  # 02 취소
            "ORD_QTY": "0", "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y",
        }
        try:
            self._request("POST", "/uapi/domestic-stock/v1/trading/order-rvsecncl", self._tr("cancel"), body=body)
            order.status = OrderStatus.CANCELLED
        except KISError as e:
            order.message += f" / 취소실패 {e}"
        return order

    def poll_fills(self) -> list[Fill]:
        """당일 체결 내역 조회 → 처음 보는 체결만 반환(실시간은 웹소켓 체결통보 H0STCNI0 권장)."""
        today = datetime.now().strftime("%Y%m%d")
        js = self._request("GET", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", self._tr("daily_ccld"), params={
            "CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "INQR_STRT_DT": today, "INQR_END_DT": today,
            "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", "PDNO": "", "CCLD_DVSN": "01", "ORD_GNO_BRNO": "",
            "ODNO": "", "INQR_DVSN_3": "00", "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        })
        fills = []
        for r in js.get("output1", []):
            cum_qty = int(r.get("tot_ccld_qty", 0))           # 누적 체결수량
            qty = cum_qty - self._seen_fills.get(r["odno"], 0)  # 이번에 새로 체결된 수량(부분체결 대응)
            if qty <= 0:
                continue
            self._seen_fills[r["odno"]] = cum_qty
            side = Side.SELL if r.get("sll_buy_dvsn_cd") == "01" else Side.BUY  # 01 매도, 02 매수
            px = float(r.get("avg_prvs", 0))  # 평균체결가(부분체결 반복 시 근사치)
            notional = qty * px
            # client_order_id 자리에 증권사 주문번호를 넣고, 실행엔진이 자체 주문ID로 매핑한다.
            fills.append(Fill(r["odno"], r["pdno"], side, qty, px, notional * self.cost.commission_rate,
                              notional * self.cost.sell_tax_rate if side == Side.SELL else 0.0))
        return fills

    @staticmethod
    def restricted_reason(q: Quote) -> str | None:
        if q.halted:
            return "거래정지"
        return RESTRICTED_STATUS.get(q.status_code)
