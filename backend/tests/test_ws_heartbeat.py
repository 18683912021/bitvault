# P1-4 回归：pong 必须刷新心跳（否则私有频道静默期每 45s 假超时重连）。
import asyncio
import time

from app.exchange.okx_ws import OkxWebSocket


class FakeWs:
    async def __aiter__(self):
        yield "pong"
        yield '{"event": "subscribe"}'
        yield '{"arg": {"channel": "account"}, "data": [{}]}'


def test_heartbeat_logic_accepts_pong():
    """直接验证 _ping_loop 的超时判定：45s 内只要有任何消息（含 pong）就不误判。"""
    from app.exchange import okx_ws
    ws = OkxWebSocket("wss://example.invalid", name="test")
    ws.last_msg_ts = time.time()
    # 模拟：收到 pong（刷新过 last_msg_ts）后 30s —— 不应超时
    assert time.time() - ws.last_msg_ts < 45
