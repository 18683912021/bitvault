"""BitVault 后端主应用：服务装配、Key 运行时切换、后台任务、前端静态托管。"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import config, db
from app.account.account_service import AccountService
from app.api import routes, ws as ws_routes
from app.brain.autopilot import Autopilot
from app.core import event_bus, security
from app.exchange.okx_client import OkxClient
from app.exchange.okx_ws import OkxWebSocket
from app.market.data_service import DataService
from app.paper.paper_engine import PaperEngine
from app.risk.risk_engine import RiskEngine
from app.strategy.manager import StrategyManager
from app.trading.oms import OMS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bitvault.main")


class Services:
    """全局服务容器：公共行情常驻，私有连接随 API Key 切换重建。"""

    def __init__(self):
        db.init()
        self.public_client = OkxClient(demo=False)
        self.data = DataService(self.public_client)
        self.public_ws: OkxWebSocket | None = None
        self.business_ws: OkxWebSocket | None = None
        self.risk = RiskEngine()
        self.paper: PaperEngine | None = None
        self.brain: Autopilot | None = None
        self.manager: StrategyManager | None = None
        # 系统级模式（venue）：'paper' 模拟虚拟资金 / 'okx' 实盘真实资金。
        # autopilot 跟随此模式执行；持久化在 settings，启动恢复上次模式。
        self.venue: str = "paper"
        # 私有连接部分
        self.env = "none"
        self.private_client: OkxClient | None = None
        self.account: AccountService | None = None
        self.oms: OMS | None = None
        self.private_ws: OkxWebSocket | None = None
        self._key_row: dict | None = None
        self._time_task: asyncio.Task | None = None

    def has_key(self) -> bool:
        return self.private_client is not None

    def set_venue(self, venue: str) -> str:
        """切换系统级模式。okx 模式若未连 Key，自动落回 paper（安全兜底，不报错）。"""
        if venue not in ("paper", "okx"):
            venue = "paper"
        if venue == "okx" and not self.has_key():
            venue = "paper"   # 未连 Key 不能进实盘，静默落回模拟
        self.venue = venue
        db.set_setting("system_venue", venue)
        log.info("系统级模式切换 → %s", venue)
        event_bus.publish("venue", {"venue": venue})
        return venue

    def strategy_registry(self):
        from app.strategy.templates import STRATEGY_REGISTRY

        return STRATEGY_REGISTRY

    async def start_base(self) -> None:
        db.init()
        try:
            await self.public_client.sync_time()
        except Exception as e:
            log.warning("初始时间同步失败(OKX不可达): %s", e)
        await self.data.load_instruments()
        # 公共端点：tickers / books5 / trades
        self.public_ws = OkxWebSocket(
            config.OKX_WS_PUBLIC,
            client=None,
            on_message=self.data.on_ws_message,
            name="public",
        )
        self.public_ws.set_channels(self.data.subscribe_public_channels())
        await self.public_ws.start()
        self.data.public_ws_ref = self.public_ws   # 注入：兜底循环据此判断 WS 健康度
        # business 端点：candle* K 线（OKX 升级后 candle 仅在 business 端点）
        self.business_ws = OkxWebSocket(
            config.OKX_WS_BUSINESS,
            client=None,
            on_message=self.data.on_ws_message,
            name="business",
        )
        self.business_ws.set_channels(self.data.subscribe_business_channels())
        await self.business_ws.start()
        self.data.business_ws_ref = self.business_ws
        await self.data.start_rest_fallback()
        # 本地模拟盘：无 Key 常驻可用（paper 模式）
        self.paper = PaperEngine(self.data, self.risk)
        await self.paper.start()
        # Autopilot（纯规则自动驾驶）
        self.brain = Autopilot(self.data, self.paper, self.risk)
        self.brain.bind_svc(self)   # 注入容器：autopilot 据此读取系统级 venue + 实盘 oms/account
        await self.brain.start()
        self.manager = StrategyManager(oms=None, risk=self.risk, account=None, data=self.data,
                                       paper_oms=self.paper)
        await self.manager.start()
        self._time_task = asyncio.create_task(self._time_loop())
        # 10 分钟涨跌预测（事件合约参考）：每 10 秒计算并经 WS 推送到首页
        self._forecast_task: asyncio.Task | None = asyncio.create_task(self._forecast_loop())
        # 恢复上次激活的 Key
        row = db.query_one("SELECT * FROM api_keys WHERE is_active=1 ORDER BY id DESC")
        if row:
            try:
                await self.rebuild_connection(row)
            except Exception as e:
                log.error("恢复 Key 连接失败: %s", e)
        # 恢复上次系统级模式（venue）。若上次是 okx 但当前 Key 已断，set_venue 自动落回 paper。
        saved_venue = db.get_setting("system_venue") or "paper"
        self.venue = self.set_venue(saved_venue if isinstance(saved_venue, str) else "paper")
        log.info("BitVault 后端就绪 env=%s venue=%s", self.env, self.venue)

    async def _time_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(3600)
                await self.public_client.sync_time()
                if self.private_client:
                    await self.private_client.sync_time()
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("时间同步失败: %s", e)

    async def _forecast_loop(self) -> None:
        from app.brain import forecast10

        n = 0
        while True:
            t0 = asyncio.get_event_loop().time()
            try:
                result = forecast10.compute(self.data, "BTC-USDT")
                if result.get("direction") != "unknown" or result.get("ref_price"):
                    event_bus.publish("forecast", result)
                    n += 1
                if n % 6 == 0:
                    cost = (asyncio.get_event_loop().time() - t0) * 1000
                    log.info("forecast #%d cost=%.1fms p_up=%s px=%s", n, cost,
                             result.get("p_up"), result.get("ref_price"))
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("预测计算异常: %s", e)
            await asyncio.sleep(10)

    async def disconnect_private(self) -> None:
        if self.private_ws:
            await self.private_ws.stop()
        if self.account:
            await self.account.stop()
        if self.oms:
            await self.oms.stop()
        if self.private_client:
            await self.private_client.aclose()
        self.private_client = None
        self.account = None
        self.oms = None
        self.private_ws = None
        self._key_row = None
        self.env = "none"
        self.risk.oms = None
        self.risk.account = None
        self.risk.env = "none"
        # Key 断开时若在实盘模式，安全落回模拟（autopilot 下次决策会自动用 paper 引擎）
        if self.venue == "okx":
            self.venue = "paper"
            db.set_setting("system_venue", "paper")
            event_bus.publish("venue", {"venue": "paper"})
            log.info("Key 断开，系统级模式自动落回 paper")
        if self.manager:
            self.manager.oms = None
            self.manager.account = None

    async def rebuild_connection(self, key_row: dict) -> None:
        await self.disconnect_private()
        api_key = security.decrypt(key_row["key_enc"])
        secret = security.decrypt(key_row["secret_enc"])
        passphrase = security.decrypt(key_row["passphrase_enc"])
        env = key_row["env"]

        client = OkxClient(api_key, secret, passphrase, demo=(env == "demo"))
        await client.sync_time()
        self.private_client = client
        self.env = env
        self._key_row = dict(key_row)

        account = AccountService(client, env)
        self.account = account
        oms = OMS(client, account, self.risk, self.data, env)
        self.oms = oms
        self.risk.env = env
        await oms.start()
        await account.start()

        self.private_ws = OkxWebSocket(
            config.OKX_WS_PRIVATE_DEMO if env == "demo" else config.OKX_WS_PRIVATE,
            client=client,
            on_message=self._on_private_message,
            name=f"private-{env}",
        )
        self.private_ws.set_channels([
            {"channel": "orders", "instType": "SPOT"},
            {"channel": "orders", "instType": "SWAP"},
            {"channel": "account"},
        ])
        await self.private_ws.start()

        if self.manager:
            self.manager.oms = oms
            self.manager.account = account
        log.info("私有连接建立 env=%s key=%s", env, key_row["key_masked"])
        event_bus.publish("risk", {"event": "connection", "env": env})

    async def _on_private_message(self, msg: dict) -> None:
        arg = msg.get("arg") or {}
        channel = arg.get("channel", "")
        data = msg.get("data") or []
        if channel == "orders" and self.oms:
            await self.oms.handle_ws_orders(data)
        elif channel == "account" and self.account:
            # 节流刷新（WS 账户推送较频繁）
            now = asyncio.get_event_loop().time()
            last = getattr(self.account, "_last_ws_refresh", 0)
            if now - last > 3:
                self.account._last_ws_refresh = now
                asyncio.create_task(self._safe_refresh())

    async def _safe_refresh(self) -> None:
        try:
            await self.account.refresh()
        except Exception:
            pass


async def _lifespan(app: FastAPI):
    svc = Services()
    app.state.svc = svc
    await svc.start_base()
    yield
    await svc.disconnect_private()
    if svc.public_ws:
        await svc.public_ws.stop()
    if svc.business_ws:
        await svc.business_ws.stop()
    if svc.data and svc.data._fallback_task:
        svc.data._fallback_task.cancel()
    if svc.paper:
        await svc.paper.stop()
    if svc.brain:
        await svc.brain.stop()
    if svc.manager:
        await svc.manager.stop()
    if svc._time_task:
        svc._time_task.cancel()
    if getattr(svc, "_forecast_task", None):
        svc._forecast_task.cancel()
    await svc.public_client.aclose()


app = FastAPI(title="BitVault", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
app.include_router(routes.router)
app.include_router(ws_routes.router)

if os.path.isdir(config.FRONTEND_DIST):
    assets = os.path.join(config.FRONTEND_DIST, "assets")
    if os.path.isdir(assets):
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        candidate = os.path.join(config.FRONTEND_DIST, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(config.FRONTEND_DIST, "index.html"))
