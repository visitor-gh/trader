from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from ktrader.analysis.factors import compute_factor_table
from ktrader.analysis.screener import ScreenConfig, score
from ktrader.backtest.engine import Backtester, BacktestConfig
from ktrader.backtest.metrics import pnl_attribution
from ktrader.backtest.strategies import BuyAndHoldEqual, MultiFactorStrategy
from ktrader.broker.base import AccountSnapshot, Order, OrderStatus, OrderType, Position, Quote, Side
from ktrader.broker.kis_overseas import KISOverseasBroker
from ktrader.broker.paper import PaperBroker
from ktrader.broker.router import RoutingBroker
from ktrader.config import KISConfig, RiskConfig
from ktrader.data.global_collector import SecEdgarClient
from ktrader.data.preprocess import align_fundamentals_pit
from ktrader.data.sample import make_global_sample
from ktrader.data.validate import validate_ohlcv
from ktrader.execution.engine import ExecutionConfig, TradingEngine
from ktrader.fx import FxRates, decompose_return
from ktrader.krx import KST
from ktrader.ledger.store import Ledger
from ktrader.markets import MARKETS, parse_symbol, register_instrument
from ktrader.risk.manager import RiskManager
from ktrader.tax import estimate_overseas_cgt


@pytest.fixture(scope="module")
def gsample():
    return make_global_sample(per_market=6, n_days=420, seed=3)


# ---------------------------------------------------------------- 종목 식별·시장 규칙
@pytest.mark.parametrize("sym,market,local,lot", [
    ("005930", "KR", "005930", 1), ("035720.KQ", "KR", "035720", 1), ("AAPL", "US", "AAPL", 1),
    ("BRK-B", "US", "BRK-B", 1), ("7203.T", "JP", "7203", 100), ("0700.HK", "HK", "00700", 100)])
def test_parse_symbol(sym, market, local, lot):
    i = parse_symbol(sym)
    assert (i.market, i.local_code, i.lot) == (market, local, lot)


def test_parse_symbol_rejects_garbage():
    with pytest.raises(ValueError):
        parse_symbol("12345")
    with pytest.raises(ValueError):
        parse_symbol("7203.TK")


def test_lot_override():
    register_instrument("0005.HK", lot=400)
    assert parse_symbol("0005.HK").lot == 400


def test_tick_tables():
    us, jp, hk = MARKETS["US"], MARKETS["JP"], MARKETS["HK"]
    assert us.tick_size(150) == 0.01 and us.tick_size(0.5) == 0.0001
    assert us.round_price(150.237, "buy") == 150.23 and us.round_price(150.231, "sell") == 150.24
    assert jp.tick_size(2_500) == 1 and jp.tick_size(4_000) == 5
    assert hk.tick_size(36.3) == 0.05 and hk.is_on_tick(36.35) and not hk.is_on_tick(36.33)


def test_us_session_follows_dst():
    us = MARKETS["US"]
    # 2026-07-01(서머타임): 한국시간 22:35 = 뉴욕 09:35 → 정규장
    assert us.session_phase(datetime(2026, 7, 1, 22, 35, tzinfo=KST)) == "continuous"
    # 2026-11-03(서머타임 종료): 한국시간 22:35 = 뉴욕 08:35 → 장 전, 23:35 → 정규장
    assert us.session_phase(datetime(2026, 11, 3, 22, 35, tzinfo=KST)) == "closed"
    assert us.session_phase(datetime(2026, 11, 3, 23, 35, tzinfo=KST)) == "continuous"


def test_lunch_breaks():
    assert MARKETS["JP"].session_phase(datetime(2026, 11, 4, 12, 0, tzinfo=KST)) == "break"
    assert MARKETS["HK"].session_phase(datetime(2026, 11, 4, 13, 30, tzinfo=KST)) == "break"  # 홍콩 12:30
    assert MARKETS["HK"].session_phase(datetime(2026, 11, 4, 14, 30, tzinfo=KST)) == "continuous"


def test_no_price_limits_outside_kr():
    assert MARKETS["US"].price_limits(100) == (0.0, float("inf"))


