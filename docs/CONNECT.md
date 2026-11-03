# 증권사 서버 연동·운영 가이드 (한국투자증권 Open API)

> 순서: **① 계좌·키 준비 → ② 설치 → ③ 환경변수 → ④ 연결 점검(`check`) → ⑤ 모의 주문 1회 → ⑥ 상시 운영 → ⑦ 실전 전환**
> 각 단계를 통과한 뒤에만 다음 단계로 넘어갑니다. ④까지는 주문이 나가지 않습니다.

---

## 0. 연결 구조

```
 ┌──────────── 내 운영 서버(PC 또는 클라우드 VM) ────────────┐
 │  .env (앱키·시크릿·계좌번호, 권한 600)                      │
 │  python -m ktrader trade --loop                            │
 │    ├ KISBroker ─────────┐                                 │
 │    └ KISOverseasBroker ─┤  HTTPS(REST)                    │
 │  ledger.sqlite3 (원장)  │                                  │
 └─────────────────────────┼─────────────────────────────────┘
                           ▼  아웃바운드만 필요(인바운드 포트 개방 불필요)
   모의: https://openapivts.koreainvestment.com:29443
   실전: https://openapi.koreainvestment.com:9443
   (실시간 WebSocket 확장 시: 모의 :31000 / 실전 :21000)
```

- 내 서버가 증권사로 **요청을 보내는 방식**이라 공인 IP·도메인·인바운드 포트가 필요 없습니다.
- 방화벽은 위 주소의 **아웃바운드 443이 아닌 포트(9443, 29443)** 를 허용해야 합니다. 회사망·일부 클라우드에서 막혀 있는 경우가 많습니다.
- 모의투자 키는 모의 서버에서만, 실전 키는 실전 서버에서만 동작합니다.

---

## 1. 계좌·키 준비

