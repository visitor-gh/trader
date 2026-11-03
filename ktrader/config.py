"""시스템 전역 설정.

모든 수치(수수료, 세율, 리스크 한도)는 이 파일 한 곳에서만 정의한다.
세율·수수료는 법령과 증권사 계약에 따라 바뀌므로 운영 전 반드시 최신 값으로 확인한다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum


class TradingMode(str, Enum):
    PAPER = "paper"  # 로컬 모의체결(가상 브로커). 외부 주문 없음
    MOCK = "mock"    # 증권사 모의투자 서버(한국투자증권 모의계좌)
    REAL = "real"    # 실계좌 주문. 이중 확인 절차 필요


@dataclass(frozen=True)
class CostModel:
    """거래 비용 모델.

    commission_rate : 위탁수수료율(매수·매도 각각 부과). 증권사·계약별로 다르다.
    sell_tax_rate   : 매도 시 부과되는 증권거래세(+농어촌특별세) 합계.
                      2026년 시행 기준 KOSPI·KOSDAQ 모두 0.20%로 설정했다.
                      세법 개정 시 반드시 갱신한다.
    slippage_bps    : 슬리피지(예상 체결가와 실제 체결가의 차이), 1bp = 0.01%.
    """

    commission_rate: float = 0.00015
    sell_tax_rate: float = 0.0020
    slippage_bps: float = 10.0

    def buy_cost(self, notional: float) -> float:
        return notional * self.commission_rate

    def sell_cost(self, notional: float) -> float:
        return notional * (self.commission_rate + self.sell_tax_rate)


@dataclass(frozen=True)
class RiskConfig:
    """리스크 한도. 비율 값은 모두 계좌 총평가금액(equity) 대비."""

    max_position_weight: float = 0.10       # 한 종목 최대 비중
    max_positions: int = 20                 # 최대 보유 종목 수
    max_gross_exposure: float = 1.00        # 총 투자 비중(레버리지·미수 금지 → 1.0 이하)
    risk_per_trade: float = 0.01            # ATR 기반 사이징 시 1회 거래 허용 손실
    atr_multiple: float = 2.0               # 손절 거리 = ATR × 배수
    stop_loss_pct: float = 0.08             # 매입가 대비 고정 손절선
    trailing_stop_pct: float = 0.12         # 보유 중 최고가 대비 추적 손절선
    daily_loss_limit: float = 0.02          # 당일 손실 한도 → 초과 시 신규 매수 중단
    max_drawdown_kill: float = 0.15         # 고점 대비 손실 한도 → 초과 시 킬 스위치(전체 중단)
    max_order_value: float = 20_000_000     # 1회 주문 최대 금액(원) — 팻 핑거(오입력) 방지
    max_price_deviation: float = 0.03       # 지정가와 현재가 괴리 허용 범위
    max_orders_per_minute: int = 30         # 주문 폭주 방지
    duplicate_order_window_sec: int = 60    # 같은 종목·같은 방향 중복 주문 차단 시간
    min_avg_trading_value: float = 1_000_000_000  # 20일 평균 거래대금 하한(원) — 유동성 필터
    max_participation: float = 0.05         # 일 거래량 대비 최대 주문 비율


@dataclass(frozen=True)
class KISConfig:
    """한국투자증권 Open API 접속 정보. 비밀값은 반드시 환경변수로만 주입한다."""

    app_key: str = ""
    app_secret: str = ""
    account_no: str = ""  # "12345678-01" 형식(앞 8자리 CANO, 뒤 2자리 상품코드)
    is_mock: bool = True

    @property
    def base_url(self) -> str:
        if self.is_mock:
            return "https://openapivts.koreainvestment.com:29443"
        return "https://openapi.koreainvestment.com:9443"

    @staticmethod
    def from_env(is_mock: bool = True) -> "KISConfig":
        return KISConfig(
            app_key=os.environ.get("KIS_APP_KEY", ""),
            app_secret=os.environ.get("KIS_APP_SECRET", ""),
            account_no=os.environ.get("KIS_ACCOUNT_NO", ""),
            is_mock=is_mock,
        )


@dataclass
class AppConfig:
    mode: TradingMode = TradingMode.PAPER
    data_dir: str = "./var/data"
    db_path: str = "./var/ledger.sqlite3"
    report_dir: str = "./var/reports"
    cost: CostModel = field(default_factory=CostModel)
    risk: RiskConfig = field(default_factory=RiskConfig)
    dart_api_key: str = ""

    @staticmethod
    def from_env() -> "AppConfig":
        mode = TradingMode(os.environ.get("TRADER_MODE", "paper"))
        return AppConfig(
            mode=mode,
            data_dir=os.environ.get("TRADER_DATA_DIR", "./var/data"),
            db_path=os.environ.get("TRADER_DB_PATH", "./var/ledger.sqlite3"),
            report_dir=os.environ.get("TRADER_REPORT_DIR", "./var/reports"),
            dart_api_key=os.environ.get("DART_API_KEY", ""),
        )
