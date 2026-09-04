"""V1 vs V2 对比表（Phase 13）：读 data/backtest 的 metrics JSON，输出用户验收口径对比。

用法：
  python scripts/compare_v1_v2.py --v1 v1_5m_2025-09-01_2026-09-01 --v2 v2base_5m_2025-09-01_2026-09-01
"""
from __future__ import annotations

import argparse
import json
import os

ROWS = [
    ("total_return_pct", "净收益%"),
    ("win_rate", "胜率%"),
    ("profit_factor", "Profit Factor"),
    ("expectancy", "Expectancy(USDT/笔)"),
    ("avg_win", "平均盈利(USDT)"),
    ("avg_loss", "平均亏损(USDT)"),
    ("payoff_ratio", "盈亏比"),
    ("sharpe", "Sharpe"),
    ("sortino", "Sortino"),
    ("calmar", "Calmar"),
    ("max_dd_pct", "最大回撤%"),
    ("max_consec_losses", "最大连续亏损"),
    ("round_trips", "交易次数"),
    ("trade_count", "交易次数(V1口径)"),
    ("fee_total", "手续费(USDT)"),
    ("slippage_total", "滑点(USDT)"),
    ("mfe_r_median", "MFE中位(R)"),
    ("avg_hold_bars", "平均持仓(bar)"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--v1", required=True, help="metrics_<v1 tag> 的 tag")
    ap.add_argument("--v2", required=True, help="metrics_<v2 tag> 的 tag")
    args = ap.parse_args()

    def _load(tag: str) -> dict:
        path = os.path.join("data", "backtest", f"metrics_{tag}.json")
        with open(path, encoding="utf-8") as f:
            return json.load(f)["metrics"]

    v1, v2 = _load(args.v1), _load(args.v2)
    m1 = {"trade_count": v1.get("trade_count") or v1.get("round_trips"), **v1}

    print(f"V1 = {args.v1}\nV2 = {args.v2}\n")
    print(f"{'指标':<24}{'V1':>14}{'V2':>14}{'变化':>14}")
    print("-" * 66)
    for key, label in ROWS:
        a, b = m1.get(key), v2.get(key)
        if a is None and b is None:
            continue
        delta = ""
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a is not None:
            delta = f"{b - a:+.2f}"
        print(f"{label:<24}{str(a):>14}{str(b):>14}{delta:>14}")

    mc1 = v1.get("monte_carlo") or {}
    mc2 = v2.get("monte_carlo") or {}
    if mc1 or mc2:
        print("\nMonte Carlo（2000 次重排）:")
        print(f"{'指标':<24}{'V1':>14}{'V2':>14}")
        for k, label in (("median_max_dd_pct", "回撤中位%"), ("p95_max_dd_pct", "回撤P95%"),
                         ("p95_max_consec_losses", "连亏P95")):
            print(f"{label:<24}{str(mc1.get(k, '-')):>14}{str(mc2.get(k, '-')):>14}")


if __name__ == "__main__":
    main()
