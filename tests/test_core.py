import json
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from ktrader.analysis import indicators as ind
from ktrader.analysis.factors import compute_factor_table
from ktrader.analysis.screener import ScreenConfig, score
from ktrader.backtest.engine import Backtester, BacktestConfig
from ktrader.backtest.metrics import compute_metrics
from ktrader.backtest.strategies import MomentumStrategy
from ktrader.broker.base import AccountSnapshot, Order, OrderStatus, OrderType, Position, Quote, Side
from ktrader.broker.kis import KISBroker
from ktrader.broker.paper import PaperBroker
from ktrader.config import KISConfig, RiskConfig, TradingMode
from ktrader.data.preprocess import align_fundamentals_pit, clean_ohlcv
from ktrader.data.sample import inject_errors, make_sample_panel
from ktrader.data.validate import DataQualityError, assert_quality, validate_ohlcv
from ktrader.execution.engine import ExecutionConfig, ReconcileError, TradingEngine
from ktrader.krx import KST, price_limits, round_to_tick, session_phase, tick_size
from ktrader.ledger.store import Ledger
from ktrader.risk.manager import RiskManager
from ktrader.security.compliance import ComplianceGuard, assert_mode_allowed
from ktrader.security.secrets import DataCipher, redact


@pytest.fixture(scope="module")
def sample():
    return make_sample_panel(n_tickers=20, n_days=400, seed=1)


# ---------------------------------------------------------------- KRX 규칙
@pytest.mark.parametrize("price,tick", [(1_999, 1), (2_000, 5), (19_990, 10), (20_000, 50), (199_900, 100), (200_000, 500), (500_000, 1_000)])
def test_tick_size(price, tick):
    assert tick_size(price) == tick


def test_round_to_tick_direction():
    assert round_to_tick(20_049, "buy") == 20_000
    assert round_to_tick(20_001, "sell") == 20_050


def test_price_limits():
    lo, hi = price_limits(10_000)
    assert (lo, hi) == (7_000, 13_000)
    assert hi % tick_size(hi) == 0 and lo % tick_size(lo) == 0


def test_session_phase():
    assert session_phase(datetime(2026, 11, 3, 10, 0, tzinfo=KST)) == "continuous"
    assert session_phase(datetime(2026, 11, 3, 15, 25, tzinfo=KST)) == "close_auction"
    assert session_phase(datetime(2026, 11, 1, 10, 0, tzinfo=KST)) == "closed"  # 일요일


# ---------------------------------------------------------------- 데이터 품질
def _one(panel, t):
    return pd.DataFrame({k: panel[k][t] for k in ("open", "high", "low", "close", "volume")})


def test_validation_passes_clean_and_catches_errors(sample):
    panel, _, _ = sample
    t = panel["close"].columns[0]
    df = _one(panel, t)
    assert validate_ohlcv(df, t).max_severity in ("OK", "INFO")
    rep = validate_ohlcv(inject_errors(df), t)
    checks = {i.check for i in rep.issues}
    assert {"중복날짜", "OHLC불일치", "가격제한폭초과", "거래량0"} <= checks
    with pytest.raises(DataQualityError):
        assert_quality(rep)


def test_clean_marks_suspension(sample):
    panel, _, _ = sample
    t = panel["close"].columns[0]
    df = _one(panel, t)
    cal = df.index
    gappy = df.drop(cal[50:53])
    out = clean_ohlcv(gappy, cal)
    assert out["suspended"].sum() == 3 and out["close"].notna().all()


def test_point_in_time_alignment_has_no_lookahead():
    cal = pd.bdate_range("2025-01-01", "2025-06-30")
    rec = pd.DataFrame({"ticker": ["005930"], "period_end": ["2024-12-31"], "report": ["FY"], "roe": [0.1]})
    w = align_fundamentals_pit(rec, cal, "roe")
    assert w.loc[:"2025-03-28", "005930"].isna().all()   # 결산 후 90일 전에는 알 수 없음
    assert w.loc["2025-04-01", "005930"] == 0.1


# ---------------------------------------------------------------- 지표·선별
def test_rsi_bounds(sample):
    r = ind.rsi(sample[0]["close"], 14).dropna(how="all")
    assert ((r >= 0) & (r <= 100)).all().all()


