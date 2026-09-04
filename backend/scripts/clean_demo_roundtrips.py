"""清空 test_demo 测试闭环数据（不影响其他真实持仓/成交/autopilot 状态）。

仅删除 source='test_demo' 的 paper orders 及其关联 trades，保持模拟盘真实状态不变。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db


def main() -> None:
    db.init()
    # 先找出 test_demo 的 cl_ord_id，再删 trades（按 cl_ord_id 关联）
    rows = db.query("SELECT cl_ord_id FROM orders WHERE source='test_demo' AND venue='paper'")
    cls = [r["cl_ord_id"] for r in rows if r["cl_ord_id"]]
    if not cls:
        print("无 test_demo 测试记录，已是干净状态。")
        return
    placeholders = ",".join("?" * len(cls))
    n_trades = db.execute(
        f"DELETE FROM trades WHERE cl_ord_id IN ({placeholders})", tuple(cls)
    )
    n_orders = db.execute(
        "DELETE FROM orders WHERE source='test_demo' AND venue='paper'", ()
    )
    print(f"已清空 test_demo 测试数据：orders={n_orders} trades={n_trades}")


if __name__ == "__main__":
    main()
