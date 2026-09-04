"""清洗 OKX 下载的历史数据中的异常 bar（数据完整性，任务书 P0）。

问题：5m/15m/1H/4H/1D 部分时段存在 glitch bar（如 BTC 价 17,481,639 或 3.0），
      来自 OKX 接口偶发异常。会污染 ATR/EMA/swing 因子、回测成交价、benchmark。

清洗策略（保守，不删 bar 以免破坏时间线）：
  逐 bar 检测：若该 bar 的 OHLC 中位价偏离"前 20 根 bar 收盘中位价"超过 3x 或 <0.33x，
  视为 glitch → 用"前一根收盘价 carry-forward"替换整根 OHLC（保留 vol）。
  边界：前 20 根 warmup 期不清洗（无足够参照）。

输出：每周期清洗条数 + 落库（UPDATE bars）+ 备份表 bars_dirty（清洗前副本）。
"""
import os
import sys
import statistics
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db

INST_ID = "BTC-USDT"
PERIODS = ["5m", "15m", "1H", "4H", "1D"]
WINDOW = 20           # 滚动参照窗口
HI_RATIO = 3.0        # 偏离 >3x 视为异常
LO_RATIO = 1.0 / 3.0   # 偏离 <0.33x 视为异常


def _is_glitch(o, h, l, c, ref_median):
    """任一 OHLC 偏离参照中位价超阈值 → glitch（防混合 valid+glitch 单根漏检）。"""
    if ref_median <= 0:
        return False
    for v in (o, h, l, c):
        if v > ref_median * HI_RATIO or v < ref_median * LO_RATIO:
            return True
    # 单根内部不合理（h < l 或 c < l*0.5 等）
    if h < l or c < l * 0.5 or c > h * 2:
        return True
    return False


def clean_period(period):
    rows = db.query(
        "SELECT open_time, o, h, l, c, vol FROM bars WHERE inst_id=? AND period=? "
        "ORDER BY open_time ASC", (INST_ID, period))
    if not rows:
        print(f"  {period}: 无数据")
        return 0
    db.execute(
        "CREATE TABLE IF NOT EXISTS bars_dirty AS SELECT * FROM bars WHERE 1=0")
    # 前向填充：维护 last_valid_c，glitch bar 用 last_valid_c 替换。
    # 处理连续 glitch cluster（整个簇用簇前最后一个有效收盘填充）。
    total_cleaned = 0
    for _pass in range(8):
        rows = db.query(
            "SELECT open_time, o, h, l, c FROM bars WHERE inst_id=? AND period=? "
            "ORDER BY open_time ASC", (INST_ID, period))
        closes = [r["c"] for r in rows]
        last_valid_c = closes[WINDOW - 1] if len(closes) > WINDOW else closes[0]
        # 先校准 last_valid_c：回退找首个非 glitch
        for k in range(WINDOW - 1, -1, -1):
            ref_k = statistics.median(closes[max(0, k - WINDOW):k]) if k > 0 else closes[0]
            if not _is_glitch(rows[k]["o"], rows[k]["h"], rows[k]["l"], rows[k]["c"], ref_k or closes[0]):
                last_valid_c = closes[k]
                break
        cleaned_this = []
        for i in range(WINDOW, len(rows)):
            ref = statistics.median(closes[i - WINDOW:i])
            r = rows[i]
            if _is_glitch(r["o"], r["h"], r["l"], r["c"], ref):
                cleaned_this.append((r["open_time"], period, last_valid_c,
                                     last_valid_c, last_valid_c, last_valid_c))
                closes[i] = last_valid_c   # 填充，下根的 ref 也会含此值
            else:
                last_valid_c = closes[i]
        for ot, p, o, h, l, c in cleaned_this:
            db.execute(
                "INSERT INTO bars_dirty SELECT * FROM bars WHERE inst_id=? AND period=? AND open_time=? "
                "AND NOT EXISTS (SELECT 1 FROM bars_dirty WHERE inst_id=? AND period=? AND open_time=?)",
                (INST_ID, p, ot, INST_ID, p, ot))
            db.execute(
                "UPDATE bars SET o=?, h=?, l=?, c=? WHERE inst_id=? AND period=? AND open_time=?",
                (o, h, l, c, INST_ID, p, ot))
        total_cleaned += len(cleaned_this)
        if not cleaned_this:
            break
    print(f"  {period}: 共 {len(rows)} 根，清洗 {total_cleaned} 根 glitch bar（前向填充多轮）")
    return total_cleaned


def main():
    db.init()
    print("== 数据清洗：异常 bar 检测与修复 ==")
    total = 0
    for p in PERIODS:
        total += clean_period(p)
    print(f"\n合计清洗 {total} 根 glitch bar。备份在 bars_dirty 表。")

    # 复检
    print("\n== 复检：清洗后是否仍有异常 ==")
    for p in PERIODS:
        rows = db.query(
            "SELECT open_time, o, h, l, c FROM bars WHERE inst_id=? AND period=? "
            "ORDER BY open_time ASC", (INST_ID, p))
        bad = 0
        for i in range(WINDOW, len(rows)):
            ref = statistics.median([rows[j]["c"] for j in range(i-WINDOW, i)])
            r = rows[i]
            if _is_glitch(r["o"], r["h"], r["l"], r["c"], ref):
                bad += 1
        print(f"  {p}: 残留异常 {bad} 根")


if __name__ == "__main__":
    main()
