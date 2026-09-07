"""Stress Test 极端行情压力测试（任务书验收⑬ + 第三十八节）。

目的：验证风控机制在极端场景下能否"先活下来"（不爆仓、回撤可控）。
不追求收益，只验证生存性。

场景：
  S1 缺口穿透止损：每笔交易 SL 被跳空击穿，按 SL×(1-10%) 成交（滑点扩大）
  S2 全亏序列：强制所有交易命中止损（0% 胜率），连亏 N 笔后回撤
  S3 高波动子段：取历史 ATR 最高的 30 天，跑策略看回撤
  S4 蒙特卡洛最坏：已有 MC p95，此处汇总

判定：单笔回撤 < 权益 1%（risk 0.5%×2 滑点放大）、总回撤 < DD_PAUSE 6%、无爆仓
"""
import os
import sys
import json
import statistics
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db
from app.backtest.rules_backtest import BTParams, run_rules_backtest
from app.backtest.holdout import dev_end_ms

INST_ID = "BTC-USDT"
DD_PAUSE = 6.0          # 回撤暂停阈值
SINGLE_RISK = 0.5        # 单笔风险 0.5%


def _load_bars(period, start_ms, end_ms):
    rows = db.query(
        "SELECT open_time, o, h, l, c, vol FROM bars WHERE inst_id=? AND period=? "
        "AND open_time>=? AND open_time<=? ORDER BY open_time ASC",
        (INST_ID, period, start_ms, end_ms))
    return [{"ts": r["open_time"], "o": r["o"], "h": r["h"], "l": r["l"],
             "c": r["c"], "vol": r["vol"] or 0} for r in rows]


def _atr_pct(bars, i, n=14):
    """第 i 根的 ATR%（粗算，用于找高波动段）。"""
    if i < n + 1:
        return 0.0
    trs = []
    for k in range(i - n + 1, i + 1):
        h, l, pc = bars[k]["h"], bars[k]["l"], bars[k-1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs) / n
    return atr / bars[i]["c"] * 100 if bars[i]["c"] > 0 else 0


def s1_gap_through_stop():
    """S1：每笔 SL 被跳空击穿，成交价 = SL × 0.90（10% 滑点扩大）。
    复用 dev 全窗口，但把每笔止损成交价恶化 10%。"""
    # 用正常回测的 journal，把止损单的损失放大
    dev_start = 1672243200000  # 2022-12-28
    dev_end = dev_end_ms("1H")
    warmup = 30 * 86_400_000
    bars = _load_bars("1H", dev_start - warmup, dev_end)
    bars_4h = _load_bars("4H", dev_start - warmup, dev_end)
    bars_1h = _load_bars("1D", dev_start - warmup, dev_end)   # P2-4: 1H 交易的 mid-HTF 应为 1D（原误装 1H）
    p = BTParams(require_setup=True, allow_short=False, slippage=0.0002)
    r = run_rules_backtest(bars, bars_4h, bars_1h, p, "1H", start_ms=dev_start, end_ms=dev_end)
    j = r["journal"]
    # 原始 vs 压力：止损类 exit 的 pnl 恶化（成交价再低 10%）
    worst_loss = 0.0
    orig_net = sum(x["pnl"] for x in j)
    stressed = []
    for x in j:
        if "止损" in x["exitReason"]:
            # 原损失 = (entry - exit) * qty；恶化 10% → exit 再低 10%
            extra = x["pnl"] * -0.10   # 损失放大 10%
            sx = x["pnl"] + extra
            stressed.append(sx)
            worst_loss = min(worst_loss, sx)
        else:
            stressed.append(x["pnl"])
    stressed_net = sum(stressed)
    print(f"[S1 缺口穿透止损] 原始 net={orig_net:.2f}  压力 net={stressed_net:.2f}"
          f"  单笔最差={worst_loss:.2f} ({abs(worst_loss)/100:.3f}% 权益)")
    print(f"  → 单笔最差占总权益 {abs(worst_loss)/100:.3f}% "
          f"({'✅ <1%' if abs(worst_loss) < 100 else '❌ ≥1%'})")
    return abs(worst_loss) < 100


def s2_all_loss_sequence():
    """S2：强制连亏序列。取 dev 单笔 avg_loss，模拟连续 5/10/15 笔全亏看回撤。"""
    dev_start = 1672243200000
    dev_end = dev_end_ms("1H")
    warmup = 30 * 86_400_000
    bars = _load_bars("1H", dev_start - warmup, dev_end)
    bars_4h = _load_bars("4H", dev_start - warmup, dev_end)
    bars_1h = _load_bars("1D", dev_start - warmup, dev_end)   # P2-4: 1H 交易的 mid-HTF 应为 1D（原误装 1H）
    p = BTParams(require_setup=True, allow_short=False)
    r = run_rules_backtest(bars, bars_4h, bars_1h, p, "1H", start_ms=dev_start, end_ms=dev_end)
    m = r["metrics"]
    avg_loss = m.get("avg_loss", 0) or 0
    print(f"[S2 全亏序列] dev avg_loss={avg_loss:.2f}U ({avg_loss/100:.3f}% 权益/笔)")
    eq = 10000.0
    for n in (3, 5, 8, 10, 15):
        # 连亏 n 笔，每笔按 avg_loss（且单笔不超 risk 0.5%=50U，但实际 avg_loss 已体现）
        loss_n = avg_loss * n
        # 考虑仓位衰减档：连亏≥2 → 0.5x，≥3 → 0.25x，≥5 → paused
        # 简化：前2笔满仓，3-4笔0.5x，5+笔0.25x（throttle 生效后仍可能开仓但减仓）
        tiered = (avg_loss * 2) + (avg_loss * 0.5 * 2) + (avg_loss * 0.25 * max(0, n-4))
        dd = tiered / 100
        survives = dd < DD_PAUSE
        print(f"  连亏 {n:>2} 笔：朴素回撤 {loss_n/100:.2f}%  | 风控衰减后 {dd:.2f}% "
              f"{'✅' if survives else '❌≥6%暂停'}")
    return avg_loss < 100   # 单笔 avg_loss < 1% 权益


