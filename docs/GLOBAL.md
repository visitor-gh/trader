# 해외 시장 확장 설계서 (미국·일본·홍콩)

> 기본 설계서 [`DESIGN.md`](DESIGN.md)의 국내 시스템을 다중 시장·다중 통화로 확장한 내용이다.
> 실행 확인: `python -m ktrader demo-global` (합성 데이터·가상 종목코드), `pytest -q` (56개 테스트, 해외 27개)

---

## 0. 확장 원칙

| 원칙 | 의미 | 구현 |
|---|---|---|
| 시장 규칙은 한 곳에 | 호가단위·가격제한·거래시간·매매단위·결제주기를 시장별 `MarketSpec` 하나로 정의 | `ktrader/markets.py` |
| 종목 표기로 시장 자동 판별 | `005930`, `AAPL`, `7203.T`, `0700.HK` → 시장·통화·매매단위 결정. 형식 오류는 즉시 거부 | `parse_symbol()` |
| 원화 기준 통합 | 평가금액·손익·리스크 한도·세금은 모두 원화로 환산해 비교 | `ktrader/fx.py` |
| 미래 환율 금지 | t일 평가에는 t일(또는 그 이전) 환율만 사용 | `FxRates.rate(asof)`, `series()`는 과거 방향 채움만 |
| 기존 인터페이스 유지 | 전략·리스크·실행 코드는 `Broker` 하나만 본다. 시장별 브로커는 라우터가 연결 | `broker/router.py` |
| 국내 동작 불변 | 국내 전용 사용 시 결과가 확장 전과 동일(데모 수치 일치 확인) | 기본값 KRW·환율 1.0 |

새 시장(중국 상해·심천, 베트남 등) 추가 절차: ① `MARKETS`에 `MarketSpec` 추가 ② `parse_symbol` 규칙 추가 ③ `MARKET_COSTS`에 비용 추가 ④ `OVERSEAS_TR_IDS`에 주문 코드 추가 ⑤ 테스트 추가.

---

## 1. 확장 후 시스템 구조

```
                         ┌───────────────────────────── 데이터 계층 ─────────────────────────────┐
 pykrx / KRX ───────────►│ MarketDataCollector (국내)                                             │
 yfinance / FDR ────────►│ GlobalDataCollector (해외 일봉·환율)  ── attach_fx() ──► panel["fx"],   │
 SEC EDGAR (filed일) ───►│ SecEdgarClient (미국 재무, 시점정합)                     panel["value_base"]
 ECOS / yfinance FX ────►│ FxRates (외화 1단위당 원화)                                             │
                         └──────────────────────────────┬────────────────────────────────────────┘
                                                        ▼
                    ┌─────────────── 분석 계층 ───────────────┐
                    │ factors: 휴장일 직전값, 원화 환산 가격·거래대금 │
                    │ screener: 시장별 표준화, 대상 시장 필터      │
                    │ backtest: 시장별 비용·매매단위·휴장·환율      │
                    │ metrics.pnl_attribution: 주가/환율 효과 분해 │
                    └──────────────────────┬──────────────────┘
                                           ▼ 목표 비중(원화 기준)
 ┌──────────────────────────── 실거래 계층 ────────────────────────────┐
 │ TradingEngine.open_markets() → 지금 정규장인 시장의 종목만 주문            │
 │ RiskManager: 원화 환산 주문금액·외화자산 비중·시장별 비중·매매단위·호가단위   │
 │ RoutingBroker ─┬─ KR ─► KISBroker (국내 주식 API)                      │
 │                └─ US/JP/HK ─► KISOverseasBroker (해외주식 API)          │
 │ Ledger: 체결별 통화·환율 기록 → 원화 실현손익 → 해외주식 양도세 추정(tax.py) │
 └───────────────────────────────────────────────────────────────────┘
```

---

## 2. 시장별 규칙(`markets.py::MARKETS`)

