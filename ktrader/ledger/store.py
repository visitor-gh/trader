"""거래 원장(ledger): 주문·체결·잔고·성과·감사로그를 SQLite에 영구 기록.

- 감사로그(audit_log)는 해시 체인(hash chain) 구조: 각 행이 직전 행의 해시를 포함하므로
  과거 기록이 사후에 수정·삭제되면 verify_audit_chain()이 즉시 탐지한다.
- 계좌번호 등 민감정보는 저장하지 않거나 마스킹/암호화해서 저장한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime

import pandas as pd

from ..broker.base import Fill, Order
from ..krx import now_kst

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
  client_order_id TEXT PRIMARY KEY, broker_order_id TEXT, ticker TEXT, side TEXT, qty INTEGER,
  order_type TEXT, price REAL, status TEXT, filled_qty INTEGER, avg_fill_price REAL,
  reason TEXT, risk_note TEXT, message TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT, client_order_id TEXT, ticker TEXT, side TEXT,
  qty INTEGER, price REAL, fee REAL, tax REAL, filled_at TEXT, currency TEXT DEFAULT 'KRW', fx_rate REAL DEFAULT 1.0
);
CREATE TABLE IF NOT EXISTS equity (
  ts TEXT PRIMARY KEY, cash REAL, market_value REAL, total_equity REAL, n_positions INTEGER
);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ticker TEXT, target_weight REAL, score REAL, explanation TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, event TEXT, payload TEXT, prev_hash TEXT, hash TEXT
);
CREATE INDEX IF NOT EXISTS ix_fills_ticker ON fills(ticker);
"""


