"""浏览器 WebSocket 推送：事件总线 → 前端实时数据（tick/bar/depth/trade/order/account/log/risk）。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core import event_bus

router = APIRouter()

TOPICS = ["tick", "bar", "depth", "trade", "order", "account", "log", "risk", "strategy", "forecast"]


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
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
                "risk": s.risk.status(),
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