| 항목 | 한국 KR | 미국 US | 일본 JP | 홍콩 HK |
|---|---|---|---|---|
| 종목 표기 | `005930`, `005930.KS`, `035720.KQ` | `AAPL`, `BRK-B` | `7203.T` | `0700.HK` |
| 통화 | KRW | USD | JPY | HKD |
| 정규장(현지) | 09:00–15:20 연속 + 15:20–15:30 종가단일가 | 09:30–16:00 | 09:00–11:30, 12:30–15:25 연속 + 15:25–15:30 종가 | 09:30–12:00, 13:00–16:00 + 16:00–16:10 종가경매 |
| 정규장(한국시각) | 09:00–15:30 | 서머타임 22:30–05:00 / 해제 23:30–06:00 | 09:00–15:30(점심 휴장) | 10:30–17:00(점심 휴장) |
| 호가단위 | 7단계(1~1,000원) | $1 이상 $0.01, 미만 $0.0001 | 가격대별 표(TOPIX500은 별도) | 스프레드 표(0.001~5) |
| 일일 가격제한 | ±30% | 없음(LULD·서킷브레이커) | 가격대별 절대금액 제한 | 없음(VCM) |
| 매매단위 | 1주 | 1주 | 100주 | 종목별 board lot(기본 100, `register_instrument`로 지정) |
| 결제 | T+2 | T+1 | T+2 | T+2 |

**서머타임 처리**: 시간대를 `America/New_York` 등 IANA 이름으로 저장하고 `zoneinfo`로 변환하므로 매년 3월·11월 전환이 자동 반영된다(테스트: `test_us_session_follows_dst`).
**휴장일**: `TradingEngine.holidays = {"US": {...}, "JP": {...}}`로 시장별 지정. 운영에서는 `exchange_calendars`(XNYS, XTKS, XHKG) 등으로 매년 갱신한다.
**일본 가격제한**: 거래소가 가격대별 '절대금액'으로 정하므로 비율 하나로 표현할 수 없다. 현재는 제한 없음으로 두고 증권사 시세의 상·하한가 필드를 그대로 쓰는 방식으로 확장한다.

---

## 3. 데이터 수집과 검증

### 3.1 데이터 소스

| 데이터 | 소스 | 공식 여부 | 주의 |
|---|---|---|---|
| 해외 일봉(수정주가) | `yfinance` → `FinanceDataReader` 폴백 | 비공식 | 상장폐지 종목 누락(생존편향), 이용약관 확인 |
| 미국 재무제표 | SEC EDGAR XBRL `companyfacts` | **공식·무료** | User-Agent 필수(`SEC_USER_AGENT`), 초당 10회 이하 |
| 일본 재무제표 | EDINET API(금융청) | 공식(키 필요) | 확장 예정 |
| 홍콩 재무제표 | 유료 데이터 벤더 권장 | — | |
| 현재 밸류에이션 스냅샷 | `yfinance` info | 비공식 | **실시간 선별 전용. 과거 백테스트 사용 금지(미래정보)** |
| 환율 | 한국은행 ECOS(공식) → `yfinance`/`FDR` 폴백 | 혼합 | 엔화 100엔당 고시 소스는 1엔당으로 변환 |
| 실시간 시세·주문 | 한국투자증권 해외주식 API | 공식 | 아래 5장 |

```python
from ktrader.data.global_collector import GlobalDataCollector, SecEdgarClient
from ktrader.data.preprocess import align_fundamentals_pit
from ktrader.fx import attach_fx

col = GlobalDataCollector("./var/data")
symbols = ["005930", "000660", "AAPL", "MSFT", "7203.T", "0700.HK"]
panel = col.get_ohlcv_panel(symbols, "20220103", "20261030")          # 날짜 = 모든 시장 거래일 합집합
fx = col.get_fx_rates({"USD", "JPY", "HKD"}, "20220103", "20261030")
panel = attach_fx(panel, fx)                                           # + panel["fx"], panel["value_base"]

sec = SecEdgarClient()                                                 # SEC_USER_AGENT 환경변수 필요
rec = pd.concat([sec.pit_records(t) for t in ("AAPL", "MSFT")])
roe = align_fundamentals_pit(rec, panel["close"].index, "roe")         # 'filed'(실제 제출일) 기준
```

