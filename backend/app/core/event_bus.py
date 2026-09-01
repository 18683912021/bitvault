"""进程内异步事件总线：K线/成交/订单/账户/日志/风控 六类事件。"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any, Callable

_topics_subs: dict[str, list[asyncio.Queue]] = defaultdict(list)


def subscribe(topics: list[str]) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=2000)
    for t in topics:
        _topics_subs[t].append(q)
    return q


def unsubscribe(q: asyncio.Queue, topics: list[str]) -> None:
    for t in topics:
        if q in _topics_subs[t]:
            _topics_subs[t].remove(q)


def publish(topic: str, data: Any) -> None:
    for q in list(_topics_subs.get(topic, [])):
        try:
            q.put_nowait((topic, data, time.time()))
        except asyncio.QueueFull:
            # 慢消费者丢弃最旧事件，避免阻塞行情主链路
            try:
                q.get_nowait()
                q.put_nowait((topic, data, time.time()))
            except Exception:
                pass


def handler_names() -> list[str]:
    return sorted(_topics_subs.keys())