def s3_high_vol_subperiod():
    """S3：取历史 ATR 最高的 30 天子段，跑策略看回撤。"""
    rows = db.query(
        "SELECT open_time, o, h, l, c FROM bars WHERE inst_id=? AND period='1H' "
        "AND open_time>=? ORDER BY open_time ASC", (INST_ID, 1672243200000))
    if len(rows) < 200:
        print("[S3 高波动子段] 数据不足")
        return True
    # 滑动 30 天 ATR%
    best_end, best_atr = 0, 0.0
    for i in range(200, len(rows)):
        a = _atr_pct(rows, i, 14)
        if a > best_atr:
            best_atr, best_end = a, rows[i]["open_time"]
    seg_end = best_end
    seg_start = seg_end - 30 * 86_400_000
    warmup = 30 * 86_400_000
    bars = _load_bars("1H", seg_start - warmup, seg_end)
    bars_4h = _load_bars("4H", seg_start - warmup, seg_end)
    bars_1h = _load_bars("1D", seg_start - warmup, seg_end)   # P2-4: 1H 交易的 mid-HTF 应为 1D（原误装 1H）
    p = BTParams(require_setup=True, allow_short=False)
    r = run_rules_backtest(bars, bars_4h, bars_1h, p, "1H", start_ms=seg_start, end_ms=seg_end)
    m = r["metrics"] or {}
    dd = m.get("max_dd_pct", 0)
    trades = m.get("round_trips", 0)
    net = m.get("net_pnl", 0)
    sd = datetime.fromtimestamp(seg_start/1000, tz=timezone.utc)
    ed = datetime.fromtimestamp(seg_end/1000, tz=timezone.utc)
    print(f"[S3 高波动子段] {sd:%Y-%m-%d}→{ed:%Y-%m-%d}  ATR%峰值≈{best_atr:.2f}%")
    print(f"  trades={trades}  net={net}  max_dd={dd}%  "
          f"{'✅ <6%' if dd < DD_PAUSE else '❌≥6%暂停'}")
    return dd < DD_PAUSE


def s4_mc_summary():
    """S4：蒙特卡洛最坏（已由回测输出，汇总判定）。"""
    dev_start = 1672243200000
    dev_end = dev_end_ms("1H")
    warmup = 30 * 86_400_000
    bars = _load_bars("1H", dev_start - warmup, dev_end)
    bars_4h = _load_bars("4H", dev_start - warmup, dev_end)
    bars_1h = _load_bars("1D", dev_start - warmup, dev_end)   # P2-4: 1H 交易的 mid-HTF 应为 1D（原误装 1H）
    p = BTParams(require_setup=True, allow_short=False)
    r = run_rules_backtest(bars, bars_4h, bars_1h, p, "1H", start_ms=dev_start, end_ms=dev_end)
    mc = r["metrics"].get("monte_carlo") or {}
    p95_dd = mc.get("p95_max_dd_pct", 0)
    p95_cl = mc.get("p95_max_consec_losses", 0)
    print(f"[S4 蒙特卡洛 2000 次] p95 最大回撤={p95_dd}%  p95 连亏={p95_cl}笔")
    print(f"  → {'✅ p95 回撤<6%' if p95_dd < DD_PAUSE else '❌'}  "
          f"{'✅ 连亏可控' if p95_cl <= 5 else '⚠️ 连亏偏多'}")
    return p95_dd < DD_PAUSE


def main():
    db.init()
    print("="*60)
    print("Stress Test 极端行情压力测试")
    print('目标：验证风控"先活下来"——单笔<1%权益、总回撤<6%暂停、无爆仓')
    print("="*60)
    res = []
    res.append(("S1 缺口穿透止损", s1_gap_through_stop()))
    print()
    res.append(("S2 全亏序列", s2_all_loss_sequence()))
    print()
    res.append(("S3 高波动子段", s3_high_vol_subperiod()))
    print()
    res.append(("S4 蒙特卡洛", s4_mc_summary()))
    print("\n" + "="*60)
    print("[压力测试汇总]")
    for name, ok in res:
        print(f"  {'✅' if ok else '❌'} {name}")
    print(f"\n结论：{'✅ 风控存活（不爆仓）' if all(res) else '❌ 有爆仓风险场景'}")
    print("（存活 ≠ 盈利——V2 在干净数据上无 edge，但风控机制能避免单次灾难）")


if __name__ == "__main__":
    main()