### 3.2 시점정합(미국 재무)
SEC 데이터는 같은 회계기간 값이 정정공시·다음 해 비교표시로 여러 번 등장한다. `fact_frame()`은 **가장 먼저 제출된 값**을 남기고 그 제출일(`filed`)을 공개일로 쓴다. 그날 시장이 실제로 알 수 있었던 값만 쓰는 것이다(테스트: `test_sec_fact_frame_keeps_first_filing`).

### 3.3 검증 규칙 변화
| 검사 | 국내 | 해외(가격제한 없음) |
|---|---|---|
| 하루 변동 한도 | ±30% 초과 → ERROR | 사용하지 않음 |
| 분할의심 | — | 하루 가격비가 1/2·1/3·1/4·2·3배 등 분할비율 ±8% 이내 → ERROR(수정주가 데이터일 때) |
| 급등락 | — | ±50% 초과 → WARN(실제 이벤트일 수 있음) |

데모 출력:
```
[ZUS01] 768행, 최고 심각도 ERROR
  - ERROR 분할의심: 분할·병합 비율(1/2, 1/3, 2배 등)과 일치하는 하루 변동 (1건) 예: 2023-05-26
```

### 3.4 다중 시장 패널의 휴장일
날짜 축은 모든 시장 거래일의 합집합이다. 어떤 시장이 쉬는 날 그 종목 칸은 NaN이다.
- 지표 계산: 직전 종가로 채운 값 사용(`close.ffill()`)
- 거래정지 판단: 마지막 **실제 거래일**의 거래량이 0일 때만 정지로 본다(휴장일과 구분)
- 백테스트 체결: 시가가 NaN이면 체결하지 않고 해당 종목만 다음 거래일로 이월

---

## 4. 분석·추천

### 4.1 원화 환산 필터
최소 주가·최소 거래대금은 원화로 환산한 값(`price_base`, `value_base`)으로 비교한다. 달러 150짜리 주식이 '1,000원 미만 동전주'로 잘못 제외되는 문제를 막는다.

### 4.2 시장별 표준화(`ScreenConfig.standardize_by_market=True`, 기본값)
일본은 PBR이 구조적으로 낮고 미국은 높다. 전체를 한꺼번에 z-score로 만들면 '저평가' 점수가 일본에 몰린다. 그래서 **각 시장 안에서** 표준화한 뒤 합친다.

데모 결과(합성 데이터):
```
시장별 표준화 미적용 → 상위 12종목 시장 구성: {'JP': 5, 'HK': 4, 'KR': 2, 'US': 1}
시장별 표준화 적용   → 상위 12종목 시장 구성: {'JP': 4, 'US': 3, 'KR': 3, 'HK': 2}
```

추천 설명에는 시장이 함께 표시된다:
```
[ZUS06 가상US06 · 미국(NYSE·NASDAQ·AMEX)] 종합점수 +0.72 (순위 2/42)
  ▶ 선정 근거(기여도 큰 순):
    + 12-1개월 모멘텀: 우수 (기여 +0.22, 실제값 0.447)
    + 이익수익률(1/PER): 매우 우수 (기여 +0.21, 실제값 0.141)
```

`ScreenConfig(markets={"US"})`처럼 대상 시장만 고를 수 있다.

---

## 5. 백테스트(다중 시장)

