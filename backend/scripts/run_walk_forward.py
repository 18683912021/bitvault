"""Walk-Forward 验证 + 参数稳定区选择（Phase 10-12）。

流程：
  窗口 = Train(参数选择) + Test(OOS)，滚动前进。
  Train 内：对参数网格逐组合回测 → 按"邻域平均 expectancy"选参数（参数高原，防单点过拟合）
  Test  内：用 Train 选出的参数跑 OOS → 汇总全部 OOS 段 = WF 报告

用法：
  python scripts/run_walk_forward.py --period 15m --start 2024-09-01 --end 2026-09-01 \
      --train-months 9 --test-months 3 \
      --grid '{"score_min": [60, 65, 70, 75, 80], "trail_atr": [2.0, 2.5, 3.0]}'
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.backtest.rules_backtest import BTParams, run_rules_backtest  # noqa: E402
from app.backtest.holdout import dev_end_ms  # noqa: E402

INST_ID = "BTC-USDT"
DAY_MS = 86_400_000


def _ms(date_str: str, end_of_day: bool = False) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def _load_bars(period: str, start_ms: int, end_ms: int) -> list[dict]:
    rows = db.query(
        "SELECT open_time, o, h, l, c, vol FROM bars WHERE inst_id=? AND period=? "
        "AND open_time>=? AND open_time<=? ORDER BY open_time ASC",
        (INST_ID, period, start_ms, end_ms),
    )
    return [{"ts": r["open_time"], "o": r["o"], "h": r["h"], "l": r["l"],
             "c": r["c"], "vol": r["vol"] or 0} for r in rows]


def _smooth_pick(axis: list, results: dict) -> object:
    """邻域平均选择：取（自身 + 相邻参数值）的 mean expectancy 最高者 → 参数高原而非单点。
    全部无效（交易不足）时回退到 axis 中位数（= 生产默认），避免返回 None 污染下游。"""
    best, best_score = None, -10**9
    for j, v in enumerate(axis):
        neighbors = [results[v]["expectancy"]]
        if j > 0:
            neighbors.append(results[axis[j - 1]]["expectancy"])
        if j < len(axis) - 1:
            neighbors.append(results[axis[j + 1]]["expectancy"])
        score = sum(neighbors) / len(neighbors)
        if score > best_score:
            best, best_score = v, score
    if best is None:
        best = sorted(axis)[len(axis) // 2]      # 中位数 = 生产默认值
    return best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="15m")
    ap.add_argument("--start", default="2024-09-01")
    ap.add_argument("--end", default="", help="开发窗口终点；默认=dev_end_ms(period)（Holdout 守卫）")
    ap.add_argument("--train-months", type=int, default=9)
    ap.add_argument("--test-months", type=int, default=3)
    ap.add_argument("--grid", default='{"score_min": [60, 65, 70, 75, 80]}')
    ap.add_argument("--min-trades", type=int, default=8, help="Train 段最少交易数（不足视为无效）")
    ap.add_argument("--fixed", default="{}", help="固定参数 JSON")
    ap.add_argument("--no-short", action="store_true")
    args = ap.parse_args()

    db.init()
    grid: dict = json.loads(args.grid)
    fixed: dict = json.loads(args.fixed)
    if args.no_short:
        fixed["allow_short"] = False
    axes = [sorted(v) for v in grid.values()]
    keys = list(grid.keys())
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*axes)]
    print(f"参数网格 {len(combos)} 组合 × {keys}")

    # ---- Holdout 守卫：默认 --end = dev_end_ms(period)，WF 不触碰 Holdout ----
    if args.end:
        end_ms = _ms(args.end, True)
    else:
        end_ms = dev_end_ms(args.period)
        print(f"Holdout 守卫：--end 默认 dev_end_ms('{args.period}')="
              f"{datetime.fromtimestamp(end_ms/1000, tz=timezone.utc):%Y-%m-%d}，"
              f"WF 不触碰 Holdout。显式 --end 可覆盖（仅最终验证）。")
    start_ms = _ms(args.start)
    train_ms = args.train_months * 30 * DAY_MS
    test_ms = args.test_months * 30 * DAY_MS
    warmup_ms = 30 * DAY_MS

    bars_all = _load_bars(args.period, start_ms - warmup_ms, end_ms)
    bars_4h = _load_bars("4H", start_ms - warmup_ms, end_ms)
    # 1H 交易时 mid HTF 取 1D（真正的更高周期，避免同频自证），其余取 1H
    _mid_period = "1D" if args.period == "1H" else "1H"
    bars_1h = _load_bars(_mid_period, start_ms - warmup_ms, end_ms)
    print(f"bars: {args.period}={len(bars_all)} 4H={len(bars_4h)} {_mid_period}={len(bars_1h)}")

    def _slice(a: int, b: int) -> list[dict]:
        return [x for x in bars_all if a <= x["ts"] < b]

    windows = []
    w_start = start_ms
    while w_start + train_ms + test_ms <= end_ms:
        windows.append((w_start, w_start + train_ms, w_start + train_ms + test_ms))
        w_start += test_ms
    print(f"WF 窗口 {len(windows)} 个")

    oos_parts = []
    is_rows = []
    for wi, (w0, w1, w2) in enumerate(windows):
        print(f"\n===== 窗口 {wi + 1}/{len(windows)}: "
              f"Train {datetime.fromtimestamp(w0/1000, tz=timezone.utc):%Y-%m-%d}"
              f"~{datetime.fromtimestamp(w1/1000, tz=timezone.utc):%Y-%m-%d} "
              f"Test →{datetime.fromtimestamp(w2/1000, tz=timezone.utc):%Y-%m-%d} =====")
        train_bars = _slice(w0 - warmup_ms, w1 + DAY_MS)
        test_bars = _slice(w1 - warmup_ms, w2 + DAY_MS)

        # ---- Train：网格回测 ----
        train_res = {}
        t0 = time.time()
        for ov in combos:
            params = BTParams(**{**fixed, **ov})
            r = run_rules_backtest(train_bars, bars_4h, bars_1h, params,
                                   args.period, start_ms=w0, end_ms=w1)
            m = r["metrics"]
            train_res[json.dumps(ov, sort_keys=True)] = {
                "expectancy": m.get("expectancy", 0.0),
                "round_trips": m.get("round_trips", 0),
                "profit_factor": m.get("profit_factor"),
                "max_dd_pct": m.get("max_dd_pct", 0),
                "sharpe": m.get("sharpe", 0),
            }
        print(f"  Train 网格 {len(combos)} 组合用时 {time.time() - t0:.0f}s")

        # 逐维邻域平滑选择（当前只支持一维网格扩展多维时取笛卡尔最优维度均值）
        chosen: dict = {}
        for k, axis in zip(keys, axes):
            axis_res = {}
            for v in axis:
                best_combo = None
                for ov in combos:
                    if ov[k] != v:
                        continue
                    rec = train_res[json.dumps(ov, sort_keys=True)]
                    if rec["round_trips"] < args.min_trades:
                        continue
                    if best_combo is None or rec["expectancy"] > best_combo["expectancy"]:
                        best_combo = rec
                axis_res[v] = best_combo or {"expectancy": -10**9}
            chosen[k] = _smooth_pick(axis, axis_res)
        print(f"  Train 选参（邻域平滑）: {chosen}")

        # ---- Test：OOS ----
        params = BTParams(**{**fixed, **chosen})
        r = run_rules_backtest(test_bars, bars_4h, bars_1h, params,
                               args.period, start_ms=w1, end_ms=w2)
        m = r["metrics"]
        print(f"  OOS: ret={m.get('total_return_pct')}% dd={m.get('max_dd_pct')}% "
              f"trades={m.get('round_trips')} wr={m.get('win_rate')}% "
              f"PF={m.get('profit_factor')} exp={m.get('expectancy')} "
              f"consec_loss={m.get('max_consec_losses')}")
        oos_parts.append({"window": wi, "chosen": chosen, "oos": m})
        is_rows.append({"window": wi, "chosen": chosen,
                        "train": {k: train_res[json.dumps(ov, sort_keys=True)]
                                  for ov, k in ((ov, json.dumps(ov, sort_keys=True))
                                                for ov in combos)}})

    # ---- 汇总 OOS 拼接净值 ----
    oos_metrics = [w["oos"] for w in oos_parts if w["oos"]]
    total_pnl = sum(m.get("net_pnl", 0) for m in oos_metrics)
    total_trades = sum(m.get("round_trips", 0) for m in oos_metrics)
    report = {
        "period": args.period, "start": args.start, "end": args.end,
        "train_months": args.train_months, "test_months": args.test_months,
        "grid": grid, "fixed": fixed,
        "windows": oos_parts,
        "wf_summary": {
            "n_windows": len(oos_parts),
            "total_oos_net_pnl": round(total_pnl, 2),
            "total_oos_trades": total_trades,
            "avg_oos_expectancy": round(total_pnl / total_trades, 4) if total_trades else 0,
            "positive_windows": sum(1 for m in oos_metrics
                                    if (m.get("net_pnl") or 0) > 0),
        },
    }
    out_dir = os.path.join("data", "backtest")
    os.makedirs(out_dir, exist_ok=True)
    tag = f"wf_{args.period}_{args.start}_{args.end}"
    with open(os.path.join(out_dir, f"{tag}.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n===== WF OOS 汇总 =====")
    print(json.dumps(report["wf_summary"], ensure_ascii=False, indent=2))
    print(f"报告: data/backtest/{tag}.json")


if __name__ == "__main__":
    main()
