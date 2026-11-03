"""한국거래소(KRX) 시장 규칙: 호가단위, 가격제한폭, 거래시간.

- 호가단위(tick size): 주문 가격이 반드시 맞춰야 하는 최소 가격 단위.
  2023년 1월부터 KOSPI·KOSDAQ 공통 7단계 체계.
- 가격제한폭(상·하한가): 전일 기준가 대비 ±30%.
- 정규장: 09:00~15:30 (08:30~09:00 시가 단일가, 15:20~15:30 종가 단일가).
  ※ 2025년 출범한 대체거래소(NXT)의 프리·애프터마켓은 본 시스템 범위 밖이다.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone

KST = timezone(timedelta(hours=9))

# (가격 상한 미만, 호가단위)
_TICK_TABLE = [
    (2_000, 1),
    (5_000, 5),
    (20_000, 10),
    (50_000, 50),
    (200_000, 100),
    (500_000, 500),
    (float("inf"), 1_000),
]

PRICE_LIMIT_RATE = 0.30


def tick_size(price: float) -> int:
    if price <= 0:
        raise ValueError(f"가격은 0보다 커야 합니다: {price}")
    for upper, tick in _TICK_TABLE:
        if price < upper:
            return tick
    return 1_000  # pragma: no cover


def round_to_tick(price: float, side: str = "nearest") -> int:
    """가격을 호가단위에 맞춘다.

    side="buy"  → 내림(의도보다 비싸게 사지 않도록)
    side="sell" → 올림(의도보다 싸게 팔지 않도록)
    """
    tick = tick_size(price)
    q = price / tick
    if side == "buy":
        n = math.floor(q + 1e-9)
    elif side == "sell":
        n = math.ceil(q - 1e-9)
    else:
        n = round(q)
    result = int(n * tick)
    # 경계값에서 호가단위가 바뀌는 경우(예: 1,999.5 → 2,000) 재보정
    t2 = tick_size(result)
    if result % t2:
        result = int((math.floor if side == "buy" else math.ceil)(result / t2) * t2)
    return max(result, 1)


def price_limits(base_price: float) -> tuple[int, int]:
    """(하한가, 상한가). 기준가 × (1 ± 30%)를 호가단위로 절사한다."""
    upper_raw = base_price * (1 + PRICE_LIMIT_RATE)
    lower_raw = base_price * (1 - PRICE_LIMIT_RATE)
    upper = int(math.floor(upper_raw / tick_size(upper_raw)) * tick_size(upper_raw))
    lower = int(math.ceil(lower_raw / tick_size(lower_raw)) * tick_size(lower_raw))
    return lower, upper


def is_trading_day(d: date, holidays: set[date] | None = None) -> bool:
    return d.weekday() < 5 and d not in (holidays or set())


def session_phase(now: datetime, holidays: set[date] | None = None) -> str:
    """현재 시각의 장 구분: closed / pre_auction / continuous / close_auction."""
    now = now.astimezone(KST)
    if not is_trading_day(now.date(), holidays):
        return "closed"
    t = now.time()
    if time(8, 30) <= t < time(9, 0):
        return "pre_auction"
    if time(9, 0) <= t < time(15, 20):
        return "continuous"
    if time(15, 20) <= t < time(15, 30):
        return "close_auction"
    return "closed"


def now_kst() -> datetime:
    return datetime.now(tz=KST)
