"""调试 2023-03-14 glitch cluster：直接查 DB + 试清洗。"""
import os, sys, statistics
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db

WINDOW, HI, LO = 20, 3.0, 1.0/3.0

def is_glitch(o,h,l,c,ref):
    med = statistics.median([o,h,l,c])
    if ref<=0: return False
    if med>ref*HI or med<ref*LO: return True
    if h<l or c<l*0.5 or c>h*2: return True
    return False

def main():
    db.init()
    # 直接查 2023-03-14 15:00 ~ 22:00
    ts0 = 1678806000000  # 2023-03-14 15:00 UTC
    rows = db.query(
        "SELECT open_time,o,h,l,c FROM bars WHERE inst_id=? AND period='1H' AND open_time>=? AND open_time<=? ORDER BY open_time ASC",
        ("BTC-USDT", ts0-3*3600000, ts0+8*3600000))
    print("== 2023-03-14 14:00~22:00 (DB 当前值) ==")
    for r in rows:
        print(f"  {datetime.fromtimestamp(r['open_time']/1000,tz=timezone.utc):%Y-%m-%d %H:%M} o={r['o']} c={r['c']}")

    # 直接手动清洗这 8 根（15:00~22:00）用 14:00 的收盘
    fix_c = 25818.6  # 14:00 收盘
    for r in rows:
        if r["open_time"] >= ts0 and r["open_time"] <= ts0 + 7*3600000:  # 15:00~22:00
            db.execute(
                "UPDATE bars SET o=?, h=?, l=?, c=? WHERE inst_id=? AND period='1H' AND open_time=?",
                (fix_c, fix_c, fix_c, fix_c, "BTC-USDT", r["open_time"]))
            print(f"  手动清洗 {datetime.fromtimestamp(r['open_time']/1000,tz=timezone.utc):%H:%M} -> {fix_c}")

    # 复检
    rows2 = db.query("SELECT open_time,o,h,l,c FROM bars WHERE inst_id=? AND period='1H' ORDER BY open_time ASC", ("BTC-USDT",))
    closes=[r["c"] for r in rows2]
    bad=0
    for i in range(WINDOW,len(closes)):
        ref=statistics.median(closes[i-WINDOW:i])
        if is_glitch(rows2[i]["o"],rows2[i]["h"],rows2[i]["l"],rows2[i]["c"],ref):
            bad+=1
            print(f"  仍异常 {datetime.fromtimestamp(rows2[i]['open_time']/1000,tz=timezone.utc):%Y-%m-%d %H:%M} c={rows2[i]['c']} ref={ref:.0f}")
    print(f"复检残留异常: {bad}")

if __name__=="__main__":
    main()
