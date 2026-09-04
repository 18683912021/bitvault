"""BTC Buy & Hold Benchmark 对比（任务书第三十六节）。

目标不是绝对收益最高，而是"降低风险和回撤的情况下获得稳定收益"。
因此重点对比：收益 / 最大回撤 / Sharpe / Sortino / Calmar，而非单纯收益率。

输出 dev 窗口 + holdout 窗口两段的 strategy vs buy&hold 对比表。
不依赖任何参数优化（纯分析性），可在已污染 Holdout 上跑（仅作 benchmark 对照，不调参）。
"""
import os
import sys
import json
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db
from app.backtest.rules_backtest import BTParams, run_rules_backtest
from app.backtest.holdout import dev_end_ms, holdout_start_ms, holdout_end_ms

INST_ID = "BTC-USDT"


def _load_bars(period, start_ms, end_ms):
    rows = db.query(
        "SELECT open_time, o, h, l, c, vol FROM bars WHERE inst_id=? AND period=? "
        "AND open_time>=? AND open_time<=? ORDER BY open_time ASC",
        (INST_ID, period, start_ms, end_ms))
    return [{"ts": r["open_time"], "o": r["o"], "h": r["h"], "l": r["l"],
             "c": r["c"], "vol": r["vol"] or 0} for r in rows]


def _buyhold_metrics(bars, start_ms, end_ms, initial=10000.0, period="1H"):
    """BTC 一次性买入持有，满仓 SPOT，无杠杆无再平衡。"""
    ppy = {"5m": 105_120, "15m": 35_040, "1H": 8_760}.get(period, 8760)
    # 从 start_ms 开始的第一个 bar 开盘买入
    eq_curve = []
    qty = None
    for b in bars:
        if b["ts"] < start_ms:
            continue
        if b["ts"] >= end_ms:
            break
        if qty is None:
            # taker 买入扣手续费
            qty = (initial * (1 - 0.001)) / b["o"]
        eq = qty * b["c"]
        eq_curve.append({"ts": b["ts"], "equity": round(eq, 2)})
    if not eq_curve:
        return None
    eq = [e["equity"] for e in eq_curve]
    # 末尾卖出扣手续费（仅用于净收益口径对齐）
    final_eq_net = eq[-1] * (1 - 0.001)
    total_return = (final_eq_net / initial - 1) * 100
    years = len(eq) / ppy
    annual = ((final_eq_net / initial) ** (1 / years) - 1) * 100 if years > 0 and final_eq_net > 0 else 0
    peak_v, max_dd = eq[0], 0.0
    for v in eq:
        peak_v = max(peak_v, v)
        max_dd = max(max_dd, (peak_v - v) / peak_v * 100)
    rets = [(eq[i] - eq[i - 1]) / eq[i - 1] for i in range(1, len(eq)) if eq[i - 1] > 0]
    mean = sum(rets) / len(rets) if rets else 0
    var = sum((r - mean) ** 2 for r in rets) / len(rets) if rets else 0
    std = (var ** 0.5) or 1e-12
    sharpe = mean / std * (ppy ** 0.5) if rets else 0
    downside = [r for r in rets if r < 0]
    dstd = (sum(r ** 2 for r in downside) / len(downside)) ** 0.5 if downside else 1e-12
    sortino = mean / dstd * (ppy ** 0.5) if rets else 0
    return {
        "total_return_pct": round(total_return, 2),
        "annual_pct": round(annual, 2),
        "max_dd_pct": round(max_dd, 2),
        "calmar": round(annual / max_dd, 2) if max_dd > 0 else None,
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "final_equity": round(final_eq_net, 2),
    }


def _strat_metrics(bars, bars_4h, bars_1h, start_ms, end_ms, period, initial=10000.0):
    params = BTParams(require_setup=True, allow_short=False,
                      initial_equity=initial)
    r = run_rules_backtest(bars, bars_4h, bars_1h, params, period,
                           start_ms=start_ms, end_ms=end_ms)
    m = r["metrics"]
    return {
        "total_return_pct": m.get("total_return_pct"),
        "annual_pct": m.get("annual_pct"),
        "max_dd_pct": m.get("max_dd_pct"),
        "calmar": m.get("calmar"),
        "sharpe": m.get("sharpe"),
        "sortino": m.get("sortino"),
        "final_equity": m.get("final_equity"),
        "round_trips": m.get("round_trips"),
        "profit_factor": m.get("profit_factor"),
        "net_pnl": m.get("net_pnl"),
    }, r