# ---------------------------------------------------------------- 환율
def test_fx_no_lookahead_and_decompose():
    idx = pd.bdate_range("2026-01-01", periods=5)
    fx = FxRates({"USD": pd.Series([1300, 1310, 1320, 1330, 1340.0], index=idx)})
    assert fx.rate("USD", idx[1]) == 1310
    assert fx.rate("KRW") == 1.0
    d = decompose_return(pd.Series([100, 110.0], index=idx[[0, 4]]), fx.rates["USD"])
    assert d["주가효과"].iloc[0] == pytest.approx(0.10)
    assert d["원화기준합계"].iloc[0] == pytest.approx(1.1 * 1340 / 1300 - 1)
    with pytest.raises(ValueError):
        FxRates({"JPY": pd.Series([-1.0], index=idx[:1])})


# ---------------------------------------------------------------- 검증·선별
def test_split_detection_without_price_limits(gsample):
    panel = gsample[0]
    t = "ZUS01"
    df = pd.DataFrame({k: panel[k][t] for k in ("open", "high", "low", "close", "volume")}).dropna()
    df.iloc[50:, :4] /= 2
    checks = {i.check for i in validate_ohlcv(df, t).issues}
    assert "분할의심" in checks and "가격제한폭초과" not in checks


def test_screen_by_market_standardization(gsample):
    panel, fund, _, _ = gsample
    tbl = compute_factor_table(panel, fund, panel["close"].index[-1])
    assert set(tbl["market"]) == {"KR", "US", "JP", "HK"}
    assert (tbl.loc["ZUS01", "price_base"] / tbl.loc["ZUS01", "price"]) > 1000  # 달러 → 원화
    res = score(tbl, cfg=ScreenConfig(standardize_by_market=True, min_liquidity=0))
    for m, g in res.scores.groupby("market"):
        assert abs(g["z_book_yield"].mean()) < 1e-9   # 시장 내 평균 0
    only_us = score(tbl, cfg=ScreenConfig(markets={"US"}, min_liquidity=0))
    assert set(only_us.scores["market"]) == {"US"}


def test_pit_with_filed_dates():
    cal = pd.bdate_range("2026-01-01", "2026-04-30")
    rec = pd.DataFrame({"ticker": ["AAPL"], "period_end": ["2025-09-27"], "report": ["FY"],
                        "disclosed_at": ["2026-02-10"], "roe": [1.5]})
    w = align_fundamentals_pit(rec, cal, "roe")
    assert w.loc[:"2026-02-09", "AAPL"].isna().all() and w.loc["2026-02-10", "AAPL"] == 1.5


def test_sec_fact_frame_keeps_first_filing():
    facts = {"facts": {"us-gaap": {"NetIncomeLoss": {"units": {"USD": [
        {"start": "2024-01-01", "end": "2024-12-31", "val": 100, "form": "10-K", "fy": 2024, "fp": "FY", "filed": "2025-02-20"},
        {"start": "2024-01-01", "end": "2024-12-31", "val": 90, "form": "10-K", "fy": 2025, "fp": "FY", "filed": "2026-02-20"},
        {"start": "2024-07-01", "end": "2024-09-30", "val": 30, "form": "10-Q", "fy": 2024, "fp": "Q3", "filed": "2024-11-01"},
    ]}}}}}
    df = SecEdgarClient.fact_frame(facts, "net_income")
    assert len(df) == 1 and df["value"].iloc[0] == 100   # 정정 전 최초 공시값 = 당시 알 수 있던 값
    with pytest.raises(ValueError):
        SecEdgarClient(user_agent="")


# ---------------------------------------------------------------- 백테스트
def test_global_backtest_fx_costs_and_lots(gsample):
    panel, fund, _, _ = gsample
    cfg = BacktestConfig(rebalance="M", warmup_days=260)
    r = Backtester(cfg).run(panel, MultiFactorStrategy(top_n=8, screen=ScreenConfig(min_liquidity=0)), fund)
    tr = r.trades
    assert set(tr["market"]) - {"KR"}                          # 해외 체결 발생
    jp = tr[tr["market"] == "JP"]
    assert (jp["qty"] % 100 == 0).all()                       # 일본 100주 단위
    us = tr[tr["market"] == "US"].iloc[0]
    assert us["fx"] > 1000 and us["cost"] > us["qty"] * us["price"] * us["fx"] * 0.0025  # 수수료+환전
    for _, row in tr.iterrows():                               # 휴장일에는 체결 없음
        assert np.isfinite(panel["open"].loc[row["date"], row["ticker"]])
    att = pnl_attribution(r, panel)
    assert att.loc["KR", "환율효과"] == 0


