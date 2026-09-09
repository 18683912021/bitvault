# business 心跳来源区分：REST 兜底不得刷新"业务心跳"（防自我救活冻结）。
import asyncio
import time

from app.market.data_service import DataService


class FakeClient:
    pass


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_rest_does_not_refresh_business_heartbeat():
    ds = DataService(FakeClient())
    ds._last_candle_at = 0.0
    rows = [[1000, "100", "101", "99", "100.5", "1", "1", "1", "0"]]  # confirm=0 避免落库
    _run(ds.on_ws_message({"arg": {"channel": "candle5m", "instId": "BTC-USDT"}, "data": rows}, via_rest=True))
    assert ds._last_candle_at == 0.0, "REST 兜底不得刷新业务心跳（否则 WS 断流被自我救活）"


def test_ws_refreshes_business_heartbeat():
    ds = DataService(FakeClient())
    ds._last_candle_at = 0.0
    rows = [[1000, "100", "101", "99", "100.5", "1", "1", "1", "0"]]  # confirm=0 避免落库
    _run(ds.on_ws_message({"arg": {"channel": "candle5m", "instId": "BTC-USDT"}, "data": rows}))
    assert ds._last_candle_at > 0, "真实 WS 推送应刷新业务心跳"
