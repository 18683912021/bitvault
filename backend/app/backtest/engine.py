"""回测引擎：与实盘同一套策略接口（BaseStrategy），信号 N 收盘产生、N+1 开盘价 ± 滑点成交。

成本模型：手续费(taker)、滑点、（合约策略的）资金费率暂按周期近似、止损触发模拟。
"""
from __future__ import annotations

import json
import time

from app import db
from app.strategy.base import StrategyContext

PERIODS_PER_YEAR = {
    "1m": 525_600, "3m": 175_200, "5m": 105_120, "15m": 35_040, "30m": 17_520,
    "1H": 8_760, "2H": 4_380, "4H": 2_190, "6H": 1_460, "12H": 730, "1D": 365, "1W": 52,
}


class SimContext(StrategyContext):
    """回测 ctx：订单进入 SimBroker 排队，下一根 bar 撮合。"""

    instance_id = None
    mode = "backtest"

    def __init__(self, broker: "SimBroker"):
        self.broker = broker

    async def submit_order(self, intent: dict) -> dict:
        return self.broker.queue_order(intent)

    def last_price(self) -> float:
        return self.broker.last_close or 0.0

    def log(self, msg: str, level: str = "info") -> None:
        pass

    def save_state(self) -> None:
        pass

    async def cancel_instance_orders(self) -> int:
        return self.broker.cancel_pending()

    async def close_instance_position(self) -> bool:
        return self.broker.market_close_position()


