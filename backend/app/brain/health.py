"""策略健康（P2-9）：基于最近 roundtrip 的运行时指标 → GREEN / YELLOW / RED。

第一版只做监控与报告，不自动修改任何策略参数/仓位（后续再研究自动降档）。
"""
from __future__ import annotations

from app.trading.roundtrips import compute_round_trips


def compute_health(venue: str = "paper", limit: int = 50) -> dict:
    rt = compute_round_trips(venue=venue, limit=limit)
    closed = rt.get("closed") or []
    n = len(closed)
    if n == 0:
        return {"health": "UNKNOWN", "reason": "暂无已完成交易", "n": 0}
    wins = [c for c in closed if float(c.get("pnl") or 0) > 0]
    losses = [c for c in closed if float(c.get("pnl") or 0) < 0]
    win_rate = len(wins) / n
    gross_win = sum(float(c["pnl"]) for c in wins)
    gross_loss = -sum(float(c["pnl"]) for c in losses)
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
    expectancy = sum(float(c.get("pnl") or 0) for c in closed) / n
    # 最大连续亏损
    streak = max_streak = 0
    for c in closed:
        if float(c.get("pnl") or 0) < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    if win_rate < 0.35 or profit_factor < 0.9 or max_streak >= 4:
        level = "RED"
    elif win_rate < 0.45 or profit_factor < 1.1 or max_streak >= 3:
        level = "YELLOW"
    else:
        level = "GREEN"
    return {
        "health": level,
        "n": n,
        "win_rate": round(win_rate, 3),
        "profit_factor": round(profit_factor, 2),
        "expectancy": round(expectancy, 2),
        "max_consecutive_losses": max_streak,
        "last_pnl": round(float(closed[-1].get("pnl") or 0), 2),
    }


def health_status(venue: str = "paper") -> dict:
    """供 /health 使用；聚合健康 + 最近指标。"""
    h = compute_health(venue)
    h["venue"] = venue
    return h
