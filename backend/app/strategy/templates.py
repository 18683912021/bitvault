"""内置策略模板：双均线趋势 / 网格交易。回测与实盘共用本实现。"""
from __future__ import annotations

from app.strategy.base import BaseStrategy


class MaCrossStrategy(BaseStrategy):
    """双均线：金叉开多、死叉平仓（可选拱做空）。止损经交易所附带止损单（红线 R6）。"""
    type = "ma_cross"
    label = "双均线趋势"
    params_schema = [
        {"key": "fast", "label": "快线周期", "type": "int", "default": 5, "min": 2, "max": 200},
        {"key": "slow", "label": "慢线周期", "type": "int", "default": 20, "min": 3, "max": 500},
        {"key": "direction", "label": "方向", "type": "select", "default": "long_only",
         "options": [{"value": "long_only", "label": "只多"}, {"value": "long_short", "label": "多空"}]},
        {"key": "position_usdt", "label": "单笔仓位(USDT)", "type": "float", "default": 100, "min": 10},
        {"key": "sl_pct", "label": "止损比例%", "type": "float", "default": 2.0, "min": 0.2, "max": 20},
    ]

    async def on_start(self) -> None:
        self.state.setdefault("closes", [])
        self.state.setdefault("pos_side", None)   # None / long / short
        self.state.setdefault("open_sz", 0.0)
        self.state.setdefault("sl_px", None)
        self.state.setdefault("prev_diff", None)
        self.ctx.log(f"双均线启动 fast={self.p['fast']} slow={self.p['slow']} 方向={self.p['direction']}")

    async def on_bar(self, bar: dict) -> None:
        closes: list = self.state["closes"]
        closes.append(bar["c"])
        del closes[:-(self.p["slow"] + 1)]
        if len(closes) < self.p["slow"]:
            return
        fast = sum(closes[-self.p["fast"]:]) / self.p["fast"]
        slow = sum(closes[-self.p["slow"]:]) / self.p["slow"]
        diff = fast - slow
        prev = self.state.get("prev_diff")
        self.state["prev_diff"] = diff
        if prev is None:
            return

        px = bar["c"]
        # 金叉
        if prev <= 0 < diff:
            if self.state["pos_side"] == "short":
                await self._close_short(px, reason="金叉平空")
            if self.state["pos_side"] is None:
                await self._open_long(px, reason="金叉开多")
        # 死叉
        elif prev >= 0 > diff:
            if self.state["pos_side"] == "long":
                await self._close_long(px, reason="死叉平多")
            elif self.state["pos_side"] is None and self.p["direction"] == "long_short":
                await self._open_short(px, reason="死叉开空")

        # 本地二级止损兜底（交易所侧为主）
        sl = self.state.get("sl_px")
        if self.state["pos_side"] == "long" and sl and bar["l"] <= sl:
            await self._close_long(sl, reason="本地止损兜底")
        elif self.state["pos_side"] == "short" and sl and bar["h"] >= sl:
            await self._close_short(sl, reason="本地止损兜底")
        self.ctx.save_state()

    async def _open_long(self, px: float, reason: str) -> None:
        sz = float(self.p["position_usdt"]) / px
        sl = px * (1 - float(self.p["sl_pct"]) / 100)
        try:
            await self.market_order("buy", sz, sl_trigger_px=sl)
            self.state["pos_side"] = "long"
            self.state["open_sz"] = sz
            self.state["sl_px"] = sl
            self.state["entry_px"] = px
            self.ctx.log(f"{reason}: 买入 {sz:.6f} BTC @≈{px:.1f} 交易所止损={sl:.1f}")
        except Exception as e:
            self.ctx.log(f"{reason} 失败: {e}", "error")

    async def _open_short(self, px: float, reason: str) -> None:
        sz = float(self.p["position_usdt"]) / px
        sl = px * (1 + float(self.p["sl_pct"]) / 100)
        try:
            await self.market_order("sell", sz, sl_trigger_px=sl)
            self.state["pos_side"] = "short"
            self.state["open_sz"] = sz
            self.state["sl_px"] = sl
            self.state["entry_px"] = px
            self.ctx.log(f"{reason}: 卖出 {sz:.6f} BTC @≈{px:.1f} 交易所止损={sl:.1f}")
        except Exception as e:
            self.ctx.log(f"{reason} 失败: {e}", "error")

    async def _close_long(self, px: float, reason: str) -> None:
        try:
            await self.market_order("sell", self.state["open_sz"], reduce_only=True)
            self.ctx.log(f"{reason}: 平多 @≈{px:.1f}")
        except Exception as e:
            self.ctx.log(f"{reason} 失败: {e}", "error")
            return
        self.state.update({"pos_side": None, "open_sz": 0.0, "sl_px": None})

    async def _close_short(self, px: float, reason: str) -> None:
        try:
            await self.market_order("buy", self.state["open_sz"], reduce_only=True)
            self.ctx.log(f"{reason}: 平空 @≈{px:.1f}")
        except Exception as e:
            self.ctx.log(f"{reason} 失败: {e}", "error")
            return
        self.state.update({"pos_side": None, "open_sz": 0.0, "sl_px": None})


