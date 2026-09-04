"""5U 小资金验证：1H 回测 + 仓位计算检查。"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db
from app.backtest.rules_backtest import BTParams, run_rules_backtest
from app.backtest.holdout import dev_end_ms
from datetime import datetime, timezone

db.init()

INST_ID = "BTC-USDT"

def _ms(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

def _load_bars(period, start_ms, end_ms):
    rows = db.query(
        "SELECT open_time, o, h, l, c, vol FROM bars WHERE inst_id=? AND period=? "
        "AND open_time>=? AND open_time<=? ORDER BY open_time ASC",
        (INST_ID, period, start_ms, end_ms),
    )
    return [{"ts": r["open_time"], "o": r["o"], "h": r["h"], "l": r["l"],
             "c": r["c"], "vol": r["vol"] or 0} for r in rows]

period = "1H"
start_ms = _ms("2023-01-01")
end_ms = dev_end_ms(period)
warmup = start_ms - 30 * 86400_000

bars = _load_bars(period, warmup, end_ms)
bars_4h = _load_bars("4H", warmup, end_ms)
bars_1d = _load_bars("1D", warmup, end_ms)
print(f"bars: {period}={len(bars)}  4H={len(bars_4h)}  1D={len(bars_1d)}")

# 5U 小资金 + V2 最优配置
params = BTParams(
    initial_equity=5.0,
    max_order_usdt=5.0,
    require_setup=True,
    setup_filter="breakout_retest",
    allow_short=False,
)

t0 = time.time()
result = run_rules_backtest(bars, bars_4h, bars_1d, params, period, start_ms,
                           end_ms=dev_end_ms(period))
print(f"\n回测用时 {time.time() - t0:.1f}s")

m = result["metrics"]
print("\n=== 5U 小资金 1H 回测结果（2023-01 → 2026-03 dev 窗口）===")
print(f"初始资金:   5.0000 USDT")
print(f"最终权益:   {m.get('final_equity', 0):.4f} USDT")
print(f"净盈亏:     {m.get('net_pnl', 0):.4f} USDT")
print(f"总收益率:   {m.get('total_return_pct', 0):.2f}%")
print(f"完整交易:   {m.get('round_trips', 0)} 笔")
print(f"胜率:       {m.get('win_rate', 0):.1f}%")
print(f"盈亏比(PF): {m.get('profit_factor', 0)}")
print(f"期望值:     {m.get('expectancy', 0):.4f} USDT/笔")
print(f"最大回撤:   {m.get('max_dd_pct', 0):.2f}%")
print(f"总手续费:   {m.get('fee_total', 0):.4f} USDT")
print(f"Sharpe:     {m.get('sharpe', 0)}")

# 按 round_id 聚合查看每笔完整交易
journal = result["journal"]
rounds = {}
for rec in journal:
    rid = rec.get("round_id", 0)
    rounds.setdefault(rid, []).append(rec)

print(f"\n=== 交易明细（{len(rounds)} 笔完整交易，{len(journal)} 条平仓记录）===")
for rid, recs in sorted(rounds.items()):
    total_pnl = sum(r["pnl"] for r in recs)
    total_fee = sum(r.get("fee_close", 0) + r.get("fee_open_share", 0) for r in recs)
    entry = recs[0].get("entry", 0)
    sl = recs[0].get("stopLoss", 0)
    tp = recs[0].get("takeProfit", 0)
    regime = recs[0].get("regime", "")
    setup = recs[0].get("setup", "")
    score = recs[0].get("entryScore", 0)
    pos_size = recs[0].get("positionSize", 0)
    mfe_r = recs[-1].get("mfe_r", 0)
    exits = " + ".join(r.get("exitReason", "") for r in recs)
    print(f"  #{rid} entry={entry:.1f} sl={sl:.1f} tp={tp:.1f} score={score} "
          f"首次卖出={pos_size:.2f}U pnl={total_pnl:.4f}U fee={total_fee:.4f}U "
          f"regime={regime} setup={setup} mfe_r={mfe_r:.2f}")
    print(f"       平仓: {exits}")

# 关键验证点
print("\n=== 关键验证 ===")
notional_zero = [r for r in journal if r.get("positionSize", 0) == 0]
print(f"positionSize=0 的记录: {len(notional_zero)}")
# positionSize 是卖出 notional（部分平仓时可能 < 1U），这不代表开仓失败
# 真正要检查的是 open 时的 notional >= 1.0
# 从 journal 反推：fee_open = notional * fee_taker，所以 notional = fee_open / fee_taker
fee_opens = [r.get("fee_open_share", 0) for r in journal]
notionals = [f / 0.001 for f in fee_opens if f > 0]
print(f"开仓 notional 推算（fee_open/0.001）: {[round(n,2) for n in notionals]}")
below_1 = [n for n in notionals if n < 1.0]
print(f"不足 1U 的开仓: {len(below_1)} 笔")
if not below_1:
    print("✅ 所有开仓均 ≥ 1U（满足最小下单额）")

# 费用占比
total_fee = m.get("fee_total", 0)
net_pnl = m.get("net_pnl", 0)
print(f"\n费用总计: {total_fee:.4f}U / 净盈亏: {net_pnl:.4f}U")
if net_pnl > 0:
    print(f"费用占利润比: {total_fee / (net_pnl + total_fee) * 100:.1f}%")
    print("✅ 费用可控，净利润为正")
elif net_pnl == 0:
    print("⚠️ 盈亏平衡")
else:
    print(f"⚠️ 净亏损 {net_pnl:.4f}U")

print("\n✅ 验证完成")
