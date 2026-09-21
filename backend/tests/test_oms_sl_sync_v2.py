# OMS 交易所侧止损同步 v2 的合约级测试（对应上一轮只读审查的 4 项改造）。
#
# 背景：v1 的实现是「先撤旧、再挂新」——一旦撤旧成功而挂新失败，交易所侧会
# 完全失去止损（且不会自动重试），比不同步更危险。v2 改为：
#   1) 合约优先 amend-algos 原地改触发价（无空窗、不误撤）
#   2) 回退路径改为「先挂新、再撤旧」——挂新失败时旧单仍在，保护不中断
#   3) 撤旧按 side/posSide/state 过滤并校验逐项 sCode，且排除刚挂的新单
#   4) 重启时可通过 find_position_sl 读回已有止损（autopilot 侧另测）
from app.market.data_service import DataService
from app.trading.oms import OMS
from conftest import FakeRisk

ETH_SWAP = {"instId": "ETH-USDT-SWAP", "instType": "SWAP", "tickSz": 0.01, "lotSz": 0.01,
            "minSz": 0.01, "ctVal": 0.1, "baseCcy": "", "quoteCcy": "", "settleCcy": "USDT"}
ETH_SPOT = {"instId": "ETH-USDT", "instType": "SPOT", "tickSz": 0.01, "lotSz": 1e-06,
            "minSz": 0.0001, "ctVal": 0.0, "baseCcy": "ETH", "quoteCcy": "USDT", "settleCcy": ""}


class FakeData(DataService):
    def __init__(self, inst):
        self.instruments = {inst["instId"]: dict(inst)}

    def is_supported(self, inst_id):
        return inst_id in self.instruments


class FakeAccount:
    def __init__(self, pos_mode="net_mode"):
        self.account_config = {"posMode": pos_mode}

    def pos_side_for(self, side, reduce_only):
        if self.account_config.get("posMode") == "long_short_mode":
            return "long" if side == "sell" else "short"
        return "net"


class FakeOkxClient:
    def __init__(self, pending=None, pending_seq=None, amend_code="0", place_code="0",
                 cancel_codes=None, raise_on=()):
        self.pending = list(pending or [])
        self.pending_seq = [list(x) for x in pending_seq] if pending_seq else None
        self._seq_i = 0
        self.amend_code = amend_code
        self.place_code = place_code
        self.cancel_codes = list(cancel_codes or [])
        self.raise_on = set(raise_on)
        self.calls: list[tuple] = []

    async def get_pending_algo_orders(self, inst_id, ord_type="conditional"):
        self.calls.append(("get_pending", inst_id, ord_type))
        if "get_pending" in self.raise_on:
            raise RuntimeError("mock 查询超时")
        if self.pending_seq is not None:
            i = min(self._seq_i, len(self.pending_seq) - 1)
            self._seq_i += 1
            return list(self.pending_seq[i])
        return list(self.pending)

    async def amend_algo_order(self, body):
        self.calls.append(("amend", dict(body)))
        if "amend" in self.raise_on:
            raise RuntimeError("mock amend 网络错误")
        return [{"algoId": body.get("algoId"), "sCode": self.amend_code, "sMsg": "" if self.amend_code == "0" else "amend rejected"}]

    async def place_algo_order(self, body):
        self.calls.append(("place", dict(body)))
        if "place" in self.raise_on:
            raise RuntimeError("mock 挂单超时")
        return [{"algoId": "NEW1", "sCode": self.place_code,
                 "sMsg": "" if self.place_code == "0" else "place rejected"}]

    async def cancel_algo_orders(self, orders):
        self.calls.append(("cancel", [dict(o) for o in orders]))
        if "cancel" in self.raise_on:
            raise RuntimeError("mock 撤单超时")
        out = []
        for i, o in enumerate(orders):
            code = self.cancel_codes[i] if i < len(self.cancel_codes) else "0"
            out.append({"algoId": o["algoId"], "sCode": code, "sMsg": "" if code == "0" else "cancel rejected"})
        return out

    def names(self):
        return [c[0] for c in self.calls]

    def first(self, name):
        for c in self.calls:
            if c[0] == name:
                return c
        return None


def _oms(inst, client, pos_mode="net_mode"):
    return OMS(client=client, account=FakeAccount(pos_mode), risk=FakeRisk(),
               data=FakeData(inst), env="live")


def _sl_algos(*algos):
    """构造 orders-algo-pending 行（含止损触发价与方向）。"""
    out = []
    for a in algos:
        out.append(a)
    return out


