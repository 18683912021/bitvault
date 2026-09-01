"""交易闭环（Round Trip）统计：FIFO 把买卖成交配对成「开仓→平仓」完整回合。

现货模型：buy 开/加仓，sell 减/平仓。sell 按先进先出消耗 open 队列，
每耗尽一段就生成一条已平仓记录：开仓价、平仓价、双边资费、净收入。
数据源统一用 trades 表 join orders（venue 过滤 / source 标注），模拟盘与 OKX 实盘通用。
"""
from __future__ import annotations

from app import db


def _load_fills(venue: str, inst_id: str | None) -> list[dict]:
    sql = (
        "SELECT t.ts, t.inst_id, t.side, t.px, t.sz, t.fee, o.source"
        " FROM trades t JOIN orders o ON t.cl_ord_id = o.cl_ord_id"
        " WHERE o.venue=?"
    )
    args: list = [venue]
    if inst_id:
        sql += " AND t.inst_id=?"
        args.append(inst_id)
    sql += " ORDER BY t.ts ASC, t.rowid ASC"
    return db.query(sql, args)


def compute_round_trips(venue: str = "paper", inst_id: str | None = None,
                        limit: int = 50) -> dict:
    """返回 {closed: 最近 limit 条, total, open_qty, open_avg_px}。

    closed 条目：inst_id, side, sz, open_ts, close_ts, open_px, close_px,
                 fee(双边合计), pnl(净收入,已扣双边费), pnl_pct, source, hold_s
    """
    fills = _load_fills(venue, inst_id)

    queue: list[dict] = []   # 未平仓 buy 段：{sz, px, fee, ts, source, inst_id}
    closed: list[dict] = []

    for f in fills:
        px, sz = float(f["px"]), float(f["sz"])
        fee = float(f["fee"] or 0.0)
        if f["side"] == "buy":
            queue.append({"sz": sz, "px": px, "fee": fee, "ts": f["ts"],
                          "source": f["source"] or "manual", "inst_id": f["inst_id"]})
        elif sz > 0:
            remain = sz
            close_fee_unit = fee / sz            # 卖出侧单位手续费
            while remain > 1e-12 and queue:
                seg = queue[0]
                take = min(seg["sz"], remain)
                open_fee = seg["fee"] * take / seg["sz"]
                close_fee = close_fee_unit * take
                cost = seg["px"] * take + open_fee
                pnl = px * take - close_fee - cost
                closed.append({
                    "inst_id": seg["inst_id"], "side": "long",
                    "sz": round(take, 8),
                    "open_ts": seg["ts"], "close_ts": f["ts"],
                    "open_px": round(seg["px"], 8), "close_px": round(px, 8),
                    "fee": round(open_fee + close_fee, 8),
                    "pnl": round(pnl, 6),
                    "pnl_pct": round(pnl / cost, 6) if cost > 0 else 0.0,
                    "source": seg["source"],
                    "hold_s": max(0, (f["ts"] - seg["ts"]) // 1000),
                })
                seg["sz"] -= take
                if seg["sz"] <= 1e-12:
                    queue.pop(0)
                remain -= take
            # 卖出超过持仓（理论上现货不可能）——忽略超出部分

    open_qty = sum(q["sz"] for q in queue)
    open_avg_px = (sum(q["sz"] * q["px"] for q in queue) / open_qty) if open_qty > 1e-12 else 0.0
    return {
        "closed": closed[-limit:],
        "total": len(closed),
        "open_qty": round(open_qty, 8),
        "open_avg_px": round(open_avg_px, 8),
    }
