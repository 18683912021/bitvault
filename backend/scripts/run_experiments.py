"""批量对比实验（5m 12 个月）：V2 基线 vs 实验 A/B/C，输出对比表。

实验假设（来自 v2base 亏损归因）：
  A. require_setup=True        无形态禁止开仓（setup=none 30 笔 win10% -30.9U）
  B. signal_exit_only_loss=True Signal Exit 仅浮亏触发（27 笔 win3.7% -38.5U）
  C. A + B 组合

用法：python scripts/run_experiments.py
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.backtest.rules_backtest import BTParams, run_rules_backtest  # noqa: E402

INST_ID = "BTC-USDT"
PERIOD = "5m"
START, END = "2025-09-01", "2026-09-01"
DAY_MS = 86_400_000
WARMUP_MS = 30 * DAY_MS


def _ms(date_str: str, end_of_day: bool = False) -> int:
    from datetime import datetime, timezone
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def _load(period: str, a: int, b: int) -> list[dict]:
    rows = db.query(
        "SELECT open_time, o, h, l, c, vol FROM bars WHERE inst_id=? AND period=? "
        "AND open_time>=? AND open_time<=? ORDER BY open_time ASC",
        (INST_ID, period, a, b))
    return [{"ts": r["open_time"], "o": r["o"], "h": r["h"], "l": r["l"],
             "c": r["c"], "vol": r["vol"] or 0} for r in rows]


def main() -> None:
    db.init()
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="5m")
    ap.add_argument("--start", default="2025-09-01")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--set", default="round2", help="实验组: round1 | round2 | x15m")
    args = ap.parse_args()
    global PERIOD, START, END
    PERIOD, START, END = args.period, args.start, args.end

    db.init()
    start_ms, end_ms = _ms(START), _ms(END, True)
    bars = _load(PERIOD, start_ms - WARMUP_MS, end_ms)
    bars_4h = _load("4H", start_ms - WARMUP_MS, end_ms)
    bars_1h = _load("1H", start_ms - WARMUP_MS, end_ms)
    print(f"bars: {PERIOD}={len(bars)} 4H={len(bars_4h)} 1H={len(bars_1h)}")

    sets = {
        "round1": {
            "base": {},
            "A_require_setup": {"require_setup": True},
            "B_sigexit_lossonly": {"signal_exit_only_loss": True},
            "C_A_plus_B": {"require_setup": True, "signal_exit_only_loss": True},
        },
        "round2": {   # 在 A 基础上：BE 提前 + trailing 变体
            "A_require_setup": {"require_setup": True},
            "D_A_be05": {"require_setup": True, "tp1_r": 0.5},
            "E_A_be075": {"require_setup": True, "tp1_r": 0.75},
            "F_A_trail20": {"require_setup": True, "trail_atr": 2.0},
            "G_A_be05_trail20": {"require_setup": True, "tp1_r": 0.5, "trail_atr": 2.0},
        },
        "x15m": {     # 跨周期验证 A
            "base": {},
            "A_require_setup": {"require_setup": True},
            "D_A_be05": {"require_setup": True, "tp1_r": 0.5},
        },
        "round3": {   # Signal Exit 确认增强（根因修复：占 OOS 亏损 76%）
            "A_se1": {"require_setup": True, "signal_exit_bars": 1},
            "A_se2": {"require_setup": True, "signal_exit_bars": 2},
            "A_se3": {"require_setup": True, "signal_exit_bars": 3},
        },
        "round4": {   # 止损放宽（5m 止损占亏损 102%，怀疑 1.8×ATR 太紧被噪音扫）
            "A_sl25": {"require_setup": True, "sl_atr_trend": 2.5},
            "A_sl30": {"require_setup": True, "sl_atr_trend": 3.0},
        },
        "round5": {   # 只保留 breakout_retest（pullback 追高全亏，breakout_retest 胜率高）
            "A_only_brk": {"require_setup": True, "setup_filter": "breakout_retest"},
        },
    }
    set_names = args.set.replace("+", ",").split(",")
    experiments = {}
    for sn in set_names:
        experiments.update(sets[sn])
    summary = {}
    for tag, ov in experiments.items():
        t0 = time.time()
        r = run_rules_backtest(bars, bars_4h, bars_1h, BTParams(**ov), PERIOD, start_ms)
        m = r["metrics"]
        summary[tag] = {k: m.get(k) for k in
                        ("total_return_pct", "round_trips", "win_rate", "profit_factor",
                         "expectancy", "payoff_ratio", "max_dd_pct", "max_consec_losses",
                         "fee_total", "net_pnl", "mfe_r_median")}
        print(f"[{tag}] {time.time()-t0:.0f}s  ret={m['total_return_pct']}% "
              f"trades={m['round_trips']} wr={m['win_rate']}% PF={m['profit_factor']} "
              f"exp={m['expectancy']} consec={m['max_consec_losses']}", flush=True)
        out = os.path.join("data", "backtest")
        with open(os.path.join(out, f"metrics_{tag}_5m_{START}_{END}.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"metrics": m, "params": r["params"]}, f, ensure_ascii=False, indent=2)

    print("\n===== 对比表（5m, 2025-09 ~ 2026-09）=====")
    keys = list(next(iter(summary.values())).keys())
    print(f"{'experiment':<20}" + "".join(f"{k[:14]:>16}" for k in keys))
    for tag, row in summary.items():
        print(f"{tag:<20}" + "".join(f"{str(row[k]):>16}" for k in keys))


if __name__ == "__main__":
    main()