# ============ 1) 合约：优先 amend 原地改（无空窗） ============
def test_swap_amends_existing_algo_in_place(fresh_db):
    client = FakeOkxClient(pending=_sl_algos(
        {"algoId": "OLD1", "slTriggerPx": "2580", "sz": "5", "state": "live",
         "side": "sell"}))          # 单向持仓模式：交易所不返回 posSide
    oms = _oms(ETH_SWAP, client)
    res = oms_sync(oms, "ETH-USDT-SWAP", 2620.0, "long", 5.0)

    assert res["ok"] is True and res["mode"] == "amend" and res["algo_id"] == "OLD1"
    assert client.names() == ["get_pending", "amend"], f"应只查一次+原地改，实际 {client.names()}"
    body = client.first("amend")[1]
    assert body["algoId"] == "OLD1" and body["newSlTriggerPx"] == "2620.0" and body["newSz"] == "5.0"


def test_swap_amend_failure_falls_back_to_place_then_cancel(fresh_db):
    old = {"algoId": "OLD1", "slTriggerPx": "2580", "sz": "5", "state": "live", "side": "sell"}
    # 查询#1 找到旧单 → amend 失败 → 回退：先挂新 → 查询#2 再撤旧
    client = FakeOkxClient(pending_seq=[[old], [old]], amend_code="51000")
    oms = _oms(ETH_SWAP, client)
    res = oms_sync(oms, "ETH-USDT-SWAP", 2620.0, "long", 5.0)

    assert res["ok"] is True and res["mode"] == "replace"
    names = client.names()
    assert names[:3] == ["get_pending", "amend", "place"], f"顺序应为 查询→amend→挂新，实际 {names}"
    assert names[3] == "get_pending" and names[4] == "cancel", f"撤旧应在挂新之后，实际 {names}"


# ============ 2) 关键保护：挂新失败 → 绝不撤旧 ============
def test_place_failure_never_cancels_old_stop(fresh_db):
    """v1 的致命场景：撤旧成功+挂新失败=无保护。v2 必须保证挂新失败时不撤旧。"""
    client = FakeOkxClient(pending=[], place_code="51008")     # 无旧单可 amend，挂新被拒
    oms = _oms(ETH_SWAP, client)
    res = oms_sync(oms, "ETH-USDT-SWAP", 2620.0, "long", 5.0)

    assert res["ok"] is False and "旧止损仍在" in res["err"]
    assert "cancel" not in client.names(), "挂新失败时绝不能撤旧止损（否则交易所裸奔）"


def test_place_exception_never_cancels_old_stop(fresh_db):
    client = FakeOkxClient(pending=[], raise_on=("place",))
    oms = _oms(ETH_SWAP, client)
    res = oms_sync(oms, "ETH-USDT-SWAP", 2620.0, "long", 5.0)
    assert res["ok"] is False and "旧止损仍在" in res["err"]
    assert "cancel" not in client.names()


def test_query_failure_does_not_touch_exchange(fresh_db):
    client = FakeOkxClient(raise_on=("get_pending",))
    oms = _oms(ETH_SWAP, client)
    res = oms_sync(oms, "ETH-USDT-SWAP", 2620.0, "long", 5.0)
    assert res["ok"] is False and "查询" in res["err"]
    assert client.names() == ["get_pending"], "查询失败后不得再挂/撤"


# ============ 3) 撤旧范围过滤 + 逐项 sCode ============
def test_cancel_is_scoped_and_new_algo_excluded(fresh_db):
    """只撤「同方向的、未触发的、带止损触发价的」旧单，且必须排除刚挂的新单。"""
    mixed = _sl_algos(
        {"algoId": "OLD1", "slTriggerPx": "2580", "state": "live", "side": "sell"},
        {"algoId": "NEW1", "slTriggerPx": "2620", "state": "live", "side": "sell"},   # 刚挂的新单
        {"algoId": "OTHER_SIDE", "slTriggerPx": "2700", "state": "live", "side": "buy"},  # 反方向
        {"algoId": "TP_ONLY", "tpTriggerPx": "2700", "state": "live", "side": "sell"},    # 非止损
        {"algoId": "FIRED", "slTriggerPx": "2500", "state": "effective", "side": "sell"},  # 已触发
    )
    client = FakeOkxClient(pending_seq=[[], mixed])   # 查询#1 空 → 挂新；查询#2 混合挂单 → 撤旧
    oms = _oms(ETH_SWAP, client)
    res = oms_sync(oms, "ETH-USDT-SWAP", 2620.0, "long", 5.0)

    assert res["ok"] is True and res["canceled"] == 1 and res["cancel_errors"] == 0
    canceled = [o["algoId"] for o in client.first("cancel")[1]]
    assert canceled == ["OLD1"], f"只应撤 OLD1，实际 {canceled}"


