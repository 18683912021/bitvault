"""定位清洗后仍标异常的 bar，人工判断是 glitch 还是真实剧烈波动。"""
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
    rows = db.query("SELECT open_time,o,h,l,c,vol FROM bars WHERE inst_id='BTC-USDT' AND period='1H' ORDER BY open_time ASC")
    closes=[r["c"] for r in rows]
    for i in range(WINDOW,len(rows)):
        ref=statistics.median(closes[i-WINDOW:i])
        r=rows[i]
        if is_glitch(r["o"],r["h"],r["l"],r["c"],ref):
            print(f"残留异常 {datetime.fromtimestamp(r['open_time']/1000,tz=timezone.utc):%Y-%m-%d %H:%M}")
            print(f"  o={r['o']} h={r['h']} l={r['l']} c={r['c']} vol={r['vol']} 参照中位={ref:.0f}")
            # 显示前后 5 根
            print("  前后 5 根收盘：")
            for j in range(max(0,i-5), min(len(rows),i+6)):
                mark = " <-- 异常" if j==i else ""
                print(f"    {datetime.fromtimestamp(rows[j]['open_time']/1000,tz=timezone.utc):%Y-%m-%d %H:%M} c={rows[j]['c']}{mark}")

if __name__=="__main__":
    main()
