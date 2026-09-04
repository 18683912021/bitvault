"""Holdout 配置自检 + rules_backtest 端到端冒烟（dev 窗口 vs holdout 分段）。"""
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db
from app.backtest import holdout
from app.backtest.rules_backtest import BTParams, run_rules_backtest

INST_ID = "BTC-USDT"


def _load_bars(period, start_ms, end_ms):
    rows = db.query(
        "SELECT open_time, o, h, l, c, vol FROM bars WHERE inst_id=? AND period=? "
        "AND open_time>=? AND open_time<=? ORDER BY open_time ASC",
        (INST_ID, period, start_ms, end_ms),
    )
    return [{"ts": r["open_time"], "o": r["o"], "h": r["h"], "l": r["l"],
             "c": r["c"], "vol": r["vol"] or 0} for r in rows]


def main():
    db.init()
    print("=" * 60)
    print("Holdout 配置")
    print("=" * 60)
    for p in ["5m", "15m", "1H", "4H", "1D"]:
        print(f"  {holdout.describe(p)}")

    # 取 1H 做端到端冒烟（数据最长，能体现开发/Holdout 切分）
    period = "1H"
    h_start, h_end = holdout.holdout_start_ms(period), holdout.holdout_end_ms(period)
    dev_end = holdout.dev_end_ms(period)
    start_ms = h_start - 90 * 86_400_000   # Holdout 前 90 天 warmup + 一小段开发
    bars = _load_bars(period, start_ms, h_end)
    bars_4h = _load_bars("4H", start_ms, h_end)
    bars_1h = _load_bars("1H", start_ms, h_end)
    print(f"\n冒烟：{period} bars={len(bars)} 4H={len(bars_4h)} 1H={len(bars_1h)}")

    # ---- dev 窗口回测（end_ms=dev_end，不触碰 Holdout）----
    print(f"\n[dev 窗口] start={datetime.fromtimestamp(start_ms/1000, tz=timezone.utc):%Y-%m-%d}"
          f" end(dev)={datetime.fromtimestamp(dev_end/1000, tz=timezone.utc):%Y-%m-%d}")
    p = BTParams(require_setup=True, setup_filter="breakout_retest", allow_short=False)
    t0 = time.time()
    r_dev = run_rules_backtest(bars, bars_4h, bars_1h, p, period,
                               start_ms=h_start - 60 * 86_400_000, end_ms=dev_end)
    print(f"  用时 {time.time()-t0:.1f}s  trades={r_dev['metrics'].get('round_trips')}"
          f"  net={r_dev['metrics'].get('net_pnl')}")
    # 守卫：dev equity 最后一个 ts 必须 < h_start
    if r_dev["equity"]:
        last_ts = r_dev["equity"][-1]["ts"]
        assert last_ts < h_start, f"❌ dev equity 末点 {last_ts} 落入 Holdout（{h_start}）"
        print(f"  ✅ dev equity 末点 {datetime.fromtimestamp(last_ts/1000, tz=timezone.utc):%Y-%m-%d}"
              f" < Holdout 起点")

    # ---- Holdout 段回测（start_ms=h_start，end_ms=None）----
    print(f"\n[Holdout 段] start={datetime.fromtimestamp(h_start/1000, tz=timezone.utc):%Y-%m-%d}"
          f" end={datetime.fromtimestamp(h_end/1000, tz=timezone.utc):%Y-%m-%d}")
    t0 = time.time()
    r_hold = run_rules_backtest(bars, bars_4h, bars_1h, p, period,
                                start_ms=h_start, end_ms=None)
    print(f"  用时 {time.time()-t0:.1f}s  trades={r_hold['metrics'].get('round_trips')}"
          f"  net={r_hold['metrics'].get('net_pnl')}")
    if r_hold["equity"]:
        first_ts = r_hold["equity"][0]["ts"]
        last_ts = r_hold["equity"][-1]["ts"]
        assert first_ts >= h_start, f"❌ Holdout equity 首点 {first_ts} < Holdout 起点 {h_start}"
        print(f"  ✅ Holdout equity 首点 {datetime.fromtimestamp(first_ts/1000, tz=timezone.utc):%Y-%m-%d}"
              f" 末点 {datetime.fromtimestamp(last_ts/1000, tz=timezone.utc):%Y-%m-%d}")

    print("\n✅ Holdout 切分自检通过：dev 段不触碰 Holdout，Holdout 段独立运行。")


if __name__ == "__main__":
    main()
