"""증권사 서버 연결 점검(읽기 전용 — 주문을 절대 보내지 않는다).

점검 순서
  1) 환경변수(앱키·시크릿·계좌번호) 존재 여부와 형식
  2) 접근토큰 발급(인증 성공 여부)
  3) 국내 현재가 조회
  4) 국내 잔고 조회(계좌번호·권한 확인)
  5) (선택) 해외 현재가 조회
  6) (선택) 해외 잔고 조회

각 단계 실패 시 원인 후보와 조치 방법을 함께 출력한다.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import requests

from .config import KISConfig
from .security.secrets import mask, redact


@dataclass
class CheckResult:
    step: str
    ok: bool
    detail: str
    hint: str = ""
    elapsed_ms: float = 0.0

    def line(self) -> str:
        mark = "✔" if self.ok else "✘"
        s = f"{mark} {self.step:<22s} {self.detail}  ({self.elapsed_ms:.0f}ms)"
        if not self.ok and self.hint:
            s += f"\n    → 조치: {self.hint}"
        return s


def _hint(err: Exception, is_mock: bool) -> str:
    msg = str(err)
    server = "모의(openapivts…:29443)" if is_mock else "실전(openapi…:9443)"
    if isinstance(err, (requests.ConnectionError, requests.Timeout)):
        return (f"{server} 서버에 접속하지 못했습니다. 인터넷 연결, 회사·클라우드 방화벽의 아웃바운드 포트"
                f"({'29443' if is_mock else '9443'}) 허용 여부, 프록시 설정을 확인하십시오.")
    if "401" in msg or "403" in msg or "유효하지 않은" in msg or "appkey" in msg.lower():
        return ("앱키·앱시크릿이 틀렸거나 모의/실전 구분이 맞지 않습니다. 모의투자용 키는 모의 서버에서만, "
                "실전용 키는 실전 서버에서만 동작합니다. KIS Developers에서 해당 계좌의 키를 다시 확인하십시오.")
    if "1분" in msg or "토큰" in msg:
        return "접근토큰은 짧은 시간에 반복 발급할 수 없습니다. 1분 이상 기다린 뒤 다시 실행하십시오."
    if "초당" in msg or "429" in msg or "건수" in msg:
        return "초당 요청 한도를 넘었습니다. 잠시 후 다시 시도하고, 여러 프로그램이 같은 키를 동시에 쓰지 않는지 확인하십시오."
    if "계좌" in msg:
        return ("계좌번호(앞 8자리-뒤 2자리)와 상품코드를 확인하십시오. 모의투자는 실계좌와 다른 모의계좌번호를 씁니다. "
                "앱키를 발급받은 계좌와 같은 계좌여야 합니다.")
    if "해외" in msg or "신청" in msg:
        return "해외주식 거래 신청(또는 모의투자 해외주식 신청)이 되어 있는지 확인하십시오."
    return "오류 메시지를 KIS Developers 오류코드 표에서 확인하십시오."


def _run(step: str, fn, is_mock: bool) -> tuple[CheckResult, object]:
    t0 = time.perf_counter()
    try:
        out, detail = fn()
        return CheckResult(step, True, detail, elapsed_ms=(time.perf_counter() - t0) * 1000), out
    except Exception as e:  # 점검 도구이므로 모든 오류를 결과로 보고
        return CheckResult(step, False, redact(f"{type(e).__name__}: {e}")[:300], _hint(e, is_mock),
                           (time.perf_counter() - t0) * 1000), None


def check_env(cfg: KISConfig) -> CheckResult:
    missing = [k for k, v in (("KIS_APP_KEY", cfg.app_key), ("KIS_APP_SECRET", cfg.app_secret),
                              ("KIS_ACCOUNT_NO", cfg.account_no)) if not v]
    if missing:
        return CheckResult("환경변수", False, f"누락: {', '.join(missing)}",
                           ".env에 값을 넣고 셸에 불러왔는지 확인하십시오(set -a; source .env; set +a).")
    cano, _, prdt = cfg.account_no.partition("-")
    if len(cano) != 8 or len(prdt) != 2 or not (cano + prdt).isdigit():
        return CheckResult("환경변수", False, f"계좌번호 형식 오류({mask(cfg.account_no)})", "'12345678-01' 형식으로 입력하십시오.")
    return CheckResult("환경변수", True, f"앱키 {mask(cfg.app_key)}, 계좌 {cano[:2]}******-{prdt} "
                                       f"→ {'모의' if cfg.is_mock else '실전'} 서버 {cfg.base_url}")


def connection_check(cfg: KISConfig, kr_symbol: str = "005930", overseas_symbol: str | None = "AAPL",
                     session=None, overseas_session=None) -> list[CheckResult]:
    """읽기 전용 연결 점검. session은 테스트용(기본: requests.Session)."""
    from .broker.kis import KISBroker
    from .broker.kis_overseas import KISOverseasBroker

    results = [check_env(cfg)]
    if not results[0].ok:
        return results
    kr = KISBroker(cfg, session=session)

    def token():
        kr._access_token()
        return None, f"발급 성공(만료 {kr._token_exp:%Y-%m-%d %H:%M})"

    r, _ = _run("접근토큰 발급", token, cfg.is_mock)
    results.append(r)
    if not r.ok:
        return results   # 인증 실패 시 이후 단계는 의미 없음

    def kr_quote():
        q = kr.get_quote(kr_symbol)
        return q, f"{kr_symbol} 현재가 {q.price:,.0f}원 (상한 {q.upper_limit:,.0f} / 하한 {q.lower_limit:,.0f})"

    def kr_acct():
        a = kr.get_account()
        return a, f"예수금 {a.cash:,.0f}원, 보유 {len(a.positions)}종목, 총평가 {a.total_equity:,.0f}원"

    results.append(_run("국내 현재가", kr_quote, cfg.is_mock)[0])
    results.append(_run("국내 잔고", kr_acct, cfg.is_mock)[0])

    if overseas_symbol:
        ov = KISOverseasBroker(cfg, fx_provider=lambda c: float("nan"), session=overseas_session or session)
        ov._token, ov._token_exp = kr._token, kr._token_exp   # 토큰 재사용(재발급 빈도 제한 회피)

        def ov_quote():
            q = ov.get_quote(overseas_symbol)
            return q, f"{overseas_symbol} 현재가 {q.price:,} {q.currency} (거래량 {q.volume:,.0f})"

        def ov_acct():
            a = ov.get_account()
            cash = ", ".join(f"{c} {v:,.2f}" for c, v in a.cash_by_ccy.items()) or "외화 예수금 없음"
            return a, f"해외 보유 {len(a.positions)}종목, {cash}"

        results.append(_run("해외 현재가", ov_quote, cfg.is_mock)[0])
        results.append(_run("해외 잔고", ov_acct, cfg.is_mock)[0])
    return results


def print_report(results: list[CheckResult], is_mock: bool) -> bool:
    print(f"한국투자증권 Open API 연결 점검 ({'모의투자' if is_mock else '실전'} 서버, 읽기 전용 — 주문 없음)")
    print("-" * 72)
    for r in results:
        print(r.line())
    ok = all(r.ok for r in results)
    print("-" * 72)
    print("모든 점검 통과: 다음 단계는 'trade --mode mock --targets 파일.csv' 1회 실행입니다." if ok
          else "실패 항목의 조치 방법을 확인한 뒤 다시 실행하십시오.")
    if os.environ.get("TRADER_MODE") == "real" and is_mock:
        print("※ TRADER_MODE=real 이지만 이번 점검은 모의 서버로 수행했습니다.")
    return ok
