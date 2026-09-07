"""OKX API v5 REST 客户端：HMAC-SHA256 签名、限频退避、模拟盘头、时间同步。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from app import config

log = logging.getLogger("bitvault.okx")


class OkxApiError(Exception):
    def __init__(self, code: str, msg: str, data: Any = None):
        super().__init__(f"OKX[{code}] {msg}")
        self.code = str(code)
        self.msg = msg
        self.data = data


class OkxClient:
    """一个客户端实例绑定一组 API Key（demo/live）。公共接口无需 Key。"""

    def __init__(
        self,
        api_key: str = "",
        secret: str = "",
        passphrase: str = "",
        demo: bool = True,
    ):
        self.api_key = api_key
        self.secret = secret
        self.passphrase = passphrase
        self.demo = demo
        self._time_offset_ms = 0.0  # 服务器时间 - 本地时间
        self._http = httpx.AsyncClient(
            base_url=config.OKX_REST_BASE,
            timeout=httpx.Timeout(15.0, connect=8.0),
            limits=httpx.Limits(max_connections=20),
        )
        self._sem = asyncio.Semaphore(8)
        self._last_req_ts = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------- 时间同步（签名时间戳容差约 ±30s，漂移会导致全部请求失败） ----------
    async def sync_time(self) -> float:
        t0 = time.time()
        data = await self.request("GET", "/api/v5/public/time", auth=False)
        server_ms = float(data[0]["ts"])
        rtt = (time.time() - t0) * 1000
        self._time_offset_ms = server_ms - (t0 * 1000 + rtt / 2)
        return self._time_offset_ms

    def _iso_ts(self) -> str:
        now = datetime.now(timezone.utc) + timedelta(milliseconds=self._time_offset_ms)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    # ---------- 签名与请求 ----------
    def _sign(self, ts: str, method: str, request_path: str, body: str) -> str:
        message = ts + method + request_path + body
        mac = hmac.new(
            self.secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        )
        return base64.b64encode(mac.digest()).decode()

    def _headers(self, method: str, request_path: str, body: str) -> dict:
        ts = self._iso_ts()
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h.update(
                {
                    "OK-ACCESS-KEY": self.api_key,
                    "OK-ACCESS-SIGN": self._sign(ts, method, request_path, body),
                    "OK-ACCESS-TIMESTAMP": ts,
                    "OK-ACCESS-PASSPHRASE": self.passphrase,
                }
            )
        if self.demo:
            h["x-simulated-trading"] = "1"
        return h

    async def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        body: dict | None = None,
        auth: bool = True,
        retries: int = 3,
    ) -> Any:
        query = ""
        if params:
            query = "?" + urlencode({k: v for k, v in params.items() if v is not None})
        request_path = path + query
        body_str = json.dumps(body) if body else ""
        last_err: Exception | None = None

        for attempt in range(retries):
            # 简单令牌限频：全局 ≤ 15 req/s（OKX 各端点限额保守取值）
            async with self._sem:
                gap = 1.0 / 15.0
                now = time.monotonic()
                wait = self._last_req_ts + gap - now
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_req_ts = time.monotonic()

                headers = self._headers(method, request_path, body_str) if auth else {"Content-Type": "application/json"}
                if self.demo and not auth:
                    headers["x-simulated-trading"] = "1"
                try:
                    resp = await self._http.request(
                        method, request_path, headers=headers, content=body_str or None
                    )
                except httpx.HTTPError as e:
                    last_err = e
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue

            if resp.status_code in (429, 500, 502, 503):
                last_err = OkxApiError(str(resp.status_code), "rate/limit or server error")
                await asyncio.sleep(0.8 * (2**attempt))
                continue

            try:
                payload = resp.json()
            except Exception:
                last_err = OkxApiError(str(resp.status_code), resp.text[:200])
                continue

            code = str(payload.get("code", ""))
            if code == "0":
                return payload.get("data", [])
            # 频控类错误码重试：50011 限频、50013 请求繁忙
            if code in ("50011", "50013") and attempt < retries - 1:
                last_err = OkxApiError(code, payload.get("msg", ""))
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            raise OkxApiError(code, payload.get("msg", "unknown"), payload.get("data"))
        raise last_err or OkxApiError("-1", "request failed")

    # ---------- 公共接口 ----------
    async def get_instruments(self, inst_type: str) -> list[dict]:
        return await self.request("GET", "/api/v5/public/instruments", {"instType": inst_type}, auth=False)

    async def get_ticker(self, inst_id: str) -> list[dict]:
        return await self.request("GET", "/api/v5/market/ticker", {"instId": inst_id}, auth=False)

    async def get_tickers(self, inst_type: str) -> list[dict]:
        """全量 tickers（按市场类型一次拉取，用于成交额排序）。"""
        return await self.request("GET", "/api/v5/market/tickers", {"instType": inst_type}, auth=False)

    async def get_candles(self, inst_id: str, bar: str, limit: int = 300, after: int | None = None) -> list[list]:
        return await self.request(
            "GET", "/api/v5/market/candles",
            {"instId": inst_id, "bar": bar, "limit": min(limit, 300), "after": after}, auth=False,
        )

    async def get_history_candles(self, inst_id: str, bar: str, after: int | None = None, before: int | None = None) -> list[list]:
        return await self.request(
            "GET", "/api/v5/market/history-candles",
            {"instId": inst_id, "bar": bar, "after": after, "before": before}, auth=False,
        )

    async def get_funding_rate(self, inst_id: str) -> list[dict]:
        return await self.request("GET", "/api/v5/public/funding-rate", {"instId": inst_id}, auth=False)

    async def get_books5(self, inst_id: str) -> list[dict]:
        # OKX 已废弃 /market/books5，统一用 /market/books?sz=5
        return await self.request("GET", "/api/v5/market/books", {"instId": inst_id, "sz": 5}, auth=False)

    async def get_trades_public(self, inst_id: str, limit: int = 10) -> list[dict]:
        return await self.request(
            "GET", "/api/v5/market/trades", {"instId": inst_id, "limit": min(limit, 500)}, auth=False
        )

    # ---------- 私有接口 ----------
    async def get_account_config(self) -> list[dict]:
        return await self.request("GET", "/api/v5/account/config")

    async def get_balance(self, ccy: str | None = None) -> list[dict]:
        return await self.request("GET", "/api/v5/account/balance", {"ccy": ccy} if ccy else None)

    async def get_positions(self, inst_id: str | None = None) -> list[dict]:
        return await self.request("GET", "/api/v5/account/positions", {"instId": inst_id} if inst_id else None)

    async def set_leverage(self, inst_id: str, lever: str, mgn_mode: str) -> list[dict]:
        return await self.request(
            "POST", "/api/v5/account/set-leverage",
            {"instId": inst_id, "lever": lever, "mgnMode": mgn_mode},
        )

    async def place_order(self, order: dict) -> list[dict]:
        return await self.request("POST", "/api/v5/trade/order", body=order)

    async def cancel_order(self, inst_id: str, ord_id: str | None = None, cl_ord_id: str | None = None) -> list[dict]:
        body: dict = {"instId": inst_id}
        if ord_id:
            body["ordId"] = ord_id
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id
        return await self.request("POST", "/api/v5/trade/cancel-order", body=body)

    async def get_order(self, inst_id: str, ord_id: str | None = None, cl_ord_id: str | None = None) -> list[dict]:
        params: dict = {"instId": inst_id}
        if ord_id:
            params["ordId"] = ord_id
        if cl_ord_id:
            params["clOrdId"] = cl_ord_id
        return await self.request("GET", "/api/v5/trade/order", params)

    async def get_pending_orders(self, inst_id: str | None = None) -> list[dict]:
        return await self.request("GET", "/api/v5/trade/orders-pending", {"instId": inst_id} if inst_id else None)

    async def get_orders_history(self, inst_id: str, limit: int = 100) -> list[dict]:
        return await self.request(
            "GET", "/api/v5/trade/orders-history-archive", {"instId": inst_id, "limit": limit}
        )

    async def get_fills(self, inst_id: str, limit: int = 100) -> list[dict]:
        return await self.request("GET", "/api/v5/trade/fills", {"instId": inst_id, "limit": limit})
