"""V2 规则回测运行器：读本地 bars → run_rules_backtest → 输出 metrics/journal 到 data/backtest/。

Holdout 守卫（任务书第二十九节）：
  默认 --end = dev_end_ms(period)（开发窗口上界 = Holdout 起点），参数优化绝不触碰 Holdout。
  仅最终验证时显式 --include-holdout 才允许跑到数据末尾（用 run_holdout.py 更稳妥）。

用法：
  python scripts/run_v2_backtest.py                          # 默认 5m 开发窗口（不含 Holdout）
  python scripts/run_v2_backtest.py --start 2025-09-01 --tag base
  python scripts/run_v2_backtest.py --params '{"score_min": 65}' --tag s65
  python scripts/run_v2_backtest.py --include-holdout --tag full   # 仅最终验证用
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.backtest.rules_backtest import BTParams, run_rules_backtest  # noqa: E402
from app.backtest.holdout import dev_end_ms, holdout_end_ms  # noqa: E402

INST_ID = "BTC-USDT"


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inst", default=INST_ID)
    ap.add_argument("--period", default="5m")
    ap.add_argument("--start", default="2025-09-01")
    ap.add_argument("--end", default="", help="开发窗口终点；默认=dev_end_ms(period)，留空走默认")
    ap.add_argument("--params", default="{}", help="BTParams 覆盖 JSON")
    ap.add_argument("--tag", default="v2")
    ap.add_argument("--no-short", action="store_true")
    ap.add_argument("--include-holdout", action="store_true",
                    help="允许跑到数据末尾（含 Holdout）—— 仅最终验证用，否则禁止")
    args = ap.parse_args()

    db.init()
    # ---- Holdout 守卫：默认 --end = dev_end_ms(period) ----
    if args.end:
        end_ms = _ms(args.end, True)
    elif args.include_holdout:
        end_ms = holdout_end_ms(args.period)
        print(f"⚠️  --include-holdout：本次回测覆盖到数据末尾（含 Holdout 段），"
              f"仅限最终验证。")
    else:
        end_ms = dev_end_ms(args.period)
        print(f"Holdout 守卫：--end 默认取 dev_end_ms('{args.period}')="
              f"{datetime.fromtimestamp(end_ms/1000, tz=timezone.utc):%Y-%m-%d}，"
              f"参数优化不触碰 Holdout。加 --include-holdout 可解除（仅最终验证）。")

    start_ms = _ms(args.start)
    # 前置 30 天 warmup 缓冲（5m×120 根 + 4H×23 根 + 指标收敛），start_ms 之前不交易不记净值
    bars = _load_bars(args.period, start_ms - 30 * 86400_000, end_ms)
    bars_4h = _load_bars("4H", start_ms - 30 * 86400_000, end_ms)
    # 1H 交易时 mid HTF 取 1D（真正的更高周期，避免同频自证），其余取 1H
    _mid_period = "1D" if args.period == "1H" else "1H"
    bars_1h = _load_bars(_mid_period, start_ms - 30 * 86400_000, end_ms)
    print(f"bars: {args.period}={len(bars)}  4H={len(bars_4h)}  {_mid_period}={len(bars_1h)}")

    params = BTParams(**{**json.loads(args.params),
                         **({"allow_short": False} if args.no_short else {})})
    import time
    t0 = time.time()
    result = run_rules_backtest(bars, bars_4h, bars_1h, params, args.period,
                                start_ms,
                                end_ms=None if args.include_holdout else dev_end_ms(args.period))
    print(f"回测用时 {time.time() - t0:.1f}s")

    out_dir = os.path.join("data", "backtest")
    os.makedirs(out_dir, exist_ok=True)
    end_tag = datetime.fromtimestamp(end_ms/1000, tz=timezone.utc).strftime("%Y-%m-%d")
    tag = f"{args.tag}_{args.period}_{args.start}_{end_tag}"
    with open(os.path.join(out_dir, f"metrics_{tag}.json"), "w", encoding="utf-8") as f:
        json.dump({"metrics": result["metrics"], "params": result["params"]}, f,
                  ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, f"journal_{tag}.json"), "w", encoding="utf-8") as f:
        json.dump(result["journal"], f, ensure_ascii=False, indent=1)
    eq_sample = result["equity"][::max(1, len(result["equity"]) // 3000)]
    with open(os.path.join(out_dir, f"equity_{tag}.json"), "w", encoding="utf-8") as f:
        json.dump(eq_sample, f, ensure_ascii=False)

    m = result["metrics"]
    print(json.dumps({k: v for k, v in m.items()
                      if k not in ("by_exit", "by_regime", "by_setup")},
                     ensure_ascii=False, indent=2))
    for key in ("by_exit", "by_regime", "by_setup"):
        if m.get(key):
            print(f"\n[{key}]")
            for k, v in m[key].items():
                print(f"  {k:<22} n={v['n']:<5} win={v['win_rate']}%  pnl={v['pnl']}")


if __name__ == "__main__":
    main()
