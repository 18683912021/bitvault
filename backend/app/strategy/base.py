"""策略基类：回测与实盘共用同一接口（on_bar / on_tick / on_order），保证行为一致。"""
from __future__ import annotations

from typing import Any


class StrategyContext:
    """策略与外部世界的唯一交互通道。实盘由 Manager 注入，回测由 SimBroker 注入。"""

    instance_id: int | None = None
    mode: str = "backtest"

    async def submit_order(self, intent: dict) -> Any:
        """intent: side, sz_base, ord_type(market/limit/post_only), px,
        reduce_only, sl_trigger_px, tp_trigger_px"""
        raise NotImplementedError

    def last_price(self) -> float:
        raise NotImplementedError

    def log(self, msg: str, level: str = "info") -> None:
        raise NotImplementedError

    def save_state(self) -> None:
        raise NotImplementedError


class BaseStrategy:
    type: str = "base"
    label: str = "策略基类"
    params_schema: list[dict] = []

    def __init__(self, params: dict, ctx: StrategyContext):
        self.p: dict = {s["key"]: s.get("default") for s in self.params_schema}
        self.p.update(params or {})
        self.ctx = ctx
        self.state: dict = {}

    # ---- 生命周期 ----
    async def on_start(self) -> None:
        pass

    async def on_stop(self) -> None:
        pass

    # ---- 事件 ----
    async def on_bar(self, bar: dict) -> None:
        pass

    async def on_tick(self, tick: dict) -> None:
        pass

    async def on_order(self, order: dict) -> None:
        pass

    # ---- 便捷下单 ----
    async def market_order(self, side: str, sz_base: float, reduce_only: bool = False,
                           sl_trigger_px: float | None = None) -> Any:
        return await self.ctx.submit_order({
            "side": side, "sz_base": sz_base, "ord_type": "market",
            "reduce_only": reduce_only, "sl_trigger_px": sl_trigger_px,
        })

    async def limit_order(self, side: str, px: float, sz_base: float,
                          reduce_only: bool = False, post_only: bool = True) -> Any:
        return await self.ctx.submit_order({
            "side": side, "sz_base": sz_base, "ord_type": "post_only" if post_only else "limit",
            "px": px, "reduce_only": reduce_only,
        })

    def describe(self) -> dict:
        return {"type": self.type, "label": self.label, "params": self.p}