| 항목 | 처리 |
|---|---|
| 시차 | 날짜 기준 정렬. 미국 t일 종가(한국 t+1일 새벽)는 한국 t+1일 시가보다 먼저 확정되므로 'd일 신호 → d+1일 시가 체결'이 모든 시장에서 미래 정보 없이 성립 |
| 현금 | 원화 단일 계좌(통합증거금 가정). 해외 체결 시 당일 환율로 환산 |
| 비용 | `MARKET_COSTS`: 수수료, 거래세(한국 매도), 인지세(홍콩 매수·매도), 미국 매도 규제수수료 근사, 환전 스프레드 |
| 매매단위 | 목표 수량을 시장 매매단위로 내림(일본 100주) |
| 휴장 | 해당 시장 휴장일에는 체결하지 않고 그 종목만 이월 |
| 평가 | 종가 × 당일 환율(원화) |

### 5.1 손익 기여 분해(`pnl_attribution`)
```
주가효과 = 보유수량 × (오늘 종가 − 어제 종가) × 어제 환율
환율효과 = 보유수량 × 오늘 종가 × (오늘 환율 − 어제 환율)
```

데모 결과(합성 데이터, 실제 성과 아님):
```
          cagr  ann_vol  sharpe    mdd  turnover    total_cost
국내 멀티팩터  0.092    0.155   0.454 -0.112     2.874  1,531,063
글로벌 멀티팩터 0.166    0.108   1.198 -0.083     1.178  8,140,248
글로벌 동일가중 0.116    0.077   1.083 -0.046     0.753  4,294,086

[글로벌 멀티팩터] 시장별 손익 기여(원)
              주가효과      환율효과          합계
HK      17,134,156 1,391,521  18,525,677
JP       5,646,482 3,129,576   8,776,057
KR       5,103,703         0   5,103,703
US      13,806,729 3,455,072  17,261,801
거래·비용 등        NaN       NaN -11,740,208
```

**해석**
- 여러 시장에 나눠 담으면서 변동성이 15.5% → 10.8%로 줄고 샤프가 개선됐다(분산효과). 실제 시장에서는 국가 간 상관관계가 위기 때 높아지므로 이 효과가 작아질 수 있다.
- 해외 손익 중 약 8백만원이 **환율 효과**다. 원화 약세 구간에서는 해외 자산이 이익을 보지만, 반대로 원화 강세 시에는 주가가 올라도 원화 손익이 줄 수 있다.
- 거래비용이 국내 전용의 5배 이상이다. 해외 수수료(0.25%)와 환전 스프레드가 국내보다 훨씬 크기 때문이다. 해외 비중이 큰 전략은 **회전율을 낮게** 설계해야 한다.
- '거래·비용 등'은 비용(약 8.1백만원)과 체결일 장중 손익처럼 일별 근사에서 빠지는 부분이다.

---

## 6. 자동매매(다중 시장)

### 6.1 하루 운영 시간표(한국시각, 2026년 11월 서머타임 해제 기준)

| 시각(KST) | 열린 시장 | 작업 |
|---|---|---|
| 07:30 | — | 전일 미국 종가·환율 수집, 검증, 목표 비중 산출 |
| 09:00–15:30 | KR, JP(11:30–12:30 점심 휴장) | 국내·일본 종목 주문 |
| 10:30–17:00 | HK(13:00–14:00 점심 휴장) | 홍콩 종목 주문 |
| 23:30–06:00 | US(서머타임 기간은 22:30–05:00) | 미국 종목 주문 |
| 06:30 | — | 일일 정산: 원화 환산 평가, 시장별·통화별 비중, 감사로그 확인 |

`TradingEngine.run_cycle()`은 매 사이클 `open_markets()`로 지금 정규장인 시장을 확인하고, **그 시장 종목만** 주문한다. 닫힌 시장의 리밸런싱은 그 시장이 열리는 다음 사이클에서 처리된다(테스트: `test_engine_only_trades_open_markets`).

### 6.2 한국투자증권 해외주식 API(`broker/kis_overseas.py`)

