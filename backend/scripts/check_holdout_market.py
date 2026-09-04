"""Holdout 期间 BTC 行情 + 失败交易归因分析。"""
import os
import sys
import json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db
from app.backtest.holdout import holdout_start_ms, holdout_end_ms


def main():
    db.init()
    h_start, h_end = holdout_start_ms("1H"), holdout_end_ms("1H")
    rows = db.query(
        "SELECT open_time, o, h, l, c FROM bars WHERE inst_id='BTC-USDT' AND period='1H' "
        "AND open_time>=? AND open_time<=? ORDER BY open_time ASC",
        ("BTC-USDT", "1H", h_start, h_end)) if False else db.query(
        "SELECT open_time, o, h, l, c FROM bars WHERE inst_id=? AND period=? "
        "AND open_time>=? AND open_time<=? ORDER BY open_time ASC",
        ("BTC-USDT", "1H", h_start, h_end))
    if not rows:
        print("无 1H 数据")
        return
    first_o, last_c = rows[0]["o"], rows[-1]["c"]
    hi = max(r["h"] for r in rows)
    lo = min(r["l"] for r in rows)
    print(f"== Holdout 1H 行情 ({datetime.fromtimestamp(h_start/1000, tz=timezone.utc):%Y-%m-%d}"
          f" → {datetime.fromtimestamp(h_end/1000, tz=timezone.utc):%Y-%m-%d}) ==")
    print(f"  开盘 {first_o:.0f}  收盘 {last_c:.0f}  最高 {hi:.0f}  最低 {lo:.0f}")
    print(f"  BTC 收益率 {((last_c/first_o)-1)*100:.1f}%  振幅 {((hi/lo)-1)*100:.1f}%")

    # 月度拆分
    print("\n  月度 BTC 走势：")
    months = {}
    for r in rows:
        m = datetime.fromtimestamp(r["open_time"]/1000, tz=timezone.utc).strftime("%Y-%m")
        months.setdefault(m, []).append(r)
    for m, rs in months.items():
        mo, mc = rs[0]["o"], rs[-1]["c"]
        mhi, mlo = max(x["h"] for x in rs), min(x["l"] for x in rs)
        print(f"    {m}: 开 {mo:.0f} 收 {mc:.0f} ({((mc/mo)-1)*100:+.1f}%)  高 {mhi:.0f} 低 {mlo:.0f}")

    # 失败交易归因
    print("\n== Holdout 5 笔失败交易归因 ==")
    jf = "data/backtest/journal_HOLDOUT_FINAL_1H_20260301_20260901.json"
    if os.path.exists(jf):
        recs = json.load(open(jf, encoding="utf-8"))
        for r in recs:
            ts = datetime.fromtimestamp(r["timestamp"]/1000, tz=timezone.utc)
            print(f"  {ts:%Y-%m-%d %H:%M} entry={r['entry']} exit={r['exitPrice']} "
                  f"reason={r['exitReason']} pnl={r['pnl']} mfe_r={r['mfe_r']} mae_r={r['mae_r']} "
                  f"setup={r['setup']} regime={r['regime']} hold={r['holdingBars']}b")


if __name__ == "__main__":
    main()