def test_backtest_equity_in_krw(gsample):
    panel, fund, _, fx = gsample
    r = Backtester(BacktestConfig(warmup_days=260)).run(panel, BuyAndHoldEqual(), fund)
    d = r.equity.index[-1]
    pos = r.positions.loc[d]
    mv = sum(q * panel["close"].ffill().loc[d, t] * panel["fx"].ffill().loc[d, t] for t, q in pos.items() if q)
    assert r.equity.iloc[-1] == pytest.approx(r.cash.iloc[-1] + mv)


# ---------------------------------------------------------------- 리스크·실행
def _usq(px=100.0, fx=1400.0):
    return Quote("ZUS01", px, px, float("inf"), 0.0, 1e6, currency="USD", fx_rate=fx)


def test_risk_uses_krw_notional_and_foreign_limits():
    rm = RiskManager(RiskConfig(max_order_value=5_000_000, max_foreign_weight=0.3))
    acct = AccountSnapshot(100_000_000, {}, 100_000_000)
    d = rm.validate_order(Order("ZUS01", Side.BUY, 40, OrderType.LIMIT, 100.0), acct, _usq())
    assert not d.approved and any("1회 주문금액" in x for x in d.reasons)   # 40×100×1400 = 560만원
    held = {"ZUS02": Position("ZUS02", 200, 100, 100, currency="USD", fx_rate=1400)}  # 2,800만원(28%)
    acct2 = AccountSnapshot(72_000_000, held, 100_000_000)
    d = rm.validate_order(Order("ZUS01", Side.BUY, 20, OrderType.LIMIT, 100.0), acct2, _usq())
    assert not d.approved and any("외화자산" in x for x in d.reasons)


def test_risk_lot_and_tick_per_market():
    rm = RiskManager()
    acct = AccountSnapshot(100_000_000, {}, 100_000_000)
    jq = Quote("9901.T", 2500, 2500, float("inf"), 0, 1e6, currency="JPY", fx_rate=9.2, lot=100)
    d = rm.validate_order(Order("9901.T", Side.BUY, 150, OrderType.LIMIT, 2500), acct, jq)
    assert any("매매단위" in x for x in d.reasons)
    d = rm.validate_order(Order("ZUS01", Side.BUY, 1, OrderType.LIMIT, 100.005), acct, _usq())
    assert any("호가단위" in x for x in d.reasons)


def test_risk_without_auto_fx_needs_foreign_cash():
    rm = RiskManager(RiskConfig(auto_fx=False))
    acct = AccountSnapshot(100_000_000, {}, 100_000_000, cash_by_ccy={"KRW": 100_000_000, "USD": 0.0})
    d = rm.validate_order(Order("ZUS01", Side.BUY, 5, OrderType.LIMIT, 100.0), acct, _usq())
    assert not d.approved and any("USD 예수금" in x for x in d.reasons)


def test_engine_only_trades_open_markets():
    b = PaperBroker(cash=100_000_000)
    b.set_quote("005930", 70_000, 70_000)
    b.set_quote("ZUS01", 100.0, 100.0, fx_rate=1400)
    # 2026-11-04 10:00 KST: 한국 정규장, 미국 휴장 시간
    clock = lambda: datetime(2026, 11, 4, 10, 0, tzinfo=KST)  # noqa: E731
    eng = TradingEngine(b, RiskManager(clock=clock), Ledger(":memory:"),
                        lambda a: (pd.Series({"005930": 0.05, "ZUS01": 0.05}), {}), ExecutionConfig(), clock=clock)
    res = eng.run_cycle()
    assert {o.ticker for o in res["orders"]} == {"005930"}
    # 같은 날 23:40 KST: 미국 정규장(서머타임 종료 후 09:40 EST)
    clock2 = lambda: datetime(2026, 11, 4, 23, 40, tzinfo=KST)  # noqa: E731
    eng.clock = clock2
    eng.risk.clock = clock2
    res = eng.run_cycle()
    assert {o.ticker for o in res["orders"]} == {"ZUS01"}
    assert all(o.status == OrderStatus.FILLED for o in res["orders"])