| 기능 | 경로 | TR_ID(실전 / 모의) |
|---|---|---|
| 현재가 | GET `/uapi/overseas-price/v1/quotations/price` | HHDFS00000300 |
| 미국 매수 / 매도 | POST `/uapi/overseas-stock/v1/trading/order` | TTTT1002U·TTTT1006U / VTTT1002U·VTTT1001U |
| 일본 매수 / 매도 | 〃 | TTTS0308U·TTTS0307U / VTTS0308U·VTTS0307U |
| 홍콩 매수 / 매도 | 〃 | TTTS1002U·TTTS1001U / VTTS1002U·VTTS1001U |
| 정정·취소 | POST `/uapi/overseas-stock/v1/trading/order-rvsecncl` | TTTT1004U / VTTT1004U |
| 잔고 | GET `/uapi/overseas-stock/v1/trading/inquire-balance` | TTTS3012R / VTTS3012R |
| 주문체결내역 | GET `/uapi/overseas-stock/v1/trading/inquire-ccnl` | TTTS3035R / VTTS3035R |

| 거래소 | 주문 코드(OVRS_EXCG_CD) | 시세 코드(EXCD) |
|---|---|---|
| 나스닥 / 뉴욕 / 아멕스 | NASD / NYSE / AMEX | NAS / NYS / AMS |
| 도쿄 | TKSE | TSE |
| 홍콩 | SEHK | HKS |

> 주문 코드와 시세 코드가 다르다는 점이 가장 흔한 실수다. 미국 종목의 세부 거래소는 `register_instrument("KO", exchange="NYSE")`로 지정한다(기본 NASD).
> TR_ID·필드명은 증권사 사정으로 바뀔 수 있으므로 운영 전 KIS 개발자센터 최신 문서와 대조하고, 모의서버에서 시장별로 1회 이상 주문·취소·조회를 확인한다. 모의서버는 일부 시장·기능만 지원한다.

주문 요청 예(테스트 `test_kis_overseas_order_and_quote_shape`로 고정):
```json
{"CANO":"12345678","ACNT_PRDT_CD":"01","OVRS_EXCG_CD":"NASD","PDNO":"AAPL",
 "ORD_QTY":"3","OVRS_ORD_UNPR":"189.5","ORD_SVR_DVSN_CD":"0","ORD_DVSN":"00"}
```

**정책**: 해외주식은 **지정가만** 허용한다. 미국 시장가는 KIS에서 일반 주문으로 지원되지 않으며, 유동성이 낮은 시간대에 불리한 체결 위험이 크다.

### 6.3 라우팅
```python
from ktrader.broker.kis import KISBroker
from ktrader.broker.kis_overseas import KISOverseasBroker
from ktrader.broker.router import RoutingBroker

kis = KISConfig.from_env(is_mock=True)
overseas = KISOverseasBroker(kis, fx_provider=lambda c: fx.rate(c))
broker = RoutingBroker({"KR": KISBroker(kis), "US": overseas, "JP": overseas, "HK": overseas})
```
CLI: `python -m ktrader trade --mode mock --targets targets.csv`. CSV의 종목 표기를 보고 필요한 브로커를 자동 구성한다.

---

## 7. 리스크 관리(다중 시장·통화)

| 항목 | 기본값 | 의미 |
|---|---|---|
| 주문금액 한도 | 원화 2천만원 | 해외 주문도 **원화 환산** 금액으로 비교 |
| 외화자산 비중 한도 `max_foreign_weight` | 70% | 원화 외 통화 자산 합계. 환율 급변 시 손실 제한 |
| 시장별 비중 한도 `max_market_weight` | KR 100%, US 60%, JP 30%, HK 20% | 특정 국가 위험 집중 방지 |
| 매매단위 검사 | 시장·종목별 | 일본 150주 같은 주문 거부 |
| 호가단위 검사 | 시장별 표 | 미국 $100.005 같은 가격 거부 |
| 통화 일치 검사 | — | 시세 통화와 종목 통화가 다르면 거부(데이터 오류 방지) |
| 통합증거금 `auto_fx` | True | False면 해당 외화 예수금 안에서만 매수 |
| 손절 | 현지통화 기준 | 주가 하락으로 인한 손절. 환율 손실은 외화자산 비중 한도로 관리 |

