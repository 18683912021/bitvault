"""策略实例管理器：生命周期、事件分发、状态持久化、重启恢复（红线 R7）。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback

from app import db
from app.core import event_bus

log = logging.getLogger("bitvault.strategy")


class InstanceContext:
    """每个实例一个 ctx，打通 策略 -> OMS（必经风控）。paper 模式路由到本地模拟引擎。"""

    def __init__(self, manager: "StrategyManager", instance_id: int, mode: str):
        self.manager = manager
        self.instance_id = instance_id
        self.mode = mode

    @property
    def oms(self):
        if self.mode == "paper" and self.manager.paper_oms:
            return self.manager.paper_oms
        return self.manager.oms

    async def submit_order(self, intent: dict) -> dict:
        intent.setdefault("inst_id", self.manager.instance_inst_id(self.instance_id))
        intent["instance_id"] = self.instance_id
        intent["source"] = f"strategy:{self.instance_id}"
        return await self.oms.place_intent(intent)

    def last_price(self) -> float:
        return self.manager.data.last_price(self.manager.instance_inst_id(self.instance_id))

    def log(self, msg: str, level: str = "info") -> None:
        self.manager.instance_log(self.instance_id, msg, level)

    def save_state(self) -> None:
        self.manager.persist_state(self.instance_id)

    async def cancel_instance_orders(self) -> int:
        return await self.oms.cancel_instance_orders(self.instance_id)

    async def close_instance_position(self) -> bool:
        return await self.manager.close_instance_position(self.instance_id)


class StrategyManager:
    def __init__(self, oms, risk, account, data, paper_oms=None):
        self.oms = oms
        self.paper_oms = paper_oms   # 本地模拟撮合引擎（paper 模式专用）
        self.risk = risk
        self.account = account
        self.data = data
        self.runtimes: dict[int, dict] = {}   # instance_id -> {"strategy": obj, "ctx": ctx}
        self.logs: dict[int, list] = {}
        risk.manager = self
        self._bar_q = event_bus.subscribe(["bar", "instance_order"])
        self._dispatch_task = None

    async def start(self) -> None:
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        await self._recover_instances()

    async def stop(self) -> None:
        if self._dispatch_task:
            self._dispatch_task.cancel()

    # ---------- 事件分发 ----------
    async def _dispatch_loop(self) -> None:
        while True:
            topic, data, _ = await self._bar_q.get()
            try:
                if topic == "bar":
                    for iid, rt in list(self.runtimes.items()):
                        if rt.get("paused"):
                            continue
                        st = db.query_one("SELECT * FROM strategy_instances WHERE id=?", (iid,))
                        if not st or not st["status"].startswith("running"):
                            continue
                        period = json.loads(st["params_json"]).get("period", "1m")
                        if data["inst_id"] == st["inst_id"] and data["period"] == period:
                            try:
                                await rt["strategy"].on_bar(data)
                            except Exception as e:
                                self.instance_log(iid, f"on_bar 异常: {e}\n{traceback.format_exc()[-500:]}", "error")
                elif topic == "instance_order":
                    iid = data.get("instance_id")
                    rt = self.runtimes.get(iid)
                    if rt:
                        try:
                            await rt["strategy"].on_order(data)
                        except Exception as e:
                            self.instance_log(iid, f"on_order 异常: {e}", "error")
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.error("事件分发异常: %s", e)

    # ---------- CRUD ----------
    def create(self, strategy_type: str, name: str, inst_id: str, mode: str,
               params: dict, period: str) -> int:
        sid = db.execute(
            "INSERT INTO strategies (name, type, params_json, created_at) VALUES (?,?,?,?)",
            (name, strategy_type, json.dumps(params), int(time.time() * 1000)),
        )
        full_params = dict(params)
        full_params["period"] = period
        iid = db.execute(
            "INSERT INTO strategy_instances (strategy_id, name, inst_id, mode, status, params_json, state_json)"
            " VALUES (?,?,?,?, 'stopped', ?, '{}')",
            (sid, name, inst_id, mode, json.dumps(full_params)),
        )
        db.add_audit("user", "strategy_create", {"type": strategy_type, "name": name, "mode": mode})
        return iid

    def delete(self, instance_id: int) -> None:
        row = db.query_one("SELECT * FROM strategy_instances WHERE id=?", (instance_id,))
        if row and row["status"].startswith("running"):
            raise ValueError("实例运行中，请先停止")
        if row:
            db.execute("DELETE FROM strategy_instances WHERE id=?", (instance_id,))
            db.execute("DELETE FROM strategies WHERE id=?", (row["strategy_id"],))

    def instance_inst_id(self, instance_id: int) -> str:
        row = db.query_one("SELECT inst_id FROM strategy_instances WHERE id=?", (instance_id,))
        return row["inst_id"] if row else ""

    # ---------- 生命周期 ----------
    async def start_instance(self, instance_id: int, confirm: bool = False) -> dict:
        from app.strategy.templates import STRATEGY_REGISTRY
        row = db.query_one("SELECT * FROM strategy_instances WHERE id=?", (instance_id,))
        if not row:
            raise ValueError("实例不存在")
        if row["status"].startswith("running"):
            raise ValueError("实例已在运行")
        if row["mode"] != "paper" and self.risk.env == "none":
            raise ValueError("请先在系统设置中配置并激活 API Key，再启动策略实例（本地模拟模式无需 Key）")
        if row["mode"] == "live":
            if not confirm:
                raise ValueError("实盘启动需人工确认（勾选确认后重试）")
            if self.risk.env != "live":
                raise ValueError("当前连接的是模拟盘 Key，无法启动实盘实例")
        if row["mode"] == "demo" and self.risk.env != "demo":
            raise ValueError("当前连接的是实盘 Key，无法启动模拟实例")
        if row["mode"] == "paper" and not self.paper_oms:
            raise ValueError("本地模拟引擎未就绪")
        if self.risk.state.get("halted"):
            raise ValueError(f"风控熔断中，禁止启动策略：{self.risk.state.get('halt_reason')}")
        if instance_id in self.runtimes:
            raise ValueError("实例已在运行时表中")

        params = json.loads(row["params_json"])
        cls = STRATEGY_REGISTRY[self._type_of(row)]
        ctx = InstanceContext(self, instance_id, row["mode"])
        strategy = cls({k: v for k, v in params.items() if k != "period"}, ctx)
        strategy.state = json.loads(row["state_json"] or "{}")
        strategy.state.setdefault("pnl", 0.0)

        self.runtimes[instance_id] = {"strategy": strategy, "ctx": ctx, "paused": False}
        status = {"live": "running_live", "demo": "running_demo"}.get(row["mode"], "running_paper")
        db.execute(
            "UPDATE strategy_instances SET status=?, started_at=?, last_error=NULL WHERE id=?",
            (status, int(time.time() * 1000), instance_id),
        )
        try:
            await strategy.on_start()
            if hasattr(strategy, "place_initial_orders"):
                await strategy.place_initial_orders()
        except Exception as e:
            self.runtimes.pop(instance_id, None)
            db.execute("UPDATE strategy_instances SET status='error', last_error=? WHERE id=?",
                       (str(e)[:300], instance_id))
            raise
        self.persist_state(instance_id)
        db.add_audit(f"strategy:{instance_id}", "start", {"mode": row["mode"], "confirm": confirm})
        self.instance_log(instance_id, f"实例启动 mode={row['mode']}")
        event_bus.publish("strategy", {"event": "started", "instance_id": instance_id})
        return {"ok": True, "status": status}

    def _type_of(self, row: dict) -> str:
        s = db.query_one("SELECT type FROM strategies WHERE id=?", (row["strategy_id"],))
        return s["type"] if s else ""

    async def stop_instance(self, instance_id: int, close_position: bool = False) -> dict:
        row = db.query_one("SELECT * FROM strategy_instances WHERE id=?", (instance_id,))
        if not row:
            raise ValueError("实例不存在")
        rt = self.runtimes.pop(instance_id, None)
        if rt:
            try:
                await rt["strategy"].on_stop()
            except Exception as e:
                log.warning("on_stop 异常: %s", e)
            await self.oms.cancel_instance_orders(instance_id)
        if close_position:
            await self.close_instance_position(instance_id)
        db.execute(
            "UPDATE strategy_instances SET status='stopped', stopped_at=? WHERE id=?",
            (int(time.time() * 1000), instance_id),
        )
        db.add_audit(f"strategy:{instance_id}", "stop", {"close_position": close_position})
        self.instance_log(instance_id, "实例已停止")
        event_bus.publish("strategy", {"event": "stopped", "instance_id": instance_id})
        return {"ok": True}

    async def pause_instance(self, instance_id: int) -> None:
        rt = self.runtimes.get(instance_id)
        if not rt:
            raise ValueError("实例未在运行")
        rt["paused"] = True
        db.execute("UPDATE strategy_instances SET status='paused' WHERE id=?", (instance_id,))
        self.instance_log(instance_id, "已暂停（不再产生新信号）")

    async def resume_instance(self, instance_id: int) -> None:
        rt = self.runtimes.get(instance_id)
        if not rt:
            raise ValueError("实例未在运行")
        rt["paused"] = False
        st = db.query_one("SELECT mode FROM strategy_instances WHERE id=?", (instance_id,))
        status = {"live": "running_live", "demo": "running_demo"}.get(st["mode"], "running_paper")
        db.execute("UPDATE strategy_instances SET status=? WHERE id=?", (status, instance_id))
        self.instance_log(instance_id, "已恢复")

    async def close_instance_position(self, instance_id: int) -> bool:
        """平掉实例记录的持仓（按策略状态中记录的方向与数量）。"""
        rt = self.runtimes.get(instance_id)
        st = db.query_one("SELECT state_json FROM strategy_instances WHERE id=?", (instance_id,))
        state = json.loads(st["state_json"] or "{}") if st else {}
        pos_side = state.get("pos_side")
        open_sz = float(state.get("open_sz") or 0)
        inst_id = self.instance_inst_id(instance_id)
        if not pos_side or open_sz <= 0 or not inst_id:
            self.instance_log(instance_id, "无已记录持仓可平", "warning")
            return False
        try:
            intent = {
                "inst_id": inst_id, "side": "sell" if pos_side == "long" else "buy",
                "ord_type": "market", "sz_base": open_sz, "reduce_only": True,
                "instance_id": instance_id, "source": f"strategy:{instance_id}:close",
            }
            await self.oms.place_intent(intent)
            self.instance_log(instance_id, f"实例平仓下单 {pos_side} {open_sz}")
            state.update({"pos_side": None, "open_sz": 0.0, "sl_px": None})
            db.execute("UPDATE strategy_instances SET state_json=? WHERE id=?",
                       (json.dumps(state), instance_id))
            return True
        except Exception as e:
            self.instance_log(instance_id, f"平仓失败: {e}", "error")
            return False

    # ---------- 风控联动 ----------
    async def halt_all(self, reason: str) -> None:
        for iid in list(self.runtimes.keys()):
            try:
                rt = self.runtimes.pop(iid)
                await rt["strategy"].on_stop()
                db.execute("UPDATE strategy_instances SET status='halted', last_error=? WHERE id=?",
                           (reason[:300], iid))
                self.instance_log(iid, f"风控熔断停机: {reason}", "error")
            except Exception as e:
                log.error("halt 实例失败 %s: %s", iid, e)

    async def suspend_all(self, reason: str) -> None:
        for iid, rt in self.runtimes.items():
            rt["paused"] = True
            db.execute("UPDATE strategy_instances SET status='paused', last_error=? WHERE id=?",
                       (reason[:300], iid))
            self.instance_log(iid, f"策略已暂停: {reason}", "warning")

    # ---------- 重启恢复（红线 R7：实盘一律待人工确认） ----------
    async def _recover_instances(self) -> None:
        for row in db.query("SELECT * FROM strategy_instances WHERE status LIKE 'running%' OR status='halted'"):
            if row["mode"] == "live":
                db.execute("UPDATE strategy_instances SET status='pending_confirm' WHERE id=?", (row["id"],))
                self.instance_log(row["id"], "进程重启：实盘实例需人工确认后恢复（安全红线 R7）", "warning")
            else:
                db.execute("UPDATE strategy_instances SET status='stopped' WHERE id=?", (row["id"],))
                self.instance_log(row["id"], "进程重启：模拟实例已自动停止，可一键恢复", "info")

    # ---------- 日志与状态 ----------
    def instance_log(self, instance_id: int, msg: str, level: str = "info") -> None:
        entry = {"instance_id": instance_id, "ts": int(time.time() * 1000), "level": level, "msg": msg}
        lst = self.logs.setdefault(instance_id, [])
        lst.append(entry)
        del lst[:-200]
        event_bus.publish("log", entry)
        if level in ("error", "warning"):
            db.execute("INSERT INTO signals (instance_id, ts, action, reason) VALUES (?,?,?,?)",
                       (instance_id, entry["ts"], level, msg[:300]))

    def persist_state(self, instance_id: int) -> None:
        rt = self.runtimes.get(instance_id)
        if rt:
            db.execute("UPDATE strategy_instances SET state_json=? WHERE id=?",
                       (json.dumps(rt["strategy"].state, ensure_ascii=False), instance_id))

    def list_instances(self) -> list[dict]:
        rows = db.query("SELECT * FROM strategy_instances ORDER BY id DESC")
        out = []
        for r in rows:
            r = dict(r)
            s = db.query_one("SELECT type FROM strategies WHERE id=?", (r["strategy_id"],))
            r["type"] = s["type"] if s else ""
            r["logs"] = self.logs.get(r["id"], [])[-20:]
            out.append(r)
        return out
