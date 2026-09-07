# P0-2 回归：策略实例的平仓/撤单必须经 executor 路由（paper → PaperEngine，绝不直连 OMS）。
import asyncio

import pytest

from app.strategy.manager import StrategyManager
from conftest import FakeRisk


class FakePaper:
    def __init__(self):
        self.intents = []
        self.cancels = []

    async def place_intent(self, intent):
        self.intents.append(intent)
        return {"ok": True}

    async def cancel_instance_orders(self, instance_id):
        self.cancels.append(instance_id)
        return []


class FakeData:
    def last_price(self, inst_id, *a, **k):
        return 100.0


class _NoopStrategy:
    async def on_stop(self):
        return None


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mk_instance(fresh_db, mode="paper"):
    fresh_db.execute(
        "INSERT INTO strategies (name, type, params_json, created_at) VALUES (?,?,?,?)",
        ("t", "ma_cross", "{}", 1000))
    sid = fresh_db.query_one("SELECT id FROM strategies LIMIT 1")["id"]
    fresh_db.execute(
        "INSERT INTO strategy_instances (strategy_id, name, inst_id, mode, status, params_json, state_json)"
        " VALUES (?,?,?,?,?,?,?)",
        (sid, "i1", "BTC-USDT", mode, "running_paper", "{}",
         '{"pos_side": "long", "open_sz": 0.1}'))
    return fresh_db.query_one("SELECT id FROM strategy_instances LIMIT 1")["id"]


def test_paper_close_never_touches_real_oms(fresh_db):
    """paper 实例平仓：oms=None（无 Key）也不崩溃，且订单只进 PaperEngine。"""
    iid = _mk_instance(fresh_db)
    paper = FakePaper()
    mgr = StrategyManager(oms=None, risk=FakeRisk(), account=None,
                          data=FakeData(), paper_oms=paper)
    ok = _run(mgr.close_instance_position(iid))
    assert ok is True
    assert len(paper.intents) == 1
    intent = paper.intents[0]
    assert intent["inst_id"] == "BTC-USDT"
    assert intent["reduce_only"] is True
    assert intent["side"] == "sell"


def test_stop_instance_routes_cancel_to_paper(fresh_db):
    iid = _mk_instance(fresh_db)
    paper = FakePaper()
    mgr = StrategyManager(oms=None, risk=FakeRisk(), account=None,
                          data=FakeData(), paper_oms=paper)
    mgr.runtimes[iid] = {"mode": "paper", "strategy": _NoopStrategy()}
    _run(mgr.stop_instance(iid, close_position=True))
    assert iid in paper.cancels, "撤单应路由到 PaperEngine（P0-2）"
    assert len(paper.intents) == 1, "平仓请求进 PaperEngine 而非 OMS"
