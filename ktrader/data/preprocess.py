"""전처리: 정렬·중복 제거·거래일 정렬·거래정지 처리·시점 정합(point-in-time) 재무 데이터."""
from __future__ import annotations

import numpy as np
import pandas as pd

# 재무제표 공시 지연(결산일 → 실제 공개일). 법정 제출기한 기준 보수적 가정.
# 사업보고서: 결산 후 90일, 분기·반기보고서: 분기 종료 후 45일
REPORT_LAG_DAYS = {"FY": 90, "Q": 45}


def clean_ohlcv(df: pd.DataFrame, calendar: pd.DatetimeIndex | None = None) -> pd.DataFrame:
    """입력: 원시 OHLCV, 거래소 달력
    출력: 정제된 OHLCV + 'suspended'(거래정지 추정) 컬럼

    처리 규칙
      - 중복 날짜는 마지막 값 유지, 날짜 오름차순 정렬
      - 달력 기준 재색인: 상장 전 구간은 NaN 유지(채우지 않음 → 생존편향·미래정보 방지)
      - 상장 후 누락일/거래량 0일은 '거래정지'로 표시, 종가는 직전값으로 채우고 시·고·저가는 종가로 맞춤
      - 거래정지일에는 백테스트에서 주문이 체결되지 않도록 suspended=True 플래그를 남김
    """
    d = df[~df.index.duplicated(keep="last")].sort_index().copy()
    if calendar is not None:
        cal = calendar[calendar >= d.index.min()]
        d = d.reindex(cal)
    d["suspended"] = d["close"].isna() | (d["volume"].fillna(0) == 0)
    d["close"] = d["close"].ffill()
    for col in ("open", "high", "low"):
        d[col] = d[col].where(~d["suspended"], d["close"])
    d["volume"] = d["volume"].fillna(0)
    if "value" in d.columns:
        d["value"] = d["value"].fillna(0)
    return d


def winsorize(s: pd.Series, lower: float = 0.01, upper: float = 0.99) -> pd.Series:
    """극단값(상·하위 1%)을 경계값으로 잘라 팩터 점수가 소수 종목에 휘둘리지 않게 한다."""
    lo, hi = s.quantile(lower), s.quantile(upper)
    return s.clip(lo, hi)


def zscore(s: pd.Series) -> pd.Series:
    std = s.std(ddof=0)
    if not np.isfinite(std) or std == 0:
        return s * 0.0
    return (s - s.mean()) / std


def align_fundamentals_pit(
    records: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    value_col: str,
) -> pd.DataFrame:
    """재무 데이터를 '그 날짜에 실제로 알 수 있었던 값'으로 정렬한다(미래 정보 누수 방지).

    입력 records: 컬럼 [ticker, period_end(결산일), report(FY|Q), value_col, (선택) disclosed_at]
    출력: DataFrame(날짜×종목) — 각 날짜에 공개되어 있던 최신 값
    disclosed_at(실제 공시일)이 있으면 그것을, 없으면 결산일+법정기한을 공개일로 간주한다.
    """
    r = records.copy()
    r["period_end"] = pd.to_datetime(r["period_end"])
    if "disclosed_at" in r.columns:
        r["available"] = pd.to_datetime(r["disclosed_at"])
    else:
        lag = r["report"].map(lambda x: REPORT_LAG_DAYS["FY"] if x == "FY" else REPORT_LAG_DAYS["Q"])
        r["available"] = r["period_end"] + pd.to_timedelta(lag, unit="D")
    wide = r.sort_values("available").pivot_table(index="available", columns="ticker", values=value_col, aggfunc="last")
    return wide.reindex(wide.index.union(calendar)).ffill().reindex(calendar)


def build_trading_calendar(panel_close: pd.DataFrame, min_coverage: float = 0.5) -> pd.DatetimeIndex:
    """여러 종목 중 절반 이상이 데이터를 가진 날을 거래일로 간주(공식 달력이 없을 때의 대안).
    운영에서는 KRX 휴장일 공지(또는 exchange_calendars 'XKRX')를 사용한다.
    """
    coverage = panel_close.notna().mean(axis=1)
    return pd.DatetimeIndex(coverage[coverage >= min_coverage].index)
