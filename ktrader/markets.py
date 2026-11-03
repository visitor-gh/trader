"""다중 시장(국내·해외) 규칙 레지스트리.

종목 식별 규칙(야후 파이낸스 표기와 호환):
  005930 / 005930.KS / 035720.KQ → KR(한국거래소)
  AAPL, BRK-B                     → US(뉴욕증권거래소·나스닥·NYSE American)
  7203.T                          → JP(도쿄증권거래소)
  0700.HK                         → HK(홍콩거래소)

시장마다 다른 것: 통화, 시간대(서머타임 포함), 거래시간(점심 휴장), 호가단위, 가격제한폭,
매매단위(일본 100주, 홍콩 종목별 board lot), 결제주기, 거래비용.
새 시장(중국·베트남 등)은 MarketSpec 하나를 추가하고 parse_symbol 규칙만 넣으면 된다.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Callable
from zoneinfo import ZoneInfo

from . import krx


# --------------------------------------------------------------------- 호가단위
def _us_tick(price: float) -> float:
    # 미국: 1달러 이상 0.01달러, 1달러 미만 0.0001달러(Reg NMS 612조)
    return 0.01 if price >= 1.0 else 0.0001


_JP_TICKS = [(3_000, 1), (5_000, 5), (30_000, 10), (50_000, 50), (300_000, 100), (500_000, 500),
             (3_000_000, 1_000), (5_000_000, 5_000), (30_000_000, 10_000), (50_000_000, 50_000),
             (float("inf"), 100_000)]


def _jp_tick(price: float) -> float:
    # 도쿄증권거래소 일반 종목 호가단위(TOPIX500 구성종목은 더 촘촘한 별도 표 적용 → 운영 시 종목별 확인)
    for upper, t in _JP_TICKS:
        if price <= upper:
            return t
    return 100_000  # pragma: no cover


_HK_TICKS = [(0.25, 0.001), (0.5, 0.005), (10, 0.01), (20, 0.02), (100, 0.05), (200, 0.1),
             (500, 0.2), (1_000, 0.5), (2_000, 1), (5_000, 2), (float("inf"), 5)]


def _hk_tick(price: float) -> float:
    # 홍콩거래소 호가 스프레드 표(Spread Table)
    for upper, t in _HK_TICKS:
        if price <= upper:
            return t
    return 5  # pragma: no cover


@dataclass(frozen=True)
class Session:
    start: time
    end: time


@dataclass(frozen=True)
class MarketSpec:
    code: str
    name_ko: str
    currency: str
    tz: str
    sessions: tuple[Session, ...]                 # 연속매매 구간(점심 휴장은 구간 분리)
    tick_fn: Callable[[float], float]
    price_limit: float | None                     # 일일 가격제한폭 비율(없으면 None)
    default_lot: int = 1                          # 매매단위
    settlement: str = "T+2"
    open_auction: Session | None = None           # 시가 단일가(동시호가)
    close_auction: Session | None = None          # 종가 단일가
    kis_order_exchanges: tuple[str, ...] = ()     # KIS 주문용 거래소 코드(OVRS_EXCG_CD)
    kis_quote_exchanges: tuple[str, ...] = ()     # KIS 시세용 거래소 코드(EXCD)
    notes: str = ""

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    # ----------------------------------------------------------------- 가격
    def tick_size(self, price: float) -> float:
        if price <= 0:
            raise ValueError(f"가격은 0보다 커야 합니다: {price}")
        return self.tick_fn(price)

    def round_price(self, price: float, side: str = "nearest") -> float:
        """매수 내림, 매도 올림. 부동소수 오차를 없애기 위해 정수 배수로 계산."""
        tick = self.tick_size(price)
        q = price / tick
        n = math.floor(q + 1e-9) if side == "buy" else math.ceil(q - 1e-9) if side == "sell" else round(q)
        decimals = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
        out = round(max(n, 1) * tick, decimals)
        return int(out) if tick >= 1 else out

    def is_on_tick(self, price: float) -> bool:
        tick = self.tick_size(price)
        return abs(price / tick - round(price / tick)) < 1e-6

    def price_limits(self, base_price: float) -> tuple[float, float]:
        """(하한, 상한). 가격제한폭이 없는 시장은 (0, inf)."""
        if self.code == "KR":
            return krx.price_limits(base_price)
        if self.price_limit is None:
            return 0.0, float("inf")
        return base_price * (1 - self.price_limit), base_price * (1 + self.price_limit)

    # ----------------------------------------------------------------- 수량
    def round_lot(self, qty: float, lot: int | None = None) -> int:
        lot = lot or self.default_lot
        return int(math.floor(qty / lot + 1e-9) * lot)

    # ----------------------------------------------------------------- 시간
    def local_now(self, now: datetime) -> datetime:
        return now.astimezone(self.zone)

    def session_phase(self, now: datetime, holidays: set[date] | None = None) -> str:
        """closed / pre_auction / continuous / break / close_auction (현지 시각 기준, 서머타임 자동 반영)."""
        local = self.local_now(now)
        if local.weekday() >= 5 or local.date() in (holidays or set()):
            return "closed"
        t = local.time()
        if self.open_auction and self.open_auction.start <= t < self.open_auction.end:
            return "pre_auction"
        if self.close_auction and self.close_auction.start <= t < self.close_auction.end:
            return "close_auction"
        for s in self.sessions:
            if s.start <= t < s.end:
                return "continuous"
        if self.sessions[0].start <= t < self.sessions[-1].end:
            return "break"
        return "closed"


MARKETS: dict[str, MarketSpec] = {
    "KR": MarketSpec(
        "KR", "한국(KOSPI·KOSDAQ)", "KRW", "Asia/Seoul",
        (Session(time(9, 0), time(15, 20)),), krx.tick_size, 0.30, 1, "T+2",
        open_auction=Session(time(8, 30), time(9, 0)), close_auction=Session(time(15, 20), time(15, 30)),
        notes="상·하한가 ±30%, 2023년 통합 호가단위",
    ),
    "US": MarketSpec(
        "US", "미국(NYSE·NASDAQ·AMEX)", "USD", "America/New_York",
        (Session(time(9, 30), time(16, 0)),), _us_tick, None, 1, "T+1",
        kis_order_exchanges=("NASD", "NYSE", "AMEX"), kis_quote_exchanges=("NAS", "NYS", "AMS"),
        notes="가격제한폭 없음(LULD 변동성 일시정지·서킷브레이커), 2024.5.28부터 T+1 결제, 서머타임 적용",
    ),
    "JP": MarketSpec(
        "JP", "일본(도쿄증권거래소)", "JPY", "Asia/Tokyo",
        (Session(time(9, 0), time(11, 30)), Session(time(12, 30), time(15, 25))), _jp_tick, None, 100, "T+2",
        close_auction=Session(time(15, 25), time(15, 30)),
        kis_order_exchanges=("TKSE",), kis_quote_exchanges=("TSE",),
        notes="매매단위 100주, 가격대별 '절대금액' 일일 가격제한(제한폭 표는 거래소 공시 참조), 2024.11 종료시각 15:30 연장",
    ),
    "HK": MarketSpec(
        "HK", "홍콩(HKEX)", "HKD", "Asia/Hong_Kong",
        (Session(time(9, 30), time(12, 0)), Session(time(13, 0), time(16, 0))), _hk_tick, None, 100, "T+2",
        open_auction=Session(time(9, 0), time(9, 30)), close_auction=Session(time(16, 0), time(16, 10)),
        kis_order_exchanges=("SEHK",), kis_quote_exchanges=("HKS",),
        notes="가격제한폭 없음(VCM 변동성 완화장치), 종목별 매매단위(board lot) 상이",
    ),
}


@dataclass(frozen=True)
class Instrument:
    symbol: str          # 시스템 내부 키(예: AAPL, 7203.T, 005930)
    market: str          # KR / US / JP / HK
    local_code: str      # 거래소 원 코드(예: AAPL, 7203, 00700, 005930)
    lot: int = 1
    exchange: str = ""   # 세부 거래소(NASD/NYSE/AMEX 등) — 미국 주문 시 필요

    @property
    def spec(self) -> MarketSpec:
        return MARKETS[self.market]

    @property
    def currency(self) -> str:
        return self.spec.currency


_KR_RE = re.compile(r"^(\d{6})(?:\.(KS|KQ))?$")
_US_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

# 종목별 매매단위·세부거래소 등 예외 정보(운영 시 종목 마스터 파일로 관리)
INSTRUMENT_OVERRIDES: dict[str, dict] = {}


def parse_symbol(symbol: str) -> Instrument:
    """문자열 → Instrument. 형식이 맞지 않으면 ValueError(오입력 방지)."""
    s = symbol.strip().upper()
    ov = INSTRUMENT_OVERRIDES.get(s, {})
    if m := _KR_RE.match(s):
        return Instrument(s, "KR", m.group(1), **ov)
    if s.endswith(".T") and s[:-2].isdigit() and len(s[:-2]) == 4:
        return Instrument(s, "JP", s[:-2], ov.get("lot", 100), ov.get("exchange", "TKSE"))
    if s.endswith(".HK") and s[:-3].isdigit():
        return Instrument(s, "HK", s[:-3].zfill(5), ov.get("lot", 100), ov.get("exchange", "SEHK"))
    if _US_RE.match(s) and not s[0].isdigit():
        return Instrument(s, "US", s, ov.get("lot", 1), ov.get("exchange", "NASD"))
    raise ValueError(f"종목 표기를 해석할 수 없습니다: {symbol!r} (예: 005930, AAPL, 7203.T, 0700.HK)")


def register_instrument(symbol: str, **overrides) -> None:
    """예: register_instrument('0005.HK', lot=400), register_instrument('KO', exchange='NYSE')"""
    INSTRUMENT_OVERRIDES[symbol.upper()] = overrides


def market_of(symbol: str) -> str:
    return parse_symbol(symbol).market
