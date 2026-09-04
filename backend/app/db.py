"""SQLite 持久层（WAL 模式）。所有表结构见 PRD 第 7 节。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from app import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    env TEXT NOT NULL CHECK(env IN ('demo','live')),
    key_enc TEXT NOT NULL,
    secret_enc TEXT NOT NULL,
    passphrase_enc TEXT NOT NULL,
    key_masked TEXT NOT NULL,
    permission TEXT NOT NULL DEFAULT 'trade',
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bars (
    inst_id TEXT NOT NULL, period TEXT NOT NULL, open_time INTEGER NOT NULL,
    o REAL, h REAL, l REAL, c REAL, vol REAL, vol_ccy REAL, source TEXT,
    PRIMARY KEY (inst_id, period, open_time)
);
CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    inst_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('demo','live','paper')),
    status TEXT NOT NULL DEFAULT 'stopped',
    params_json TEXT NOT NULL DEFAULT '{}',
    state_json TEXT NOT NULL DEFAULT '{}',
    started_at INTEGER, stopped_at INTEGER,
    pnl REAL DEFAULT 0,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER, ts INTEGER,
    action TEXT, reason TEXT, payload_json TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cl_ord_id TEXT UNIQUE, ord_id TEXT,
    instance_id INTEGER,
    inst_id TEXT NOT NULL, td_mode TEXT, side TEXT, pos_side TEXT,
    ord_type TEXT, px REAL, sz REAL, filled_sz REAL DEFAULT 0,
    avg_px REAL, state TEXT DEFAULT 'pending_submit',
    fee REAL DEFAULT 0, source TEXT DEFAULT 'manual',
    sl_trigger_px REAL, error_code TEXT, error_msg TEXT,
    leverage INTEGER DEFAULT 1,
    created_at INTEGER, updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ord_id TEXT, cl_ord_id TEXT, inst_id TEXT, side TEXT,
    px REAL, sz REAL, fee REAL, instance_id INTEGER,
    ts INTEGER
);
CREATE TABLE IF NOT EXISTS account_snapshots (
    ts INTEGER PRIMARY KEY, equity REAL, available REAL,
    margin_used REAL, unrealized_pnl REAL, env TEXT
);
CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_type TEXT NOT NULL, params_json TEXT NOT NULL,
    inst_id TEXT NOT NULL, period TEXT NOT NULL,
    start_ts INTEGER, end_ts INTEGER,
    fee_rate REAL, slippage REAL,
    status TEXT DEFAULT 'pending', progress REAL DEFAULT 0,
    metrics_json TEXT, equity_json TEXT, trades_json TEXT, error TEXT,
    created_at INTEGER, finished_at INTEGER
);
CREATE TABLE IF NOT EXISTS risk_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT UNIQUE NOT NULL, params_json TEXT NOT NULL, enabled INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, rule_type TEXT, level TEXT,
    action TEXT, detail_json TEXT
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, actor TEXT, action TEXT, payload_json TEXT, result TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, level TEXT, title TEXT, body TEXT, read INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT
);
"""


def _row_to_dict(cursor: sqlite3.Cursor, row: sqlite3.Row) -> dict:
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


def init() -> None:
    global _conn
    with _lock:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        _migrate()
        _conn.commit()
        _seed_defaults()


