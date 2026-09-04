"""V1 基线回测：系统初版策略（ma_cross 双均线）走现有 SimBroker 引擎。

与 V2 同区间、同费率（taker 0.10% + 滑点 0.02%）、同初始资金，保证对比公平。
V1 特征（即当前系统初版行为）：固定仓位 100U、固定 2% 止损、金叉开多死叉平多、
信号收盘产生 + 下一根开盘成交（无未来函数）。

用法：
  python scripts/run_v1_baseline.py --period 5m --start 2025-09-01 --end 2026-09-01
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.backtest.engine import run_backtest  # noqa: E402

INST_ID = "BTC-USDT"
DAY_MS = 86_400_000


def _ms(date_str: str, end_of_day: bool = False) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="5m")
    ap.add_argument("--start", default="2025-09-01")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--fast", type=int, default=5)
    ap.add_argument("--slow", type=int, default=20)
    ap.add_argument("--position-usdt", type=float, default=100)
    ap.add_argument("--sl-pct", type=float, default=2.0)
    args = ap.parse_args()

    db.init()
    start_ms, end_ms = _ms(args.start), _ms(args.end, True)
    # 现有引擎无独立 warmup：从 start 前提供建仓缓冲由策略内部滑动窗口处理，
    # 但为公平对齐 V2 的 warmup 口径，这里直接从 start 开始（ma_cross 用 SMA，几十根即热）。
    job = {
        "inst_id": INST_ID, "period": args.period,
        "start_ms": start_ms, "end_ms": end_ms,
        "strategy_type": "ma_cross",
        "params": {"fast": args.fast, "slow": args.slow, "direction": "long_only",
                   "position_usdt": args.position_usdt, "sl_pct": args.sl_pct},
        "fee_rate": 0.001, "slippage": 0.0002, "initial_equity": 10000,
    }
    result = run_backtest(job)
    m = result["metrics"]

    out_dir = os.path.join("data", "backtest")
    os.makedirs(out_dir, exist_ok=True)
    tag = f"v1_{args.period}_{args.start}_{args.end}"
    with open(os.path.join(out_dir, f"metrics_{tag}.json"), "w", encoding="utf-8") as f:
        json.dump({"metrics": m, "params": job["params"], "caveats": result.get("caveats")},
                  f, ensure_ascii=False, indent=2)
    eq_sample = result["equity"][::max(1, len(result["equity"]) // 3000)]
    with open(os.path.join(out_dir, f"equity_{tag}.json"), "w", encoding="utf-8") as f:
        json.dump(eq_sample, f, ensure_ascii=False)

    print(json.dumps(m, ensure_ascii=False, indent=2))
    if result.get("caveats"):
        print("\n[caveats]")
        for c in result["caveats"]:
            print(f"  - {c}")


if __name__ == "__main__":
    main()