def _compare_table(name, s, b):
    print(f"\n{'='*60}\n{name}\n{'='*60}")
    print(f"{'指标':<18}{'策略':>14}{'Buy&Hold':>14}{'差值':>14}")
    print("-" * 60)
    for k, label in [("total_return_pct", "总收益%"), ("annual_pct", "年化%"),
                     ("max_dd_pct", "最大回撤%"), ("calmar", "Calmar"),
                     ("sharpe", "Sharpe"), ("sortino", "Sortino"),
                     ("final_equity", "最终权益")]:
        sv, bv = s.get(k), b.get(k)
        if isinstance(sv, (int, float)) and isinstance(bv, (int, float)):
            d = round(sv - bv, 2)
            print(f"{label:<18}{sv:>14}{bv:>14}{d:>+14}")
        else:
            print(f"{label:<18}{str(sv):>14}{str(bv):>14}{'>':>14}")
    if s.get("round_trips") is not None:
        print(f"{'交易笔数':<18}{s.get('round_trips'):>14}{'1(B&H)':>14}")


def main():
    db.init()
    period = "1H"
    warmup = 30 * 86_400_000

    # ---- dev 窗口 ----
    dev_start = _ms("2022-12-29")
    dev_end = dev_end_ms(period)
    bars = _load_bars(period, dev_start - warmup, dev_end)
    bars_4h = _load_bars("4H", dev_start - warmup, dev_end)
    bars_1h = _load_bars("1H", dev_start - warmup, dev_end)
    print(f"dev: {datetime.fromtimestamp(dev_start/1000, tz=timezone.utc):%Y-%m-%d}"
          f" → {datetime.fromtimestamp(dev_end/1000, tz=timezone.utc):%Y-%m-%d}  bars={len(bars)}")
    t0 = time.time()
    s_dev, _ = _strat_metrics(bars, bars_4h, bars_1h, dev_start, dev_end, period)
    b_dev = _buyhold_metrics(bars, dev_start, dev_end, period=period)
    print(f"  策略用时 {time.time()-t0:.1f}s")
    _compare_table(f"DEV 窗口 ({period})", s_dev, b_dev)

    # ---- holdout 窗口（仅 benchmark 对照，不调参）----
    h_start, h_end = holdout_start_ms(period), holdout_end_ms(period)
    bars_h = _load_bars(period, h_start - warmup, h_end)
    bars_4h_h = _load_bars("4H", h_start - warmup, h_end)
    bars_1h_h = _load_bars("1H", h_start - warmup, h_end)
    print(f"\nholdout: {datetime.fromtimestamp(h_start/1000, tz=timezone.utc):%Y-%m-%d}"
          f" → {datetime.fromtimestamp(h_end/1000, tz=timezone.utc):%Y-%m-%d}  bars={len(bars_h)}")
    t0 = time.time()
    s_h, _ = _strat_metrics(bars_h, bars_4h_h, bars_1h_h, h_start, h_end, period)
    b_h = _buyhold_metrics(bars_h, h_start, h_end, period=period)
    print(f"  策略用时 {time.time()-t0:.1f}s")
    _compare_table(f"HOLDOUT 窗口 ({period}, 仅对照)", s_h, b_h)

    # 任务书核心：低风险低回撤 > 高收益
    print(f"\n{'='*60}\n核心结论（任务书：降风险回撤 > 绝对收益）\n{'='*60}")
    print(f"DEV:  策略回撤 {s_dev['max_dd_pct']}% vs B&H {b_dev['max_dd_pct']}%  "
          f"策略收益 {s_dev['total_return_pct']}% vs B&H {b_dev['total_return_pct']}%")
    print(f"HOLD: 策略回撤 {s_h['max_dd_pct']}% vs B&H {b_h['max_dd_pct']}%  "
          f"策略收益 {s_h['total_return_pct']}% vs B&H {b_h['total_return_pct']}%")

    out = {"dev": {"strategy": s_dev, "buyhold": b_dev},
           "holdout": {"strategy": s_h, "buyhold": b_h}}
    os.makedirs("data/backtest", exist_ok=True)
    with open("data/backtest/benchmark_1H.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n报告: data/backtest/benchmark_1H.json")


def _ms(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


if __name__ == "__main__":
    main()
