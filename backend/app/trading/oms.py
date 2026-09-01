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
        risk.oms = self

    async def start(self) -> None:
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())

    async def stop(self) -> None:
        if self._reconcile_task:
            self._reconcile_task.cancel()

    # ================= 下单（唯一管道） =================
    async def place_intent(self, intent: dict) -> dict:
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

        notional = px * sz_base
        # 红线 R4：所有订单必须过事前风控
        self.risk.check_pretrade(
            inst_id=inst_id, side=side, sz=sz_base, px=px, notional=notional,
            reduce_only=reduce_only, source=source, instance_id=instance_id,
        )

        # tdMode / posSide 由账户配置决定
        is_swap = ins["instType"] == "SWAP"
        acct_lv = str(self.account.account_config.get("acctLv") or "")
        if is_swap and acct_lv == "1":
            raise RiskBlocked("当前 OKX 账户为现货模式，无法交易合约，请在 OKX 端切换账户模式")
        td_mode = "cash" if not is_swap else (intent.get("td_mode") or "isolated")
        pos_side = self.account.pos_side_for(side, reduce_only) if is_swap else None

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
            "created_at": now, "updated_at": now,
        }
        order_id = db.execute(
            "INSERT INTO orders (cl_ord_id, instance_id, inst_id, td_mode, side, pos_side, ord_type,"
            " px, sz, state, source, sl_trigger_px, created_at, updated_at)"
            " VALUES (:cl_ord_id,:instance_id,:inst_id,:td_mode,:side,:pos_side,:ord_type,"
            " :px,:sz,:state,:source,:sl_trigger_px,:created_at,:updated_at)",
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
            state_map = {
                "live": "live", "partially_filled": "partially_filled",
                "filled": "filled", "canceled": "canceled", "partially_canceled": "partially_canceled",
            }
            new_state = state_map.get(r.get("state"), row["state"])
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
            row = self._publish_order(row["id"])
            event_bus.publish("order", row)
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
        rows = db.query(
            f"SELECT * FROM orders WHERE instance_id=? AND state IN ({','.join('?' * len(OPEN_STATES))})",
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
        rows = db.query(
            f"SELECT * FROM orders WHERE state IN ({','.join('?' * len(OPEN_STATES))})",
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
            " state, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (body["clOrdId"], resp[0].get("ordId", ""), inst_id, body["tdMode"], side,
             body.get("posSide", ""), "ioc", px, sz, "live", "risk_flatten", now, now),
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
        except Exception as e:
            log.warning("订单查询失败 %s: %s", row["cl_ord_id"], e)

    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                local_open = db.query(
                    f"SELECT * FROM orders WHERE state IN ({','.join('?' * len(OPEN_STATES))})",
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
