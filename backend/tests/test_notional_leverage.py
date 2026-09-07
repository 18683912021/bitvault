# P0-3（杠杆写入） 与 P0-6（名义金额×ctVal） 与 P1-5（acctLv fail-closed） 回归。
import asyncio

import pytest

from app.trading.oms import OMS
from app.risk.risk_engine import RiskBlocked
from conftest import FakeRisk


class FakeClient:
    def __init__(self):
        self.set_leverage_calls = []
        self.place_calls = []
        self.set_leverage_err = None
        self.place_resp = lambda: [{"ordId": "x1", "sCode": "0", "sMsg": ""}]

    async def set_leverage(self, inst_id, lever, mgn_mode):
        self.set_leverage_calls.append((inst_id, lever, mgn_mode))
        if self.set_leverage_err:
            raise self.set_leverage_err

    async def place_order(self, body):
        self.place_calls.append(body)
        return self.place_resp()


class FakeAccount:
    def __init__(self):
        self.account_config = {"acctLv": "2"}   # 2/3=合约模式（P1-5：空配置时开仓被拒见单独用例）
        self.positions = []

    def pos_side_for(self, side, reduce_only):
        return "net"


class FakeData:
    def __init__(self):
        self._ins = {
            "BTC-USDT": {"instType": "SPOT", "tickSz": 0.01, "lotSz": 0.0001,
                         "minSz": 0.0001, "ctVal": 0.0, "baseCcy": "BTC",
                         "instId": "BTC-USDT"},
            "BTC-USDT-SWAP": {"instType": "SWAP", "tickSz": 0.1, "lotSz": 1,
                              "minSz": 1, "ctVal": 0.0001, "baseCcy": "BTC",
                              "instId": "BTC-USDT-SWAP"},
        }
        self.last = 100.0

    def instrument(self, i):
        return self._ins.get(i)

    def normalize_sz(self, inst_id, sz):
        ins = self.instrument(inst_id)
        if ins["instType"] == "SWAP":
            return float(int(sz / ins["ctVal"])), None
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


@pytest.fixture()
def oms(fresh_db):
    client, acct, risk, data = FakeClient(), FakeAccount(), FakeRisk(), FakeData()
    o = OMS(client, acct, risk, data, "demo")
    return o, client, acct, risk, data


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_swap_notional_uses_ctval(oms):
    o, client, acct, risk, data = oms
    _run(o.place_intent({"inst_id": "BTC-USDT-SWAP", "side": "sell", "ord_type": "market",
                         "sz_base": 0.002, "reduce_only": False, "source": "t", "leverage": 2}))
    # 20 张 × 保护价 99.8（sell 侧 100-0.2%）× ctVal(0.0001) = 0.1996 USDT（旧实现会算出 1996）
    assert abs(risk.calls[-1]["notional"] - 0.1996) < 1e-9


def test_set_leverage_called_before_order(oms):
    o, client, acct, risk, data = oms
    _run(o.place_intent({"inst_id": "BTC-USDT-SWAP", "side": "buy", "ord_type": "market",
                         "sz_base": 0.002, "reduce_only": False, "source": "t", "leverage": 2}))
    assert ("BTC-USDT-SWAP", "2", "isolated") in client.set_leverage_calls
    assert client.place_calls, "杠杆设置成功后才允许下单"


def test_set_leverage_failure_blocks_open(oms):
    o, client, acct, risk, data = oms
    client.set_leverage_err = RuntimeError("OKX 401")
    with pytest.raises(RiskBlocked):
        _run(o.place_intent({"inst_id": "BTC-USDT-SWAP", "side": "buy", "ord_type": "market",
                             "sz_base": 0.002, "reduce_only": False, "source": "t", "leverage": 2}))
    assert client.place_calls == [], "设置杠杆失败必须禁止开仓（fail-closed）"


def test_no_set_leverage_for_reduce_or_spot(oms):
    o, client, acct, risk, data = oms
    _run(o.place_intent({"inst_id": "BTC-USDT-SWAP", "side": "sell", "ord_type": "market",
                         "sz_base": 0.002, "reduce_only": True, "source": "t", "leverage": 2}))
    assert client.set_leverage_calls == [], "减仓不应设置杠杆"
    _run(o.place_intent({"inst_id": "BTC-USDT", "side": "buy", "ord_type": "market",
                         "sz_base": 0.01, "reduce_only": False, "source": "t", "leverage": 1}))
    assert client.set_leverage_calls == []   # 减仓/现货均不设置杠杆


def test_acctlv_fail_closed_blocks_swap_open(oms):
    o, client, acct, risk, data = oms
    acct.account_config = {}   # 获取失败/未知
    with pytest.raises(RiskBlocked):
        _run(o.place_intent({"inst_id": "BTC-USDT-SWAP", "side": "sell", "ord_type": "market",
                             "sz_base": 0.002, "reduce_only": False, "source": "t", "leverage": 2}))
    # 减仓仍放行（即使无法确认模式）
    acct.account_config = {}
    _run(o.place_intent({"inst_id": "BTC-USDT-SWAP", "side": "buy", "ord_type": "market",
                         "sz_base": 0.002, "reduce_only": True, "source": "t", "leverage": 2}))