def _migrate() -> None:
    """存量库升级：orders.venue/leverage 列 + strategy_instances.mode 支持 paper。"""
    cols = [r[1] for r in _conn.execute("PRAGMA table_info(orders)").fetchall()]
    if "venue" not in cols:
        _conn.execute("ALTER TABLE orders ADD COLUMN venue TEXT DEFAULT 'okx'")
    if "leverage" not in cols:
        _conn.execute("ALTER TABLE orders ADD COLUMN leverage INTEGER DEFAULT 1")

    row = _conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='strategy_instances'"
    ).fetchone()
    if row and "'paper'" not in (row["sql"] or ""):
        # 重建表以放开 mode CHECK 约束（本地单人库，数据量小）
        _conn.execute("ALTER TABLE strategy_instances RENAME TO strategy_instances_old")
        _conn.execute("""
            CREATE TABLE strategy_instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                inst_id TEXT NOT NULL,
                mode TEXT NOT NULL CHECK(mode IN ('demo','live','paper')),
                status TEXT NOT NULL DEFAULT 'stopped',
                params_json TEXT NOT NULL DEFAULT '{}',
                state_json TEXT NOT NULL DEFAULT '{}',
                started_at INTEGER, stopped_at INTEGER,
                pnl REAL DEFAULT 0,
                last_error TEXT
            )
        """)
        _conn.execute("""
            INSERT INTO strategy_instances
            SELECT id, strategy_id, name, inst_id, mode, status, params_json, state_json,
                   started_at, stopped_at, pnl, last_error FROM strategy_instances_old
        """)
        _conn.execute("DROP TABLE strategy_instances_old")


def _seed_defaults() -> None:
    now = int(time.time() * 1000)
    defaults = {
        "max_order_notional": {"max_usdt": 1000},
        "max_position_pct": {"pct": 20},
        "max_daily_loss_pct": {"pct": 3},
        "max_drawdown_pct": {"pct": 10},
        "order_rate_limit": {"per_sec": 5},
        "leverage_cap": {"lever": config.LEVERAGE_CAP},
        "auto_reduce_liq": {"enabled": False, "margin_ratio_warn": 0.10, "margin_ratio_danger": 0.05},
    }
    for t, params in defaults.items():
        _conn.execute(
            "INSERT OR IGNORE INTO risk_rules (type, params_json, enabled) VALUES (?,?,1)",
            (t, json.dumps(params)),
        )
    _conn.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('risk_state', ?)",
        (json.dumps({"day_key": "", "day_start_equity": None, "peak_equity": None, "halted": False, "halt_reason": ""}),),
    )
    # autopilot 默认配置（llm_config 由 main.py 启动时加密写入，避免 db→security 循环依赖）
    _conn.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('autopilot_config', ?)",
        (json.dumps({"enabled": False, "period": "1m", "min_confidence": 0.6,
                     "max_position_pct": 10, "max_order_usdt": 200}),),
    )
    _conn.commit()


def query(sql: str, params: tuple = ()) -> list[dict]:
    with _lock:
        cur = _conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def query_one(sql: str, params: tuple = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> int:
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur.lastrowid or cur.rowcount


def executemany(sql: str, seq: list[tuple]) -> None:
    with _lock:
        _conn.executemany(sql, seq)
        _conn.commit()


def upsert_bars(rows: list[tuple]) -> int:
    with _lock:
        _conn.executemany(
            "INSERT OR REPLACE INTO bars (inst_id, period, open_time, o,h,l,c,vol,vol_ccy, source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        _conn.commit()
        return len(rows)


def get_setting(key: str, default: Any = None) -> Any:
    row = query_one("SELECT value FROM settings WHERE key=?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return row["value"]


def set_setting(key: str, value: Any) -> None:
    execute(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def add_audit(actor: str, action: str, payload: dict, result: str = "ok") -> None:
    execute(
        "INSERT INTO audit_logs (ts, actor, action, payload_json, result) VALUES (?,?,?,?,?)",
        (int(time.time() * 1000), actor, action, json.dumps(payload, ensure_ascii=False), result),
    )


def add_risk_event(rule_type: str, level: str, action: str, detail: dict) -> None:
    ts = int(time.time() * 1000)
    execute(
        "INSERT INTO risk_events (ts, rule_type, level, action, detail_json) VALUES (?,?,?,?,?)",
        (ts, rule_type, level, action, json.dumps(detail, ensure_ascii=False)),
    )
    execute(
        "INSERT INTO notifications (ts, level, title, body) VALUES (?,?,?,?)",
        (ts, level, f"风控事件：{rule_type}", json.dumps(detail, ensure_ascii=False)),
    )
