"""浏览器 WebSocket 推送：事件总线 → 前端实时数据（tick/bar/depth/trade/order/account/log/risk）。
P1-2：WebSocket 连接必须携带有效 token（?token=xxx），无 token 或无效 token 拒绝连接。"""
from __future__ import annotations

import asyncio
import hmac
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app import config
from app.core import event_bus

router = APIRouter()

TOPICS = ["tick", "bar", "depth", "trade", "order", "account", "log", "risk", "strategy", "forecast"]


def _ws_verify_token(token: str) -> bool:
    """校验 WebSocket ?token 参数（与 HTTP Bearer Token 同源校验）。"""
    expected = config.API_TOKEN.strip()
    if not expected:
        return False   # fail-closed：未配置令牌时拒绝一切 WS 连接
    return bool(token) and hmac.compare_digest(token, expected)


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    # P1-2：WebSocket 认证 —— 从 query params 提取 token 校验
    token = websocket.query_params.get("token", "")
    if not _ws_verify_token(token):
        await websocket.close(code=4001, reason="access denied: invalid or missing token")
        return
    await websocket.accept()
    q = event_bus.subscribe(TOPICS)
    try:
        # 连接即推快照
        s = websocket.app.state.svc
        await websocket.send_text(json.dumps({
            "topic": "snapshot",
            "data": {
                "tickers": s.data.tickers,
                "env": s.env,
                "risk": s.risk.status(s.venue),
            },
        }, ensure_ascii=False, default=str))
        while True:
            topic, data, ts = await q.get()
            payload = {"topic": topic, "data": data, "ts": ts}
            await websocket.send_text(json.dumps(payload, ensure_ascii=False, default=str))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        event_bus.unsubscribe(q, TOPICS)