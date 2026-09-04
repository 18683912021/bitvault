"""OKX WebSocket 客户端：公共/私有频道、登录、心跳、指数退避重连、自动重订阅、代理支持。

私有频道登录签名：base64(hmac_sha256(secret, timestamp + 'GET' + '/users/self/verify'))

代理支持（websockets>=14）：
- 自动检测环境变量 WSS_PROXY / HTTPS_PROXY / https_proxy / all_proxy
- 通过 HTTP CONNECT 隧道穿透 HTTP 代理建立 WSS 连接
- 如需禁用代理：设置 NO_PROXY 包含目标域名，或清空上述环境变量
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Awaitable, Callable
from urllib.parse import urlparse

from websockets.asyncio.client import connect as ws_connect

from app.exchange.okx_client import OkxClient

log = logging.getLogger("bitvault.okx_ws")


def _proxy_from_env() -> str | None:
    """从环境变量检测代理 URL，优先级：WSS_PROXY > HTTPS_PROXY > https_proxy > all_proxy。"""
    for key in ("WSS_PROXY", "HTTPS_PROXY", "https_proxy", "all_proxy"):
        val = os.environ.get(key)
        if val and val.strip():
            return val.strip()
    return None


def _should_bypass_proxy(ws_url: str) -> bool:
    """检查 NO_PROXY 规则是否匹配 WS 目标主机。"""
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    if not no_proxy:
        return False
    host = urlparse(ws_url).hostname or ""
    patterns = [p.strip() for p in no_proxy.split(",") if p.strip()]
    for pat in patterns:
        if host == pat or host.endswith("." + pat) or pat == "*":
            return True
    return False


class OkxWebSocket:
    def __init__(
        self,
        url: str,
        client: OkxClient | None = None,
        on_message: Callable[[dict], Awaitable[None]] | None = None,
        on_status: Callable[[str], None] | None = None,
        name: str = "ws",
    ):
        self.url = url
        self.client = client
        self.on_message = on_message
        self.on_status = on_status
        self.name = name
        self._ws = None
        self._channels: list[dict] = []
        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        self.status = "disconnected"
        self.last_msg_ts = 0.0

    def set_channels(self, channels: list[dict]) -> None:
        """channels: [{"channel":"candle1m","instId":"BTC-USDT"}, ...]"""
        self._channels = channels

    async def start(self) -> None:
        self._stopping = False
        self._tasks.append(asyncio.create_task(self._run()))

    async def stop(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        self.status = "disconnected"

    def _login_payload(self) -> dict | None:
        if not self.client or not self.client.api_key:
            return None
        # 使用与 REST 一致的时间源（含偏移）
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc) + timedelta(milliseconds=self.client._time_offset_ms)
        ts = str(int(now.timestamp()))
        prehash = ts + "GET" + "/users/self/verify"
        sign = base64.b64encode(
            hmac.new(self.client.secret.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        return {
            "op": "login",
            "args": [
                {
                    "apiKey": self.client.api_key,
                    "passphrase": self.client.passphrase,
                    "timestamp": ts,
                    "sign": sign,
                }
            ],
        }

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                ws_kwargs: dict = {"ping_interval": None, "max_size": 2**23, "open_timeout": 15}
                proxy = _proxy_from_env()
                if proxy and not _should_bypass_proxy(self.url):
                    ws_kwargs["proxy"] = proxy
                    log.info("[%s] 使用代理: %s", self.name, proxy)
                async with ws_connect(self.url, **ws_kwargs) as ws:
                    self._ws = ws
                    self._set_status("connected")
                    backoff = 1.0
                    if self.client and self.client.api_key:
                        login = self._login_payload()
                        if login:
                            await ws.send(json.dumps(login))
                            ack = json.loads(await asyncio.wait_for(ws.recv(), 10))
                            if ack.get("event") != "login" and ack.get("code") != "0":
                                self._set_status("login_failed")
                                log.error("[%s] 登录失败: %s", self.name, ack)
                                await asyncio.sleep(5)
                                continue
                    if self._channels:
                        await ws.send(json.dumps({"op": "subscribe", "args": self._channels}))
                    recv_task = asyncio.create_task(self._recv_loop(ws))
                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    done, pending = await asyncio.wait(
                        {recv_task, ping_task}, return_when=asyncio.FIRST_EXCEPTION
                    )
                    for t in pending:
                        t.cancel()
                    for t in done:
                        exc = t.exception()
                        if exc and not isinstance(exc, asyncio.CancelledError):
                            raise exc
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("[%s] 连接异常: %s", self.name, e)
            self._ws = None
            self._set_status("reconnecting")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _recv_loop(self, ws) -> None:
        async for raw in ws:
            if raw == "pong":
                continue
            self.last_msg_ts = time.time()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("event") in ("subscribe", "unsubscribe", "error"):
                if msg.get("event") == "error":
                    log.error("[%s] 订阅错误: %s", self.name, msg)
                continue
            if self.on_message:
                await self.on_message(msg)

    async def _ping_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(20)
            await ws.send("ping")
            # 心跳超时检测：45s 无任何消息视为断线，抛出异常触发重连
            if time.time() - self.last_msg_ts > 45 and self.last_msg_ts > 0:
                raise ConnectionError("heartbeat timeout")

    def _set_status(self, s: str) -> None:
        if self.status != s:
            self.status = s
            log.info("[%s] 状态 -> %s", self.name, s)
            if self.on_status:
                self.on_status(s)

    async def send(self, payload: dict) -> None:
        if self._ws:
            await self._ws.send(json.dumps(payload))
