"""Holdout 最终验证（任务书第二十九节：建立真正的 Holdout）。

红线（方法论约束）：
- 此脚本仅在**全部开发完成**后运行**一次**，作为最终 OOS 验证。
- 一旦在此脚本上看过结果，就**不得**再回头调参（否则 Holdout 等同于 IS，丧失 OOS 意义）。
- 跑此脚本前，参数必须已通过 Walk-Forward + 邻域平滑 + 多周期 OOS 确认为"稳定区参数"，
  而非"历史最优单点"。

数据范围：仅 Holdout 段 [holdout_start_ms, holdout_end_ms]，不与前文重叠。
前置 30 天 warmup 取自 Holdout 起点之前（属开发窗口尾部，仅作指标 warmup，不计收益）。

用法：
  # 默认用 V2 生产参数（autopilot config 默认值）验证 1H Holdout
  python scripts/run_holdout.py --period 1H

  # 显式指定最终参数（推荐：从 WF 选出的稳定区参数）
  python scripts/run_holdout.py --period 1H --params '{"score_min":70,"trail_atr":2.5}'

  # 必须显式确认"这是最终验证"
  python scripts/run_holdout.py --period 1H --confirm-final
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.backtest.rules_backtest import BTParams, run_rules_backtest  # noqa: E402
from app.backtest.holdout import (holdout_start_ms, holdout_end_ms,  # noqa: E402
                                  assert_not_in_holdout, describe)

INST_ID = "BTC-USDT"


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
    ap.add_argument("--period", default="1H")
    ap.add_argument("--params", default="{}", help="最终参数 JSON（来自 WF 稳定区选择）")
    ap.add_argument("--no-short", action="store_true")
    ap.add_argument("--confirm-final", action="store_true",
                    help="确认这是最终验证（看过结果后不得再调参）")
    args = ap.parse_args()

    if not args.confirm_final:
        print("=" * 70)
        print("⚠️  Holdout 最终验证 —— 方法论红线")
        print("=" * 70)
        print("此脚本仅用于全部开发完成后的【一次性】最终 OOS 验证。")
        print("看过 Holdout 结果后，不得再回头调参（否则 Holdout 沦为 IS，OOS 失效）。")
        print("请先完成：Walk-Forward + 邻域平滑选参 + 多周期 OOS，确认参数处于稳定区。")
        print("确认后加 --confirm-final 重跑。")
        return

    db.init()
    h_start, h_end = holdout_start_ms(args.period), holdout_end_ms(args.period)
    print(describe(args.period))
    print(f"⏰ Holdout 段: "
          f"{datetime.fromtimestamp(h_start/1000, tz=timezone.utc):%Y-%m-%d}"
          f" → {datetime.fromtimestamp(h_end/1000, tz=timezone.utc):%Y-%m-%d}")

    # warmup：Holdout 起点前 30 天（属开发窗口尾部，仅作指标收敛，不交易不记净值）
    warmup_ms = 30 * 86_400_000
    bars = _load_bars(args.period, h_start - warmup_ms, h_end)
    bars_4h = _load_bars("4H", h_start - warmup_ms, h_end)
    # 1H 交易时 mid HTF 取 1D（真正的更高周期，避免同频自证），其余取 1H
    _mid_period = "1D" if args.period == "1H" else "1H"
    bars_1h = _load_bars(_mid_period, h_start - warmup_ms, h_end)
    print(f"bars（含 warmup）: {args.period}={len(bars)}  4H={len(bars_4h)}  {_mid_period}={len(bars_1h)}")

    # 守卫：开发窗口内的 bar 不得进入 Holdout（此脚本只验证 Holdout，反向也成立）
    # —— 这里反向校验：bars 中 ts >= h_start 的才是 Holdout bar，warmup 区 ts < h_start
    if bars and bars[-1]["ts"] < h_start:
        print("❌ Holdout 段无数据，请确认数据已下载到该周期。")
        return

    params = BTParams(**{**json.loads(args.params),
                         **({"allow_short": False} if args.no_short else {})})
    print(f"最终参数: {json.dumps(params.__dict__, ensure_ascii=False)}")

    t0 = time.time()
    # 注意：start_ms=h_start（Holdout 起点）；不传 end_ms（跑到 Holdout 末尾）
    result = run_rules_backtest(bars, bars_4h, bars_1h, params, args.period,
                                start_ms=h_start, end_ms=None)
    print(f"回测用时 {time.time() - t0:.1f}s")

    out_dir = os.path.join("data", "backtest")
    os.makedirs(out_dir, exist_ok=True)
    tag = (f"HOLDOUT_FINAL_{args.period}_"
           f"{datetime.fromtimestamp(h_start/1000, tz=timezone.utc):%Y%m%d}_"
           f"{datetime.fromtimestamp(h_end/1000, tz=timezone.utc):%Y%m%d}")
    with open(os.path.join(out_dir, f"metrics_{tag}.json"), "w", encoding="utf-8") as f:
        json.dump({"metrics": result["metrics"], "params": result["params"],
                   "holdout": {"start_ms": h_start, "end_ms": h_end,
                               "period": args.period}}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, f"journal_{tag}.json"), "w", encoding="utf-8") as f:
        json.dump(result["journal"], f, ensure_ascii=False, indent=1)
    eq_sample = result["equity"][::max(1, len(result["equity"]) // 3000)]
    with open(os.path.join(out_dir, f"equity_{tag}.json"), "w", encoding="utf-8") as f:
        json.dump(eq_sample, f, ensure_ascii=False)

    m = result["metrics"]
    print("\n" + "=" * 70)
    print("🎯 HOLDOUT 最终验证结果")
    print("=" * 70)
    print(json.dumps({k: v for k, v in m.items()
                      if k not in ("by_exit", "by_regime", "by_setup")},
                     ensure_ascii=False, indent=2))
    print(f"\n报告: data/backtest/metrics_{tag}.json")
    # 验收标准对照（任务书第三十九节）
    print("\n[验收对照]")
    checks = [
        ("① OOS 正期望", m.get("expectancy", 0) > 0),
        ("② 成本后正收益", (m.get("net_pnl") or 0) > 0),
        ("⑥ 连亏可控", (m.get("max_consec_losses") or 0) <= 5),
        ("⑩ 足够样本", (m.get("round_trips") or 0) >= 10),
    ]
    for name, ok in checks:
        print(f"  {'✅' if ok else '❌'} {name}")
    print("\n⚠️  看过此结果后不得再调参。后续改进必须基于新的 Holdout 段（向前滚动）。")


if __name__ == "__main__":
    main()