| 순서 | 할 일 | 비고 |
|---|---|---|
| 1 | 한국투자증권 계좌 개설 | 비대면 개설 가능 |
| 2 | **모의투자 신청** → 모의계좌번호 발급 | 홈페이지·앱의 모의투자 메뉴. 실계좌와 번호가 다름 |
| 3 | KIS Developers(https://apiportal.koreainvestment.com) 로그인 → Open API 서비스 신청 | 모의투자용 앱키·시크릿 발급 |
| 4 | (해외) 해외주식 거래 신청, 필요 시 통합증거금 신청 | 통합증거금이 없으면 외화 예수금이 있어야 해외 매수 가능 → `RiskConfig.auto_fx=False` |
| 5 | (해외·미국) W-8BEN 제출 | 미국 배당 원천징수 조약세율 적용 |
| 6 | 실전 키는 **모의투자 검증이 끝난 뒤** 따로 발급 | 실전·모의 키를 같은 파일에 섞지 않기 |

> 메뉴 이름과 신청 절차는 증권사 사정으로 바뀔 수 있습니다. 화면이 다르면 KIS Developers 공지와 FAQ를 확인하세요.

---

## 2. 설치

**Linux / macOS**
```bash
git clone https://github.com/visitor-gh/trader.git
```
```bash
cd trader && python3 -m venv .venv && source .venv/bin/activate
```
```bash
pip install -e ".[data,dev]"
```
```bash
pytest -q
```

**Windows (PowerShell)**
```powershell
git clone https://github.com/visitor-gh/trader.git; cd trader
py -3 -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[data,dev]"
```

Python 3.10 이상이 필요합니다. `[data]`는 pykrx·FinanceDataReader·yfinance(해외 시세·환율)를 설치합니다.

---

## 3. 환경변수 설정

```bash
cp .env.example .env && chmod 600 .env
```

`.env`를 열어 값을 넣습니다(주석은 반드시 별도 줄에):
```
TRADER_MODE=mock
KIS_APP_KEY=발급받은_모의투자_앱키
KIS_APP_SECRET=발급받은_모의투자_시크릿
KIS_ACCOUNT_NO=12345678-01
```

셸에 불러오기:

**Linux / macOS**
```bash
set -a; source .env; set +a
```

**Windows (PowerShell, 현재 창에만 적용)**
```powershell
Get-Content .env | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
  $k, $v = $_ -split '=', 2; [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process') }
```

**지켜야 할 것**
- 앱키·시크릿·계좌번호를 코드, Git, 메신저, 이슈, AI 채팅창에 붙여넣지 않습니다. 노출되면 KIS Developers에서 즉시 재발급합니다.
- 프로그램 로그에는 키·토큰·계좌번호가 자동으로 마스킹됩니다(`security/secrets.py`).

---

## 4. 연결 점검(읽기 전용 — 주문 없음)

```bash
python -m ktrader check --mode mock
```

점검 항목: 환경변수 → 접근토큰 발급 → 국내 현재가 → 국내 잔고 → 해외 현재가 → 해외 잔고.
해외주식을 아직 신청하지 않았다면 `--no-overseas`를 붙입니다. 모의 서버가 지원하지 않는 해외 기능은 실패로 나올 수 있습니다.

성공 시 출력 형식:
```
한국투자증권 Open API 연결 점검 (모의투자 서버, 읽기 전용 — 주문 없음)
------------------------------------------------------------------------
✔ 환경변수                   앱키 PS******gh, 계좌 12******-01 → 모의 서버 https://openapivts.koreainvestment.com:29443
✔ 접근토큰 발급                발급 성공(만료 2026-11-05 09:12)
✔ 국내 현재가                 005930 현재가 70,000원 (상한 90,300 / 하한 48,650)
✔ 국내 잔고                  예수금 10,000,000원, 보유 0종목, 총평가 10,000,000원
✔ 해외 현재가                 AAPL 현재가 189.43 USD (거래량 51,234,567)
✔ 해외 잔고                  해외 보유 0종목, USD 0.00
------------------------------------------------------------------------
모든 점검 통과: 다음 단계는 'trade --mode mock --targets 파일.csv' 1회 실행입니다.
```

실패하면 각 줄 아래에 `→ 조치:`로 원인 후보가 나옵니다(아래 9장 문제 해결 표 참고).
접근토큰은 짧은 시간에 반복 발급할 수 없으니, 실패 후 재시도는 1분 이상 간격을 둡니다.

---

## 5. 모의 주문 1회

### 5.1 목표 비중 파일 만들기

자동 생성(국내 종목, 실제 데이터 수집):
```bash
python -m ktrader screen --market KOSPI --top 10 --out targets.csv
```

또는 직접 작성(`targets.csv`, 비중 0.05 = 5%):
```
ticker,weight,reason
005930,0.05,연동 테스트
AAPL,0.03,해외 연동 테스트
```
`--date`를 생략하면 가장 최근 거래일(주말·휴장일이면 그 이전 거래일)을 기준으로 합니다.
파일은 실행 전에 검증됩니다. 종목 표기 오류, 비중 1 초과(예: 5를 5%로 착각), 합계 100% 초과, 중복이 있으면 실행하지 않습니다.

### 5.2 1회 실행

```bash
python -m ktrader trade --mode mock --targets targets.csv
```

- **장중에 실행해야 합니다.** 국내 09:00–15:20, 미국 23:30–06:00(서머타임 기간 22:30–05:00, 한국시각). 그 밖의 시간에는 `연속매매 중인 시장 없음`으로 건너뜁니다.
- 처음 실행 시 모의계좌의 기존 보유 종목을 원장에 '기초 잔고'로 등록합니다(이후 잔고 대조의 기준).

### 5.3 결과 확인

```bash
sqlite3 var/ledger.sqlite3 "SELECT ticker, side, qty, price, status, risk_note, message FROM orders ORDER BY created_at DESC LIMIT 20;"
```
```bash
sqlite3 var/ledger.sqlite3 "SELECT * FROM fills ORDER BY id DESC LIMIT 20;"
```
한국투자증권 앱의 모의투자 화면에서도 같은 주문·체결이 보이는지 대조합니다.

---

## 6. 상시 운영

### 6.1 어디서 돌릴까

| 선택지 | 장점 | 주의 |
|---|---|---|
| 개인 PC | 비용 없음 | 절전·재부팅·인터넷 끊김. 미국장(밤)까지 돌리려면 24시간 켜둬야 함 |
| 클라우드 VM(서울 리전) | 24시간 안정, 증권사 서버와 지연 짧음 | 방화벽 아웃바운드 9443·29443 허용, 디스크 암호화, SSH 키 로그인만 허용 |

- 시계 동기화 필수(`timedatectl`로 NTP 활성 확인). 장 시간 판단과 감사로그가 시각에 의존합니다.
- 코드가 시간대를 직접 처리하므로 서버 시간대는 UTC여도 됩니다.

### 6.2 systemd로 상시 실행(Linux)

`/etc/systemd/system/ktrader-trade.service`
```ini
[Unit]
Description=ktrader trading loop
After=network-online.target
Wants=network-online.target

[Service]
User=trader
WorkingDirectory=/opt/trader
EnvironmentFile=/opt/trader/.env
ExecStart=/opt/trader/.venv/bin/python -m ktrader trade --mode mock --targets /opt/trader/targets.csv --loop --interval 60
Restart=on-failure
RestartSec=120

[Install]
WantedBy=multi-user.target
```

매일 아침 목표 비중 갱신 — `/etc/systemd/system/ktrader-screen.service`
```ini
[Unit]
Description=ktrader daily screening

[Service]
Type=oneshot
User=trader
WorkingDirectory=/opt/trader
EnvironmentFile=/opt/trader/.env
ExecStart=/opt/trader/.venv/bin/python -m ktrader screen --top 10 --out /opt/trader/targets.csv
```

`/etc/systemd/system/ktrader-screen.timer`
```ini
[Unit]
Description=Run ktrader screening on weekdays 07:30 KST

[Timer]
OnCalendar=Mon..Fri 07:30 Asia/Seoul
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now ktrader-trade.service ktrader-screen.timer
```
```bash
journalctl -u ktrader-trade -f
```

- 실행 중인 `trade --loop`은 `targets.csv`가 바뀌면 다음 사이클부터 새 목표를 씁니다. 새 파일이 잘못되었으면 기존 목표를 유지하고 오류를 기록합니다.
- `RestartSec=120`: 재시작 직후 토큰을 다시 발급하므로 1분 이상 간격을 둡니다.
- 킬 스위치·잔고 불일치로 루프가 멈추면 서비스가 정상 종료로 끝납니다. 원인을 확인하기 전까지 자동 재시작되지 않게 하려는 의도입니다.

### 6.3 운영 중 지켜야 할 것

- **자동매매 계좌에서 손으로 매매하지 않습니다.** 원장과 증권사 잔고가 달라져 프로그램이 즉시 멈춥니다(의도된 안전장치). 수동 매매는 다른 계좌를 쓰세요.
- 같은 앱키로 여러 프로그램을 동시에 돌리지 않습니다(초당 요청 한도 초과).
- 원장 백업(매일):
```bash
sqlite3 /opt/trader/var/ledger.sqlite3 ".backup '/opt/trader/backup/ledger-$(date +%F).sqlite3'"
```
- 휴장일 등록: `TradingEngine.holidays`에 시장별 휴장일을 넣습니다(미국 조기폐장일 포함).

---

## 7. 해외주식 연동 추가 사항

- `trade`는 목표 CSV에 해외 종목이 있으면 해외주식 API로 자동 라우팅합니다.
- 해외 종목의 원화 환산 환율은 yfinance로 가져옵니다(`pip install -e ".[data]"` 필요).
- 미국 NYSE·AMEX 상장 종목은 거래소 지정이 필요합니다. 기본값은 NASD이며, 운영 코드에서 `register_instrument("KO", exchange="NYSE")`로 등록합니다.
- 해외는 지정가만 씁니다. 미국장은 한국시각 밤에 열리므로 `trade --loop`을 24시간 실행해 두면 국내·해외를 각 시장 시간에 맞춰 처리합니다.

---

## 8. 실전 전환

모의투자 결과가 백테스트 기대치와 크게 다르지 않은 것을 최소 1~3개월 확인한 뒤 진행합니다.

1. KIS Developers에서 **실전 계좌용** 앱키·시크릿 발급
2. 별도 파일(`.env.real`)에 실전 키와 실계좌번호 입력, `TRADER_MODE=real`
3. 연결 점검:
```bash
python -m ktrader check --mode real
```
4. `RiskConfig`의 한도를 소액 기준으로 낮춤(예: `max_order_value`, `max_position_weight`)
5. 확인 문구 설정 후 1회 실행:
```bash
TRADER_REAL_CONFIRM=I_UNDERSTAND_REAL_MONEY_RISK python -m ktrader trade --mode real --targets targets.csv
```
6. 체결·원장·앱 화면 대조 → 이상 없으면 systemd 서비스의 `--mode`를 `real`로 변경

---

## 9. 문제 해결

| 증상 | 원인 후보 | 조치 |
|---|---|---|
| `ConnectionError`, 시간 초과 | 방화벽이 9443/29443 차단, 프록시 | 아웃바운드 허용, 다른 네트워크에서 `check`로 비교 |
| 토큰 발급 401/403 | 앱키·시크릿 오타, 모의/실전 키를 반대 서버에 사용 | `--mode`와 키 종류 일치 확인, 재발급 |
| 토큰 발급 반복 실패 | 짧은 시간 재발급 제한 | 1분 이상 대기 후 재시도 |
| 잔고 조회 실패(계좌 관련 메시지) | 모의계좌번호가 아닌 실계좌번호 입력, 상품코드(뒤 2자리) 오류 | 모의투자는 모의계좌번호 사용, `12345678-01` 형식 |
| 초당 거래건수 초과 | 동시 실행, 너무 짧은 간격 | 프로그램 1개만 실행, `--interval` 늘리기 |
| `연속매매 중인 시장 없음` | 장 시간 외 실행 | 정규장 시간에 실행(5.2) |
| 주문이 모두 `rejected_risk` | 리스크 한도(비중·주문금액·현금) | `orders.risk_note` 열에서 사유 확인 |
| `ReconcileError`로 중단 | 수동 매매, 체결 누락 | 증권사 체결내역과 원장 대조 후 원인 해결, 원장 정정 뒤 재시작 |
| 해외 현재가 실패 | 해외주식 미신청, 모의서버 미지원, 거래소 코드(NYSE/AMEX) 미지정 | 신청 상태 확인, `register_instrument` |
| 주문 접수 불명(`error`) | 전송 중 네트워크 끊김 | **재주문 금지**. 증권사 앱에서 접수 여부 확인 후 판단 |

TR_ID·필드명은 증권사 공지로 바뀔 수 있습니다. 갑자기 특정 기능만 실패하면 KIS Developers 공지와 API 문서를 먼저 확인하세요(`broker/kis.py::TR_IDS`, `broker/kis_overseas.py::OVERSEAS_TR_IDS`).
