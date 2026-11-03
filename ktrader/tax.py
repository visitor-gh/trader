"""세금 추정(참고용). 실제 신고는 증권사 양도소득 자료와 세무 전문가 확인을 따른다.

해외주식 양도소득세(개인, 2026년 현행 기준으로 설정 — 매년 세법 개정 확인 필요)
  - 과세 대상: 해외 상장주식 매도로 실현한 연간(1.1~12.31) 순양도차익(이익과 손실 통산)
  - 기본공제: 연 250만원
  - 세율: 22% (양도소득세 20% + 지방소득세 2%)
  - 신고·납부: 다음 해 5월 확정신고
국내 상장주식: 소액주주는 장내 매도 양도세 비과세(대주주 요건 해당 시 과세), 매도 시 거래세는 비용 모델에서 처리.
배당: 미국 배당은 현지 원천징수 15%(조세조약, W-8BEN 제출 시). 금융소득 연 2천만원 초과 시 종합과세.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

OVERSEAS_CGT_DEDUCTION = 2_500_000
OVERSEAS_CGT_RATE = 0.22


@dataclass
class OverseasCgtEstimate:
    year: int
    gains: float
    losses: float
    net: float
    taxable: float
    tax: float

    def text(self) -> str:
        return (f"{self.year}년 해외주식 양도소득 추정: 이익 {self.gains:,.0f}원, 손실 {self.losses:,.0f}원, "
                f"순익 {self.net:,.0f}원 → 기본공제 {OVERSEAS_CGT_DEDUCTION:,}원 차감 후 과세표준 {self.taxable:,.0f}원, "
                f"예상 세액 {self.tax:,.0f}원(22%). ※ 참고용 추정치")


def estimate_overseas_cgt(realized: pd.DataFrame, year: int) -> OverseasCgtEstimate:
    """입력: Ledger.realized_trades() 결과(원화 실현손익, currency, filled_at)"""
    df = realized.copy()
    df["filled_at"] = pd.to_datetime(df["filled_at"], utc=True, format="ISO8601")
    df = df[(df["currency"] != "KRW") & (df["filled_at"].dt.year == year)]
    gains = float(df.loc[df.realized_pnl > 0, "realized_pnl"].sum())
    losses = abs(float(df.loc[df.realized_pnl < 0, "realized_pnl"].sum()))
    net = gains - losses
    taxable = max(net - OVERSEAS_CGT_DEDUCTION, 0.0)
    return OverseasCgtEstimate(year, gains, losses, net, taxable, round(taxable * OVERSEAS_CGT_RATE))
