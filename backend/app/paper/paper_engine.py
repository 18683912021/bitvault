"""本地模拟盘（Paper Trading）：OKX 真实行情 + 本地撮合，规则与费用对齐 OKX VIP0 常规费率。

- 现货+合约模型：USDT 现金 + 币持仓 + 杠杆持仓（多/空）。
- 费率：SPOT maker 0.08% / taker 0.10%（OKX VIP0 常规）。
- 精度：tickSz / lotSz / minSz 与 OKX instruments 一致（复用 data.round_px / normalize_sz）。
- 保护价（红线 R2）：市价单转 IOC 限价 ±0.2%，本地按最新价成交，超保护价不成交。
- 事前风控（红线 R4）：与实盘共用 risk.check_pretrade。
- 订单/成交复用 orders / trades 表（venue='paper'），前端订单页可直接展示。
- 杠杆持仓含多空双向：buy 开多 / sell 开空 / sell+reduce 平多 / buy+reduce 平空。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

from app import config, db
from app.core import event_bus
from app.risk.risk_engine import RiskBlocked

log = logging.getLogger("bitvault.paper")

OPEN_STATES = ("pending_submit", "live", "partially_filled")

# OKX VIP0 常规费率（现货）
FEE_MAKER = 0.0008
FEE_TAKER = 0.0010

PAPER_KEY = "paper_account"


def _new_cl_ord_id() -> str:
    return f"PP{int(time.time() * 1000):x}{random.randint(0, 0xFFFF):04x}"


class PaperEngine:
    """模拟撮合引擎：实现与 OMS 相同的下单接口，由 manager 按 mode 路由。"""

    def __init__(self, data, risk):
        self.data = data
        self.risk = risk
        self._bar_q = event_bus.subscribe(["bar", "tick"])
        self._task: asyncio.Task | None = None
        risk.paper = self   # 供风控查询模拟权益（可选）

    # ---------- 模拟账户 ----------
    def account(self) -> dict:
        acct = db.get_setting(PAPER_KEY) or {}
        if "usdt" not in acct:
            acct = {"usdt": 10000.0, "coins": {}, "initial": 10000.0}
        if "lev_positions" not in acct:
            acct["lev_positions"] = []
        return acct

    def _save(self, acct: dict) -> None:
        db.set_setting(PAPER_KEY, acct)

    def reset(self, initial: float = 10000.0) -> dict:
        acct = {"usdt": initial, "coins": {}, "initial": initial, "lev_positions": []}
        self._save(acct)
        # 清空模拟盘全部记录：订单/成交（交易闭环随之清零）、autopilot 决策与通知
        db.execute("DELETE FROM trades WHERE cl_ord_id IN (SELECT cl_ord_id FROM orders WHERE venue='paper')")
        db.execute("DELETE FROM orders WHERE venue='paper'")
        db.execute("DELETE FROM signals WHERE instance_id=0")
        db.execute("DELETE FROM notifications WHERE title LIKE '自动驾驶%'")
        db.add_audit("user", "paper_reset", {"initial": initial, "purge_records": True})
        return self.summary()

    def summary(self) -> dict:
        acct = self.account()
        pos_rows = []
        equity = float(acct["usdt"])
        # 现货持仓
        for coin, sz in acct["coins"].items():
            if sz <= 0:
                continue
            inst_id = f"{coin}-USDT"
            last = self.data.last_price(inst_id)
            equity += sz * last
            pos_rows.append({"inst_id": inst_id, "sz": round(sz, 8), "last": last,
                             "notional": round(sz * last, 2), "leverage": 1})
        # 杠杆持仓
        for lp in acct.get("lev_positions", []):
            inst_id = lp["inst_id"]
            last = self.data.last_price(inst_id)
            sz = float(lp["sz"])
            entry = float(lp["entry_px"])
            lev = int(lp["leverage"])
            margin = float(lp["margin"])
            lp_side = lp.get("side", "long")
            upl = (last - entry) * sz if lp_side == "long" else (entry - last) * sz
            equity += margin + upl
            pos_rows.append({
                "inst_id": inst_id, "sz": round(sz, 8), "last": last,
                "notional": round(sz * last, 2),
                "leverage": lev, "entry_px": entry, "side": lp_side,
                "margin": round(margin, 2),
                "liq_px": round(float(lp["liq_px"]), 2),
                "upl": round(upl, 4),
            })
        return {
            "equity": round(equity, 2), "usdt": round(float(acct["usdt"]), 2),
            "initial": float(acct["initial"]), "positions": pos_rows,
            "pnl": round(equity - float(acct["initial"]), 2),
        }

    # ---------- 事件驱动撮合 ----------
    async def start(self) -> None:
        # 引擎重启后挂单簿为空：清理上次进程遗留的 paper 孤儿单（永远无法成交）
        n = db.execute(
            f"UPDATE orders SET state='canceled', updated_at=? WHERE venue='paper'"
            f" AND state IN ({','.join('?' * len(OPEN_STATES))})",
            (int(time.time() * 1000), *OPEN_STATES),
        )
        if n:
            log.info("清理上次进程遗留的 paper 挂单 %d 张", n)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                topic, data, _ = await self._bar_q.get()
                if topic == "bar":
                    self._match_bar(data)
                elif topic == "tick":
                    self._match_tick(data)
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("撮合循环异常: %s", e)

    def _open_orders(self, inst_id: str) -> list[dict]:
        return db.query(
            f"SELECT * FROM orders WHERE venue='paper' AND inst_id=? AND state IN ({','.join('?' * len(OPEN_STATES))})"
            " ORDER BY id ASC",
            (inst_id, *OPEN_STATES),
        )

    def _match_bar(self, bar: dict) -> None:
        """K 线撮合：high/low 穿越限价即成交（与 OKX 限价撮合语义一致）。"""
        self._check_liquidation(bar["inst_id"], bar.get("l", 0), bar.get("h", 0))
        for row in self._open_orders(bar["inst_id"]):
            px = float(row["px"] or 0)
            if px <= 0:
                continue
            hit = (row["side"] == "buy" and bar["l"] <= px) or (row["side"] == "sell" and bar["h"] >= px)
            if hit:
                self._fill(row, px, "limit")

    def _match_tick(self, tick: dict) -> None:
        """tick 兜底撮合：最新价穿越限价（bar 粒度不够实时时的补充）。"""
        last = float(tick.get("last") or 0)
        if last <= 0:
            return
        self._check_liquidation(tick["instId"], last, last)
        for row in self._open_orders(tick["instId"]):
            px = float(row["px"] or 0)
            if px <= 0:
                continue
            hit = (row["side"] == "buy" and last <= px) or (row["side"] == "sell" and last >= px)
            if hit:
                self._fill(row, px, "limit")

    # ---------- 爆仓检查 ----------
    def _check_liquidation(self, inst_id: str, low_px: float, high_px: float) -> None:
        """杠杆持仓爆仓检查：多头价格跌破强平价 / 空头价格涨破强平价。"""
        if low_px <= 0:
            return
        acct = self.account()
        lev_positions = acct.get("lev_positions", [])
        changed = False
        for lp in lev_positions[:]:
            if lp["inst_id"] != inst_id:
                continue
            liq_px = float(lp["liq_px"])
            side = lp.get("side", "long")
            # 多头：价格跌到强平价；空头：价格涨到强平价
            hit = (low_px <= liq_px) if side == "long" else (high_px >= liq_px)
            if not hit:
                continue
            sz = float(lp["sz"])
            entry = float(lp["entry_px"])
            margin = float(lp["margin"])
            lev = int(lp["leverage"])
            close_px = liq_px
            close_side = "sell" if side == "long" else "buy"
            now = int(time.time() * 1000)
            db.execute(
                "INSERT INTO orders (cl_ord_id, inst_id, td_mode, side, ord_type, px, sz,"
                " state, source, venue, leverage, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_new_cl_ord_id(), inst_id, "isolated", close_side, "liquidation",
                 close_px, sz, "filled", "autopilot:liquidation", "paper", lev, now, now),
            )
            db.execute(
                "INSERT INTO trades (ord_id, cl_ord_id, inst_id, side, px, sz, fee, instance_id, ts)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                ("", _new_cl_ord_id(), inst_id, close_side, close_px, sz, 0, None, now),
            )
            db.add_audit("system", "paper_liquidation",
                         {"inst_id": inst_id, "sz": sz, "entry": entry,
                          "liq_px": liq_px, "margin": margin, "leverage": lev, "side": side}, "ok")
            lev_positions.remove(lp)
            changed = True
            log.warning("模拟盘爆仓 %s %s sz=%.8f entry=%.2f liq=%.2f margin=%.2f lev=%d",
                        inst_id, side, sz, entry, liq_px, margin, lev)
            event_bus.publish("paper_liquidation", {
                "inst_id": inst_id, "sz": sz, "entry_px": entry,
                "liq_px": liq_px, "margin": margin, "leverage": lev, "side": side,
            })
        if changed:
            acct["lev_positions"] = lev_positions
            self._save(acct)

    # ---------- 下单（唯一管道，接口对齐 OMS.place_intent） ----------
    async def place_intent(self, intent: dict) -> dict:
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
        if last_px <= 0:
            raise RiskBlocked("无最新行情，暂不能撮合")

        # 红线 R2：市价单 → IOC + ±0.2% 保护价（与实盘 OMS 一致）
        if ord_type == "market":
            direction = 1 + config.PROTECTIVE_PX_PCT if side == "buy" else 1 - config.PROTECTIVE_PX_PCT
            px = self.data.round_px(inst_id, last_px * direction)
            send_ord_type = "ioc"
            maker = False
        else:
            px = self.data.round_px(inst_id, float(intent["px"]))
            if px <= 0:
                raise RiskBlocked("限价单价格非法")
            send_ord_type = "post_only" if ord_type == "post_only" else "limit"
            maker = True

        sz_base, err = self.data.normalize_sz(inst_id, float(intent["sz_base"]))
        if err:
            raise RiskBlocked(err)
        if sz_base <= 0:
            raise RiskBlocked("下单数量为 0")

        notional = px * sz_base
        # 红线 R4：事前风控（白名单/单笔限额/熔断），与实盘同一套规则；
        # 频率限制仅对 okx 通道生效（本地撮合无交易所配额）
        self.risk.check_pretrade(
            inst_id=inst_id, side=side, sz=sz_base, px=px, notional=notional,
            reduce_only=reduce_only, source=source, instance_id=instance_id,
            venue="paper", leverage=leverage,
        )

        # 余额检查（本地账户）
        acct = self.account()
        coin = inst_id.replace("-USDT", "")
        fee_rate = FEE_MAKER if maker else FEE_TAKER
        if leverage > 1:
            # 杠杆模式：开仓扣保证金，平仓返还保证金+盈亏
            margin = notional / leverage
            fee = notional * fee_rate
            acct = self.account()
            pos_long = self._find_lev_position(acct, inst_id, "long")
            pos_short = self._find_lev_position(acct, inst_id, "short")
            if side == "buy" and not reduce_only:
                # 开多
                cost = margin + fee
                if cost > float(acct["usdt"]) + 1e-9:
                    raise RiskBlocked(f"模拟账户 USDT 不足（保证金）：需 {cost:.2f}，可用 {acct['usdt']:.2f}")
            elif side == "sell" and not reduce_only:
                # 开空
                cost = margin + fee
                if cost > float(acct["usdt"]) + 1e-9:
                    raise RiskBlocked(f"模拟账户 USDT 不足（保证金）：需 {cost:.2f}，可用 {acct['usdt']:.2f}")
            elif side == "sell" and reduce_only:
                # 平多：需要匹配的多头持仓
                if not pos_long or float(pos_long["sz"]) < sz_base - 1e-12:
                    raise RiskBlocked(f"模拟账户多头持仓不足：需 {sz_base}，可用 {pos_long['sz'] if pos_long else 0}")
            elif side == "buy" and reduce_only:
                # 平空：需要匹配的空头持仓
                if not pos_short or float(pos_short["sz"]) < sz_base - 1e-12:
                    raise RiskBlocked(f"模拟账户空头持仓不足：需 {sz_base}，可用 {pos_short['sz'] if pos_short else 0}")
        else:
            # 现货模式（原逻辑不变）
            if side == "buy":
                cost = notional * (1 + fee_rate)
                if cost > float(acct["usdt"]) + 1e-9:
                    raise RiskBlocked(f"模拟账户 USDT 不足：需 {cost:.2f}，可用 {acct['usdt']:.2f}")
            else:
                have = float(acct["coins"].get(coin, 0.0))
                if sz_base > have + 1e-9:
                    raise RiskBlocked(f"模拟账户 {coin} 持仓不足：需 {sz_base}，可用 {have}")

        td_mode = "isolated" if leverage > 1 else "cash"
        # pos_side：合约模式区分多空方向，供 roundtrips 正确配对
        if leverage > 1:
            if not reduce_only:
                pos_side = "long" if side == "buy" else "short"
            else:
                pos_side = "short" if side == "buy" else "long"
        else:
            pos_side = "long"
        now = int(time.time() * 1000)
        order_id = db.execute(
            "INSERT INTO orders (cl_ord_id, instance_id, inst_id, td_mode, side, pos_side, ord_type,"
            " px, sz, state, source, venue, leverage, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_new_cl_ord_id(), instance_id, inst_id, td_mode, side, pos_side, send_ord_type,
             px, sz_base, "live", source, "paper", leverage, now, now),
        )
        db.add_audit(source, "paper_place", {"instId": inst_id, "side": side,
                                             "ordType": send_ord_type, "px": px, "sz": sz_base,
                                             "leverage": leverage}, "ok")
        row = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
        row["reduce_only"] = reduce_only   # 传递到 _fill（DB 无此列，内存注入）

        if send_ord_type == "ioc":
            # 市价：按最新价 ± 滑点成交（保守模拟吃单冲击）；超出保护价则压到保护价
            SLIP = 0.0002
            fill_px = last_px * (1 + SLIP) if side == "buy" else last_px * (1 - SLIP)
            if side == "buy" and fill_px > px:
                fill_px = px
            if side == "sell" and fill_px < px:
                fill_px = px
            self._fill(row, fill_px, "market")
        else:
            # 限价：进入本地挂单簿，等 bar/tick 撮合
            event_bus.publish("order", dict(row))
            if instance_id:
                event_bus.publish("instance_order", dict(row))
        return db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))

    def _find_lev_position(self, acct: dict, inst_id: str, side: str = "long") -> dict | None:
        """查找匹配 inst_id 和 side 的杠杆持仓（FIFO）。"""
        for lp in acct.get("lev_positions", []):
            if lp["inst_id"] == inst_id and lp.get("side", "long") == side:
                return lp
        return None

    # ---------- 成交结算 ----------
    def _fill(self, row: dict, px: float, kind: str) -> None:
        inst_id = row["inst_id"]
        coin = inst_id.replace("-USDT", "")
        sz = float(row["sz"])
        maker = row["ord_type"] in ("limit", "post_only")
        fee_rate = FEE_MAKER if maker else FEE_TAKER
        notional = px * sz
        fee = notional * fee_rate
        leverage = int(row.get("leverage", 1) or 1)

        acct = self.account()
        usdt = float(acct["usdt"])
        coins = acct["coins"]

        if leverage > 1:
            # ---- 杠杆模式（多空双向）----
            lev_positions = acct.get("lev_positions", [])
            reduce_only = bool(row.get("reduce_only", False))
            if row["side"] == "buy" and not reduce_only:
                # 开多：扣保证金+手续费
                margin = notional / leverage
                usdt -= margin + fee
                liq_px = px * (1 - 1.0 / leverage + 0.005)
                lev_positions.append({
                    "inst_id": inst_id, "coin": coin, "sz": sz, "side": "long",
                    "entry_px": px, "leverage": leverage,
                    "margin": round(margin, 8), "liq_px": round(liq_px, 2),
                    "open_ts": int(time.time() * 1000),
                    "cl_ord_id": row["cl_ord_id"],
                })
            elif row["side"] == "sell" and not reduce_only:
                # 开空：扣保证金+手续费
                margin = notional / leverage
                usdt -= margin + fee
                liq_px = px * (1 + 1.0 / leverage - 0.005)
                lev_positions.append({
                    "inst_id": inst_id, "coin": coin, "sz": sz, "side": "short",
                    "entry_px": px, "leverage": leverage,
                    "margin": round(margin, 8), "liq_px": round(liq_px, 2),
                    "open_ts": int(time.time() * 1000),
                    "cl_ord_id": row["cl_ord_id"],
                })
            elif row["side"] == "sell" and reduce_only:
                # 平多：返还保证金+盈亏-手续费
                lp = self._find_lev_position(acct, inst_id, "long")
                if not lp:
                    log.error("平多但找不到多头持仓 inst_id=%s", inst_id)
                    return
                entry = float(lp["entry_px"])
                lp_margin = float(lp["margin"])
                lp_sz = float(lp["sz"])
                pnl = (px - entry) * sz
                margin_portion = lp_margin * (sz / lp_sz) if lp_sz > 0 else 0
                usdt += margin_portion + pnl - fee
                new_sz = lp_sz - sz
                if new_sz <= 1e-12:
                    lev_positions.remove(lp)
                else:
                    lp["sz"] = new_sz
                    lp["margin"] = round(lp_margin - margin_portion, 8)
            elif row["side"] == "buy" and reduce_only:
                # 平空：返还保证金+盈亏-手续费（空头盈利=entry-px）
                lp = self._find_lev_position(acct, inst_id, "short")
                if not lp:
                    log.error("平空但找不到空头持仓 inst_id=%s", inst_id)
                    return
                entry = float(lp["entry_px"])
                lp_margin = float(lp["margin"])
                lp_sz = float(lp["sz"])
                pnl = (entry - px) * sz  # 空头：跌了赚
                margin_portion = lp_margin * (sz / lp_sz) if lp_sz > 0 else 0
                usdt += margin_portion + pnl - fee
                new_sz = lp_sz - sz
                if new_sz <= 1e-12:
                    lev_positions.remove(lp)
                else:
                    lp["sz"] = new_sz
                    lp["margin"] = round(lp_margin - margin_portion, 8)
            acct.update({"usdt": usdt, "lev_positions": lev_positions})
        else:
            # ---- 现货模式（原逻辑不变）----
            if row["side"] == "buy":
                usdt -= notional + fee
                coins[coin] = float(coins.get(coin, 0.0)) + sz
            else:
                usdt += notional - fee
                coins[coin] = float(coins.get(coin, 0.0)) - sz
                if abs(coins[coin]) < 1e-9:
                    coins[coin] = 0.0
            acct.update({"usdt": usdt, "coins": coins})
        self._save(acct)

        now = int(time.time() * 1000)
        db.execute(
            "UPDATE orders SET state='filled', filled_sz=?, avg_px=?, fee=?, updated_at=? WHERE id=?",
            (sz, px, fee, now, row["id"]),
        )
        db.execute(
            "INSERT INTO trades (ord_id, cl_ord_id, inst_id, side, px, sz, fee, instance_id, ts)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (f"PP-{row['id']}", row["cl_ord_id"], inst_id, row["side"], px, sz, fee,
             row["instance_id"], now),
        )
        updated = db.query_one("SELECT * FROM orders WHERE id=?", (row["id"],))
        event_bus.publish("order", updated)
        if updated["instance_id"]:
            event_bus.publish("instance_order", updated)

    # ---------- 撤单（接口对齐 OMS） ----------
    async def cancel_order(self, cl_ord_id: str) -> dict:
        row = db.query_one("SELECT * FROM orders WHERE cl_ord_id=? AND venue='paper'", (cl_ord_id,))
        if not row:
            raise RiskBlocked("订单不存在")
        if row["state"] not in OPEN_STATES:
            return {"ok": True, "state": row["state"]}
        db.execute("UPDATE orders SET state='canceled', updated_at=? WHERE id=?",
                   (int(time.time() * 1000), row["id"]))
        updated = db.query_one("SELECT * FROM orders WHERE id=?", (row["id"],))
        event_bus.publish("order", updated)
        db.add_audit("user" if row["source"] == "manual" else row["source"], "paper_cancel",
                     {"clOrdId": cl_ord_id}, "ok")
        return {"ok": True, "state": "canceled"}

    async def cancel_instance_orders(self, instance_id: int) -> int:
        rows = db.query(
            f"SELECT * FROM orders WHERE venue='paper' AND instance_id=? AND state IN ({','.join('?' * len(OPEN_STATES))})",
            (instance_id, *OPEN_STATES),
        )
        n = 0
        for r in rows:
            try:
                await self.cancel_order(r["cl_ord_id"])
                n += 1
            except Exception as e:
                log.warning("撤模拟单失败 %s: %s", r["cl_ord_id"], e)
        return n