class GridStrategy(BaseStrategy):
    """网格交易：区间内低买高卖，成交后反手挂相邻格；突破边界行为可配（停止/清仓/跟随）。"""
    type = "grid"
    label = "网格交易"
    params_schema = [
        {"key": "upper", "label": "网格上轨", "type": "float", "default": 0, "min": 0},
        {"key": "lower", "label": "网格下轨", "type": "float", "default": 0, "min": 0},
        {"key": "grids", "label": "格数", "type": "int", "default": 10, "min": 2, "max": 50},
        {"key": "per_grid_usdt", "label": "每格金额(USDT)", "type": "float", "default": 50, "min": 5},
        {"key": "breakout", "label": "突破边界行为", "type": "select", "default": "stop",
         "options": [{"value": "stop", "label": "停止新单"},
                     {"value": "clear", "label": "清仓停止"},
                     {"value": "follow", "label": "跟随重设"}]},
    ]

    async def on_start(self) -> None:
        upper = float(self.p["upper"])
        lower = float(self.p["lower"])
        if upper <= lower or lower <= 0:
            self.ctx.log("网格参数非法：需要 upper > lower > 0", "error")
            raise ValueError("网格参数非法")
        self.state.update({
            "levels": [],        # 由 manager 设置网格中心后填充
            "open_orders": {},   # level_str -> cl_ord_id
            "filled_buys": 0, "filled_sells": 0, "realized_usdt": 0.0,
        })
        center = self.ctx.last_price() or (upper + lower) / 2
        self._build_grid(center)
        self.ctx.log(f"网格启动 [{lower:.1f}, {upper:.1f}] {self.p['grids']} 格，中心 {center:.1f}")

    def _build_grid(self, center: float) -> None:
        n = int(self.p["grids"])
        upper, lower = float(self.p["upper"]), float(self.p["lower"])
        levels = [lower + (upper - lower) * i / n for i in range(n + 1)]
        self.state["levels"] = levels
        self.state["center_idx"] = min(
            range(len(levels)), key=lambda i: abs(levels[i] - center)
        )

    async def place_initial_orders(self) -> None:
        levels = self.state["levels"]
        ci = self.state["center_idx"]
        px = self.ctx.last_price()
        for i, lv in enumerate(levels):
            if i == ci:
                continue
            side = "buy" if lv < px else "sell"
            await self._place_level_order(i, side)

    def _level_sz(self, px: float) -> float:
        return float(self.p["per_grid_usdt"]) / px

    async def _place_level_order(self, idx: int, side: str) -> None:
        levels = self.state["levels"]
        if idx < 0 or idx >= len(levels):
            return
        key = str(idx)
        if key in self.state["open_orders"]:
            return
        px = levels[idx]
        try:
            res = await self.limit_order(side, px, self._level_sz(px))
            if res and res.get("cl_ord_id"):
                self.state["open_orders"][key] = res["cl_ord_id"]
        except Exception as e:
            self.ctx.log(f"挂单失败 L{idx} {side} @{px:.1f}: {e}", "error")

    async def on_order(self, order: dict) -> None:
        if order.get("state") not in ("filled", "partially_canceled", "canceled"):
            return
        cl = order.get("cl_ord_id")
        idx = next((k for k, v in self.state["open_orders"].items() if v == cl), None)
        if idx is None:
            return
        del self.state["open_orders"][idx]
        i = int(idx)
        levels = self.state["levels"]
        if order["state"] == "filled":
            px = float(order.get("avg_px") or levels[i])
            if order["side"] == "buy":
                self.state["filled_buys"] += 1
                # 买入成交 -> 上一格挂卖单止盈
                self.ctx.log(f"L{i} 买入成交 @ {px:.1f} -> 挂卖 L{i+1}")
                await self._place_level_order(i + 1, "sell")
            else:
                self.state["filled_sells"] += 1
                self.state["realized_usdt"] += self._grid_profit(px)
                self.ctx.log(f"L{i} 卖出成交 @ {px:.1f} -> 挂买 L{i-1}")
                await self._place_level_order(i - 1, "buy")
        self.ctx.save_state()

    def _grid_profit(self, sell_px: float) -> float:
        # 近似：每格利润 = 每格金额 × 格宽比例
        levels = self.state["levels"]
        if len(levels) < 2:
            return 0.0
        step_pct = (levels[1] - levels[0]) / levels[0]
        return float(self.p["per_grid_usdt"]) * step_pct

    async def on_bar(self, bar: dict) -> None:
        px = bar["c"]
        upper, lower = float(self.p["upper"]), float(self.p["lower"])
        if lower < px < upper:
            return
        behavior = self.p["breakout"]
        side_txt = "上破" if px >= upper else "下破"
        self.ctx.log(f"{side_txt}边界 px={px:.1f}，行为={behavior}", "warning")
        if behavior == "stop":
            await self.ctx.cancel_instance_orders()  # 撤所有挂单，持仓保留
        elif behavior == "clear":
            await self.ctx.cancel_instance_orders()
            await self.ctx.close_instance_position()
        elif behavior == "follow":
            await self.ctx.cancel_instance_orders()
            self._build_grid(px)
            await self.place_initial_orders()
        self.ctx.save_state()

    async def on_stop(self) -> None:
        await self.ctx.cancel_instance_orders()
        self.ctx.log("网格停止：已撤销全部网格挂单（持仓保留，请人工处理或在 UI 一键平仓）", "warning")


STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    MaCrossStrategy.type: MaCrossStrategy,
    GridStrategy.type: GridStrategy,
}
