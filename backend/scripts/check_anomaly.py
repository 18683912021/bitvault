"""定位 2023-07-02 附近异常 bar，看是否单点数据 glitch。"""
import os, sys
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db

def main():
    db.init()
    # 2023-07-01 ~ 2023-07-03 1H bar
    rows = db.query(
        "SELECT open_time, o, h, l, c, vol FROM bars WHERE inst_id=? AND period='1H' "
        "AND open_time>=1688169600000 AND open_time<=1688342400000 ORDER BY open_time ASC",
        ("BTC-USDT",))
    print("== 1H bars 2023-07-01 ~ 07-03 ==")
    for r in rows:
        print(f"  {datetime.fromtimestamp(r['open_time']/1000, tz=timezone.utc):%Y-%m-%d %H:%M}"
              f" o={r['o']} h={r['h']} l={r['l']} c={r['c']} vol={r['vol']}")

    # 全量扫异常（c 或 o 任意一个偏离前后 >10x）
    print("\n== 全量扫异常 bar（o/h/l/c 偏离中位价 >5x 或 <0.2x）==")
    rows = db.query(
        "SELECT open_time, o, h, l, c FROM bars WHERE inst_id=? AND period='1H' "
        "ORDER BY open_time ASC", ("BTC-USDT",))
    anomalies = []
    for i in range(2, len(rows)-2):
        ref = (rows[i-1]["c"] + rows[i+1]["c"]) / 2  # 邻居均值
        for f in ("o","h","l","c"):
            v = rows[i][f]
            if ref > 0 and (v > ref * 5 or v < ref * 0.2):
                anomalies.append((rows[i]["open_time"], f, v, ref))
                break
    print(f"异常 bar 总数: {len(anomalies)}")
    for ts, f, v, ref in anomalies[:20]:
        print(f"  {datetime.fromtimestamp(ts/1000, tz=timezone.utc):%Y-%m-%d %H:%M} "
              f"{f}={v} (邻居~{ref:.0f})")

if __name__ == "__main__":
    main()