거부 사유 예:
```
ZUS01 매수 40주 @ 100.0 USD — 리스크 점검: 거부: 1회 주문금액 5,600,000원 > 한도 5,000,000원
ZUS01 매수 20주 — 거부: 외화자산 비중 30.8% > 한도 30%(환율 위험)
9901.T 매수 150주 — 거부: 매매단위 불일치(150주, 100주 단위)
```

**환헤지**: 개인 현물 계좌에서는 환헤지 수단이 제한적이다. 이 설계는 환율 위험을 '헤지'하지 않고 **비중 한도와 손익 분해 보고로 관리**한다. 헤지가 필요하면 환헤지형 ETF나 선물환 같은 별도 수단을 검토한다.

---

## 8. 거래 기록과 세금

### 8.1 원장 변경
`fills` 테이블에 `currency`, `fx_rate`(체결 시점 환율) 열을 추가했다. 기존 DB는 시작할 때 자동으로 이전(migration)되며, 기존 행은 KRW·환율 1.0으로 간주한다(테스트: `test_ledger_migrates_old_schema`).
실현손익은 **매수가와 매도가를 각각 체결 시점 환율로 원화 환산**해 계산한다. 그래서 주가가 같아도 환율이 오르면 원화 이익이 생긴다.

### 8.2 해외주식 양도소득세 추정(`tax.py`)

| 항목 | 내용(2026년 현행 기준 — 매년 세법 확인) |
|---|---|
| 대상 | 해외 상장주식을 팔아 실현한 연간 순양도차익(같은 해 해외주식 이익과 손실 통산) |
| 기본공제 | 연 250만원 |
| 세율 | 22%(양도소득세 20% + 지방소득세 2%) |
| 신고 | 다음 해 5월 확정신고(증권사 양도세 대행 신고 서비스 활용 가능) |
| 환산 | 세법상 취득·양도가액은 각 결제일 기준환율로 환산 → 운영 시 결제일 환율로 교체 가능하도록 `fx_rate`를 별도 저장 |
| 배당 | 미국 배당은 현지에서 15% 원천징수(W-8BEN 제출 시 조세조약 세율). 국내 금융소득 연 2천만원 초과 시 종합과세 |

```
9806.HK 1300주 매도(주가 +35%, 환율 +3%) → 2026년 해외주식 양도소득 추정: 이익 2,845,540원, 손실 0원,
순익 2,845,540원 → 기본공제 2,500,000원 차감 후 과세표준 345,540원, 예상 세액 76,019원(22%). ※ 참고용 추정치
```

활용: 연말이 가까울 때 실현손익이 250만원 공제를 넘는지 확인하고, 손실 종목 정리 여부를 판단하는 참고 자료로 쓴다. 단, 세금만을 이유로 한 매매가 전략 원칙을 깨지 않도록 주의한다.

---

## 9. 준법·보안 추가 사항

