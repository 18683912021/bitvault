"""检查 1H dev 窗口是否有异常 close 价格（0 或极小值）。"""
import os, sys
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db
from app.backtest.holdout import dev_end_ms

def main():
    db.init()
    rows = db.query(
        "SELECT open_time, o, c FROM bars WHERE inst_id='BTC-USDT' AND period='1H' "
        "AND open_time>=? AND open_time<=? ORDER BY open_time ASC",
        ("BTC-USDT", "1H", 1672243200000, dev_end_ms("1H"))) if False else db.query(
        "SELECT open_time, o, c FROM bars WHERE inst_id=? AND period=? "
        "AND open_time>=? AND open_time<=? ORDER BY open_time ASC",
        ("BTC-USDT", "1H", 1672243200000, dev_end_ms("1H")))
    print(f"1H bars: {len(rows)}")
    if rows:
        print(f"first: {datetime.fromtimestamp(rows[0]['open_time']/1000, tz=timezone.utc):%Y-%m-%d %H:%M} o={rows[0]['o']} c={rows[0]['c']}")
        print(f"last:  {datetime.fromtimestamp(rows[-1]['open_time']/1000, tz=timezone.utc):%Y-%m-%d %H:%M} o={rows[-1]['o']} c={rows[-1]['c']}")
    # 找极小值（< 1000）或 0
    bad = [r for r in rows if r["c"] < 1000 or r["o"] < 1000]
    print(f"异常价 (<1000): {len(bad)} 条")
    for r in bad[:10]:
        print(f"  {datetime.fromtimestamp(r['open_time']/1000, tz=timezone.utc):%Y-%m-%d %H:%M} o={r['o']} c={r['c']}")
    # 找最大值确认范围
    mc = max(rows, key=lambda r: r["c"])
    print(f"最高 c: {datetime.fromtimestamp(mc['open_time']/1000, tz=timezone.utc):%Y-%m-%d} {mc['c']}")

if __name__ == "__main__":
    main()