class SimBroker:
    def __init__(self, fee_rate: float, slippage: float, initial_equity: float, progress_cb=None):
        self.fee_rate = fee_rate
        self.slippage = slippage
        self.initial_equity = initial_equity
        self.cash = initial_equity
        self.pos = 0.0            # signed BTC
        self.entry_px = 0.0
        self.sl_px: float | None = None
        self.pending_limit: list[dict] = []
        self.pending_market: list[dict] = []
        self.trades: list[dict] = []
        self.equity_curve: list[dict] = []
        self.bh_curve: list[dict] = []
        self.last_close = 0.0
        self.bar_count = 0
        self.total_bars = 0
        self.strategy = None
        self.fee_total = 0.0
        self._cl_seq = 0
        self._hold_bars: list[int] = []
        self._entry_bar = 0
        self._receipts: list[dict] = []
        self.progress_cb = progress_cb

    # ---------- 下单排队 ----------
    def queue_order(self, intent: dict) -> dict:
        self._cl_seq += 1
        order = {
            "cl_ord_id": f"SIM{self._cl_seq}",
            "side": intent["side"],
            "ord_type": intent.get("ord_type", "market"),
            "px": intent.get("px"),
            "sz_base": float(intent["sz_base"]),
            "reduce_only": bool(intent.get("reduce_only")),
            "sl_trigger_px": intent.get("sl_trigger_px"),
        }
        if order["ord_type"] in ("limit", "post_only"):
            self.pending_limit.append(order)
        else:
            self.pending_market.append(order)
        return {"cl_ord_id": order["cl_ord_id"]}

    def take_receipts(self) -> list[dict]:
        """取出本根撮合产生的成交回执（模拟 WS 推送，供策略 on_order 消费）。"""
        out, self._receipts = self._receipts, []
        return out

    def cancel_pending(self) -> int:
        n = len(self.pending_limit)
        self.pending_limit.clear()
        return n

    def market_close_position(self) -> bool:
        if abs(self.pos) < 1e-12:
            return False
        self.queue_order({"side": "sell" if self.pos > 0 else "buy",
                          "sz_base": abs(self.pos), "ord_type": "market", "reduce_only": True})
        return True

    # ---------- 撮合 ----------
    def _fill(self, side: str, px: float, sz: float, reduce_only: bool, ts: int, kind: str,
              cl_ord_id: str | None = None) -> None:
        fee = px * sz * self.fee_rate
        self.fee_total += fee
        signed = sz if side == "buy" else -sz
        realized = 0.0
        if reduce_only or (self.pos != 0 and (self.pos > 0) != (signed > 0)):
            closing = min(abs(self.pos), sz)
            if closing > 0:
                direction = -1 if self.pos > 0 else 1
                realized = (px - self.entry_px) * closing * direction
                self.cash += realized
                self._hold_bars.append(self.bar_count - self._entry_bar)
        self.cash -= fee
        pos_before = self.pos
        self.pos += signed
        if abs(self.pos) < 1e-12:
            self.pos = 0.0
            self.sl_px = None
        elif not reduce_only and abs(pos_before) < 1e-12:
            # 从空仓新开仓：记录开仓价与持仓起始 bar
            self.entry_px = px
            self._entry_bar = self.bar_count
        self.trades.append({
            "ts": ts, "side": side, "px": round(px, 2), "sz": round(sz, 8),
            "fee": round(fee, 4), "realized": round(realized, 4), "kind": kind,
        })
        # 成交回执：限价单成交需回执给策略（网格策略据此反手挂单）
        if cl_ord_id:
            self._receipts.append({
                "cl_ord_id": cl_ord_id, "state": "filled", "side": side,
                "avg_px": round(px, 2), "sz": sz, "fee": round(fee, 4), "ts": ts,
            })

    def match_bar(self, bar: dict) -> None:
        """撮合阶段：市价成交 -> 交易所止损 -> 限价单。信号产生的单在本根之前排队，下一根撮合。"""
        self.bar_count += 1
        o, h, l, c = bar["o"], bar["h"], bar["l"], bar["c"]
        self.last_close = c
        ts = bar["ts"]

        # 1. 上一根收盘信号产生的市价单 -> 本根开盘价 ± 滑点成交
        for od in self.pending_market:
            adj = 1 + self.slippage if od["side"] == "buy" else 1 - self.slippage
            self._fill(od["side"], o * adj, od["sz_base"], od["reduce_only"], ts, "market",
                       cl_ord_id=od.get("cl_ord_id"))
            if od.get("sl_trigger_px"):
                self.sl_px = od["sl_trigger_px"]
        self.pending_market.clear()

        # 2. 交易所侧止损模拟
        if self.sl_px is not None and abs(self.pos) > 1e-12:
            if self.pos > 0 and l <= self.sl_px:
                px = self.sl_px * (1 - self.slippage)
                self._fill("sell", px, abs(self.pos), True, ts, "stop_loss")
                self.pos = 0.0
                self.sl_px = None
            elif self.pos < 0 and h >= self.sl_px:
                px = self.sl_px * (1 + self.slippage)
                self._fill("buy", px, abs(self.pos), True, ts, "stop_loss")
                self.pos = 0.0
                self.sl_px = None

        # 3. 限价单撮合：买单 low<=px，卖单 high>=px，按限价成交
        still_pending = []
        for od in self.pending_limit:
            px = od["px"]
            hit = (od["side"] == "buy" and l <= px) or (od["side"] == "sell" and h >= px)
            if hit:
                self._fill(od["side"], px, od["sz_base"], od["reduce_only"], ts, "limit",
                           cl_ord_id=od.get("cl_ord_id"))
            else:
                still_pending.append(od)
        self.pending_limit = still_pending

    def record_equity(self, bar: dict) -> None:
        """净值记录：本根收盘后（策略信号已产生，未成交单留待下一根）。"""
        c = bar["c"]
        ts = bar["ts"]
        equity = self.cash + self.pos * c
        self.equity_curve.append({"ts": ts, "equity": round(equity, 2)})
        self.bh_curve.append({"ts": ts, "equity": round(self.initial_equity * c / self.equity_curve[0]["equity"], 2) if self.bh_curve else self.initial_equity})
        if self.progress_cb and self.bar_count % 5000 == 0:
            self.progress_cb(self.bar_count, self.total_bars)

    # ---------- 结果 ----------
    def metrics(self, period: str) -> dict:
        eq = [p["equity"] for p in self.equity_curve]
        if not eq:
            return {}
        total_return = (eq[-1] / self.initial_equity - 1) * 100
        ppy = PERIODS_PER_YEAR.get(period, 8760)
        years = len(eq) / ppy
        annual = ((eq[-1] / self.initial_equity) ** (1 / years) - 1) * 100 if years > 0 and eq[-1] > 0 else 0
        peak, max_dd, peak_v = eq[0], 0.0, eq[0]
        for v in eq:
            if v > peak_v:
                peak_v = v
            dd = (peak_v - v) / peak_v * 100
            if dd > max_dd:
                max_dd = dd
        rets = [(eq[i] - eq[i - 1]) / eq[i - 1] for i in range(1, len(eq)) if eq[i - 1] > 0]
        if rets:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / len(rets)
            std = var ** 0.5 or 1e-12
            sharpe = mean / std * (ppy**0.5)
            downside = [r for r in rets if r < 0]
            dstd = (sum(r**2 for r in downside) / len(downside)) ** 0.5 if downside else 1e-12
            sortino = mean / dstd * (ppy**0.5)
        else:
            sharpe = sortino = 0.0
        closes = [t for t in self.trades if t["realized"] != 0]
        wins = [t for t in closes if t["realized"] > 0]
        losses = [t for t in closes if t["realized"] < 0]
        gross_win = sum(t["realized"] for t in wins)
        gross_loss = abs(sum(t["realized"] for t in losses))
        # ---- V2 交易质量指标 ----
        avg_win = gross_win / len(wins) if wins else 0.0
        avg_loss = gross_loss / len(losses) if losses else 0.0
        # 每笔净期望 =（毛盈 - 毛亏 - 总费用）/ 平仓笔数
        net_pnl = gross_win - gross_loss - self.fee_total
        expectancy = net_pnl / len(closes) if closes else 0.0
        max_consec_losses, streak = 0, 0
        for t in closes:  # trades 按时间序
            if t["realized"] < 0:
                streak += 1
                max_consec_losses = max(max_consec_losses, streak)
            else:
                streak = 0
        bh = (self.bh_curve[-1]["equity"] / self.initial_equity - 1) * 100 if self.bh_curve else 0
        return {
            "total_return_pct": round(total_return, 2),
            "buy_hold_pct": round(bh, 2),
            "annual_pct": round(annual, 2),
            "max_dd_pct": round(max_dd, 2),
            "sharpe": round(sharpe, 2),
            "sortino": round(sortino, 2),
            "trade_count": len(closes),
            "win_rate": round(len(wins) / len(closes) * 100, 1) if closes else 0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "expectancy": round(expectancy, 4),          # 每笔净期望（USDT，含费）
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "payoff_ratio": round(avg_win / avg_loss, 2) if avg_loss > 0 else None,  # 盈亏比
            "max_consec_losses": max_consec_losses,
            "fee_total": round(self.fee_total, 2),
            "fee_ratio_pct": round(self.fee_total / self.initial_equity * 100, 3),
            "avg_hold_bars": round(sum(self._hold_bars) / len(self._hold_bars), 1) if self._hold_bars else 0,
            "final_equity": round(eq[-1], 2),
            "monte_carlo": self._monte_carlo(closes),
        }

    def _monte_carlo(self, closes: list[dict], runs: int = 2000) -> dict | None:
        """Monte Carlo：随机重排平仓序列，评估回撤/连亏的分布（防"顺序幸运"）。"""
        if len(closes) < 5:
            return None
        import random
        avg_fee2 = (self.fee_total / max(1, len(self.trades))) * 2   # 开+平两腿
        net = [t["realized"] - avg_fee2 for t in closes]
        rng = random.Random(42)
        dds, streaks = [], []
        for _ in range(runs):
            seq = net[:]
            rng.shuffle(seq)
            eq = self.initial_equity
            peak = eq
            maxdd = 0.0
            streak = worst = 0
            for r in seq:
                eq += r
                if eq > peak:
                    peak = eq
                dd = (peak - eq) / peak * 100 if peak > 0 else 0
                if dd > maxdd:
                    maxdd = dd
                if r < 0:
                    streak += 1
                    worst = max(worst, streak)
                else:
                    streak = 0
            dds.append(maxdd)
            streaks.append(worst)
        dds.sort()
        streaks.sort()
        return {
            "runs": runs,
            "median_max_dd_pct": round(dds[len(dds) // 2], 2),
            "p95_max_dd_pct": round(dds[int(0.95 * len(dds))], 2),
            "p95_max_consec_losses": streaks[int(0.95 * len(streaks))],
        }


def run_backtest(job: dict, progress_cb=None) -> dict:
    """在子进程中执行：直接读本地 bars 库。"""
    from app.strategy.templates import STRATEGY_REGISTRY

    strategy_type = job["strategy_type"]
    cls = STRATEGY_REGISTRY[strategy_type]
    broker = SimBroker(
        fee_rate=float(job.get("fee_rate", 0.0005)),
        slippage=float(job.get("slippage", 0.0005)),
        initial_equity=float(job.get("initial_equity", 10000)),
        progress_cb=progress_cb,
    )
    ctx = SimContext(broker)
    strategy = cls(job.get("params") or {}, ctx)
    broker.strategy = strategy

    bars = db.query(
        "SELECT open_time, o,h,l,c FROM bars WHERE inst_id=? AND period=? AND open_time>=? AND open_time<=?"
        " ORDER BY open_time ASC",
        (job["inst_id"], job["period"], job["start_ms"], job["end_ms"]),
    )
    broker.total_bars = len(bars)
    if len(bars) < 10:
        raise ValueError(f"历史数据不足（仅 {len(bars)} 根），请先下载历史行情")

    import asyncio

    async def _run():
        await strategy.on_start()
        if hasattr(strategy, "place_initial_orders"):
            await strategy.place_initial_orders()
        for row in bars:
            bar = {"ts": row["open_time"], "o": row["o"], "h": row["h"],
                   "l": row["l"], "c": row["c"], "vol": 0}
            # 1) 撮合上一根信号产生的挂单；2) 收盘价交给策略产生新信号；3) 成交回执给策略；
            # 4) 记录净值。与实盘事件顺序一致：先成交后信号。
            broker.match_bar(bar)
            await strategy.on_bar(bar)
            for receipt in broker.take_receipts():
                await strategy.on_order(receipt)
            broker.record_equity(bar)

    asyncio.run(_run())

    return {
        "metrics": broker.metrics(job["period"]),
        "equity": broker.equity_curve,
        "bh": broker.bh_curve,
        "trades": broker.trades[-500:],
        "caveats": [
            "资金费率未模拟：SWAP 标的回测收益会被高估（现货标的不适用）",
            "滑点为固定比例近似，未模拟盘口深度与极端行情恶化",
            "SPOT 网格回测允许负仓位，与实盘 '无余额拒单' 行为不一致，总收益可能失真",
        ],
    }
