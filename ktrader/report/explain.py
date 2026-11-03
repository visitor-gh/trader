"""사용자 설명(Explainability) 생성기.

'왜 이 종목인가', '왜 이 주문을 냈는가/막았는가', '성과는 어떤 의미인가'를 한국어 문장으로 만든다.
점수는 팩터 기여도(가중치 × 표준화 점수)의 합이므로, 기여도를 정렬해 상위 근거와 약점을 제시한다.
"""
from __future__ import annotations

import pandas as pd

from ..analysis.factors import FACTOR_SPECS
from ..backtest.metrics import format_metrics
from ..markets import MARKETS, parse_symbol

DISCLAIMER = ("※ 본 결과는 과거 데이터와 통계 모델에 근거한 참고 정보이며, 미래 수익을 보장하지 않습니다. "
              "투자 판단과 그 결과에 대한 책임은 투자자 본인에게 있습니다.")


def _level(z: float) -> str:
    if pd.isna(z):
        return "데이터 없음"
    if z >= 1.0:
        return "매우 우수"
    if z >= 0.3:
        return "우수"
    if z > -0.3:
        return "보통"
    if z > -1.0:
        return "미흡"
    return "매우 미흡"


def explain_recommendation(ticker: str, row: pd.Series, name: str = "", universe_size: int | None = None,
                           raw: pd.Series | None = None) -> str:
    """입력: ScreenResult.scores의 한 행, (선택) 원본 팩터값
    출력: 사람이 읽을 수 있는 추천 근거 문단
    """
    contribs = {c[len("contrib_"):]: row[c] for c in row.index if c.startswith("contrib_")}
    ranked = sorted(contribs.items(), key=lambda kv: kv[1], reverse=True)
    mk = f" · {MARKETS[row['market']].name_ko}" if "market" in row.index and row["market"] in MARKETS else ""
    head = f"[{ticker} {name}{mk}] 종합점수 {row['composite']:+.2f} (순위 {int(row['rank'])}"
    head += f"/{universe_size})" if universe_size else ")"
    lines = [head, "  ▶ 선정 근거(기여도 큰 순):"]
    for f, c in ranked[:3]:
        if c <= 0:
            break
        spec = FACTOR_SPECS[f]
        val = f", 실제값 {raw[f]:.3g}" if raw is not None and f in raw and pd.notna(raw[f]) else ""
        lines.append(f"    + {spec.label_ko}: {_level(row[f'z_{f}'])} (기여 {c:+.2f}{val}) — {spec.desc_ko}")
    weak = [(f, c) for f, c in ranked if c < 0][-2:]
    if weak:
        lines.append("  ▷ 유의할 약점:")
        for f, c in reversed(weak):
            spec = FACTOR_SPECS[f]
            lines.append(f"    - {spec.label_ko}: {_level(row[f'z_{f}'])} (기여 {c:+.2f})")
    return "\n".join(lines)


def explain_exclusions(excluded: pd.Series, top: int = 5) -> str:
    if excluded.empty:
        return "제외된 종목 없음"
    counts = excluded.value_counts()
    parts = [f"{why} {n}개" for why, n in counts.head(top).items()]
    return "투자 부적격으로 제외: " + ", ".join(parts)


def explain_order(order, decision=None, signal_reason: str = "") -> str:
    side = "매수" if order.side.value == "buy" else "매도"
    try:
        ccy = parse_symbol(order.ticker).currency
    except ValueError:
        ccy = "KRW"
    unit = "원" if ccy == "KRW" else f" {ccy}"
    s = f"{order.ticker} {side} {order.qty:,}주 @ {order.price:,}{unit} ({order.order_type.value})"
    if signal_reason or order.reason:
        s += f" — 사유: {signal_reason or order.reason}"
    if decision is not None:
        s += f" — 리스크 점검: {decision.text()}"
    if order.message:
        s += f" — 결과: {order.status.value} {order.message}"
    return s


def interpret_metrics(m: dict, benchmark: dict | None = None) -> list[str]:
    """지표 수치 → 해석 문장(규칙 기반)."""
    out = []
    sh, mdd, cagr = m.get("sharpe"), m.get("mdd"), m.get("cagr")
    if sh is not None and pd.notna(sh):
        out.append("위험 대비 수익이 우수합니다(샤프 ≥ 1)." if sh >= 1 else
                   "위험 대비 수익이 보통 수준입니다(0.5 ≤ 샤프 < 1)." if sh >= 0.5 else
                   "감수한 위험에 비해 수익이 부족합니다(샤프 < 0.5). 전략 개선 또는 비중 축소가 필요합니다.")
    if mdd is not None:
        out.append(f"최악의 경우 고점 대비 {abs(mdd):.1%} 손실 구간을 견뎌야 했습니다. "
                   f"투자금 1,000만원 기준 약 {abs(mdd) * 1e7:,.0f}원의 평가손실입니다.")
    if m.get("turnover", 0) > 6:
        out.append(f"연회전율 {m['turnover']:.1f}회로 높아 거래비용이 성과를 크게 깎을 수 있습니다.")
    if benchmark and pd.notna(cagr):
        diff = cagr - benchmark.get("cagr", 0)
        out.append(f"벤치마크 대비 연 {diff:+.2%}p {'초과' if diff > 0 else '미달'} 성과입니다.")
    if m.get("n_trades", 0) < 30:
        out.append("거래 횟수가 30회 미만이라 통계적 신뢰도가 낮습니다. 기간을 늘려 재검증하십시오.")
    return out


def backtest_report_md(name: str, m: dict, bench: dict | None = None, period: str = "") -> str:
    tbl = format_metrics(m)
    lines = [f"## 백테스트 결과: {name}", f"기간: {period}" if period else "", "",
             "| 지표 | 값 | 설명 |", "|---|---:|---|"]
    lines += [f"| {r.지표} | {r.값} | {r.설명} |" for r in tbl.itertuples()]
    lines += ["", "### 해석"] + [f"- {s}" for s in interpret_metrics(m, bench)] + ["", DISCLAIMER]
    return "\n".join(lines)
