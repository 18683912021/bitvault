"""风控引擎：事前拦截 / 事中熔断（含交易所侧状态持久化，重启不可绕过）/ 事后审计。

红线 R4：所有订单（策略+手动）必须经过 check_pretrade。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from app.core import event_bus
from app import db, config

log = logging.getLogger("bitvault.risk")


class RiskBlocked(Exception):
    pass


class RiskEngine:
    def __init__(self):
        self.rules: dict[str, dict] = {}
        self.state: dict = db.get_setting("risk_state", {}) or {}
        self._order_ts: list[float] = []
        self.oms = None        # 注入
        self.manager = None    # 注入
        self.account = None    # 注入
        self.env = "none"      # 未连接 Key 前为 none；连接 demo/live Key 后由 Services 更新
        self._breaker_lock = asyncio.Lock()
        self.load_rules()

    def load_rules(self) -> None:
        self.rules = {r["type"]: {"params": json.loads(r["params_json"]), "enabled": bool(r["enabled"])}
                      for r in db.query("SELECT * FROM risk_rules")}

    def update_rule(self, rule_type: str, params: dict, enabled: bool = True) -> None:
        db.execute(
            "UPDATE risk_rules SET params_json=?, enabled=? WHERE type=?",
            (json.dumps(params), int(enabled), rule_type),
        )
        self.load_rules()
        db.add_audit("user", "risk_rule_update", {"type": rule_type, "params": params}, "ok")
        event_bus.publish("risk", {"event": "rule_updated", "type": rule_type})

    def _rule(self, rule_type: str):
        return self.rules.get(rule_type)

    # ================= 事前拦截 =================
    def check_pretrade(
        self, inst_id: str, side: str, sz: float, px: float | None,
        notional: float, reduce_only: bool, source: str, instance_id: int | None = None,
        venue: str = "okx", leverage: int = 1,
    ) -> None:
        """抛出 RiskBlocked 表示拦截。venue=paper 时跳过下单频率限制（本地撮合无交易所配额）。"""
        if inst_id not in config.INSTRUMENT_WHITELIST:
            db.add_risk_event("whitelist", "error", "blocked", {"inst_id": inst_id})
            raise RiskBlocked(f"标的 {inst_id} 不在白名单内")

        # 全局熔断状态：只允许减仓单
        if self.state.get("halted") and not reduce_only:
            raise RiskBlocked(f"风控熔断中，禁止开仓：{self.state.get('halt_reason')}")

        # 杠杆上限强制执行（config.LEVERAGE_CAP）
        if leverage > config.LEVERAGE_CAP and not reduce_only:
            db.add_risk_event("leverage_cap", "error", "blocked",
                              {"leverage": leverage, "cap": config.LEVERAGE_CAP})
            raise RiskBlocked(f"杠杆 {leverage}x 超过上限 {config.LEVERAGE_CAP}x")

        r = self._rule("order_rate_limit")
        if r and r["enabled"] and venue == "okx":
            now = time.monotonic()
            self._order_ts = [t for t in self._order_ts if now - t < 1.0]
            if len(self._order_ts) >= r["params"]["per_sec"]:
                db.add_risk_event("order_rate_limit", "warning", "blocked", {"per_sec": r["params"]["per_sec"]})
                raise RiskBlocked("下单频率超限")
            self._order_ts.append(now)

        r = self._rule("max_order_notional")
        if r and r["enabled"] and notional > float(r["params"]["max_usdt"]):
            db.add_risk_event("max_order_notional", "warning", "blocked",
                              {"notional": round(notional, 2), "limit": r["params"]["max_usdt"]})
            raise RiskBlocked(f"单笔金额 {notional:.2f} USDT 超过上限 {r['params']['max_usdt']}")

        r = self._rule("max_position_pct")
        if r and r["enabled"] and not reduce_only and self.account:
            equity = self.account.equity()
            if equity > 0:
                pos_notional = 0.0
                for p in self.account.positions:
                    # SWAP 持仓 notionalUsd 为美元名义价值；现货按 数量×市价
                    pos_notional += float(p.get("notionalUsd") or 0) or abs(p["pos"]) * (p.get("markPx") or p.get("avgPx") or 0)
                if (pos_notional + notional) / equity * 100 > float(r["params"]["pct"]):
                    db.add_risk_event("max_position_pct", "warning", "blocked",
                                      {"pos_notional": round(pos_notional, 2), "equity": round(equity, 2)})
                    raise RiskBlocked("仓位占比超过上限")

    # ================= 事中监控 =================
    def on_account(self, payload: dict) -> None:
        equity = float(payload["summary"].get("totalEq") or 0)
        if equity <= 0:
            return
        now_ms = int(time.time() * 1000)
        day_key = time.strftime("%Y-%m-%d", time.gmtime(now_ms / 1000))
        if self.state.get("day_key") != day_key:
            self.state["day_key"] = day_key
            self.state["day_start_equity"] = equity
        peak = self.state.get("peak_equity")
        if not peak or equity > peak:
            self.state["peak_equity"] = equity
            peak = equity

        r = self._rule("max_daily_loss_pct")
        if r and r["enabled"] and not self.state.get("halted"):
            start = self.state.get("day_start_equity") or equity
            if start > 0 and (start - equity) / start * 100 >= float(r["params"]["pct"]):
                asyncio.get_event_loop().create_task(
                    self.circuit_breaker(
                        f"日内亏损 {(start - equity) / start * 100:.2f}% 达到熔断阈值 {r['params']['pct']}%"
                    )
                )
        r = self._rule("max_drawdown_pct")
        if r and r["enabled"] and not self.state.get("halted") and peak and peak > 0:
            if (peak - equity) / peak * 100 >= float(r["params"]["pct"]):
                asyncio.get_event_loop().create_task(
                    self.circuit_breaker(
                        f"账户回撤 {(peak - equity) / peak * 100:.2f}% 达到熔断阈值 {r['params']['pct']}%"
                    )
                )
        db.set_setting("risk_state", self.state)

    def status(self) -> dict:
        equity = self.account.equity() if self.account else 0
        start = self.state.get("day_start_equity") or 0
        peak = self.state.get("peak_equity") or 0
        return {
            "halted": self.state.get("halted", False),
            "halt_reason": self.state.get("halt_reason", ""),
            "day_key": self.state.get("day_key"),
            "day_start_equity": start,
            "daily_pnl_pct": round((equity - start) / start * 100, 2) if start else 0,
            "peak_equity": peak,
            "drawdown_pct": round((peak - equity) / peak * 100, 2) if peak else 0,
            "rules": self.rules,
            "env": self.env,
        }

    # ================= 熔断与 Kill Switch =================
    async def circuit_breaker(self, reason: str) -> dict:
        async with self._breaker_lock:
            if self.state.get("halted"):
                return {"already_halted": True}
            log.error("风控熔断: %s", reason)
            self.state["halted"] = True
            self.state["halt_reason"] = reason
            db.set_setting("risk_state", self.state)
            report = await self._flatten_all(reason)
            db.add_risk_event("circuit_breaker", "error", "liquidated+halted",
                              {"reason": reason, "report": report})
            event_bus.publish("risk", {"event": "circuit_breaker", "reason": reason, "report": report})
            if self.manager:
                await self.manager.halt_all(reason)
            return report

    async def kill_switch(self, actor: str = "user") -> dict:
        db.add_audit(actor, "kill_switch", {}, "executing")
        report = await self._flatten_all("Kill Switch 手动触发")
        self.state["halted"] = True
        self.state["halt_reason"] = "Kill Switch 手动触发"
        db.set_setting("risk_state", self.state)
        db.add_risk_event("kill_switch", "error", "liquidated+halted", {"report": report})
        event_bus.publish("risk", {"event": "kill_switch", "report": report})
        if self.manager:
            await self.manager.halt_all("Kill Switch 手动触发")
        db.add_audit(actor, "kill_switch", {}, json.dumps(report, ensure_ascii=False))
        return report

    async def _flatten_all(self, reason: str) -> dict:
        """撤全部挂单 + 全平合约持仓；单步失败重试 3 次（红线 M6-6）。"""
        report = {"canceled": 0, "closed": 0, "errors": [], "reason": reason}
        if not self.oms:
            report["errors"].append("OMS 未连接")
            return report
        for attempt in range(3):
            try:
                report["canceled"] += await self.oms.cancel_all_orders()
            except Exception as e:
                report["errors"].append(f"撤单: {e}")
            try:
                closed = await self.oms.close_all_positions()
                report["closed"] += closed
            except Exception as e:
                report["errors"].append(f"平仓: {e}")
            if not report["errors"]:
                break
            await asyncio.sleep(1.0)
        return report

    async def resume(self, actor: str = "user") -> None:
        """人工解除熔断（需在 UI 显式操作）。"""
        self.state["halted"] = False
        self.state["halt_reason"] = ""
        self.state["day_start_equity"] = self.account.equity() if self.account else None
        db.set_setting("risk_state", self.state)
        db.add_audit(actor, "risk_resume", {}, "ok")
        event_bus.publish("risk", {"event": "resumed"})