| 쟁점 | 내용 | 대응 |
|---|---|---|
| 해외 시장 불공정거래 | 미국 SEC·FINRA, 일본 SESC, 홍콩 SFC도 시세조종·허수호가를 금지 | `ComplianceGuard`가 종목 시장의 **현지 시각**으로 단일가 시간대를 판단하고, 자전성·취소과다 규칙을 해외에도 그대로 적용 |
| 미국 데이트레이딩 규정(PDT) | 미국 증권사 마진계좌 규정. 국내 증권사 현금계좌 해외투자는 일반적으로 해당 없음 | 신용·미수 미사용(현금 범위 내 매수) |
| W-8BEN | 미국 배당 원천징수 조약세율 적용을 위한 서류 | 증권사를 통해 제출(계좌 개설 시) |
| 외국환 거래 | 국내 증권사를 통한 개인의 해외주식 매매는 증권사가 환전·송금을 처리 | 직접 해외 송금·해외 계좌 사용 시 외국환거래법 신고 의무 별도 확인 |
| SEC 데이터 이용 | 공정 이용 정책: 식별 가능한 User-Agent, 초당 10회 이하 | `SecEdgarClient`가 User-Agent 없으면 실행 거부, 0.15초 간격 |
| 데이터 라이선스 | 해외 거래소 실시간 시세는 재배포에 거래소 라이선스 필요 | 본인 분석 용도로만 사용 |
| 비밀값 | 해외 API도 같은 KIS 키 사용 | 기존 마스킹·환경변수 원칙 동일 |

---

## 10. 추가·변경 모듈 명세

| 모듈 | 함수/클래스 | 입력 | 출력 | 동작 |
|---|---|---|---|---|
| markets | `parse_symbol(s)` | 종목 표기 | `Instrument(symbol, market, local_code, lot, exchange)` | 해석 불가 시 ValueError |
| markets | `MarketSpec.session_phase(now)` | 시각 | closed/pre_auction/continuous/break/close_auction | 현지 시각·서머타임 |
| markets | `.round_price(p, side)` / `.is_on_tick(p)` / `.price_limits(b)` / `.round_lot(q)` | 가격/수량 | 보정값 | 시장별 규칙 |
| markets | `register_instrument(s, lot=, exchange=)` | 종목, 예외값 | — | 홍콩 board lot·미국 거래소 지정 |
| fx | `FxRates.rate(ccy, asof)` | 통화, 기준일 | 원화 환율 | asof 이전 최신값 |
| fx | `attach_fx(panel, fx)` | 패널, 환율 | 패널 + fx, value_base | |
| fx | `decompose_return(price, fx)` | 현지 가격, 환율 | 주가/환율/교차/합계 | |
| data.global_collector | `GlobalDataCollector.get_ohlcv / get_fx / get_fx_rates` | 종목·통화·기간 | DataFrame/Series/FxRates | 재시도·폴백·캐시 |
| data.global_collector | `SecEdgarClient.pit_records(t)` | 미국 종목 | 시점정합 레코드 | 최초 제출값·제출일 |
| backtest.metrics | `pnl_attribution(result, panel)` | 결과, 패널 | 시장별 주가·환율 효과 | |
| broker.kis_overseas | `KISOverseasBroker` | KISConfig, fx_provider | Broker | 지정가만 |
| broker.router | `RoutingBroker(routes)` | {시장: Broker} | Broker | 잔고 원화 합산 |
| tax | `estimate_overseas_cgt(trades, year)` | 실현손익, 연도 | `OverseasCgtEstimate` | 250만원 공제, 22% |

---

## 11. 운영 전 점검표(해외)

- [ ] 사용할 거래소 코드·TR_ID를 KIS 최신 문서와 대조, 모의서버에서 시장별 주문·취소·잔고·체결 조회 확인
- [ ] 증권사 해외 수수료율·환율우대율로 `MARKET_COSTS` 갱신
- [ ] 홍콩 보유 예정 종목의 board lot, 미국 종목의 NYSE/AMEX 상장 여부를 `register_instrument`로 등록
- [ ] 시장별 휴장일 달력 등록(미국 추수감사절 다음 날 조기폐장 등 단축 거래일 포함)
- [ ] 통합증거금 신청 여부에 맞게 `RiskConfig.auto_fx` 설정
- [ ] 외화자산·시장별 비중 한도를 투자 성향에 맞게 조정
- [ ] 야간(미국장) 무인 운영 시 알림 채널과 킬 스위치 알림 동작 확인
- [ ] 연말 해외주식 실현손익과 양도세 추정 리포트 확인
