"""植入测试闭环数据以验证前端盈亏底色（浅绿/浅红）与 venue 横幅。

直接写入 orders + trades（venue='paper', source='test_demo'），不经过 paper_engine，
因此不会创建真实持仓、不影响账户余额、不会触发 autopilot（autopilot 只跟踪真实持仓）。
用完可用 scripts/clean_demo_roundtrips.py 或前端「重置模拟盘」一键清空。

闭环1（盈利）: buy 0.001 @ 75000 → sell 0.001 @ 78000  ≈ +2.847 USDT
闭环2（亏损）: buy 0.001 @ 80000 → sell 0.001 @ 77000  ≈ -3.157 USDT
"""
from __future__ import annotations

import os
import sys
import time

# 允许从 scripts/ 直接 import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db

FEE_TAKER = 0.001  # 0.10%
INST = "BTC-USDT"
SZ = 0.001


def _now_ms(offset_s: float = 0.0) -> int:
    return int((time.time() + offset_s) * 1000)


def _place(side: str, px: float, ts: int, tag: str) -> int:
    """插入一条已成交 order + 对应 trade，返回 order id。"""
    cl_ord_id = f"TEST-DEMO-{tag}"
    fee = round(px * SZ * FEE_TAKER, 8)
    oid = db.execute(
        "INSERT INTO orders (cl_ord_id, instance_id, inst_id, td_mode, side, pos_side, ord_type,"
        " px, sz, filled_sz, avg_px, state, fee, source, venue, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cl_ord_id, None, INST, "cash", side, "", "limit",
         px, SZ, SZ, px, "filled", fee, "test_demo", "paper", ts, ts),
    )
    db.execute(
        "INSERT INTO trades (ord_id, cl_ord_id, inst_id, side, px, sz, fee, instance_id, ts)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (f"PP-{oid}", cl_ord_id, INST, side, px, SZ, fee, None, ts),
    )
    return oid


def main() -> None:
    db.init()
    base = _now_ms(-600)  # 10 分钟前开始
    # 闭环1（盈利）：低买高卖
    _place("buy", 75000.0, base, "BUY1")
    _place("sell", 78000.0, base + 120_000, "SELL1")   # +2 分钟后平仓
    # 闭环2（亏损）：高买低卖
    _place("buy", 80000.0, base + 240_000, "BUY2")     # +4 分钟
    _place("sell", 77000.0, base + 360_000, "SELL2")   # +6 分钟

    # 验证
    from app.trading.roundtrips import compute_round_trips
    rt = compute_round_trips(venue="paper", limit=50)
    print(f"已植入 {rt['total']} 条闭环（paper / test_demo）：")
    for c in rt["closed"]:
        print(f"  open={c['open_px']:.0f} close={c['close_px']:.0f} "
              f"pnl={c['pnl']:+.4f} ({c['pnl_pct']*100:+.2f}%) "
              f"fee={c['fee']:.4f} hold={c['hold_s']}s source={c['source']}")


if __name__ == "__main__":
    main()