def test_cancel_per_item_scode_is_verified(fresh_db):
    """撤单响应逐项 sCode 必须校验：失败计入 cancel_errors，且不能虚报 canceled。"""
    olds = _sl_algos(
        {"algoId": "OLD1", "slTriggerPx": "2580", "state": "live", "side": "sell"},
        {"algoId": "OLD2", "slTriggerPx": "2570", "state": "live", "side": "sell"},
    )
    client = FakeOkxClient(pending_seq=[[], olds], cancel_codes=["0", "51169"])
    oms = _oms(ETH_SWAP, client)
    res = oms_sync(oms, "ETH-USDT-SWAP", 2620.0, "long", 5.0)

    assert res["ok"] is True, "新止损已挂上，撤旧部分失败不应判为整体失败"
    assert res["canceled"] == 1 and res["cancel_errors"] == 1, f"应逐项计数，实际 {res}"
    audits = fresh_db.query("SELECT result FROM audit_logs WHERE action='sync_exchange_sl'")
    assert any("ok_with_cancel_errors" in (a["result"] or "") for a in audits), "审计应标注部分失败"


def test_hedge_mode_filters_other_side_algo(fresh_db):
    """双向持仓模式：给 LONG 同步时不得撤掉 SHORT 方向的止损。"""
    both = _sl_algos(
        {"algoId": "SHORT_SL", "slTriggerPx": "2700", "state": "live", "side": "buy", "posSide": "short"},
        {"algoId": "LONG_SL", "slTriggerPx": "2580", "state": "live", "side": "sell", "posSide": "long"},
    )
    client = FakeOkxClient(pending_seq=[[], both])
    oms = _oms(ETH_SWAP, client, pos_mode="long_short_mode")
    res = oms_sync(oms, "ETH-USDT-SWAP", 2620.0, "long", 5.0)
    canceled = [o["algoId"] for o in client.first("cancel")[1]]
    assert canceled == ["LONG_SL"], f"不得误撤另一方向止损，实际 {canceled}"
    assert res["ok"] is True


# ============ 4) 现货：不支持 amend，走先挂后撤 ============
def test_spot_skips_amend_and_uses_replace(fresh_db):
    client = FakeOkxClient(pending=_sl_algos(
        {"algoId": "OLD_SPOT", "slTriggerPx": "2580", "state": "live", "side": "sell"}))
    oms = _oms(ETH_SPOT, client)
    res = oms_sync(oms, "ETH-USDT", 2620.0, "long", 0.5)

    assert res["ok"] is True and res["mode"] == "replace"
    assert "amend" not in client.names(), "现货不支持 amend-algos，不应调用"
    assert client.names().count("place") == 1 and client.names().count("cancel") >= 1
    body = client.first("place")[1]
    assert body["tdMode"] == "cash" and body["side"] == "sell" and body["slTriggerPx"] == "2620.0"
    assert body["slOrdPx"] == "-1"
    assert "posSide" not in body and "reduceOnly" not in body, "现货不得带 posSide/reduceOnly"


# ============ 5) 参数守卫 ============
def test_missing_sz_is_rejected_without_any_call(fresh_db):
    client = FakeOkxClient()
    oms = _oms(ETH_SWAP, client)
    res = oms_sync(oms, "ETH-USDT-SWAP", 2620.0, "long", 0)
    assert res["ok"] is False and "sz" in res["err"] and client.calls == []


def test_invalid_trigger_is_rejected_without_any_call(fresh_db):
    client = FakeOkxClient()
    oms = _oms(ETH_SWAP, client)
    res = oms_sync(oms, "ETH-USDT-SWAP", 0.0, "long", 5.0)
    assert res["ok"] is False and client.calls == []


def test_swap_net_mode_sets_reduce_only(fresh_db):
    client = FakeOkxClient(pending=[])
    oms = _oms(ETH_SWAP, client, pos_mode="net_mode")
    oms_sync(oms, "ETH-USDT-SWAP", 2620.0, "long", 5.0)
    body = client.first("place")[1]
    assert body["posSide"] == "net" and body["reduceOnly"] is True
    assert body["side"] == "sell" and body["tdMode"] == "isolated"


def test_short_close_side_is_buy(fresh_db):
    client = FakeOkxClient(pending=[])
    oms = _oms(ETH_SWAP, client)
    oms_sync(oms, "ETH-USDT-SWAP", 2620.0, "short", 5.0)
    body = client.first("place")[1]
    assert body["side"] == "buy", "空头止损必须是 buy 方向"


# ---- 同步调用助手（避免 asyncio 样板重复）----
def oms_sync(oms, inst_id, sl_px, pos_side, sz, algo_id=None):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            oms.sync_position_sl(inst_id, sl_px, pos_side=pos_side, sz=sz, algo_id=algo_id))
    finally:
        loop.close()
