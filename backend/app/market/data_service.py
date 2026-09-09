"""行情数据中心：合约/现货 Instrument 缓存、实时 K 线（收盘确认）、深历史下载、Ticker/盘口缓存。"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Callable

from app import config
from app.core import event_bus
from app import db

log = logging.getLogger("bitvault.market")

PERIOD_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1H": 3_600_000, "2H": 7_200_000, "4H": 14_400_000, "6H": 21_600_000,
    "12H": 43_200_000, "1D": 86_400_000, "1W": 604_800_000,
}


class DataService:
    """依赖一个公共（免签）OkxClient 与行情 WS。"""

    def __init__(self, client):
        self.client = client
        self.instruments: dict[str, dict] = {}
        self.tickers: dict[str, dict] = {}
        self.books: dict[str, dict] = {}
        self.trades: dict[str, list] = {}
        # (inst_id, period) -> {"last_confirm_ts": int}
        self._confirmed: dict[tuple, int] = {}
        self.status = {"public_ws": "disconnected", "business_ws": "disconnected", "instruments_loaded": False}
        self.public_ws_ref = None           # 由 main 注入，用于判断 WS 健康度
        self.business_ws_ref = None         # business 端点（candle 频道）
        self._last_trade_ts: dict[str, int] = {}
        self._fallback_task: asyncio.Task | None = None
        # business 通道"业务心跳"：candle 消息最新时间（candle 低频（5m 一条），
        # WS alive 不能只看连接/pong——否则 candle 推送断流会被误判健康，导致决策冻结
        self._last_candle_at: float = 0.0
        self._biz_force_reconnect_ts: float = 0.0   # 限频：最近一次强制重连时间
        # 全量标的 24h 成交额（USDT），用于选标器按总值排序（随 instruments 同步刷新）
        self.vol24h: dict[str, float] = {}
        self._refresh_task: asyncio.Task | None = None   # 支持集定时重试同步任务
        # 当前驾驶标的（动态可切换）：公共/K线频道只订阅它，切换时重订阅并预热 K 线
        self.active_inst: str = config.DEFAULT_INST_ID

    # ---------- Instruments ----------
    async def load_instruments(self, refresh: bool = True) -> None:
        """从 OKX 全量同步支持集：USDT 计价的现货 + USDT 结算的永续（state=live）。

        全覆盖同步（支持哪些币=tradeable 即为支持集），供选标器与风控使用；
        失败时保留上一次的同步结果（OKX 不可达不直接清空支持集）。
        """
        result = {}
        for inst_type in ("SPOT", "SWAP"):
            try:
                rows = await self.client.get_instruments(inst_type)
            except Exception as e:
                log.warning("拉取 %s instruments 失败: %s", inst_type, e)
                continue
            for r in rows:
                # 现货用 quoteCcy；永续用 settleCcy（SWAP 行的 quoteCcy 为空）
                qc = (r.get("quoteCcy") or "") if inst_type == "SPOT" else (r.get("settleCcy") or "")
                if qc != config.SUPPORTED_QUOTE_CCY:
                    continue
                if (r.get("state") or "") != "live":
                    continue
                result[r["instId"]] = {
                    "instId": r["instId"],
                    "instType": r["instType"],
                    "quoteCcy": r.get("quoteCcy") or "",
                    "baseCcy": r.get("baseCcy") or "",
                    "lotSz": float(r["lotSz"]),
                    "minSz": float(r["minSz"]),
                    "tickSz": float(r["tickSz"]),
                    "ctVal": float(r.get("ctVal") or 0),
                    "settleCcy": r.get("settleCcy") or "",
                    "state": r.get("state"),
                }
        if result:
            self.instruments = result
            config.InstrumentRegistry.reset(set(result))
            self.status["instruments_loaded"] = True
            await self._load_all_tickers()
            log.info("instruments 同步完成: 现货 %d / 永续 %d",
                     sum(1 for v in result.values() if v["instType"] == "SPOT"),
                     sum(1 for v in result.values() if v["instType"] == "SWAP"))
        else:
            if not self.instruments:
                # OKX 不可达：用兜底集垫底，保证选标器/风控可用（后台刷新会自动补齐）
                self.instruments = self._fallback_instruments()
                self.status["instruments_loaded"] = False
            log.warning("instruments 同步无结果，保留 %d 个已同步标的（兜底 %s）",
                        len(self.instruments), self.status["instruments_loaded"])
            await self._load_all_tickers()

    @staticmethod
    def _fallback_instruments() -> dict[str, dict]:
        """OKX 不可达时的最小兜底集：默认标的(现货)+其永续镜像。"""
        out = {}
        for inst_id in sorted(config.InstrumentRegistry.supported):
            is_swap = inst_id.endswith("-SWAP")
            out[inst_id] = {
                "instId": inst_id, "instType": "SWAP" if is_swap else "SPOT",
                "quoteCcy": "USDT", "baseCcy": inst_id.replace("-SWAP", "").split("-")[0],
                "lotSz": 0.0001, "minSz": 0.0001, "tickSz": 0.01,
                "ctVal": 0.0, "settleCcy": "USDT" if is_swap else "",
                "state": "live",
            }
        return out

    async def start_instrument_refresh(self) -> None:
        """定时重试同步支持集：OKX 网络恢复或新币上线后自动补齐（每 10 分钟）。"""
        if self._refresh_task is None:
            self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def _refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(600)
                await self.load_instruments()
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("支持集刷新失败: %s", e)

    async def _load_all_tickers(self) -> None:
        """全量 ticker：成交额（选标器"按总值排序"）+ 价格落 self.tickers。

        现货: volCcy24h 已是 USDT 金额；永续: 它是基础币数量，需乘现价换算成 USDT。
        价格同时写入 self.tickers，供 last_price/forecast 对任意支持集标的取价
        （WS 只订阅 active_inst，冷门标的切换后也有价可查）。
        """
        vols: dict[str, float] = {}
        for inst_type in ("SPOT", "SWAP"):
            try:
                rows = await self.client.get_tickers(inst_type)
            except Exception as e:
                log.debug("全量 tickers %s 失败: %s", inst_type, e)
                continue
            for t in rows:
                try:
                    if inst_type == "SWAP":
                        last = float(t.get("last") or 0)
                        vols[t["instId"]] = float(t.get("volCcy24h") or 0) * last
                    else:
                        vols[t["instId"]] = float(t.get("volCcy24h") or 0)
                    # 落价格快照（不发布 tick 广播，避免全量风暴）
                    self.tickers.setdefault(t["instId"], {}).update({
                        "instId": t["instId"],
                        "last": float(t.get("last") or 0),
                        "open24h": float(t.get("open24h") or 0),
                        "vol24h": float(t.get("vol24h") or 0),
                        "askPx": float(t.get("askPx") or 0),
                        "bidPx": float(t.get("bidPx") or 0),
                        "ts": int(t.get("ts") or time.time() * 1000),
                    })
                except (KeyError, TypeError, ValueError):
                    continue
        if vols:
            self.vol24h = vols

    def instrument(self, inst_id: str) -> dict | None:
        return self.instruments.get(inst_id)

    def is_supported(self, inst_id: str) -> bool:
        """标的是否在支持集内（OKX 已同步；未同步时回退到注册表兜底集合）。"""
        return inst_id in self.instruments or config.InstrumentRegistry.contains(inst_id)

    # ---------- 当前驾驶标的（切换） ----------
    async def set_active_inst(self, inst_id: str) -> None:
        """切换驾驶标的：退旧订新（公共+K线频道），并后台预热最近 K 线。"""
        if inst_id == self.active_inst and inst_id in self.instruments:
            return
        self.active_inst = inst_id
        if self.public_ws_ref:
            await self.public_ws_ref.resubscribe(self.subscribe_public_channels())
        if self.business_ws_ref:
            await self.business_ws_ref.resubscribe(self.subscribe_business_channels())
        self._last_trade_ts.pop(inst_id, None)  # 切标的重置去重游标，防旧成交互斥
        asyncio.ensure_future(self._warm_bars(inst_id))
        log.info("当前驾驶标的 → %s（频道已重订阅）", inst_id)

    async def _warm_bars(self, inst_id: str, limit: int = 300) -> None:
        """切换到新标的后的快速预热：各周期拉近期 300 根入库，决策/预测立即可用。"""
        for period in ("1m", "5m", "15m", "1H", "4H", "1D"):
            try:
                await self.fetch_recent_to_db(inst_id, period, limit=limit)
            except Exception as e:
                log.debug("预热K线 %s %s 失败: %s", inst_id, period, e)
            await asyncio.sleep(0.1)

    # ---------- 数量/价格规格化（向下取整，红线 R2/R5 相关） ----------
    def calculate_notional(self, inst_id: str, sz: float, px: float) -> float:
        """统一名义金额（P0-6）：现货 = 币数量×价格；合约 SWAP = 张数×价格×ctVal。

        全系统（风控/仓位/纸面/OMS/PnL/保证金）共用此口径；规格缺失时抛错（fail-closed）。"
        """
        ins = self.instrument(inst_id)
        if not ins:
            raise ValueError(f"未知标的 {inst_id}，无法计算名义金额")
        if ins["instType"] != "SWAP":
            return round(px * sz, 8)
        ctval = float(ins.get("ctVal") or 0)
        if ctval <= 0:
            raise ValueError(f"{inst_id} 合约规格缺失 ctVal，禁止按错误口径交易")
        return round(px * sz * ctval, 8)

    def normalize_sz(self, inst_id: str, sz: float) -> tuple[float, str | None]:
        """返回 (规格化数量, 错误)。SWAP 转为整数张。"""
        ins = self.instrument(inst_id)
        if not ins:
            return 0, f"未知标的 {inst_id}"
        if ins["instType"] == "SWAP" and ins["ctVal"] > 0:
            contracts = int(sz / ins["ctVal"])  # 向下取整
            if contracts < 1:
                return 0, f"数量不足 1 张（1张={ins['ctVal']} BTC）"
            return float(contracts), None
        lot = ins["lotSz"]
        normalized = int(sz / lot) * lot
        # 浮点清理
        normalized = float(f"{normalized:.8f}")
        if normalized < ins["minSz"]:
            return 0, f"数量低于最小下单量 {ins['minSz']}"
        return normalized, None

    def round_px(self, inst_id: str, px: float) -> float:
        ins = self.instrument(inst_id)
        if not ins:
            return px
        tick = ins["tickSz"]
        return round(round(px / tick) * tick, 10)

    # ---------- WS 消息入口 ----------
    async def on_ws_message(self, msg: dict, via_rest: bool = False) -> None:
        """WS 与 REST 兜底共用解析入口；via_rest=True 表示数据来自 REST 兜底——
        不得刷新 business 业务心跳（否则 REST 接管会自我救活，冻结/健康误判）。"""
        arg = msg.get("arg") or {}
        channel = arg.get("channel", "")
        data = msg.get("data") or []
        if channel.startswith("candle"):
            period = channel.replace("candle", "")
            await self._handle_candles(arg["instId"], period, data, via_rest=via_rest)
        elif channel == "tickers":
            for t in data:
                self.tickers[t["instId"]] = {
                    "instId": t["instId"], "last": float(t.get("last") or 0),
                    "open24h": float(t.get("open24h") or 0),
                    "vol24h": float(t.get("vol24h") or 0),
                    "askPx": float(t.get("askPx") or 0), "bidPx": float(t.get("bidPx") or 0),
                    "ts": int(t.get("ts") or time.time() * 1000),
                }
                event_bus.publish("tick", self.tickers[t["instId"]])
        elif channel == "books5":
            for b in data:
                book = {
                    "instId": b["instId"],
                    "asks": [[float(x[0]), float(x[1])] for x in b.get("asks", [])],
                    "bids": [[float(x[0]), float(x[1])] for x in b.get("bids", [])],
                    "ts": int(b.get("ts") or 0),
                }
                self.books[b["instId"]] = book
                event_bus.publish("depth", book)
        elif channel == "trades":
            for t in data:
                trade = {"instId": t["instId"], "px": float(t["px"]), "sz": float(t["sz"]),
                         "side": t.get("side"), "ts": int(t.get("ts") or 0)}
                # REST 轮询可能重复返回同一笔成交，按 ts 去重
                if trade["ts"] <= self._last_trade_ts.get(trade["instId"], 0):
                    continue
                self._last_trade_ts[trade["instId"]] = trade["ts"]
                lst = self.trades.setdefault(t["instId"], [])
                lst.append(trade)
                del lst[:-50]
                event_bus.publish("trade", trade)

    async def _handle_candles(self, inst_id: str, period: str, rows: list,
                             via_rest: bool = False) -> None:
        """OKX K线数据行: [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]；confirm=1 表示该 bar 已收盘。"""
        key = (inst_id, period)
        if key not in self._confirmed:
            self._confirmed[key] = 0
        for row in rows:
            ts = int(row[0])
            o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
            vol, vol_ccy = float(row[5]), float(row[6])
            confirm = str(row[8]) if len(row) > 8 else "0"
            bar = {"inst_id": inst_id, "period": period, "ts": ts, "o": o, "h": h, "l": l,
                   "c": c, "vol": vol, "vol_ccy": vol_ccy}
            if not via_rest:
                # 仅真实 WS 推送刷新业务心跳；REST 兜底复用本入口但不得自刷新
                self._last_candle_at = time.time()
            if confirm == "1":
                if ts > self._confirmed[key]:
                    self._confirmed[key] = ts
                    db.upsert_bars([(inst_id, period, ts, o, h, l, c, vol, vol_ccy, "ws")])
                    event_bus.publish("bar", bar)
            # 未收盘 bar 无消费者（P2-3：原 _buffer 从未被读取，已删除）

    def subscribe_public_channels(self) -> list[dict]:
        """公共端点频道（仅当前驾驶标的）：tickers / books5 / trades。"""
        return [
            {"channel": ch, "instId": self.active_inst}
            for ch in ("tickers", "books5", "trades")
        ]

    def subscribe_business_channels(self) -> list[dict]:
        """business 端点频道（仅当前驾驶标的）：candle* K 线。"""
        return [
            {"channel": f"candle{period}", "instId": self.active_inst}
            for period in ("1m", "5m", "15m", "1H", "4H", "1D")
        ]

    # 兼容旧入口（保留方法名，返回公共频道，避免历史调用方破坏）
    def subscribe_channels(self) -> list[dict]:
        return self.subscribe_public_channels()

    # ---------- 历史 K 线 ----------
    async def fetch_recent_to_db(self, inst_id: str, period: str, limit: int = 300) -> int:
        rows = await self.client.get_candles(inst_id, period, limit=limit)
        return self._store_candle_rows(inst_id, period, rows, "rest_recent")

    def _store_candle_rows(self, inst_id: str, period: str, rows: list, source: str) -> int:
        out = []
        for row in rows:
            if str(row[8] if len(row) > 8 else "1") != "1":
                continue  # 未收盘的不落库
            out.append((inst_id, period, int(row[0]), float(row[1]), float(row[2]),
                        float(row[3]), float(row[4]), float(row[5]), float(row[6]), source))
        if out:
            db.upsert_bars(out)
        return len(out)

    async def download_history(
        self, inst_id: str, period: str, start_ms: int, end_ms: int,
        progress_cb: Callable[[float], None] | None = None,
    ) -> int:
        """深历史下载：/market/history-candles 分页（每页 100 根，限频 20 次/2s）。"""
        total_expected = max(1, (end_ms - start_ms) // PERIOD_MS.get(period, 60_000))
        cursor_after = end_ms  # 从 end 向 start 回溯
        stored, dup_guard = 0, 0
        while cursor_after > start_ms and dup_guard < 200:
            rows = await self.client.get_history_candles(inst_id, period, after=cursor_after)
            if not rows:
                break
            self._store_candle_rows(inst_id, period, rows, "rest_history")
            oldest = min(int(r[0]) for r in rows)
            if oldest >= cursor_after - 1:  # 防御：无进展
                dup_guard += 1
            stored += len(rows)
            cursor_after = oldest
            if progress_cb:
                progress_cb(min(0.99, stored / total_expected))
            await asyncio.sleep(0.12)
        log.info("历史下载完成 %s %s: %s 根", inst_id, period, stored)
        return stored

    def bars_from_db(self, inst_id: str, period: str, start_ms: int, end_ms: int) -> list[dict]:
        rows = db.query(
            "SELECT open_time, o,h,l,c,vol FROM bars WHERE inst_id=? AND period=? "
            "AND open_time>=? AND open_time<=? ORDER BY open_time ASC",
            (inst_id, period, start_ms, end_ms),
        )
        return [
            {"ts": r["open_time"], "o": r["o"], "h": r["h"], "l": r["l"],
             "c": r["c"], "vol": r["vol"]}
            for r in rows
        ]

    def last_price(self, inst_id: str, max_age_s: int = 30) -> float:
        """返回最新价。max_age_s 秒内有效，过期返回 0.0（调用方应跳过交易）。"""
        t = self.tickers.get(inst_id)
        if not t:
            return 0.0
        ts_ms = int(t.get("ts") or 0)
        if ts_ms > 0 and (time.time() * 1000 - ts_ms) > max_age_s * 1000:
            log.warning("last_price 数据过期 %ss，inst=%s，拒绝使用旧价格", max_age_s, inst_id)
            return 0.0
        return float(t["last"])

    # ---------- REST 轮询兜底（WS 不通时由 REST 接管，复用 on_ws_message 解析） ----------
    def _ws_alive(self, ws=None) -> bool:
        """判断指定 WS 是否健康（默认查公共端点）。"""
        target = ws if ws is not None else self.public_ws_ref
        return bool(
            target
            and target.status == "connected"
            and time.time() - target.last_msg_ts < 8
        )

    async def start_rest_fallback(self) -> None:
        if self._fallback_task is None:
            self._fallback_task = asyncio.create_task(self._fallback_loop())

    async def _fallback_loop(self) -> None:
        """公共端点断 → REST 接管 ticker/盘口/成交；business 端点断 → REST 接管 K 线。
        任一端点健康时，对应数据仍低频补库（供回测）。"""
        tick = 0
        while True:
            try:
                pub_alive = self._ws_alive(self.public_ws_ref)
                biz_alive = self._ws_alive(self.business_ws_ref) and (
                    time.time() - self._last_candle_at < 150.0)
                # 自愈：连接看似在但 300s 无任何 candle 数据 → 强制重连（间歇性抖动复位）
                if (not biz_alive and self.business_ws_ref
                        and self.business_ws_ref.status == "connected"
                        and time.time() - self._last_candle_at > 300.0
                        and time.time() - self._biz_force_reconnect_ts > 60.0):
                    self._biz_force_reconnect_ts = time.time()
                    log.warning("business WS 300s 无 candle 数据，强制重连（自愈）")
                    asyncio.ensure_future(self.business_ws_ref.force_reconnect())
                self.status["public_ws"] = "connected" if pub_alive else "rest_fallback"
                self.status["business_ws"] = "connected" if biz_alive else "rest_fallback"

                # ---- 公共端点（仅当前驾驶标的） ----
                if not pub_alive:
                    await self._poll_ticker(self.active_inst)
                    await self._poll_books(self.active_inst)
                    if tick % 2 == 0:
                        await self._poll_trades(self.active_inst)
                # ---- K 线端点 ----
                if biz_alive:
                    # business 健康：每 ~30s 补一次最近 K 线入库（供回测，不打 bar 事件）
                    if tick % 15 == 0:
                        for period in ("1m", "5m", "15m", "1H", "4H", "1D"):
                            try:
                                await self.fetch_recent_to_db(self.active_inst, period, limit=100)
                            except Exception as e:
                                log.debug("补K失败 %s %s: %s", self.active_inst, period, e)
                else:
                    # business 不通：REST 接管 K 线（打 bar 事件让前端可见）
                    if tick % 3 == 0:
                        for period in ("1m", "5m", "15m", "1H", "4H", "1D"):
                            await self._poll_candles(self.active_inst, period)
                tick += 1
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("REST 兜底异常: %s", e)
            await asyncio.sleep(2)

    async def _poll_ticker(self, inst_id: str) -> None:
        try:
            rows = await self.client.get_ticker(inst_id)
            if rows:
                await self.on_ws_message({"arg": {"channel": "tickers", "instId": inst_id}, "data": rows}, via_rest=True)
        except Exception as e:
            log.debug("poll ticker %s 失败: %s", inst_id, e)

    async def _poll_books(self, inst_id: str) -> None:
        try:
            rows = await self.client.get_books5(inst_id)
            if rows:
                # /market/books REST 响应不含 instId 字段，与 WS books5 不同；补齐以复用同一解析逻辑
                for r in rows:
                    r.setdefault("instId", inst_id)
                await self.on_ws_message({"arg": {"channel": "books5", "instId": inst_id}, "data": rows}, via_rest=True)
        except Exception as e:
            log.debug("poll books %s 失败: %s", inst_id, e)

    async def _poll_trades(self, inst_id: str) -> None:
        try:
            rows = await self.client.get_trades_public(inst_id, limit=10)
            if rows:
                await self.on_ws_message({"arg": {"channel": "trades", "instId": inst_id}, "data": rows}, via_rest=True)
        except Exception as e:
            log.debug("poll trades %s 失败: %s", inst_id, e)

    async def _poll_candles(self, inst_id: str, period: str) -> None:
        try:
            rows = await self.client.get_candles(inst_id, period, limit=10)
            if rows:
                await self.on_ws_message({"arg": {"channel": f"candle{period}", "instId": inst_id}, "data": rows}, via_rest=True)
        except Exception as e:
            log.debug("poll candles %s %s 失败: %s", inst_id, period, e)
