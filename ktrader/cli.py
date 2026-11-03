"""명령행 진입점.

  python -m ktrader demo                     # 네트워크 없이 합성 데이터로 전 과정 시연
  python -m ktrader screen --date 20261030   # 실제 데이터(pykrx)로 종목 추천
  python -m ktrader backtest --start 20200101 --end 20261030 --strategy momentum
  python -m ktrader trade --mode mock        # 한국투자증권 모의투자 서버로 1회 사이클 실행
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import pandas as pd

from .analysis.factors import compute_factor_table
from .analysis.model import EqualWeight, RidgeRank, build_dataset, select_model
from .analysis.screener import ScreenConfig, score
from .backtest.engine import Backtester, BacktestConfig
from .backtest.metrics import compute_metrics, format_metrics
from .backtest.strategies import BuyAndHoldEqual, MomentumStrategy, MultiFactorStrategy, RsiMeanReversion, ValueStrategy
from .broker.paper import PaperBroker
from .config import AppConfig, KISConfig, TradingMode
from .data.preprocess import clean_ohlcv
from .data.sample import inject_errors, make_sample_panel
from .data.validate import validate_ohlcv
from .execution.engine import ExecutionConfig, TradingEngine
from .ledger.store import Ledger
from .report import charts
from .report.explain import DISCLAIMER, backtest_report_md, explain_exclusions, explain_order, explain_recommendation
from .risk.manager import RiskManager
from .security.compliance import assert_mode_allowed
from .security.secrets import install_log_masking

log = logging.getLogger("ktrader")

STRATEGIES = {
    "momentum": lambda: MomentumStrategy(top_n=10),
    "value": lambda: ValueStrategy(top_n=10),
    "multifactor": lambda: MultiFactorStrategy(top_n=10),
    "rsi": lambda: RsiMeanReversion(),
}


def _section(title: str):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def cmd_demo(args, cfg: AppConfig) -> int:
    out = cfg.report_dir
    os.makedirs(out, exist_ok=True)
    panel, fund, names = make_sample_panel(n_tickers=args.tickers, n_days=args.days, seed=args.seed)
    cal = panel["close"].index
    print("※ 합성(가상) 데이터 데모입니다. 수치는 실제 시장 성과가 아닙니다.")

    _section("1~2단계. 데이터 품질 검증")
    t0 = panel["close"].columns[0]
    one = pd.DataFrame({k: panel[k][t0] for k in ("open", "high", "low", "close", "volume")})
    print(validate_ohlcv(one, t0, cal).summary())
    print(validate_ohlcv(inject_errors(one), f"{t0}(오류주입)", cal).summary())
    clean = clean_ohlcv(one, cal)
    print(f"정제 후 거래정지 표시 행: {int(clean['suspended'].sum())}")

    _section("3단계. 종목 선별(멀티팩터) 및 추천 근거")
    asof = cal[-1]
    table = compute_factor_table(panel, fund, asof)
    res = score(table, cfg=ScreenConfig(restricted={panel['close'].columns[1]}))
    print(explain_exclusions(res.excluded))
    for t, row in res.top(3).iterrows():
        print(explain_recommendation(t, row, names[t], len(res.scores), table.loc[t]))
    charts.plot_contributions(res.top(1).iloc[0], f"{res.top(1).index[0]} factor contribution", f"{out}/contrib_top1.png")

    _section("3단계(보강). 예측 모델 선정 — 워크포워드 표본외 IC 비교")
    month_ends = pd.Series(cal, index=cal).groupby(cal.to_period("M")).last().iloc[12:-1]
    feats = ["mom_12_1", "trend_200", "rsi_14", "vol_60", "earnings_yield", "book_yield", "roe"]
    ds = build_dataset(panel, fund, list(month_ends), feats, horizon=21)
    sel = select_model(ds, {"동일가중(기준)": EqualWeight, "릿지회귀": lambda: RidgeRank(alpha=5.0)}, train_periods=6)
    print(sel.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print("→ ICIR이 더 높은 모델을 채택하되, 차이가 작으면 단순한 모델을 우선합니다.")

    _section("4단계. 전략 백테스트(비용·슬리피지·가격제한·거래정지 반영)")
    bt_cfg = BacktestConfig(initial_cash=100_000_000, rebalance="M", cost=cfg.cost, trailing_stop_pct=0.15)
    results, metrics = {}, {}
    for key in ("momentum", "value", "multifactor"):
        s = STRATEGIES[key]()
        r = Backtester(bt_cfg).run(panel, s, fund)
        results[s.name], metrics[s.name] = r.equity, compute_metrics(r)
    bench = Backtester(BacktestConfig(rebalance="M", cost=cfg.cost)).run(panel, BuyAndHoldEqual(), fund)
    results["동일가중 벤치마크"], metrics["동일가중 벤치마크"] = bench.equity, compute_metrics(bench)
    summary = pd.DataFrame(metrics).T[["cagr", "ann_vol", "sharpe", "mdd", "turnover", "n_trades"]]
    print(summary.to_string(float_format=lambda x: f"{x:,.3f}"))
    best = max((k for k in metrics if k != "동일가중 벤치마크"), key=lambda k: metrics[k]["sharpe"])
    print(f"\n[{best}] 상세")
    print(format_metrics(metrics[best]).to_string(index=False))
    period = f"{results[best].index[0].date()} ~ {results[best].index[-1].date()}"
    with open(f"{out}/backtest_report.md", "w", encoding="utf-8") as f:
        f.write(backtest_report_md(best, metrics[best], metrics["동일가중 벤치마크"], period))
    charts.plot_equity(results, f"{out}/equity.png")
    charts.plot_monthly_heatmap(results[best], f"{out}/monthly.png")

    _section("5~6단계. 자동매매 1사이클(PaperBroker, 리스크·컴플라이언스·원장 기록)")
    broker = PaperBroker(cash=50_000_000, cost=cfg.cost)
    prev = panel["close"].iloc[-2]
    for t in panel["close"].columns:
        broker.set_quote(t, float(panel["close"][t].iloc[-1]), float(prev[t]), float(panel["volume"][t].iloc[-1]))
    ledger = Ledger(":memory:")
    risk = RiskManager(cfg.risk)

    def target_fn(_acct):
        top = res.top(8)
        reasons = {t: f"멀티팩터 순위 {int(r['rank'])}위(점수 {r['composite']:+.2f})" for t, r in top.iterrows()}
        return pd.Series(0.10, index=top.index), reasons

    eng = TradingEngine(broker, risk, ledger, target_fn, ExecutionConfig(allow_outside_session=True))
    # 데모: 지정가(현재가+0.2%)가 즉시 체결되도록 시세를 그대로 두고 1회 실행
    r = eng.run_cycle()
    for o in r["orders"]:
        print(" ·", explain_order(o))
    acct = broker.get_account()
    print(f"\n계좌: 현금 {acct.cash:,.0f}원, 평가 {acct.total_equity:,.0f}원, 보유 {len(acct.positions)}종목")
    ok, bad = ledger.verify_audit_chain()
    print(f"감사로그 해시체인 무결성: {'정상' if ok else f'변조 탐지(행 {bad})'}")
    print(ledger.df("SELECT ticker, side, qty, ROUND(price) price, status, risk_note FROM orders").to_string(index=False))

    print(f"\n산출물: {out}/backtest_report.md, equity.png, monthly.png, contrib_top1.png\n{DISCLAIMER}")
    return 0


def cmd_screen(args, cfg: AppConfig) -> int:
    from .data.collector import MarketDataCollector

    col = MarketDataCollector(cfg.data_dir)
    tickers = col.get_tickers(args.date, args.market)["ticker"].tolist()[: args.limit]
    start = (pd.Timestamp(args.date) - pd.Timedelta(days=420)).strftime("%Y%m%d")
    panel = col.get_ohlcv_panel(tickers, start, args.date)
    f = col.get_fundamentals(args.date, args.market).reindex(tickers)
    asof = panel["close"].index[-1]
    fund = {k: pd.DataFrame([f[k]], index=[asof]) for k in ("PER", "PBR") if k in f}
    if {"EPS", "BPS"} <= set(f.columns):
        fund["ROE"] = pd.DataFrame([f["EPS"] / f["BPS"].where(f["BPS"] > 0)], index=[asof])
    table = compute_factor_table(panel, fund, asof)
    res = score(table)
    print(explain_exclusions(res.excluded))
    for t, row in res.top(args.top).iterrows():
        print(explain_recommendation(t, row, "", len(res.scores), table.loc[t]))
    print(DISCLAIMER)
    return 0


def cmd_backtest(args, cfg: AppConfig) -> int:
    from .data.collector import MarketDataCollector

    col = MarketDataCollector(cfg.data_dir)
    tickers = col.get_tickers(args.start, args.market)["ticker"].tolist()[: args.limit]
    panel = col.get_ohlcv_panel(tickers, args.start, args.end)
    strat = STRATEGIES[args.strategy]()
    if args.strategy in ("value", "multifactor"):
        print("가치/멀티팩터 실데이터 백테스트는 시점정합 재무 패널(align_fundamentals_pit)이 필요합니다.")
        return 2
    r = Backtester(BacktestConfig(cost=cfg.cost)).run(panel, strat)
    m = compute_metrics(r)
    print(backtest_report_md(strat.name, m, period=f"{args.start}~{args.end}"))
    return 0


def cmd_trade(args, cfg: AppConfig) -> int:
    mode = TradingMode(args.mode)
    assert_mode_allowed(mode)
    if mode == TradingMode.PAPER:
        print("paper 모드는 demo 명령으로 확인하십시오.")
        return 0
    from .broker.kis import KISBroker

    broker = KISBroker(KISConfig.from_env(is_mock=(mode == TradingMode.MOCK)), cfg.cost)
    ledger = Ledger(cfg.db_path)
    risk = RiskManager(cfg.risk)
    targets = pd.read_csv(args.targets, dtype={"ticker": str}).set_index("ticker")

    def target_fn(_acct):
        return targets["weight"], targets.get("reason", pd.Series("목표 비중", index=targets.index)).to_dict()

    eng = TradingEngine(broker, risk, ledger, target_fn)
    eng.bootstrap_from_account(broker.get_account())
    if args.loop:
        eng.run_forever(args.interval)
    else:
        print(eng.run_cycle())
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    install_log_masking()
    p = argparse.ArgumentParser(prog="ktrader", description="국내 주식 AI 분석·자동매매 시스템")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo")
    d.add_argument("--tickers", type=int, default=40)
    d.add_argument("--days", type=int, default=756)
    d.add_argument("--seed", type=int, default=7)
    s = sub.add_parser("screen")
    s.add_argument("--date", required=True)
    s.add_argument("--market", default="KOSPI")
    s.add_argument("--limit", type=int, default=200)
    s.add_argument("--top", type=int, default=10)
    b = sub.add_parser("backtest")
    b.add_argument("--start", required=True)
    b.add_argument("--end", required=True)
    b.add_argument("--market", default="KOSPI")
    b.add_argument("--limit", type=int, default=200)
    b.add_argument("--strategy", choices=list(STRATEGIES), default="momentum")
    t = sub.add_parser("trade")
    t.add_argument("--mode", choices=[m.value for m in TradingMode], default="mock")
    t.add_argument("--targets", required=True, help="CSV: ticker,weight,reason")
    t.add_argument("--loop", action="store_true")
    t.add_argument("--interval", type=int, default=60)
    args = p.parse_args(argv)
    cfg = AppConfig.from_env()
    logging.getLogger().setLevel(logging.WARNING if args.cmd == "demo" else logging.INFO)
    return {"demo": cmd_demo, "screen": cmd_screen, "backtest": cmd_backtest, "trade": cmd_trade}[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
