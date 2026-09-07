# P0-5 核心回归：纸面 reduce-only 单绝不能被当作反向开仓（含限价/市价/部分/全平）。
import asyncio

from app.paper.paper_engine import PaperEngine
from conftest import FakeRisk


class FakeData:
    def __init__(self):
        self._ins = {
            "BTC-USDT": {"instType": "SPOT", "tickSz": 0.01, "lotSz": 0.0001,
                         "minSz": 0.0001, "ctVal": 0.0, "baseCcy": "BTC",
                         "quoteCcy": "USDT", "settleCcy": "", "instId": "BTC-USDT"},
            "BTC-USDT-SWAP": {"instType": "SWAP", "tickSz": 0.1, "lotSz": 1,
                              "minSz": 1, "ctVal": 0.0001, "baseCcy": "BTC",
                              "quoteCcy": "USDT", "settleCcy": "USDT",
                              "instId": "BTC-USDT-SWAP"},
        }
        self.last = 100.0

    def instrument(self, inst_id):
        return self._ins.get(inst_id)

    def normalize_sz(self, inst_id, sz):
        ins = self.instrument(inst_id)
        if ins["instType"] == "SWAP":
            contracts = int(sz / ins["ctVal"])
            return float(contracts), None if contracts >= 1 else "数量不足 1 张"
        return sz, None

    def round_px(self, inst_id, px):
        return px

    def calculate_notional(self, inst_id, sz, px):
        ins = self.instrument(inst_id)
        if not ins:
            raise ValueError("规格缺失")
        if ins["instType"] == "SWAP":
            return round(px * sz * ins["ctVal"], 8)
        return round(px * sz, 8)

    def last_price(self, inst_id, max_age_s=30):
        return self.last


def _engine(fresh_db):
    data = FakeData()
    risk = FakeRisk()
    return PaperEngine(data, risk), data, risk


def _fund(fresh_db, usdt=10_000.0):
    fresh_db.set_setting("paper_account", {"usdt": usdt, "coins": {}, "initial": usdt,
                                           "lev_positions": []})


def _sync(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_reduce_only_limit_close_not_reverse(fresh_db):
    """LONG 开仓 → 限价 reduce-only SELL → bar 匹配成交 → 必须只是平多，绝不产生空头/剩余币。"""
    pe, data, risk = _engine(fresh_db)
    _fund(fresh_db)
    # 1) 市价开多
    _sync(pe.place_intent({
        "inst_id": "BTC-USDT", "side": "buy", "ord_type": "market",
        "sz_base": 0.01, "reduce_only": False, "source": "t", "leverage": 1}))
    acct = fresh_db.get_setting("paper_account") or {}
    assert float(acct["coins"].get("BTC", 0)) > 0.009, "现货开多应持有 BTC"

    # 2) 挂 reduce-only 限价平仓单
    _sync(pe.place_intent({
        "inst_id": "BTC-USDT", "side": "sell", "ord_type": "limit", "px": 99.0,
        "sz_base": 0.01, "reduce_only": True, "source": "t", "leverage": 1}))
    # 3) bar 匹配成交
    pe._match_bar({"inst_id": "BTC-USDT", "o": 99.0, "h": 100.5, "l": 98.0, "c": 99.0})
    acct = fresh_db.get_setting("paper_account") or {}
    assert float(acct["coins"].get("BTC", 0)) <= 1e-9, "reduce-only 平多后不应留有 BTC"
    assert list(acct["lev_positions"]) == [], "现货平仓不应出现杠杆仓位"
    assert float(acct["usdt"]) > 9_000.0, "卖出成交应回收资金"
    row = fresh_db.query_one(
        "SELECT reduce_only FROM orders WHERE venue='paper' AND side='sell' ORDER BY id DESC LIMIT 1")
    assert row and int(row["reduce_only"]) == 1, "reduce_only 必须持久化到 orders 行"


def test_reduce_only_market_close_swap(fresh_db):
    """合约：开多 → reduce-only 市价平多 → 无反向空头仓位。"""
    pe, data, risk = _engine(fresh_db)
    _fund(fresh_db)
    _sync(pe.place_intent({
        "inst_id": "BTC-USDT-SWAP", "side": "buy", "ord_type": "market",
        "sz_base": 0.001, "reduce_only": False, "source": "t", "leverage": 2}))
    acct = fresh_db.get_setting("paper_account") or {}
    assert len(acct["lev_positions"]) == 1 and acct["lev_positions"][0]["side"] == "long"

    _sync(pe.place_intent({
        "inst_id": "BTC-USDT-SWAP", "side": "sell", "ord_type": "market",
        "sz_base": 0.001, "reduce_only": True, "source": "t", "leverage": 2}))
    acct = fresh_db.get_setting("paper_account") or {}
    assert len(acct["lev_positions"]) == 0, "平多后应无杠杆仓位（含空头，P0-5 禁止反向）"


def test_partial_reduce_keeps_rest(fresh_db):
    pe, data, risk = _engine(fresh_db)
    _fund(fresh_db)
    _sync(pe.place_intent({
        "inst_id": "BTC-USDT-SWAP", "side": "buy", "ord_type": "market",
        "sz_base": 0.002, "reduce_only": False, "source": "t", "leverage": 2}))
    _sync(pe.place_intent({
        "inst_id": "BTC-USDT-SWAP", "side": "sell", "ord_type": "market",
        "sz_base": 0.001, "reduce_only": True, "source": "t", "leverage": 2}))
    acct = fresh_db.get_setting("paper_account") or {}
    assert len(acct["lev_positions"]) == 1, "部分平仓后剩余仓位仍在"
    assert abs(float(acct["lev_positions"][0]["sz"]) - 10.0) < 1e-9  # 20 张开仓 - 10 张平仓 = 10 张


def test_liquidation_clid_consistent(fresh_db):
    """P1（F5）：爆仓 order 与 trade 使用同一 cl_ord_id。"""
    pe, data, risk = _engine(fresh_db)
    _fund(fresh_db)
    _sync(pe.place_intent({
        "inst_id": "BTC-USDT-SWAP", "side": "buy", "ord_type": "market",
        "sz_base": 0.001, "reduce_only": False, "source": "t", "leverage": 2}))
    acct = fresh_db.get_setting("paper_account") or {}
    liq = float(acct["lev_positions"][0]["liq_px"])
    pe._check_liquidation("BTC-USDT-SWAP", liq - 1.0, liq - 1.0)
    rows = fresh_db.query(
        "SELECT t.cl_ord_id FROM trades t JOIN orders o ON t.cl_ord_id=o.cl_ord_id "
        "WHERE o.source='autopilot:liquidation'")
    assert len(rows) == 1, "爆仓 trade 必须能 JOIN 上 orders（同 cl_ord_id）"
