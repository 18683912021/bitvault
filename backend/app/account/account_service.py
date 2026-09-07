"""账户服务：余额/持仓轮询、账户配置检测、资金曲线快照。"""
from __future__ import annotations

import asyncio
import logging
import time

from app.core import event_bus
from app import db

log = logging.getLogger("bitvault.account")


class AccountService:
    def __init__(self, client, env: str):
        self.client = client
        self.env = env
        self.summary: dict = {}
        self.positions: list[dict] = []
        self.account_config: dict = {}
        self._snap_ts = 0
        self._task: asyncio.Task | None = None
        # P0-4：账户刷新成功后的风控回调（main 注入 → risk.on_account）
        self.on_refresh = None

    async def start(self) -> None:
        await self.refresh_config()
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def refresh_config(self) -> None:
        try:
            rows = await self.client.get_account_config()
            if rows:
                c = rows[0]
                self.account_config = {
                    "acctLv": c.get("acctLv"),
                    "acctLvDesc": {
                        "1": "现货模式", "2": "单币种保证金", "3": "跨币种保证金", "4": "组合保证金",
                    }.get(str(c.get("acctLv")), str(c.get("acctLv"))),
                    "posMode": c.get("posMode"),  # net_mode / long_short_mode
                    "uid": c.get("uid"),
                    "canTrade": c.get("acctLv") is not None,
                }
                log.info("账户配置: %s", self.account_config)
        except Exception as e:
            log.warning("获取账户配置失败: %s", e)
            self.account_config = {}

    def pos_side_for(self, side: str, reduce_only: bool) -> str:
        """根据账户持仓方式推导 posSide。"""
        if self.account_config.get("posMode") == "long_short_mode":
            if reduce_only:
                return "long" if side == "sell" else "short"
            return "long" if side == "buy" else "short"
        return "net"

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.refresh()
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("账户轮询异常: %s", e)
                await asyncio.sleep(15)

    async def refresh(self) -> dict:
        # get_balance 独立 try：失败时保留旧 summary 但标记 stale（风控可用此判断）
        try:
            bal = await self.client.get_balance()
            details = bal[0] if bal else {}
            self.summary = {
                "env": self.env,
                "totalEq": float(details.get("totalEq") or 0),
                "details": details.get("details") or [],
                "ts": int(time.time() * 1000),
                "stale": False,
            }
        except Exception as e:
            log.warning("获取余额失败: %s — 保留旧权益数据", e)
            if self.summary:
                self.summary = dict(self.summary, stale=True)
        try:
            pos = await self.client.get_positions()
            self.positions = [
                {
                    "instId": p["instId"], "posSide": p.get("posSide"), "pos": float(p.get("pos") or 0),
                    "avgPx": float(p.get("avgPx") or 0), "markPx": float(p.get("markPx") or 0),
                    "upl": float(p.get("upl") or 0), "uplRatio": float(p.get("uplRatio") or 0),
                    "lever": p.get("lever"), "mgnMode": p.get("mgnMode"),
                    "notionalUsd": float(p.get("notionalUsd") or 0),
                    "liqPx": float(p["liqPx"]) if p.get("liqPx") else None,
                    "marginRatio": float(p["marginRatio"]) if p.get("marginRatio") else None,
                    "mgnRatio": float(p["mgnRatio"]) if p.get("mgnRatio") else None,
                    "imr": float(p.get("imr") or 0),
                }
                for p in pos if float(p.get("pos") or 0) != 0
            ]
        except Exception as e:
            # 持仓获取失败 → 清空旧数据（避免幻影持仓）
            log.warning("获取持仓失败: %s — 清空旧持仓数据防止幻影持仓", e)
            self.positions = []
        payload = {"summary": self.summary, "positions": self.positions}
        event_bus.publish("account", payload)
        if self.on_refresh:
            try:
                self.on_refresh(self.summary)
            except Exception as e:
                log.debug("on_refresh 回调异常: %s", e)

        # 每 5 分钟快照一次资金曲线
        now = time.time()
        if now - self._snap_ts > 300:
            self._snap_ts = now
            try:
                # P2-2：实写可用资金/占用保证金/未实现盈亏（原先恒为 0 属虚假数据）
                avail = 0.0
                for d in self.summary.get("details") or []:
                    avail += float(d.get("availBal") or d.get("cashBal") or 0)
                margin_used = 0.0
                upl = 0.0
                for p in self.positions:
                    margin_used += float(p.get("imr") or p.get("margin") or 0)
                    upl += float(p.get("upl") or p.get("upl_px") or 0)
                db.execute(
                    "INSERT OR REPLACE INTO account_snapshots (ts, equity, available, margin_used, unrealized_pnl, env)"
                    " VALUES (?,?,?,?,?,?)",
                    (int(now * 1000), self.summary["totalEq"], round(avail, 4),
                     round(margin_used, 4), round(upl, 4), self.env),
                )
            except Exception:
                pass
        return payload

    def equity(self) -> float:
        return float(self.summary.get("totalEq") or 0)

    def curve(self, days: int = 30) -> list[dict]:
        since = int((time.time() - days * 86400) * 1000)
        return db.query(
            "SELECT ts, equity FROM account_snapshots WHERE ts>=? AND env=? ORDER BY ts ASC",
            (since, self.env),
        )