class Ledger:
    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self._migrate()
        if path != ":memory:":
            os.chmod(path, 0o600)  # 소유자만 읽기/쓰기

    def _migrate(self) -> None:
        """구버전 DB(국내 전용)에 통화·환율 컬럼 추가. 기존 행은 KRW, 환율 1.0으로 간주."""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(fills)")}
        for name, ddl in (("currency", "TEXT DEFAULT 'KRW'"), ("fx_rate", "REAL DEFAULT 1.0")):
            if name not in cols:
                self.db.execute(f"ALTER TABLE fills ADD COLUMN {name} {ddl}")
        self.db.commit()

    # ------------------------------------------------------------ 감사로그
    def log_event(self, event: str, payload: dict) -> str:
        row = self.db.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        prev = row[0] if row else "GENESIS"
        ts = now_kst().isoformat()
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        h = hashlib.sha256(f"{prev}|{ts}|{event}|{body}".encode()).hexdigest()
        self.db.execute("INSERT INTO audit_log(ts, event, payload, prev_hash, hash) VALUES (?,?,?,?,?)",
                        (ts, event, body, prev, h))
        self.db.commit()
        return h

    def verify_audit_chain(self) -> tuple[bool, int | None]:
        """(무결성 여부, 최초 변조 행 id)"""
        prev = "GENESIS"
        for id_, ts, ev, body, p, h in self.db.execute("SELECT id, ts, event, payload, prev_hash, hash FROM audit_log ORDER BY id"):
            if p != prev or hashlib.sha256(f"{prev}|{ts}|{ev}|{body}".encode()).hexdigest() != h:
                return False, id_
            prev = h
        return True, None

    # ------------------------------------------------------------ 주문·체결
    def upsert_order(self, o: Order, risk_note: str = "") -> None:
        now = now_kst().isoformat()
        self.db.execute(
            """INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(client_order_id) DO UPDATE SET broker_order_id=excluded.broker_order_id,
               status=excluded.status, filled_qty=excluded.filled_qty, avg_fill_price=excluded.avg_fill_price,
               message=excluded.message, qty=excluded.qty, updated_at=excluded.updated_at,
               risk_note=CASE WHEN excluded.risk_note!='' THEN excluded.risk_note ELSE orders.risk_note END""",
            (o.client_order_id, o.broker_order_id, o.ticker, o.side.value, o.qty, o.order_type.value, o.price,
             o.status.value, o.filled_qty, o.avg_fill_price, o.reason, risk_note, o.message,
             o.created_at.isoformat(), now),
        )
        self.db.commit()
        self.log_event("order", {"id": o.client_order_id, "ticker": o.ticker, "side": o.side.value, "qty": o.qty,
                                 "price": o.price, "status": o.status.value, "risk": risk_note})

    def record_fill(self, f: Fill) -> None:
        self.db.execute("INSERT INTO fills(client_order_id, ticker, side, qty, price, fee, tax, filled_at, currency, fx_rate) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (f.client_order_id, f.ticker, f.side.value, f.qty, f.price, f.fee, f.tax, f.filled_at.isoformat(),
                         f.currency, f.fx_rate))
        self.db.commit()
        self.log_event("fill", {"id": f.client_order_id, "ticker": f.ticker, "side": f.side.value, "qty": f.qty,
                                "price": f.price, "ccy": f.currency, "fx": f.fx_rate})

    def record_equity(self, cash: float, market_value: float, n_positions: int, ts: datetime | None = None) -> None:
        ts = (ts or now_kst()).isoformat()
        self.db.execute("INSERT OR REPLACE INTO equity VALUES (?,?,?,?,?)", (ts, cash, market_value, cash + market_value, n_positions))
        self.db.commit()

    def record_signal(self, ticker: str, weight: float, score: float, explanation: str) -> None:
        self.db.execute("INSERT INTO signals(ts, ticker, target_weight, score, explanation) VALUES (?,?,?,?,?)",
                        (now_kst().isoformat(), ticker, weight, score, explanation))
        self.db.commit()

    # ------------------------------------------------------------ 조회
    def df(self, sql: str, params=()) -> pd.DataFrame:
        return pd.read_sql_query(sql, self.db, params=params)

    def positions_from_fills(self) -> dict[str, int]:
        """체결 내역만으로 재구성한 보유수량 — 증권사 잔고와 대사(reconcile)할 때 사용."""
        rows = self.db.execute(
            "SELECT ticker, SUM(CASE WHEN side='buy' THEN qty ELSE -qty END) FROM fills GROUP BY ticker").fetchall()
        return {t: int(q) for t, q in rows if q}

    def realized_pnl(self) -> pd.DataFrame:
        """종목별 실현손익(원화, 평균단가법, 비용 차감)."""
        t = self.realized_trades()
        return t.groupby(["ticker", "currency"], as_index=False)["realized_pnl"].sum()

    def realized_trades(self) -> pd.DataFrame:
        """매도 체결 단위 실현손익(원화) — 세금 계산용.
        price는 현지통화, fee·tax는 원화로 기록되므로 매수가·매도가를 각 체결 시점 환율로 원화 환산한다.
        (세법상 양도·취득가액은 각 결제일 기준환율로 환산 — 운영 시 결제일 환율로 교체 가능)
        """
        f = self.df("SELECT * FROM fills ORDER BY id")
        rows, held, avg = [], {}, {}
        for r in f.itertuples():
            fx = getattr(r, "fx_rate", 1.0) or 1.0
            px_krw = r.price * fx
            if r.side == "buy":
                q0 = held.get(r.ticker, 0)
                avg[r.ticker] = (avg.get(r.ticker, 0) * q0 + px_krw * r.qty + r.fee + r.tax) / (q0 + r.qty)
                held[r.ticker] = q0 + r.qty
            else:
                rows.append({"ticker": r.ticker, "currency": r.currency, "filled_at": r.filled_at,
                             "realized_pnl": (px_krw - avg.get(r.ticker, px_krw)) * r.qty - r.fee - r.tax})
                held[r.ticker] = held.get(r.ticker, 0) - r.qty
        return pd.DataFrame(rows, columns=["ticker", "currency", "filled_at", "realized_pnl"])
