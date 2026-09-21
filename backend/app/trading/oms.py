"""OMS 订单管理：唯一下单管道（红线 R4）、保护价执行、幂等、对账、平仓。

订单状态: pending_submit -> live -> partially_filled -> filled / canceled / partially_canceled
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone

from app import config, db
from app.core import event_bus
from app.exchange.okx_client import OkxApiError
from app.risk.risk_engine import RiskBlocked

log = logging.getLogger("bitvault.oms")

OPEN_STATES = ("pending_submit", "live", "partially_filled")


def _new_cl_ord_id() -> str:
    return f"BV{int(time.time() * 1000):x}{random.randint(0, 0xFFFF):04x}"


class OMS:
    def __init__(self, client, account, risk, data, env: str):
        self.client = client
        self.account = account
        self.risk = risk
        self.data = data
        self.env = env
        self._reconcile_task: asyncio.Task | None = None
        self._place_lock = asyncio.Lock()
        risk.oms = self

    async def start(self) -> None:
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())

    async def stop(self) -> None:
        if self._reconcile_task:
            self._reconcile_task.cancel()

    # ================= 下单（唯一管道） =================
    async def place_intent(self, intent: dict) -> dict:
        from app.core.trade_intent import validate_intent
        validate_intent(intent)   # 统一 TradeIntent 契约（P2-4：缺失/非法字段 fail-closed）
        """
        intent: inst_id, side(buy/sell), ord_type(market/limit/post_only), px,
                sz_base(BTC 数量，SWAP 自动转张), reduce_only, instance_id,
                source(manual/strategy:{id}), sl_trigger_px, tp_trigger_px, td_mode
        """
        inst_id = intent["inst_id"]
        side = intent["side"]
        ord_type = intent.get("ord_type", "market")
        reduce_only = bool(intent.get("reduce_only", False))
        source = intent.get("source", "manual")
        instance_id = intent.get("instance_id")
        leverage = int(intent.get("leverage", 1) or 1)

        ins = self.data.instrument(inst_id)
        if not ins:
            raise RiskBlocked(f"未知标的 {inst_id}")

        last_px = self.data.last_price(inst_id)
        if ord_type == "market":
            if last_px <= 0:
                raise RiskBlocked("无最新价，无法构建保护价")
            # 红线 R2：裸市价单 → 限价 IOC + ±0.2% 保护价
            direction = 1 + config.PROTECTIVE_PX_PCT if side == "buy" else 1 - config.PROTECTIVE_PX_PCT
            px = self.data.round_px(inst_id, last_px * direction)
            send_ord_type = "ioc"
        else:
            px = self.data.round_px(inst_id, float(intent["px"]))
            if px <= 0:
                raise RiskBlocked("限价单价格非法")
            send_ord_type = "post_only" if ord_type == "post_only" else "limit"

        sz_base, err = self.data.normalize_sz(inst_id, float(intent["sz_base"]))
        if err:
            raise RiskBlocked(err)

        try:
            notional = self.data.calculate_notional(inst_id, sz_base, px)
        except ValueError as e:
            raise RiskBlocked(str(e))
        # 红线 R4：风控检查 + 下单必须原子化（并发锁防止多单同时绕过仓位/风控限制）
        async with self._place_lock:
            self.risk.check_pretrade(
                inst_id=inst_id, side=side, sz=sz_base, px=px, notional=notional,
                reduce_only=reduce_only, source=source, instance_id=instance_id,
                leverage=leverage,
            )

            # tdMode / posSide 由账户配置决定
            is_swap = ins["instType"] == "SWAP"
            acct_lv = str(self.account.account_config.get("acctLv") or "")
            # P1-5 fail-closed：无法确认账户模式（配置获取失败）时禁止 SWAP 新开仓
            if is_swap and not reduce_only:
                if acct_lv in ("", "1"):
                    raise RiskBlocked(
                        "无法确认 OKX 账户模式（acctLv 缺失或为现货模式），禁止合约开仓。"
                        "请在 OKX 端确认账户模式后重试。")
            td_mode = "cash" if not is_swap else (intent.get("td_mode") or "isolated")
            pos_side = self.account.pos_side_for(side, reduce_only) if is_swap else None

            # P0-3：开仓前把杠杆真正写入交易所；失败=禁止开仓（fail-closed，绝不按错误杠杆下单）
            if is_swap and not reduce_only and leverage > 1:
                mgn_mode = "cross" if td_mode in ("cross", "cross_margin") else "isolated"
                try:
                    await self.client.set_leverage(inst_id, str(leverage), mgn_mode)
                except Exception as e:
                    raise RiskBlocked(f"设置 OKX 杠杆 {leverage}x 失败（{mgn_mode}）：{e}；禁止开仓")

            body: dict = {
                "instId": inst_id,
                "tdMode": td_mode,
                "side": side,
                "ordType": send_ord_type,
                "sz": str(sz_base),
                "clOrdId": _new_cl_ord_id(),
            }
            if px is not None:
                body["px"] = str(px)
            if is_swap:
                body["posSide"] = pos_side
                if reduce_only and pos_side == "net":
                    body["reduceOnly"] = True
            # 红线 R6：交易所侧止损/止盈单
            algo: dict = {}
            if intent.get("sl_trigger_px"):
                algo["slTriggerPx"] = str(self.data.round_px(inst_id, float(intent["sl_trigger_px"])))
                algo["slOrdPx"] = "-1"  # 市价止损
            if intent.get("tp_trigger_px"):
                algo["tpTriggerPx"] = str(self.data.round_px(inst_id, float(intent["tp_trigger_px"])))
                algo["tpOrdPx"] = "-1"
            if algo:
                body["attachAlgoOrds"] = [algo]

            now = int(time.time() * 1000)
            order_row = {
                "cl_ord_id": body["clOrdId"], "instance_id": instance_id,
                "inst_id": inst_id, "td_mode": td_mode, "side": side,
                "pos_side": pos_side or "", "ord_type": send_ord_type,
                "px": px, "sz": sz_base, "state": "pending_submit",
                "source": source, "sl_trigger_px": float(intent["sl_trigger_px"]) if intent.get("sl_trigger_px") else None,
                "reduce_only": 1 if reduce_only else 0,
                "created_at": now, "updated_at": now,
            }
            order_id = db.execute(
                "INSERT INTO orders (cl_ord_id, instance_id, inst_id, td_mode, side, pos_side, ord_type,"
                " px, sz, state, source, venue, sl_trigger_px, reduce_only, created_at, updated_at)"
                " VALUES (:cl_ord_id,:instance_id,:inst_id,:td_mode,:side,:pos_side,:ord_type,"
                " :px,:sz,:state,:source,'okx',:sl_trigger_px,:reduce_only,:created_at,:updated_at)",
                order_row,
            )

            try:
                resp = await self.client.place_order(body)
            except OkxApiError as e:
                db.execute(
                    "UPDATE orders SET state='failed', error_code=?, error_msg=?, updated_at=? WHERE id=?",
                    (e.code, e.msg[:200], int(time.time() * 1000), order_id),
                )
                db.add_audit(source, "place_order", body, f"rejected:{e.code} {e.msg}")
                self._publish_order(order_id)
                raise RiskBlocked(f"交易所拒绝下单 [{e.code}] {e.msg}") from None

            s_code = str(resp[0].get("sCode", "")) if resp else "-1"
            if s_code != "0":
                s_msg = resp[0].get("sMsg", "") if resp else ""
                db.execute(
                    "UPDATE orders SET state='failed', error_code=?, error_msg=?, updated_at=? WHERE id=?",
                    (s_code, s_msg[:200], int(time.time() * 1000), order_id),
                )
                db.add_audit(source, "place_order", body, f"rejected:{s_code} {s_msg}")
                self._publish_order(order_id)
                raise RiskBlocked(f"交易所拒绝下单 [{s_code}] {s_msg}")

            ord_id = resp[0].get("ordId", "")
            db.execute(
                "UPDATE orders SET state='live', ord_id=?, updated_at=? WHERE id=?",
                (ord_id, int(time.time() * 1000), order_id),
            )
            db.add_audit(source, "place_order", body, "ok")
            row = self._publish_order(order_id)
            return row

    # ================= WS 订单回报 =================
    async def handle_ws_orders(self, rows: list[dict]) -> None:
        for r in rows:
            cl_ord_id = r.get("clOrdId") or ""
            row = db.query_one("SELECT * FROM orders WHERE cl_ord_id=? OR ord_id=?", (cl_ord_id, r.get("ordId")))
            if not row:
                continue
            # OKX V5 订单状态枚举：live/partially_filled/filled/canceled/part_canceled
            # 新增 closed（已平仓，历史订单）和 unknown 兜底
            raw_state = r.get("state", "")
            state_map = {
                "live": "live",
                "partially_filled": "partially_filled",
                "filled": "filled",
                "canceled": "canceled",
                "part_canceled": "partially_canceled",   # OKX V5 实际枚举
                "partially_canceled": "partially_canceled",  # 兼容旧写法
                "closed": "filled",
            }
            if raw_state in state_map:
                new_state = state_map[raw_state]
            elif raw_state:
                log.warning("收到未识别的 OKX 订单状态 state=%s ordId=%s cl=%s → 标记 unknown",
                            raw_state, r.get("ordId"), cl_ord_id)
                new_state = "unknown"
            else:
                new_state = row["state"]
            filled = float(r.get("accFillSz") or 0)
            avg_px = float(r.get("avgPx") or 0) or row["avg_px"]
            fee = float(r.get("fillFee") or 0) + float(row["fee"] or 0)
            db.execute(
                "UPDATE orders SET state=?, filled_sz=?, avg_px=?, fee=?, ord_id=?, updated_at=? WHERE id=?",
                (new_state, filled, avg_px, fee, r.get("ordId"), int(time.time() * 1000), row["id"]),
            )
            # 逐笔成交
            fill_sz = float(r.get("fillSz") or 0)
            if fill_sz > 0 and r.get("fillPx"):
                db.execute(
                    "INSERT OR IGNORE INTO trades (ord_id, cl_ord_id, inst_id, side, px, sz, fee, instance_id, ts)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (r.get("ordId"), cl_ord_id, row["inst_id"], row["side"],
                     float(r["fillPx"]), fill_sz, float(r.get("fillFee") or 0),
                     row["instance_id"], int(r.get("uTime") or time.time() * 1000)),
                )
            row = self._publish_order(row["id"])   # P2-1：内部已 publish，不再重复发
            if row["instance_id"]:
                event_bus.publish("instance_order", row)

    def _publish_order(self, order_id: int) -> dict:
        row = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,)) or {}
        event_bus.publish("order", row)
        return row

    # ================= 撤单 / 平仓 =================
    async def cancel_order(self, cl_ord_id: str) -> dict:
        row = db.query_one("SELECT * FROM orders WHERE cl_ord_id=?", (cl_ord_id,))
        if not row:
            raise RiskBlocked("订单不存在")
        if row["state"] not in OPEN_STATES:
            return {"ok": True, "state": row["state"]}
        try:
            await self.client.cancel_order(row["inst_id"], cl_ord_id=cl_ord_id)
        except OkxApiError as e:
            if e.code in ("51400", "51401", "51402", "51503"):  # 已成交/已撤销/不存在
                await self._sync_single_order(row)
                return {"ok": True, "state": "raced"}
            raise
        db.execute("UPDATE orders SET state='canceled', updated_at=? WHERE id=?",
                   (int(time.time() * 1000), row["id"]))
        db.add_audit("user" if row["source"] == "manual" else row["source"], "cancel_order",
                     {"clOrdId": cl_ord_id}, "ok")
        return {"ok": True, "state": "canceled"}

    async def cancel_instance_orders(self, instance_id: int) -> int:
        # P1-3：严格限定 venue='okx'，绝不撤 paper 挂单
        rows = db.query(
            f"SELECT * FROM orders WHERE venue='okx' AND instance_id=? AND state IN ({','.join('?' * len(OPEN_STATES))})",
            (instance_id, *OPEN_STATES),
        )
        n = 0
        for r in rows:
            try:
                await self.cancel_order(r["cl_ord_id"])
                n += 1
            except Exception as e:
                log.warning("撤策略单失败 %s: %s", r["cl_ord_id"], e)
        return n

    async def cancel_all_orders(self) -> int:
        # P1-3：严格限定 venue='okx'
        rows = db.query(
            f"SELECT * FROM orders WHERE venue='okx' AND state IN ({','.join('?' * len(OPEN_STATES))})",
            tuple(OPEN_STATES),
        )
        n = 0
        for r in rows:
            try:
                await self.client.cancel_order(r["inst_id"], cl_ord_id=r["cl_ord_id"])
                db.execute("UPDATE orders SET state='canceled', updated_at=? WHERE id=?",
                           (int(time.time() * 1000), r["id"]))
                n += 1
            except OkxApiError as e:
                if e.code in ("51400", "51401", "51402", "51503"):
                    await self._sync_single_order(r)
                    n += 1
        if n:
            db.add_audit("system", "cancel_all_orders", {}, f"canceled={n}")
        return n

    async def find_position_sl(self, inst_id: str, pos_side: str = "long") -> dict | None:
        """查找该标的上「属于本仓位方向」且尚未触发的交易所侧止损单。

        过滤：有 slTriggerPx、state ∈ {live, pause}、side = 平仓方向、双向模式下 posSide 匹配。
        找不到返回 None；查询本身失败会抛异常（由调用方决定处置）。
        """
        ins = self.data.instrument(inst_id)
        if not ins:
            return None
        is_swap = ins["instType"] == "SWAP"
        close_side = "sell" if pos_side == "long" else "buy"
        want_pos_side = self.account.pos_side_for(close_side, True) if is_swap else None
        rows = await self.client.get_pending_algo_orders(inst_id)
        for r in (rows if isinstance(rows, list) else []):
            if not r.get("slTriggerPx"):
                continue
            if str(r.get("state") or "live") not in ("live", "pause"):
                continue
            if r.get("side") and str(r["side"]) != close_side:
                continue
            if want_pos_side and r.get("posSide") and str(r["posSide"]) != want_pos_side:
                continue
            return {"algo_id": str(r.get("algoId") or ""),
                    "sl_trigger_px": float(r.get("slTriggerPx") or 0),
                    "sz": float(r.get("sz") or 0),
                    "state": r.get("state"), "side": r.get("side"), "pos_side": r.get("posSide")}
        return None

    async def sync_position_sl(self, inst_id: str, sl_px: float, pos_side: str = "long",
                               sz: float | None = None, algo_id: str | None = None) -> dict:
        """把交易所侧止损同步到新的 sl_px（安全版：任何路径都不留"无保护"窗口）。

        按优先级：
          1) 合约且已定位到旧止损单 → **amend-algos 原地改触发价**（无空窗、不误撤）
          2) 否则（现货 / amend 失败 / 未找到旧单）→ **先挂新、再撤旧**：
             - 挂新失败 → 旧单仍在（**保护不中断**），返回 ok=False
             - 挂新成功 → 撤旧时按 side/posSide/state 过滤，**校验逐项 sCode**，
               并排除刚挂的新单（避免自撤）
        返回 {"ok", "mode", "algo_id", "canceled", "cancel_errors", "sl_px"} 或 {"ok": False, "err"}；
        失败不静默——写审计与日志，由调用方告警并记录状态。
        """
        ins = self.data.instrument(inst_id)
        if not ins:
            return {"ok": False, "mode": None, "err": f"未知标的 {inst_id}"}
        trigger = self.data.round_px(inst_id, float(sl_px))
        if trigger <= 0:
            return {"ok": False, "mode": None, "err": f"止损价非法：{sl_px}"}
        if not sz or float(sz) <= 0:
            return {"ok": False, "mode": None, "err": "缺少持仓量（sz），拒绝挂止损"}
        is_swap = ins["instType"] == "SWAP"
        close_side = "sell" if pos_side == "long" else "buy"
        want_pos_side = self.account.pos_side_for(close_side, True) if is_swap else None
        new_sz = str(sz)

        # ---- 1) 定位现有止损单（调用方缓存优先，否则查交易所；只认属于本方向的）----
        target: dict | None = None
        if algo_id:
            target = {"algo_id": algo_id}
        else:
            try:
                target = await self.find_position_sl(inst_id, pos_side)
            except Exception as e:
                db.add_audit("system", "sync_exchange_sl",
                             {"inst_id": inst_id, "sl_px": trigger, "stage": "query"},
                             f"query_fail:{e}")
                return {"ok": False, "mode": None, "err": f"查询交易所止损失败：{e}"}

        # ---- 2) 原地改（仅合约）：无空窗，也不会误撤他人挂单 ----
        if target and target.get("algo_id") and is_swap:
            amend_err = ""
            try:
                resp = await self.client.amend_algo_order({
                    "instId": inst_id, "algoId": target["algo_id"],
                    "newSlTriggerPx": str(trigger), "newSz": new_sz,
                })
                code = str(resp[0].get("sCode", "")) if resp else "-1"
                if code == "0":
                    db.add_audit("system", "sync_exchange_sl",
                                 {"inst_id": inst_id, "sl_px": trigger, "mode": "amend",
                                  "algo_id": target["algo_id"]}, "ok")
                    return {"ok": True, "mode": "amend", "algo_id": target["algo_id"],
                            "canceled": 0, "cancel_errors": 0, "sl_px": trigger}
                amend_err = f"[{code}] {resp[0].get('sMsg', '') if resp else ''}"
            except Exception as e:
                amend_err = str(e)
            db.add_audit("system", "sync_exchange_sl",
                         {"inst_id": inst_id, "sl_px": trigger, "mode": "amend",
                          "algo_id": target.get("algo_id")}, f"amend_fail:{amend_err}")
            log.warning("amend-algos 失败，回退为「先挂新后撤旧」%s：%s", inst_id, amend_err)

        # ---- 3) 先挂新：失败则旧单仍在，保护不中断 ----
        body: dict = {
            "instId": inst_id,
            "tdMode": "cash" if not is_swap else "isolated",
            "side": close_side,
            "ordType": "conditional",
            "sz": new_sz,
            "slTriggerPx": str(trigger),
            "slOrdPx": "-1",
        }
        if is_swap:
            body["posSide"] = want_pos_side
            if body["posSide"] == "net":
                body["reduceOnly"] = True
        try:
            resp = await self.client.place_algo_order(body)
        except Exception as e:
            db.add_audit("system", "sync_exchange_sl",
                         {"inst_id": inst_id, "sl_px": trigger, "body": body}, f"place_fail:{e}")
            return {"ok": False, "mode": None, "err": f"挂新止损失败（旧止损仍在）：{e}"}
        s_code = str(resp[0].get("sCode", "")) if resp else "-1"
        if s_code != "0":
            s_msg = resp[0].get("sMsg", "") if resp else ""
            db.add_audit("system", "sync_exchange_sl",
                         {"inst_id": inst_id, "sl_px": trigger, "body": body},
                         f"rejected:{s_code} {s_msg}")
            return {"ok": False, "mode": None,
                    "err": f"交易所拒绝止损单 [{s_code}] {s_msg}（旧止损仍在）"}
        new_algo_id = str(resp[0].get("algoId") or "")

        # ---- 4) 撤旧：方向/状态过滤 + 校验逐项 sCode + 排除刚挂的新单 ----
        canceled = cancel_errors = 0
        try:
            rows = await self.client.get_pending_algo_orders(inst_id)
            targets = []
            for r in (rows if isinstance(rows, list) else []):
                aid = str(r.get("algoId") or "")
                if not aid or aid == new_algo_id:
                    continue
                if not r.get("slTriggerPx"):
                    continue
                if str(r.get("state") or "live") not in ("live", "pause"):
                    continue
                if r.get("side") and str(r["side"]) != close_side:
                    continue
                if want_pos_side and r.get("posSide") and str(r["posSide"]) != want_pos_side:
                    continue
                targets.append({"instId": inst_id, "algoId": aid})
            if targets:
                cresp = await self.client.cancel_algo_orders(targets)
                for it in (cresp if isinstance(cresp, list) else []):
                    if str(it.get("sCode", "")) == "0":
                        canceled += 1
                    else:
                        cancel_errors += 1
        except Exception as e:
            cancel_errors += 1
            log.warning("撤销旧止损失败 %s（新止损已挂上，保护不中断）：%s", inst_id, e)

        db.add_audit("system", "sync_exchange_sl",
                     {"inst_id": inst_id, "sl_px": trigger, "sz": sz, "mode": "replace",
                      "algo_id": new_algo_id, "canceled": canceled, "cancel_errors": cancel_errors},
                     "ok" if cancel_errors == 0 else "ok_with_cancel_errors")
        return {"ok": True, "mode": "replace", "algo_id": new_algo_id,
                "canceled": canceled, "cancel_errors": cancel_errors, "sl_px": trigger}

    async def close_position(self, pos: dict) -> bool:
        inst_id = pos["instId"]
        ins = self.data.instrument(inst_id)
        is_swap = bool(ins and ins["instType"] == "SWAP")
        side = "sell" if pos["pos"] > 0 else "buy"
        sz = abs(pos["pos"])  # SWAP: 张数；现货: 币数
        pos_side = self.account.pos_side_for(side, True) if is_swap else None
        last_px = self.data.last_price(inst_id)
        if last_px <= 0:
            return False
        direction = 1 - config.PROTECTIVE_PX_PCT if side == "sell" else 1 + config.PROTECTIVE_PX_PCT
        px = self.data.round_px(inst_id, last_px * direction)
        notional = None
        try:
            notional = self.data.calculate_notional(inst_id, sz, px)
        except ValueError:
            log.warning("close_position 规格异常: %s", inst_id)
        if notional is None:
            notional = round(px * sz, 2)
        # P1-8：平仓/减仓也必须经过风控检查（reduce_only 放行开仓类限制）；
        # Kill Switch 紧急平仓同样经此（reduce_only=True 放行，熔断只拦开仓）
        self.risk.check_pretrade(
            inst_id=inst_id, side=side, sz=sz, px=px, notional=notional,
            reduce_only=True, source="close_position", leverage=int(pos.get("lever") or 1),
        )
        body: dict = {
            "instId": inst_id, "tdMode": "isolated" if is_swap else "cash",
            "side": side, "ordType": "ioc", "px": str(px), "sz": str(sz),
            "clOrdId": _new_cl_ord_id(),
        }
        if is_swap:
            body["posSide"] = pos_side or "net"
            if (pos_side or "net") == "net":
                body["reduceOnly"] = True
        try:
            resp = await self.client.place_order(body)
            s_code = str(resp[0].get("sCode", "")) if resp else "-1"
        except OkxApiError as e:
            log.error("平仓失败 %s: %s", inst_id, e)
            return False
        if s_code != "0":
            log.error("平仓被拒 %s: %s", inst_id, resp)
            return False
        now = int(time.time() * 1000)
        db.execute(
            "INSERT INTO orders (cl_ord_id, ord_id, inst_id, td_mode, side, pos_side, ord_type, px, sz,"
            " state, source, venue, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (body["clOrdId"], resp[0].get("ordId", ""), inst_id, body["tdMode"], side,
             body.get("posSide", ""), "ioc", px, sz, "live", "risk_flatten", "okx", now, now),
        )
        return True

    async def close_all_positions(self) -> int:
        n = 0
        try:
            await self.account.refresh()
        except Exception:
            pass
        for pos in list(self.account.positions):
            for attempt in range(3):
                if await self.close_position(pos):
                    n += 1
                    break
                await asyncio.sleep(1.0)
        return n

    # ================= 对账（兜底） =================
    async def _sync_single_order(self, row: dict) -> None:
        try:
            data = await self.client.get_order(row["inst_id"], cl_ord_id=row["cl_ord_id"])
            if data:
                await self.handle_ws_orders([data[0]])
            else:
                # 订单不存在 → 标记 failed（防止 pending_submit 孤儿订单永驻）
                db.execute(
                    "UPDATE orders SET state='failed', error_code='not_found',"
                    " error_msg='交易所查无此单', updated_at=? WHERE id=?",
                    (int(time.time() * 1000), row["id"]),
                )
                log.warning("订单 %s 在交易所不存在 → 标记 failed", row["cl_ord_id"])
        except OkxApiError as e:
            if e.code in ("51601", "51404"):
                # 51601: 订单不存在 / 51404: 订单已撤销
                db.execute(
                    "UPDATE orders SET state='failed', error_code=?, error_msg=?, updated_at=? WHERE id=?",
                    (e.code, f"交易所: {e.msg}"[:200], int(time.time() * 1000), row["id"]),
                )
                log.warning("订单 %s 交易所返回 %s → 标记 failed", row["cl_ord_id"], e.code)
            else:
                log.warning("订单查询失败 %s: %s", row["cl_ord_id"], e)
        except Exception as e:
            log.warning("订单查询失败 %s: %s", row["cl_ord_id"], e)

    async def sync_all(self) -> None:
        """P1-6：断线重连后的主动重同步（balance/positions/open orders）。
        只处理本地 open 与远程挂单差异（终端态与 fills 由轮询/回调继续校准）。"""
        try:
            if self.account:
                await self.account.refresh()
            local_open = db.query(
                f"SELECT * FROM orders WHERE venue='okx' AND state IN ({','.join('?' * len(OPEN_STATES))})",
                tuple(OPEN_STATES),
            )
            if local_open:
                pend = await self.client.get_pending_orders()
                remote_ids = {p.get("clOrdId") or p.get("ordId") for p in pend}
                for row in local_open:
                    rid = row["cl_ord_id"] or row["ord_id"]
                    if rid not in remote_ids:
                        await self._sync_single_order(row)
        except Exception as e:
            log.warning("重连重同步失败: %s", e)

    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                local_open = db.query(
                    f"SELECT * FROM orders WHERE venue='okx' AND state IN ({','.join('?' * len(OPEN_STATES))})",
                    tuple(OPEN_STATES),
                )
                if local_open:
                    remote_ids = set()
                    try:
                        pend = await self.client.get_pending_orders()
                        remote_ids = {p.get("clOrdId") or p.get("ordId") for p in pend}
                    except Exception as e:
                        log.warning("对账拉取挂单失败: %s", e)
                        continue
                    for row in local_open:
                        rid = row["cl_ord_id"] or row["ord_id"]
                        if rid not in remote_ids:
                            await self._sync_single_order(row)  # 已不在挂单列表 -> 拉终态
                # 持仓以交易所为准（account.refresh 每 10s 校准）；策略侧持仓由策略状态记录
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("对账循环异常: %s", e)