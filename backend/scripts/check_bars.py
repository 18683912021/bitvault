"""快速检查 bars 表各周期数据覆盖范围（只读，用于决策下一步）。"""
import sqlite3
import datetime


def main():
    con = sqlite3.connect("data/bitvault.db")
    cur = con.cursor()
    print("== bars coverage (inst_id=BTC-USDT) ==")
    for p in ["5m", "15m", "1H", "4H", "1D"]:
        cur.execute(
            "SELECT MIN(open_time), MAX(open_time), COUNT(*) "
            "FROM bars WHERE inst_id='BTC-USDT' AND period=?",
            (p,),
        )
        r = cur.fetchone()
        if r and r[0]:
            print(
                f"{p:4s}: {datetime.datetime.fromtimestamp(r[0]/1000).date()} "
                f"-> {datetime.datetime.fromtimestamp(r[1]/1000).date()} "
                f"count={r[2]}"
            )
        else:
            print(f"{p:4s}: empty")
    con.close()


if __name__ == "__main__":
    main()
