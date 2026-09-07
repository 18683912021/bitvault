"""历史机会回放：找出历史上所有 breakout_retest 形态出现的时点。

方法：对本地 bars 表中 BTC-USDT 5m 历史逐根回放（收盘确认时点），
先粗筛（突破+回踩结构命中）再做完整链（compute_factors → detect_setup →
score_side → check_gate），输出形态命中时点及当时系统是否在运行。

注：check_gate 的 drawdown/loss_streak 为中性值（0），风控实际状态不可回放，
结果用于判断"纯规则层面是否有机会"，属近似复盘。
"""
from __future__ import annotations

import bisect
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.brain import signal_engine as se
from app.brain.factor_engine import compute_factors, trend_of

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "bitvault.db")
PERIOD = sys.argv[1] if len(sys.argv) > 1 else "5m"
START_MS = int(sys.argv[2]) if len(sys.argv) > 2 else 0


def load_bars(conn, inst_id: str, period: str) -> list[dict]:
    rows = conn.execute(
        "SELECT open_time ts, o, h, l, c, vol FROM bars WHERE inst_id=? AND period=? "
        "ORDER BY open_time ASC", (inst_id, period)
    ).fetchall()
    return [{"ts": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "vol": r[5]} for r in rows]


def load_htf(conn, inst_id: str, period: str) -> list[dict]:
    rows = conn.execute(
        "SELECT open_time ts, o, h, l, c, vol FROM bars WHERE inst_id=? AND period=? "
        "ORDER BY open_time ASC", (inst_id, period)
    ).fetchall()
    return [{"ts": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "vol": r[5]} for r in rows]


def htf_trend(htf: list[dict], ts: int) -> str | None:
    idx = bisect.bisect_right([b["ts"] for b in htf], ts)
    last60 = htf[max(0, idx - 60):idx]
    if len(last60) < 23:
        return None
    return trend_of(last60)


def main() -> None:
    conn = sqlite3.connect(DB)
    bars = load_bars(conn, "BTC-USDT", PERIOD)
    h4 = load_htf(conn, "BTC-USDT", "4H")
    mid_period = "1D" if PERIOD in ("1H", "2H", "4H") else "1H"
    mid = load_htf(conn, "BTC-USDT", mid_period)
    h4ts = [b["ts"] for b in h4]
    midts = [b["ts"] for b in mid]
    sig_ts = {r[0] for r in conn.execute(
        "SELECT DISTINCT ts FROM signals WHERE instance_id=0")}

    print(f"{PERIOD} bars: {len(bars)} | 4H: {len(h4)} | {mid_period}: {len(mid)} | 回放起点: {time.strftime('%Y-%m-%d %H:%M', time.localtime(START_MS/1000)) if START_MS else '全部'}")
    hits, t0 = [], time.time()

    for i in range(40, len(bars)):
        if bars[i]["ts"] < START_MS:
            continue
        win = bars[max(0, i - 119):i + 1]   # 最近120根（含当前收盘）
        n = len(win)
        if n < 40:
            continue
        highs = [b["h"] for b in win]
        lows = [b["l"] for b in win]
        closes = [b["c"] for b in win]
        # 粗筛 ATR（简单 TR 均值，与完整链近似，仅用于预筛）
        trs = [max(highs[j] - lows[j], abs(highs[j] - closes[j - 1]), abs(lows[j] - closes[j - 1]))
               for j in range(n - 14, n)]
        atr = sum(trs) / len(trs) if trs else 0
        if atr <= 0:
            continue
        for side in ("long", "short"):
            level = max(highs[-40:-3]) if side == "long" else min(lows[-40:-3])
            if side == "long":
                broke = any(closes[j] > level + 0.3 * atr for j in range(n - 3, n))
                retest = all(level - 0.2 * atr <= lows[j] <= level + 0.8 * atr
                             for j in range(n - 2, n))
            else:
                broke = any(closes[j] < level - 0.3 * atr for j in range(n - 3, n))
                retest = all(level - 0.8 * atr <= highs[j] <= level + 0.2 * atr
                             for j in range(n - 2, n))
            if not (broke and retest):
                continue
            # 完整链
            if n < 60:
                continue
            factors = compute_factors(win)
            if factors.get("error"):
                continue
            setup = se.detect_setup(win, factors, side)
            if setup.get("setup") != "breakout_retest":
                continue
            t = bars[i]["ts"]
            h4t = htf_trend(h4, t)
            midt = htf_trend(mid, t)
            score, _ = se.score_side(factors, side, setup, htf_4h=h4t, htf_1h=midt)
            plan = se.plan_trade(win, factors, side)
            ctx = {"htf_4h": h4t, "htf_1h": midt, "halted": False,
                   "drawdown_pct": 0.0, "loss_streak": 0, "leverage": 2}
            ok, _ = se.check_gate(win, factors, side, score, plan, ctx)
            running = any(abs(bars[i]["ts"] - s) < 10 * 60_000 for s in sig_ts)
            hits.append((bars[i]["ts"], side, factors.get("regime"), score, ok,
                         h4t, running))
            break   # 一根 bar 只记一次（取先命中的方向）

    hits.sort()
    print(f"回放耗时 {time.time() - t0:.1f}s | 形态命中 {len(hits)} 次\n")
    print(f"{'时间':<17}{'方向':<5}{'regime':<11}{'score':<7}{'gate':<7}{'htf4h':<6}{'系统在跑'}")
    with_pass = 0
    running_hits = 0
    for ts, side, regime, score, ok, htf, running in hits:
        t = time.strftime("%m-%d %H:%M", time.localtime(ts / 1000))
        mark = "✓ PASS" if ok else "✗ fail"
        if ok:
            with_pass += 1
        if running:
            running_hits += 1
        print(f"{t:<17}{side:<5}{str(regime):<11}{score:<7}{mark:<7}{str(htf):<6}{'✓' if running else '—'}")
    print(f"\n== 汇总: 形态命中 {len(hits)} 次；系统在跑期间命中 {running_hits} 次；全条件通过(可开单) {with_pass} 次 ==")


if __name__ == "__main__":
    main()
