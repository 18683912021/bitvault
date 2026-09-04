"""交易闭环（Round Trip）统计：FIFO 把买卖成交配对成「开仓→平仓」完整回合。

现货+合约模型：用 orders.pos_side 区分多空方向——
- pos_side=long: buy 开多 / sell 平多
- pos_side=short: sell 开空 / buy 平空
现货模式 pos_side 恒为 long（buy 开 / sell 平）。
每配对生成一条记录：开仓价、平仓价、双边资费、净收入。
数据源统一用 trades 表 join orders（venue 过滤 / source 标注），模拟盘与 OKX 实盘通用。
"""
from __future__ import annotations

from app import db


def _load_fills(venue: str, inst_id: str | None) -> list[dict]:
    sql = (
        "SELECT t.ts, t.inst_id, t.side, t.px, t.sz, t.fee, o.source, o.pos_side"
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

    long_q: list[dict] = []    # 未平仓多单段
    short_q: list[dict] = []   # 未平仓空单段
    closed: list[dict] = []

    def _pair_close(open_queue: list[dict], close_px: float, close_sz: float,
                    close_fee: float, close_ts: int, side_label: str) -> None:
        remain = close_sz
        close_fee_unit = close_fee / close_sz if close_sz > 0 else 0
        while remain > 1e-12 and open_queue:
            seg = open_queue[0]
            take = min(seg["sz"], remain)
            open_fee = seg["fee"] * take / seg["sz"]
            close_fee_portion = close_fee_unit * take
            if side_label == "long":
                pnl = close_px * take - close_fee_portion - (seg["px"] * take + open_fee)
            else:  # short: 卖出开空得资金，买入平空花资金；盈利 = (open - close) * sz - fees
                pnl = (seg["px"] - close_px) * take - open_fee - close_fee_portion
            cost = seg["px"] * take + open_fee
            closed.append({
                "inst_id": seg["inst_id"], "side": side_label,
                "sz": round(take, 8),
                "open_ts": seg["ts"], "close_ts": close_ts,
                "open_px": round(seg["px"], 8), "close_px": round(close_px, 8),
                "fee": round(open_fee + close_fee_portion, 8),
                "pnl": round(pnl, 6),
                "pnl_pct": round(pnl / abs(cost), 6) if abs(cost) > 0 else 0.0,
                "source": seg["source"],
                "hold_s": max(0, (close_ts - seg["ts"]) // 1000),
            })
            seg["sz"] -= take
            if seg["sz"] <= 1e-12:
                open_queue.pop(0)
            remain -= take

    for f in fills:
        px, sz = float(f["px"]), float(f["sz"])
        fee = float(f["fee"] or 0.0)
        pos_side = f.get("pos_side") or "long"
        src = f["source"] or "manual"

        if pos_side == "short":
            # 空头方向：sell 开空 / buy 平空
            if f["side"] == "sell":
                short_q.append({"sz": sz, "px": px, "fee": fee, "ts": f["ts"],
                                "source": src, "inst_id": f["inst_id"]})
            else:  # buy 平空
                _pair_close(short_q, px, sz, fee, f["ts"], "short")
        else:
            # 多头方向：buy 开多 / sell 平多（含现货）
            if f["side"] == "buy":
                long_q.append({"sz": sz, "px": px, "fee": fee, "ts": f["ts"],
                               "source": src, "inst_id": f["inst_id"]})
            else:  # sell 平多
                _pair_close(long_q, px, sz, fee, f["ts"], "long")

    open_qty = sum(q["sz"] for q in long_q) + sum(q["sz"] for q in short_q)
    open_avg_px = 0.0
    total_sz = 0.0
    for q in long_q + short_q:
        open_avg_px += q["sz"] * q["px"]
        total_sz += q["sz"]
    if total_sz > 1e-12:
        open_avg_px /= total_sz
    return {
        "closed": closed[-limit:],
        "total": len(closed),
        "open_qty": round(open_qty, 8),
        "open_avg_px": round(open_avg_px, 8),
    }
