"""A_only_brk 跨周期 OOS 验证：5m IS(12月) vs 15m 全量(24月) vs 1H 全量。

验证"只保留 breakout_retest"是否跨周期稳定（不依赖 5m 特定行情）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.backtest.rules_backtest import BTParams, run_rules_backtest  # noqa: E402

INST_ID = "BTC-USDT"
DAY_MS = 86_400_000


def _ms(s, eod=False):
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if eod:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def _load(period, a, b):
    rows = db.query(
        "SELECT open_time, o, h, l, c, vol FROM bars WHERE inst_id=? AND period=? "
        "AND open_time>=? AND open_time<=? ORDER BY open_time ASC", (INST_ID, period, a, b))
    return [{"ts": r["open_time"], "o": r["o"], "h": r["h"], "l": r["l"],
             "c": r["c"], "vol": r["vol"] or 0} for r in rows]


def main():
    db.init()
    # A_only_brk 参数：强制形态 + 只 breakout_retest
    params = BTParams(require_setup=True, setup_filter="breakout_retest")

    cases = [
        ("5m", "2025-09-01", "2026-09-01"),   # IS 基线
        ("15m", "2024-09-01", "2026-09-01"),  # 跨周期 OOS（24个月）
        ("1H", "2023-01-01", "2026-09-01"),   # 更长周期 OOS（~3.7年）
    ]
    summary = {}
    for period, start, end in cases:
        sm, em = _ms(start), _ms(end, True)
        warm = 30 * DAY_MS
        bars = _load(period, sm - warm, em)
        bars_4h = _load("4H", sm - warm, em)
        bars_1h = _load("1D", sm - warm, em)   # P2-4: 1H 交易的 mid-HTF 应为 1D（原误装 1H）
        if len(bars) < 200:
            print(f"[{period}] 数据不足 {len(bars)}，跳过")
            continue
        t0 = time.time()
        r = run_rules_backtest(bars, bars_4h, bars_1h, params, period, sm)
        m = r["metrics"]
        summary[period] = {k: m.get(k) for k in
                           ("total_return_pct", "round_trips", "win_rate", "profit_factor",
                            "expectancy", "max_consec_losses", "max_dd_pct", "net_pnl")}
        print(f"[{period} {start}~{end}] {time.time()-t0:.0f}s  bars={len(bars)} "
              f"ret={m['total_return_pct']}% trades={m['round_trips']} "
              f"wr={m['win_rate']}% PF={m['profit_factor']} exp={m['expectancy']} "
              f"net={m['net_pnl']}U consec={m['max_consec_losses']}", flush=True)
        out = os.path.join("data", "backtest")
        with open(os.path.join(out, f"oos_brk_{period}_{start}_{end}.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"metrics": m, "params": r["params"]}, f, ensure_ascii=False, indent=2)

    print("\n===== A_only_brk 跨周期对比 =====")
    print(f"{'period':<8}{'ret%':>8}{'trades':>8}{'wr%':>8}{'PF':>8}"
          f"{'exp':>10}{'net':>10}{'consec':>8}")
    for p, s in summary.items():
        print(f"{p:<8}{s['total_return_pct']:>8}{s['round_trips']:>8}"
              f"{s['win_rate']:>8}{str(s['profit_factor']):>8}"
              f"{s['expectancy']:>10}{s['net_pnl']:>10}{s['max_consec_losses']:>8}")


if __name__ == "__main__":
    main()