def test_screen_excludes_restricted_and_ranks(sample):
    panel, fund, _ = sample
    asof = panel["close"].index[-1]
    tbl = compute_factor_table(panel, fund, asof)
    bad = tbl.index[0]
    res = score(tbl, cfg=ScreenConfig(restricted={bad}, min_liquidity=0))
    assert bad not in res.scores.index and bad in res.excluded.index
    assert res.scores["rank"].min() == 1
    contrib = res.scores.filter(like="contrib_").sum(axis=1)
    assert np.allclose(contrib, res.scores["composite"])


# ---------------------------------------------------------------- 백테스트
class _PeekStrategy:
    """미래 데이터가 보이면 실패하는 전략."""
    name = "peek"

    def generate_weights(self, asof, panel, fundamentals=None):
        assert panel["close"].index[-1] == asof, "asof 이후 데이터가 전략에 노출됨"
        return pd.Series(1.0, index=[panel["close"].columns[0]])


def test_backtest_no_lookahead_and_next_open_execution(sample):
    panel, _, _ = sample
    cfg = BacktestConfig(rebalance="M", warmup_days=60)
    r = Backtester(cfg).run(panel, _PeekStrategy())
    first_trade = r.trades.iloc[0]
    reb_dates = sorted(r.target_weights)
    assert first_trade["date"] > reb_dates[0]          # 신호 다음날 체결
    t = first_trade["ticker"]
    o = panel["open"].loc[first_trade["date"], t]
    assert first_trade["price"] == pytest.approx(o * (1 + cfg.cost.slippage_bps / 1e4))


def test_backtest_costs_reduce_equity(sample):
    panel, _, _ = sample
    s = MomentumStrategy(lookback=120, skip=5, top_n=5, market_filter=False, min_value=0)
    free = BacktestConfig(warmup_days=130, cost=type(BacktestConfig().cost)(0, 0, 0))
    costly = BacktestConfig(warmup_days=130)
    e0 = Backtester(free).run(panel, s).equity.iloc[-1]
    e1 = Backtester(costly).run(panel, s).equity.iloc[-1]
    assert e1 < e0
    m = compute_metrics(Backtester(costly).run(panel, s))
    assert m["mdd"] <= 0 and m["n_trades"] > 0


# ---------------------------------------------------------------- 리스크
def _acct(cash=10_000_000, positions=None):
    positions = positions or {}
    mv = sum(p.qty * p.last_price for p in positions.values())
    return AccountSnapshot(cash, positions, cash + mv)


def _quote(px=10_000):
    lo, hi = price_limits(px)
    return Quote("005930", px, px, hi, lo, volume=1_000_000)


def test_risk_rejects_fat_finger_and_bad_tick():
    rm = RiskManager(RiskConfig())
    d = rm.validate_order(Order("005930", Side.BUY, 10, OrderType.LIMIT, 11_000), _acct(), _quote())
    assert not d.approved and any("괴리" in x for x in d.reasons)
    d = rm.validate_order(Order("005930", Side.BUY, 10, OrderType.LIMIT, 10_003), _acct(), _quote())
    assert not d.approved and any("호가단위" in x for x in d.reasons)


def test_risk_blocks_short_selling_and_weight():
    rm = RiskManager(RiskConfig())
    d = rm.validate_order(Order("005930", Side.SELL, 5, OrderType.LIMIT, 10_000), _acct(), _quote())
    assert not d.approved
    d = rm.validate_order(Order("005930", Side.BUY, 200, OrderType.LIMIT, 10_000), _acct(), _quote())
    assert not d.approved and any("비중" in x for x in d.reasons)


def test_duplicate_and_kill_switch():
    rm = RiskManager(RiskConfig())
    ok = rm.validate_order(Order("005930", Side.BUY, 10, OrderType.LIMIT, 10_000), _acct(), _quote())
    dup = rm.validate_order(Order("005930", Side.BUY, 10, OrderType.LIMIT, 10_000), _acct(), _quote())
    assert ok.approved and not dup.approved
    rm.update_equity(100.0)
    rm.update_equity(80.0)
    assert rm.kill_switch
    with pytest.raises(PermissionError):
        rm.reset_kill("yes")


def test_trailing_stop():
    rm = RiskManager(RiskConfig(stop_loss_pct=0.5, trailing_stop_pct=0.1))
    acct = _acct(positions={"005930": Position("005930", 10, 10_000, 10_000)})
    assert rm.check_exits(acct, {"005930": 13_000}) == []
    ex = rm.check_exits(acct, {"005930": 11_500})
    assert ex and "추적손절" in ex[0].reason


