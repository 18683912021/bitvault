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
        # 修复（venue 分账基线）：清掉整改期两路权益混用产生的脏基线（peak=10000/derived 100%），
        # 保留 halted 状态（用户人工确认后再恢复）；基线将在第一次真实权益事件时重建。
        if "peak_equity" in self.state or "day_start_equity" in self.state:
            log.warning("检测到旧版基线（paper/okx 混用），已重置：%s", self.state)
            self.state.pop("peak_equity", None)
            self.state.pop("day_start_equity", None)
            db.set_setting("risk_state", self.state)
        # 最近一次权益（分 venue），用于"大额出入金"检测（单次骤降 >20% → 基线重置，不误熔断）
        self._last_equity: dict[str, float] = {}
        self._order_ts: list[float] = []
        self.oms = None        # 注入
        self.manager = None    # 注入
        self.account = None    # 注入
        self.env = "none"      # 未连接 Key 前为 none；连接 demo/live Key 后由 Services 更新
        self._breaker_lock = asyncio.Lock()
        # 生效的杠杆上限：以风控中心 risk_rules.leverage_cap 为准（可在页面编辑），
        # 代码常量 config.LEVERAGE_CAP 只作兜底。更新规则后 load_rules() 热生效。
        self.lever_cap: int = config.LEVERAGE_CAP
        self.load_rules()

    def load_rules(self) -> None:
        self.rules = {r["type"]: {"params": json.loads(r["params_json"]), "enabled": bool(r["enabled"])}
                      for r in db.query("SELECT * FROM risk_rules")}
        try:
            cap = int(self.rules.get("leverage_cap", {}).get("params", {}).get("lever", config.LEVERAGE_CAP) or config.LEVERAGE_CAP)
            self.lever_cap = max(1, min(config.LEVERAGE_CAP, cap))
        except Exception:
            self.lever_cap = config.LEVERAGE_CAP

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
        if not config.InstrumentRegistry.contains(inst_id):
            db.add_risk_event("whitelist", "error", "blocked", {"inst_id": inst_id})
            raise RiskBlocked(f"标的 {inst_id} 不在支持集内")

        # 全局熔断状态：只允许减仓单
        if self.state.get("halted") and not reduce_only:
            raise RiskBlocked(f"风控熔断中，禁止开仓：{self.state.get('halt_reason')}")

        # 杠杆上限强制执行（以风控中心可配风险规则为准）
        if leverage > self.lever_cap and not reduce_only:
            db.add_risk_event("leverage_cap", "error", "blocked",
                              {"leverage": leverage, "cap": self.lever_cap})
            raise RiskBlocked(f"杠杆 {leverage}x 超过上限 {self.lever_cap}x")

        r = self._rule("order_rate_limit")
        if r and r["enabled"] and venue == "okx" and not reduce_only:
            # P1-8：减仓不受下单频率限制（紧急/连续平仓不应被限频拦住）
            now = time.monotonic()
            self._order_ts = [t for t in self._order_ts if now - t < 1.0]
            if len(self._order_ts) >= r["params"]["per_sec"]:
                db.add_risk_event("order_rate_limit", "warning", "blocked", {"per_sec": r["params"]["per_sec"]})
                raise RiskBlocked("下单频率超限")
            self._order_ts.append(now)

        r = self._rule("max_order_notional")
        if r and r["enabled"] and not reduce_only and notional > float(r["params"]["max_usdt"]):
            # P1-8：减仓不按"开仓单笔限额"拦（仓位本已存在，平仓不应因额度被拒）
            db.add_risk_event("max_order_notional", "warning", "blocked",
                              {"notional": round(notional, 2), "limit": r["params"]["max_usdt"]})
            raise RiskBlocked(f"单笔金额 {notional:.2f} USDT 超过上限 {r['params']['max_usdt']}")

        r = self._rule("max_position_pct")
        if r and r["enabled"] and not reduce_only and (self.account or self.paper):
            if self.account:
                equity = self.account.equity()
                positions = self.account.positions
            else:
                psum = self.paper.summary() if self.paper else {}
                equity = float(psum.get("equity") or 0)
                positions = psum.get("positions") or []
            if equity > 0:
                pos_notional = 0.0
                for p in positions:
                    # SWAP 持仓 notionalUsd 为美元名义价值；现货按 数量×市价
                    pos_notional += float(p.get("notionalUsd") or 0) or abs(
                        float(p.get("pos") or p.get("sz") or 0) * (
                            float(p.get("markPx") or p.get("avgPx") or p.get("entry_px") or 0)))
                if (pos_notional + notional) / equity * 100 > float(r["params"]["pct"]):
                    db.add_risk_event("max_position_pct", "warning", "blocked",
                                      {"pos_notional": round(pos_notional, 2), "equity": round(equity, 2)})
                    raise RiskBlocked("仓位占比超过上限")

    # ================= 事中监控 =================
    def _trigger_breaker(self, reason: str) -> None:
        """有事件循环 → 异步熔断（含平仓）；无循环（CLI/单测）→ 保守直标 halted（fail-closed）。"""
        try:
            asyncio.get_running_loop().create_task(self.circuit_breaker(reason))
        except RuntimeError:
            self.state["halted"] = True
            self.state["halt_reason"] = reason
            db.set_setting("risk_state", self.state)
            log.error("风控熔断(无事件循环，保守直标): %s", reason)

    def on_equity(self, equity: float, avail: float | None = None, venue: str = "paper") -> None:
        """P0-4 接通：账户权益事件（OKX 轮询/WS 与 Paper 决策共用）触发自动风控：
        日内亏损熔断 / 最大回撤熔断（risk_rules 热生效）；auto_reduce_liq 保守降档。

        关键：基线（峰值/日内起点）严格按 venue 分离（paper / okx），两路权益绝不混用，
        否则空账户(0.003)会被纸面初始资金(10000)判定为"回撤 100%"。
        大额出金（单次骤降 >20%）→ 该 venue 基线重置（视为资金调整，不熔断）。"""
        if equity <= 0:
            return
        pk = f"{venue}_peak_equity"
        dk = f"{venue}_day_start_equity"
        day_key = time.strftime("%Y-%m-%d", time.gmtime(int(time.time() * 1000) / 1000))
        if self.state.get(f"{venue}_day_key") != day_key:
            self.state[f"{venue}_day_key"] = day_key
            self.state[dk] = equity
        # 大额出入金检测：单次下降 >20% → 重置该 venue 基线（不按"回撤"误熔断）
        last = self._last_equity.get(venue)
        if last and last > 0 and equity < last * 0.80:
            log.warning("检测到 %s 大额权益变动（%.4f → %.4f），重置基线（按出金处理，不触发熔断）",
                        venue, last, equity)
            self.state[pk] = equity
            self.state[dk] = equity
            db.add_risk_event("baseline_reset", "warning", "reset",
                              {"venue": venue, "prev": round(last, 4), "now": round(equity, 4)})
        self._last_equity[venue] = equity
        peak = self.state.get(pk)
        if not peak or equity > peak:
            self.state[pk] = equity
            peak = equity
        if avail is not None and equity > 0:
            ratio = avail / equity
            rl = self._rule("auto_reduce_liq")
            if rl and rl["enabled"]:
                warn = float(rl["params"].get("margin_ratio_warn", 0.10))
                danger = float(rl["params"].get("margin_ratio_danger", 0.05))
                self.state["reduce_hint"] = ratio <= warn
                if ratio <= danger and not self.state.get("halted"):
                    self._trigger_breaker(
                        f"可用资金占比 {ratio:.1%} 低于危险阈值 {danger:.1%}（auto_reduce_liq）")
            else:
                self.state["reduce_hint"] = False
        else:
            self.state["reduce_hint"] = False
        # 峰值只按 venue 键维护（全局旧键已废弃，防止跨 venue 100% 误熔断）

        r = self._rule("max_daily_loss_pct")
        if r and r["enabled"] and not self.state.get("halted"):
            start = self.state.get(dk) or equity
            if start > 0 and (start - equity) / start * 100 >= float(r["params"]["pct"]):
                self._trigger_breaker(
                    f"日内亏损 {(start - equity) / start * 100:.2f}% 达到熔断阈值 {r['params']['pct']}%（{venue}）")
        r = self._rule("max_drawdown_pct")
        if r and r["enabled"] and not self.state.get("halted") and peak and peak > 0:
            if (peak - equity) / peak * 100 >= float(r["params"]["pct"]):
                self._trigger_breaker(
                    f"账户回撤 {(peak - equity) / peak * 100:.2f}% 达到熔断阈值 {r['params']['pct']}%（{venue}）")
        db.set_setting("risk_state", self.state)

    def on_account(self, payload: dict) -> None:
        """兼容入口（OKX AccountService.refresh 回调）：计算权益与可用资金后走 on_equity。"""
        summary = (payload or {}).get("summary") or {}
        equity = float(summary.get("totalEq") or 0)
        if equity <= 0:
            return
        avail = 0.0
        for d in summary.get("details") or []:
            if (d.get("ccy") or "").upper() == "USDT":
                avail += float(d.get("availBal") or d.get("cashBal") or 0)
        if avail <= 0:
            avail = equity
        self.on_equity(equity, avail=avail, venue="okx")

    def status(self) -> dict:
        equity = self.account.equity() if self.account else 0
        start = self.state.get("okx_day_start_equity") or 0
        peak = self.state.get("okx_peak_equity") or 0
        return {
            "halted": self.state.get("halted", False),
            "halt_reason": self.state.get("halt_reason", ""),
            "reduce_hint": bool(self.state.get("reduce_hint", False)),
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
