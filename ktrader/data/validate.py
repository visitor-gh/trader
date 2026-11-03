"""데이터 품질 검증.

잘못된 데이터 한 줄이 잘못된 매매 신호로 이어지므로, 모든 데이터는 분석 전에 이 모듈을 통과해야 한다.
검증 결과는 심각도(ERROR/WARN/INFO)별 이슈 목록으로 돌려주며, ERROR가 하나라도 있으면
assert_quality()가 예외를 발생시켜 파이프라인을 중단한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..krx import PRICE_LIMIT_RATE
from ..markets import MARKETS, parse_symbol

SEVERITY_ORDER = {"INFO": 0, "WARN": 1, "ERROR": 2}


@dataclass
class Issue:
    severity: str
    check: str
    detail: str
    count: int = 0
    dates: list = field(default_factory=list)


@dataclass
class DataQualityReport:
    ticker: str
    rows: int
    issues: list[Issue] = field(default_factory=list)

    @property
    def max_severity(self) -> str:
        if not self.issues:
            return "OK"
        return max((i.severity for i in self.issues), key=SEVERITY_ORDER.get)

    def add(self, severity: str, check: str, detail: str, mask: pd.Series | None = None):
        dates = []
        count = 0
        if mask is not None:
            count = int(mask.sum())
            if count == 0:
                return
            dates = [d.strftime("%Y-%m-%d") for d in mask[mask].index[:5]]
        self.issues.append(Issue(severity, check, detail, count, dates))

    def summary(self) -> str:
        if not self.issues:
            return f"[{self.ticker}] {self.rows}행 검증 통과"
        lines = [f"[{self.ticker}] {self.rows}행, 최고 심각도 {self.max_severity}"]
        for i in self.issues:
            ex = f" 예: {', '.join(i.dates)}" if i.dates else ""
            lines.append(f"  - {i.severity:5s} {i.check}: {i.detail} ({i.count}건){ex}")
        return "\n".join(lines)


class DataQualityError(RuntimeError):
    pass


def validate_ohlcv(
    df: pd.DataFrame,
    ticker: str = "?",
    calendar: pd.DatetimeIndex | None = None,
    adjusted: bool = True,
    stale_days: int = 5,
    market: str | None = None,
) -> DataQualityReport:
    """일봉 데이터 검증.

    입력: OHLCV DataFrame(DatetimeIndex), 기준 거래일 달력(선택)
    출력: DataQualityReport
    """
    rep = DataQualityReport(ticker, len(df))
    required = ["open", "high", "low", "close", "volume"]
    missing_cols = [c for c in required if c not in df.columns]
    if missing_cols:
        rep.add("ERROR", "필수컬럼", f"누락 컬럼 {missing_cols}")
        rep.issues[-1].count = len(missing_cols)
        return rep
    if df.empty:
        rep.issues.append(Issue("ERROR", "데이터없음", "행이 0개", 0))
        return rep

    # 1) 인덱스 무결성
    if not isinstance(df.index, pd.DatetimeIndex):
        rep.issues.append(Issue("ERROR", "인덱스형식", "DatetimeIndex가 아님", 1))
        return rep
    rep.add("ERROR", "중복날짜", "같은 날짜가 두 번 이상 존재", pd.Series(df.index.duplicated(), index=df.index))
    if not df.index.is_monotonic_increasing:
        rep.issues.append(Issue("WARN", "정렬", "날짜 오름차순이 아님(자동 정렬 필요)", 1))
    rep.add("ERROR", "미래날짜", "오늘 이후 날짜 존재", pd.Series(df.index > pd.Timestamp.now().normalize() + pd.Timedelta(days=1), index=df.index))

    d = df[~df.index.duplicated()].sort_index()
    o, h, l, c, v = (d[k] for k in required)

    # 2) 값 범위
    rep.add("ERROR", "결측가격", "가격 컬럼에 NaN", d[["open", "high", "low", "close"]].isna().any(axis=1))
    rep.add("ERROR", "비양수가격", "가격 ≤ 0 (거래정지일 0 기록 여부 확인)", (d[["open", "high", "low", "close"]] <= 0).any(axis=1))
    rep.add("ERROR", "음수거래량", "거래량 < 0", v < 0)

    # 3) OHLC 논리 일관성: 고가 ≥ max(시가, 종가, 저가), 저가 ≤ min(시가, 종가)
    rep.add("ERROR", "OHLC불일치", "고가<시가/종가 또는 저가>시가/종가", (h < np.maximum(o, c)) | (l > np.minimum(o, c)) | (h < l))

    # 4) 가격제한폭 초과 변동 → 수정주가 미반영(액면분할 등) 또는 오류
    ret = c.pct_change()
    if market is None:
        try:
            market = parse_symbol(ticker).market
        except ValueError:
            market = "KR"
    limit = MARKETS[market].price_limit if market in MARKETS else PRICE_LIMIT_RATE
    if limit is not None:
        limit_break = ret.abs() > limit + 0.005
        sev = "ERROR" if adjusted else "WARN"
        rep.add(sev, "가격제한폭초과", f"일간 변동률 ±{limit:.0%} 초과(분할·병합 미보정 또는 신규상장일 의심)", limit_break.fillna(False))
    else:
        # 가격제한폭이 없는 시장(미국·일본·홍콩): 실제 급등락이 가능하므로 경고로만 표시
        # 정확히 1/2, 1/3, 1/4 … 또는 2배·3배 근처의 급변은 분할·병합 미반영 가능성이 높다
        ratio = (c / c.shift(1)).dropna()
        near_split = ratio.apply(lambda r: any(abs(r / k - 1) < 0.08 for k in (0.5, 1 / 3, 0.25, 0.2, 0.1, 2, 3, 4, 5, 10)))  # 분할비율 ± 일반적 하루 변동
        rep.add("ERROR" if adjusted else "WARN", "분할의심", "분할·병합 비율(1/2, 1/3, 2배 등)과 일치하는 하루 변동",
                near_split.reindex(c.index, fill_value=False))
        rep.add("WARN", "급등락", "일간 변동률 ±50% 초과(가격제한 없는 시장, 이벤트·오류 확인)",
                (ret.abs() > 0.5).fillna(False) & ~near_split.reindex(c.index, fill_value=False))

    # 5) 거래정지 의심: 거래량 0
    rep.add("WARN", "거래량0", "거래량 0(거래정지·매매거래중단 가능)", v == 0)

    # 6) 가격 고착: N일 연속 종가 동일 & 거래량 0
    stale = (c.diff() == 0) & (v == 0)
    stale_run = stale.groupby((~stale).cumsum()).cumsum() >= stale_days
    rep.add("WARN", "가격고착", f"{stale_days}일 이상 종가 변화·거래 없음", stale_run)

    # 7) 거래량 이상치(로그 거래량 z-score > 6)
    lv = np.log1p(v)
    z = (lv - lv.rolling(60, min_periods=20).mean()) / lv.rolling(60, min_periods=20).std()
    rep.add("INFO", "거래량급증", "거래량 z-score > 6 (공시·이벤트 확인 권장)", (z > 6).fillna(False))

    # 8) 거래일 누락
    if calendar is not None:
        cal = calendar[(calendar >= d.index.min()) & (calendar <= d.index.max())]
        missing = cal.difference(d.index)
        if len(missing):
            rep.issues.append(
                Issue("WARN", "거래일누락", "거래소 달력 대비 빠진 거래일", len(missing), [x.strftime("%Y-%m-%d") for x in missing[:5]])
            )
        extra = d.index.difference(cal)
        if len(extra):
            rep.issues.append(
                Issue("ERROR", "휴장일데이터", "휴장일에 데이터 존재", len(extra), [x.strftime("%Y-%m-%d") for x in extra[:5]])
            )
    return rep


def validate_fundamentals(df: pd.DataFrame) -> DataQualityReport:
    """밸류에이션 스냅샷 검증(index=ticker)."""
    rep = DataQualityReport("fundamentals", len(df))
    for col in ("PER", "PBR"):
        if col in df.columns:
            rep.add("INFO", f"{col}=0", f"{col}가 0(적자 또는 자본잠식 → 별도 처리)", df[col] == 0)
            rep.add("WARN", f"{col}극단값", f"{col} > 1000", df[col] > 1000)
    if "market_cap" in df.columns:
        rep.add("ERROR", "시가총액비양수", "시가총액 ≤ 0", df["market_cap"] <= 0)
    def bad_symbol(x):
        try:
            parse_symbol(str(x))
            return False
        except ValueError:
            return True
    rep.add("ERROR", "종목코드형식", "해석 불가 종목 표기(예: 005930, AAPL, 7203.T, 0700.HK)",
            pd.Series([bad_symbol(x) for x in df.index], index=df.index))
    return rep


def assert_quality(report: DataQualityReport, fail_on: str = "ERROR") -> None:
    if report.max_severity != "OK" and SEVERITY_ORDER[report.max_severity] >= SEVERITY_ORDER[fail_on]:
        raise DataQualityError(report.summary())