# ---------------------------------------------------------------- 실행·원장
def test_engine_cycle_and_reconcile():
    b = PaperBroker(cash=10_000_000)
    for t, px in {"005930": 70_000, "000660": 180_000}.items():
        b.set_quote(t, px, px)
    led = Ledger(":memory:")
    eng = TradingEngine(b, RiskManager(), led,
                        lambda a: (pd.Series({"005930": 0.09, "000660": 0.09}), {"005930": "테스트"}),
                        ExecutionConfig(allow_outside_session=True))
    res = eng.run_cycle()
    assert all(o.status == OrderStatus.FILLED for o in res["orders"])
    assert led.positions_from_fills() == {t: p.qty for t, p in b.get_account().positions.items()}
    b.positions["005930"].qty += 1  # 원장 밖에서 잔고가 바뀐 상황(수동 매매 등)
    with pytest.raises(ReconcileError):
        eng.run_cycle()
    assert eng.risk.kill_switch


def test_audit_chain_detects_tampering():
    led = Ledger(":memory:")
    for i in range(3):
        led.log_event("x", {"i": i})
    assert led.verify_audit_chain() == (True, None)
    led.db.execute("UPDATE audit_log SET payload=? WHERE id=2", (json.dumps({"i": 99}),))
    assert led.verify_audit_chain() == (False, 2)


# ---------------------------------------------------------------- 보안·준법
def test_redaction_and_cipher():
    s = redact('appkey: PSabcdef123 account 12345678-01 "authorization": "Bearer eyJhbGc"')
    assert "PSabcdef123" not in s and "12345678" not in s and "eyJhbGc" not in s
    from cryptography.fernet import Fernet

    c = DataCipher(Fernet.generate_key())
    assert c.decrypt(c.encrypt("12345678-01")) == "12345678-01"


def test_real_mode_requires_confirmation(monkeypatch):
    monkeypatch.delenv("TRADER_REAL_CONFIRM", raising=False)
    with pytest.raises(PermissionError):
        assert_mode_allowed(TradingMode.REAL)
    assert_mode_allowed(TradingMode.MOCK)


def test_compliance_blocks_self_crossing():
    g = ComplianceGuard()
    resting = [Order("005930", Side.SELL, 1, OrderType.LIMIT, 70_000)]
    assert g.check(Order("005930", Side.BUY, 1, OrderType.LIMIT, 70_000), resting, 1e6,
                   datetime(2026, 11, 3, 10, 0, tzinfo=KST))


# ---------------------------------------------------------------- KIS 어댑터(네트워크 없음)
class _FakeResp:
    def __init__(self, js, status=200):
        self._js, self.status_code = js, status

    def json(self):
        return self._js

    def raise_for_status(self):
        pass


class _FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None):
        return _FakeResp({"access_token": "tok", "expires_in": 86400})

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append((method, url, headers, params, json))
        if "order-cash" in url:
            return _FakeResp({"rt_cd": "0", "msg1": "주문 전송 완료",
                              "output": {"KRX_FWDG_ORD_ORGNO": "91252", "ODNO": "0000117057"}})
        return _FakeResp({"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "오류"})


def test_kis_order_request_shape():
    sess = _FakeSession()
    b = KISBroker(KISConfig("k", "s", "12345678-01", is_mock=True), session=sess, min_interval_sec=0)
    o = b.place_order(Order("005930", Side.BUY, 3, OrderType.LIMIT, 70_000))
    method, url, headers, _, body = sess.calls[-1]
    assert o.status == OrderStatus.SUBMITTED and o.broker_order_id == "0000117057"
    assert url.startswith("https://openapivts") and headers["tr_id"] == "VTTC0012U"
    assert body == {"CANO": "12345678", "ACNT_PRDT_CD": "01", "PDNO": "005930", "ORD_DVSN": "00",
                    "ORD_QTY": "3", "ORD_UNPR": "70000"}


def test_kis_rejects_bad_account():
    with pytest.raises(ValueError):
        KISBroker(KISConfig("k", "s", "1234-5"))


def test_walk_forward_optimize_smoke(sample):
    from ktrader.backtest.validation import walk_forward_optimize

    panel, _, _ = sample
    wins = walk_forward_optimize(
        panel, lambda **p: MomentumStrategy(market_filter=False, min_value=0, **p),
        {"lookback": [60, 120], "top_n": [3, 5]}, BacktestConfig(warmup_days=130),
        train_days=120, test_days=60)
    assert wins and all(w.best_params for w in wins)