def test_router_aggregates_and_routes():
    kr, ov = PaperBroker(cash=10_000_000), PaperBroker(cash=5_000_000)
    ov.set_quote("ZUS01", 100.0, fx_rate=1400)
    r = RoutingBroker({"KR": kr, "US": ov, "JP": ov})
    assert r.broker_for("ZUS01") is ov and r.broker_for("005930") is kr
    assert r.get_account().total_equity == 15_000_000
    o = r.place_order(Order("ZUS01", Side.BUY, 2, OrderType.LIMIT, 100.0))
    assert o.status == OrderStatus.FILLED and "ZUS01" in r.get_account().positions
    with pytest.raises(KeyError):
        r.broker_for("0700.HK")


# ---------------------------------------------------------------- 원장·세금
def test_ledger_krw_pnl_and_tax():
    from ktrader.broker.base import Fill

    led = Ledger(":memory:")
    led.record_fill(Fill("a", "ZUS01", Side.BUY, 100, 100.0, 0, 0, currency="USD", fx_rate=1300))
    led.record_fill(Fill("b", "ZUS01", Side.SELL, 100, 120.0, 0, 0, currency="USD", fx_rate=1400))
    pnl = led.realized_pnl()
    assert pnl.loc[0, "realized_pnl"] == pytest.approx(100 * (120 * 1400 - 100 * 1300))  # 380만원
    est = estimate_overseas_cgt(led.realized_trades(), datetime.now(tz=KST).year)
    assert est.taxable == pytest.approx(3_800_000 - 2_500_000) and est.tax == round(1_300_000 * 0.22)


def test_ledger_migrates_old_schema(tmp_path):
    import sqlite3

    p = tmp_path / "old.sqlite3"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE fills (id INTEGER PRIMARY KEY AUTOINCREMENT, client_order_id TEXT, ticker TEXT, side TEXT,"
               " qty INTEGER, price REAL, fee REAL, tax REAL, filled_at TEXT)")
    db.execute("INSERT INTO fills(client_order_id, ticker, side, qty, price, fee, tax, filled_at) VALUES ('x','005930','buy',1,1,0,0,'2026-01-01')")
    db.commit()
    db.close()
    led = Ledger(str(p))
    assert led.df("SELECT currency, fx_rate FROM fills").iloc[0].tolist() == ["KRW", 1.0]


# ---------------------------------------------------------------- KIS 해외 어댑터(네트워크 없음)
class _Resp:
    def __init__(self, js):
        self._js, self.status_code = js, 200

    def json(self):
        return self._js

    def raise_for_status(self):
        pass


class _Sess:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None):
        return _Resp({"access_token": "t", "expires_in": 86400})

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append((url, headers, params, json))
        if url.endswith("/trading/order"):
            return _Resp({"rt_cd": "0", "msg1": "ok", "output": {"ODNO": "0030000001", "KRX_FWDG_ORD_ORGNO": "01790"}})
        if "quotations/price" in url:
            return _Resp({"rt_cd": "0", "output": {"last": "189.43", "base": "187.10", "tvol": "51234567"}})
        return _Resp({"rt_cd": "1", "msg_cd": "X", "msg1": "err"})


def test_kis_overseas_order_and_quote_shape():
    s = _Sess()
    b = KISOverseasBroker(KISConfig("k", "s", "12345678-01", is_mock=True), fx_provider=lambda c: 1400.0,
                          session=s, min_interval_sec=0)
    register_instrument("KO", exchange="NYSE")
    q = b.get_quote("KO")
    assert s.calls[-1][2] == {"AUTH": "", "EXCD": "NYS", "SYMB": "KO"} and q.currency == "USD" and q.fx_rate == 1400
    o = b.place_order(Order("AAPL", Side.BUY, 3, OrderType.LIMIT, 189.5))
    url, headers, _, body = s.calls[-1]
    assert o.status == OrderStatus.SUBMITTED and headers["tr_id"] == "VTTT1002U"
    assert body["OVRS_EXCG_CD"] == "NASD" and body["PDNO"] == "AAPL" and body["OVRS_ORD_UNPR"] == "189.5"
    b.place_order(Order("7203.T", Side.SELL, 100, OrderType.LIMIT, 2800))
    assert s.calls[-1][1]["tr_id"] == "VTTS0307U" and s.calls[-1][3]["OVRS_EXCG_CD"] == "TKSE"
    mkt = b.place_order(Order("AAPL", Side.BUY, 1, OrderType.MARKET))
    assert mkt.status == OrderStatus.REJECTED
