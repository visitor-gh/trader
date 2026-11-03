# trader — 국내·해외 주식 AI 분석·추천·자동매매 시스템 (`ktrader`)

KOSPI·KOSDAQ과 미국·일본·홍콩 종목의 **데이터 수집 → 품질 검증 → 팩터 분석·추천 → 백테스트 → 리스크 관리 → 자동 주문(한국투자증권 Open API) → 거래 기록·설명 리포트**까지 하나의 파이프라인으로 구현한 파이썬 패키지입니다.

- 전체 설계서: [`docs/DESIGN.md`](docs/DESIGN.md) (시스템 구조도, 단계별 절차·함수 명세, 리스크·보안·준법)
- **증권사 서버 연동·운영 가이드: [`docs/CONNECT.md`](docs/CONNECT.md)** (키 발급 → 연결 점검 → 모의 주문 → 상시 운영 → 실전 전환)
- 해외 확장 설계서: [`docs/GLOBAL.md`](docs/GLOBAL.md) (시장 규칙, 환율, 해외 API, 다중 통화 리스크, 해외주식 양도세)
- 네트워크 없이 바로 확인: `python -m ktrader demo` (국내), `python -m ktrader demo-global` (국내+미국·일본·홍콩)

## 빠른 시작

```bash
pip install -e ".[dev]"
```

```bash
python -m ktrader demo
```

```bash
pytest -q
```

```bash
python -m ktrader demo-global
```

데모는 합성(가상) 데이터로 검증 → 종목 추천과 근거 → 모델 선정 → 4개 전략 백테스트 → 가상 브로커 자동매매 1사이클 → 원장·감사로그 검증까지 실행하고 `var/reports/`에 리포트(`backtest_report.md`)와 차트(`equity.png`, `monthly.png`, `contrib_top1.png`)를 만듭니다.

## 실제 데이터와 증권사 연동

```bash
pip install -e ".[data]"
```

```bash
python -m ktrader screen --date 20261030 --market KOSPI --top 10
```

```bash
python -m ktrader backtest --start 20200102 --end 20261030 --strategy momentum
```

국내·해외 혼합 종목 백테스트(환율 자동 수집·원화 환산):

```bash
python -m ktrader backtest --start 20220103 --end 20261030 --strategy momentum --symbols 005930,000660,AAPL,MSFT,7203.T,0700.HK
```

먼저 주문 없이 연결만 점검합니다(키·계좌·네트워크·해외주식 신청 상태 확인):

```bash
python -m ktrader check --mode mock
```

한국투자증권 **모의투자** 서버로 목표 비중 CSV(`ticker,weight,reason`)를 1회 집행(해외 종목은 해외주식 API로 자동 라우팅, 지금 정규장인 시장의 종목만 주문):

```bash
cp .env.example .env   # KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT_NO 입력 후 환경에 로드
python -m ktrader trade --mode mock --targets targets.csv
```

실계좌(`--mode real`)는 `TRADER_REAL_CONFIRM` 확인 문구가 없으면 실행되지 않습니다. 반드시 모의투자에서 충분히 검증한 뒤 소액으로 시작하십시오.

## 모듈 구성

| 단계 | 모듈 |
|---|---|
| 1. 수집 | `ktrader/data/collector.py` (pykrx, FinanceDataReader, OpenDART, 재시도·캐시·폴백) |
| 2. 검증·전처리 | `ktrader/data/validate.py`, `ktrader/data/preprocess.py` |
| 3. 분석·추천 | `ktrader/analysis/` (지표, 팩터, 스크리너, ML 모델 선정) |
| 4. 백테스트 | `ktrader/backtest/` (엔진, 전략, 지표, 워크포워드) |
| 5. 자동매매 | `ktrader/execution/engine.py`, `ktrader/broker/` (KIS, Paper) |
| 6. 리스크·기록 | `ktrader/risk/manager.py`, `ktrader/ledger/store.py` |
| 7. 설명·시각화 | `ktrader/report/` |
| 8. 보안·준법 | `ktrader/security/` |
| 해외 확장 | `ktrader/markets.py`(시장 규칙), `ktrader/fx.py`(환율), `ktrader/data/global_collector.py`(yfinance·SEC EDGAR), `ktrader/broker/kis_overseas.py`, `ktrader/broker/router.py`, `ktrader/tax.py` |

종목 표기: `005930`(국내), `AAPL`(미국), `7203.T`(일본), `0700.HK`(홍콩).

## 주의
- 수수료율·세율·환전 스프레드(`config.CostModel`, `config.MARKET_COSTS`)와 KIS TR_ID(`broker/kis.py::TR_IDS`, `broker/kis_overseas.py::OVERSEAS_TR_IDS`)는 바뀔 수 있으니 운영 전 최신 값으로 확인하십시오.
- 이 소프트웨어는 본인 계좌용 연구·자동화 도구입니다. 타인에게 유료 추천·자동매매를 제공하려면 자본시장법상 인가·등록·신고 요건을 먼저 검토해야 합니다.
- 결과는 과거 데이터에 근거한 참고 정보이며 미래 수익을 보장하지 않습니다.
