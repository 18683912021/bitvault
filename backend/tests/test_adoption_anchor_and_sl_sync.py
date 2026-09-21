# 收养锚点一致性 + OKX 动态止损同步 回归测试。
#
# 背景（来自《多单 SL 跑到价格上方》全链路审查）：
#   问题1：_discover_position 收养时 SL 取 plan["sl"]（锚定"收养时市价"），
#          TP1/TP2/TP3 锚定 entry_px → 当市价 P > entry + risk_dist 时，
#          多头 SL 会落到开仓价之上（口径缺陷）。
#   问题2：TP1 保本 / TP2 trailing 只改本地 pos["sl_px"]，交易所侧那份
#          开仓时挂的 slTriggerPx 仍停在旧价 → 进程若在此期间崩溃，
#          实际保护比本地显示的更宽松（保护风险）。
#
# 本测试锁定：① 收养后 SL/TP 同锚点且方向正确；② 交易所侧止损随动态止损同步、
# 只朝有利方向、同价不重复下单、失败可见可追踪；③ trailing 抬高 SL 越过现价时
# 当 tick 即平仓（§6 行为确认，不做代码改动）。
import asyncio
import time
from types import SimpleNamespace

from app.brain import autopilot as ap_mod
from app.brain.autopilot import TRAIL_ATR, Autopilot
from app.market.data_service import DataService
from app.paper.paper_engine import PaperEngine
from conftest import FakeRisk

ETH = {"instId": "ETH-USDT", "instType": "SPOT", "tickSz": 0.01, "lotSz": 1e-06,
       "minSz": 0.0001, "ctVal": 0.0, "baseCcy": "ETH", "quoteCcy": "USDT", "settleCcy": ""}


class FakeData(DataService):
    def __init__(self, insts, last=2650.0):
        self.instruments = {i["instId"]: dict(i) for i in insts}
        self.last = last
        self.delegated = set()

    def set_delegated_insts(self, insts):
        self.delegated = set(insts)

    def is_supported(self, inst_id):
        return inst_id in self.instruments

    def last_price(self, inst_id, max_age_s=30):
        return self.last


class FakeOkxAccount:
    """可被平仓单扣减的 OKX 账户替身（模拟交易所持仓随成交变化）。"""

    def __init__(self):
        self.positions: list[dict] = []
        self.summary = {"totalEq": 10000.0, "details": [{"ccy": "USDT", "availBal": "10000"}]}

    def set_pos(self, inst_id, sz, entry_px):
        self.positions = [{"instId": inst_id, "pos": str(sz), "avgPx": str(entry_px)}]


class FakeOms:
    """OKX 下单引擎替身：记录 place_intent / sync_position_sl / find_position_sl 调用。"""

    def __init__(self, acct: FakeOkxAccount, fail_sync=False, existing_algo=None):
        self.acct = acct
        self.fail_sync = fail_sync
        self.existing_algo = existing_algo      # 模拟交易所侧已有止损单（用于重启恢复用例）
        self.calls: list[tuple] = []

    async def place_intent(self, intent):
        self.calls.append(("place_intent", dict(intent)))
        if intent.get("reduce_only"):
            for p in self.acct.positions:
                if p["instId"] == intent["inst_id"]:
                    left = max(0.0, float(p["pos"]) - float(intent["sz_base"]))
                    p["pos"] = str(left)
        return {"cl_ord_id": f"T{len(self.calls)}", "state": "filled", "sz": intent["sz_base"]}

    async def find_position_sl(self, inst_id, pos_side="long"):
        self.calls.append(("find_sl", inst_id, pos_side))
        return self.existing_algo

    async def sync_position_sl(self, inst_id, sl_px, pos_side="long", sz=None, algo_id=None):
        self.calls.append(("sync_sl", inst_id, sl_px, pos_side, sz, algo_id))
        if self.fail_sync:
            return {"ok": False, "mode": None, "err": "mock：交易所拒绝止损单"}
        return {"ok": True, "mode": "amend" if algo_id else "replace", "algo_id": "ALGO1",
                "canceled": 0, "cancel_errors": 0, "sl_px": sl_px}

    def sync_calls(self):
        return [c for c in self.calls if c[0] == "sync_sl"]

    def find_calls(self):
        return [c for c in self.calls if c[0] == "find_sl"]

    def place_calls(self):
        return [c for c in self.calls if c[0] == "place_intent"]


def _sync(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _paper_ap(fresh_db, last=2650.0, entry=2600.0, sz=0.5, side="long"):
    """paper venue 的 autopilot，账户里带一笔现成的 venue 持仓（供收养）。"""
    data = FakeData([ETH], last=last)
    paper = PaperEngine(data, FakeRisk())
    ap = Autopilot(data, paper, FakeRisk())
    ap.bind_svc(SimpleNamespace(venue="paper", inst_id="ETH-USDT", has_key=lambda: False,
                                oms=None, account=SimpleNamespace(positions=[], summary={}),
                                private_ws=None))
    acct = {"usdt": 10000.0, "coins": {}, "initial": 10000.0, "lev_positions": [
        {"inst_id": "ETH-USDT", "coin": "ETH", "sz": sz, "side": side, "entry_px": entry,
         "leverage": 10, "margin": 0.0, "liq_px": entry * 0.9, "open_ts": int(time.time() * 1000),
         "cl_ord_id": "seed"}]}
    fresh_db.set_setting("paper_account", acct)
    return ap, data


def _okx_ap(fresh_db, last=2650.0, acct_pos=0.5, acct_entry=2600.0, fail_sync=False):
    """okx venue 的 autopilot（live_ready），下单走 FakeOms。"""
    data = FakeData([ETH], last=last)
    paper = PaperEngine(data, FakeRisk())
    acct = FakeOkxAccount()
    acct.set_pos("ETH-USDT", acct_pos, acct_entry)
    oms = FakeOms(acct, fail_sync=fail_sync)
    ap = Autopilot(data, paper, FakeRisk())
    ap.bind_svc(SimpleNamespace(venue="okx", inst_id="ETH-USDT", has_key=lambda: True,
                                oms=oms, account=acct, private_ws=None))
    return ap, data, oms


def _mk_pos(inst_id="ETH-USDT", side="long", entry=2600.0, sz=0.5, sl=2580.0,
            risk=20.0, atr_pct=0.2, lev=10):
    sign = -1 if side == "short" else 1
    return {
        "inst_id": inst_id, "cl_ord_id": "discovered", "side": side,
        "entry_px": entry, "sl_px": sl, "sz": sz, "orig_sz": sz,
        "tp1_px": round(entry + sign * risk, 2), "tp2_px": round(entry + sign * risk * 2, 2),
        "tp3_px": round(entry + sign * risk * 3, 2),
        "tp1_done": False, "tp2_done": False,
        "high_water": entry, "low_water": entry, "mfe_px": entry, "mae_px": entry,
        "atr_pct": atr_pct, "risk_dist": risk, "open_ts": int(time.time() * 1000),
        "leverage": lev, "margin": 0.0, "liq_px": None, "setup": "discovered",
        "venue": "okx", "bars_since_open": 0,
    }


# =========================================================================
# 一、收养锚点一致性（问题 1）
# =========================================================================
def _patch_plan(monkeypatch, risk_dist, plan_sl_market=None):
    """固定 plan_trade 返回：risk_dist 指定，sl 用"市价锚点"的旧行为值（制造错位）。"""
    def fake_plan(candles, factors, side, round_px=None):
        px = 2650.0 if plan_sl_market is None else plan_sl_market
        return {"entry": px, "sl": px - risk_dist if side == "long" else px + risk_dist,
                "tp": px, "risk_dist": risk_dist, "reward_dist": risk_dist * 2,
                "rr": 2.0, "rr_target": 2.0, "regime": "range", "sl_basis": "atr",
                "risk_atr_x": 1.0}
    monkeypatch.setattr(ap_mod.se, "plan_trade", fake_plan)
    monkeypatch.setattr(ap_mod, "compute_factors", lambda candles: {"regime": "range", "atr_pct": 0.2})
    monkeypatch.setattr(ap_mod.Autopilot, "_recent_candles",
                        lambda self, period, inst_id=None: [{}] * 120)


def test_adopt_long_market_above_entry_anchors_sl_to_entry(fresh_db, monkeypatch):
    """Case 1：LONG 收养时市价(2650) 明显高于 entry(2600) → SL 必须 = 2580（低于 entry）。

    修复前 SL = plan["sl"] = 市价 2650 − 20 = 2630 > entry → 错误。
    """
    _patch_plan(monkeypatch, risk_dist=20.0, plan_sl_market=2650.0)
    ap, data = _paper_ap(fresh_db, last=2650.0, entry=2600.0)
    ap._discover_position(ap._account_summary())

    pos = ap.positions["ETH-USDT"]
    assert pos["sl_px"] == 2580.0, f"SL 必须以 entry 为锚点：期望 2580，实际 {pos['sl_px']}"
    assert pos["sl_px"] < pos["entry_px"], "LONG 的 SL 必须在开仓价下方"
    assert pos["risk_dist"] == 20.0, "风险距离应保持 plan 原值"


def test_adopt_short_market_below_entry_anchors_sl_to_entry(fresh_db, monkeypatch):
    """Case 2：SHORT 收养时市价(2550) 明显低于 entry(2600) → SL 必须 = 2620（高于 entry）。"""
    _patch_plan(monkeypatch, risk_dist=20.0, plan_sl_market=2550.0)
    ap, data = _paper_ap(fresh_db, last=2550.0, entry=2600.0, side="short")
    ap._discover_position(ap._account_summary())

    pos = ap.positions["ETH-USDT"]
    assert pos["sl_px"] == 2620.0, f"SL 必须以 entry 为锚点：期望 2620，实际 {pos['sl_px']}"
    assert pos["sl_px"] > pos["entry_px"], "SHORT 的 SL 必须在开仓价上方"


def test_adopted_sl_and_tp_share_same_anchor(fresh_db, monkeypatch):
    """Case 3：收养后 SL/TP 同锚点 —— LONG: SL < entry < TP1 < TP2 < TP3；SHORT 镜像。"""
    _patch_plan(monkeypatch, risk_dist=20.0, plan_sl_market=2700.0)     # 市价远高于 entry
    ap, data = _paper_ap(fresh_db, last=2700.0, entry=2600.0)
    ap._discover_position(ap._account_summary())
    p = ap.positions["ETH-USDT"]
    assert p["sl_px"] < p["entry_px"] < p["tp1_px"] < p["tp2_px"] < p["tp3_px"], \
        f"LONG 排列应为 SL<entry<TP1<TP2<TP3，实际 {[p['sl_px'], p['entry_px'], p['tp1_px'], p['tp2_px'], p['tp3_px']]}"

    fresh_db.set_setting("paper_account", {"usdt": 10000.0, "coins": {}, "initial": 10000.0,
                                           "lev_positions": []})
    ap2, _ = _paper_ap(fresh_db, last=2500.0, entry=2600.0, side="short")
    ap2._discover_position(ap2._account_summary())
    q = ap2.positions["ETH-USDT"]
    assert q["tp3_px"] < q["tp2_px"] < q["tp1_px"] < q["entry_px"] < q["sl_px"], \
        f"SHORT 排列应为 TP3<TP2<TP1<entry<SL，实际 {[q['tp3_px'], q['tp2_px'], q['tp1_px'], q['entry_px'], q['sl_px']]}"


def test_adopt_fallback_1_5r_anchored_to_entry(fresh_db, monkeypatch):
    """数据不足时的 1.5R 兜底同样锚定 entry（保持原 1.5R 语义与方向）。"""
    monkeypatch.setattr(ap_mod.Autopilot, "_recent_candles",
                        lambda self, period, inst_id=None: [])          # 无 K 线 → 走兜底
    ap, data = _paper_ap(fresh_db, last=2500.0, entry=2600.0)
    ap._discover_position(ap._account_summary())
    p = ap.positions["ETH-USDT"]
    expect_sl = round(2600.0 - (2600.0 * 0.008) * 1.5, 2)
    assert p["sl_px"] == expect_sl and p["sl_px"] < p["entry_px"], \
        f"兜底 SL 应为 entry − 1.5×(0.8%×entry) = {expect_sl}，实际 {p['sl_px']}"

    fresh_db.set_setting("paper_account", {"usdt": 10000.0, "coins": {}, "initial": 10000.0,
                                           "lev_positions": []})
    ap2, _ = _paper_ap(fresh_db, last=2700.0, entry=2600.0, side="short")
    ap2._discover_position(ap2._account_summary())
    q = ap2.positions["ETH-USDT"]
    expect_sl_s = round(2600.0 + (2600.0 * 0.008) * 1.5, 2)
    assert q["sl_px"] == expect_sl_s and q["sl_px"] > q["entry_px"]


# =========================================================================
# 二、交易所（OKX）动态止损同步（问题 2）
# =========================================================================
def test_okx_long_tp1_syncs_breakeven_once(fresh_db):
    """LONG TP1 到达 → 本地推保本 + 交易所侧止损同步到保本价，且只同步一次。"""
    ap, data, oms = _okx_ap(fresh_db, last=2640.0, acct_pos=0.5, acct_entry=2600.0)
    pos = _mk_pos(entry=2600.0, sz=0.5, sl=2580.0, risk=20.0)
    ap.positions["ETH-USDT"] = pos

    _sync(ap._manage_position("ETH-USDT", 2640.0))       # 触及 TP1（2600+20）
    assert pos["tp1_done"] is True
    assert pos["sl_px"] == pos["entry_px"], "TP1 后本地止损应推到保本"
    calls = oms.sync_calls()
    assert len(calls) == 1, f"应同步一次交易所止损，实际 {len(calls)} 次"
    assert calls[0][1] == "ETH-USDT" and calls[0][2] == 2600.0 and calls[0][3] == "long"
    assert calls[0][4] == pos["sz"], "同步应使用平仓后的最新持仓量"
    assert pos["exch_sl"]["ok"] is True and pos["exch_sl"]["px"] == 2600.0

    # 后续 tick 止损未变 → 不得重复下单
    ap.positions["ETH-USDT"]["tp1_done"] = True
    _sync(ap._manage_position("ETH-USDT", 2650.0))
    assert len(oms.sync_calls()) == 1, "同价不得重复同步"


def test_okx_long_trailing_syncs_only_on_change_and_only_upward(fresh_db):
    """LONG TP2 后 trailing：SL 上移才同步；未变化不同步；绝不下移。"""
    ap, data, oms = _okx_ap(fresh_db, last=2700.0, acct_pos=0.5, acct_entry=2600.0)
    pos = _mk_pos(entry=2600.0, sz=0.5, sl=2600.0, risk=20.0, atr_pct=0.2)
    pos["tp1_done"] = pos["tp2_done"] = True
    pos["high_water"] = 2700.0
    pos["exch_sl"] = {"px": 2600.0, "ok": True}          # 已同步到保本
    ap.positions["ETH-USDT"] = pos

    _sync(ap._manage_position("ETH-USDT", 2700.0))       # atr_abs=5.2 → trail≈2687（>2600）
    expect = 2700.0 - TRAIL_ATR * (0.2 / 100 * 2600.0)
    assert abs(pos["sl_px"] - expect) < 1e-9, f"trailing 应上移到 {expect}，实际 {pos['sl_px']}"
    calls = oms.sync_calls()
    assert len(calls) == 1 and abs(calls[0][2] - pos["sl_px"]) < 1e-9, f"SL 上移应同步，实际 {calls}"

    # 同价重入 → 不得重复下单（直接验证同步器守卫）
    _sync(ap._sync_exchange_sl("ETH-USDT", pos, "重复调用"))
    assert len(oms.sync_calls()) == 1, "同价不得重复同步"

    # 非有利方向（多头往回降）→ 必须拒绝，保持单调性
    pos["sl_px"] = 2650.0
    _sync(ap._sync_exchange_sl("ETH-USDT", pos, "人为回退"))
    assert len(oms.sync_calls()) == 1, "非有利方向不得同步（保持单调性）"
    assert pos["exch_sl"]["px"] > 2650.0, "同步状态不应被回退值覆盖"


def test_okx_short_tp1_and_trailing_sync_downward_only(fresh_db):
    """SHORT 镜像：TP1 保本同步、TP2 trailing 只向下同步。"""
    ap, data, oms = _okx_ap(fresh_db, last=2560.0, acct_pos=0.5, acct_entry=2600.0)
    pos = _mk_pos(side="short", entry=2600.0, sz=0.5, sl=2620.0, risk=20.0, atr_pct=0.2)
    ap.positions["ETH-USDT"] = pos

    _sync(ap._manage_position("ETH-USDT", 2560.0))       # 触及 TP1（2600−20）
    assert pos["tp1_done"] is True and pos["sl_px"] == 2600.0
    c1 = oms.sync_calls()
    assert len(c1) == 1 and c1[0][2] == 2600.0 and c1[0][3] == "short"

    pos["tp2_done"] = True
    pos["low_water"] = 2540.0
    _sync(ap._manage_position("ETH-USDT", 2540.0))       # trail = min(2600, 2540+5.2)=2545.2
    assert pos["sl_px"] < 2600.0, "SHORT trailing 应下移止损"
    c2 = oms.sync_calls()
    assert len(c2) == 2 and c2[1][2] == pos["sl_px"], "SHORT 下移应同步"


def test_okx_sync_failure_is_visible_and_tracked(fresh_db):
    """同步失败：不静默（error 通知 + 状态记录），且不影响本地止损与后续流程。"""
    ap, data, oms = _okx_ap(fresh_db, last=2640.0, acct_pos=0.5, acct_entry=2600.0, fail_sync=True)
    pos = _mk_pos(entry=2600.0, sz=0.5, sl=2580.0, risk=20.0)
    ap.positions["ETH-USDT"] = pos

    _sync(ap._manage_position("ETH-USDT", 2640.0))
    assert pos["sl_px"] == 2600.0, "本地止损不受交易所同步失败影响"
    st = pos.get("exch_sl") or {}
    assert st.get("ok") is False and "mock" in (st.get("err") or ""), f"失败状态必须可追踪：{st}"
    assert st.get("fail_count") == 1, f"失败次数必须记录：{st}"
    notes = fresh_db.query("SELECT title, body, level FROM notifications WHERE title LIKE '%autopilot_sl_sync%'")
    assert len(notes) == 1 and notes[0]["level"] == "error", f"必须产生 error 告警：{notes}"
    assert "重试" in notes[0]["body"], f"告警应说明会重试：{notes[0]['body']}"


def test_okx_failed_sync_is_retried_after_rate_limit(fresh_db):
    """失败后：限流窗口内不重试（防每 tick 下单），窗口过后自动重试一次并恢复成功。"""
    ap, data, oms = _okx_ap(fresh_db, last=2640.0, acct_pos=0.5, acct_entry=2600.0, fail_sync=True)
    pos = _mk_pos(entry=2600.0, sz=0.5, sl=2580.0, risk=20.0)
    ap.positions["ETH-USDT"] = pos

    _sync(ap._manage_position("ETH-USDT", 2640.0))          # TP1 → 同步失败
    assert len(oms.sync_calls()) == 1
    pos["tp1_done"] = pos["tp2_done"] = True
    pos["high_water"] = 2605.0                              # 固定水位 → trailing 不再改动 SL

    for _ in range(3):                                      # 限流窗口内多次 tick
        _sync(ap._manage_position("ETH-USDT", 2605.0))
    assert len(oms.sync_calls()) == 1, "限流窗口内不得重试（防每 tick 重复下单）"

    pos["exch_sl"]["ts"] = int(time.time() * 1000) - (ap_mod.SL_SYNC_RETRY_SEC + 5) * 1000
    oms.fail_sync = False                                   # 交易所恢复
    _sync(ap._manage_position("ETH-USDT", 2605.0))
    assert len(oms.sync_calls()) == 2, "限流窗口过后应自动重试"
    assert pos["exch_sl"]["ok"] is True and pos["exch_sl"]["fail_count"] == 0, \
        f"重试成功后状态应复位：{pos['exch_sl']}"


def test_recovered_exchange_sl_is_seeded_after_restart(fresh_db):
    """重启恢复：本地没有 exch_sl 时，应从交易所读回已有止损单并写入（避免重复挂单）。"""
    ap, data, oms = _okx_ap(fresh_db, last=2600.0, acct_pos=0.5, acct_entry=2600.0)
    oms.existing_algo = {"algo_id": "EXISTING1", "sl_trigger_px": 2580.0, "sz": 0.5, "state": "live"}

    pos = _mk_pos(entry=2600.0, sz=0.5, sl=2600.0, risk=20.0)   # 模拟重启后：无 exch_sl
    pos.pop("exch_sl", None)
    ap.positions["ETH-USDT"] = pos
    _sync(ap._recover_exchange_sl("ETH-USDT", pos))

    assert oms.find_calls(), "应查询交易所已有止损单"
    st = pos.get("exch_sl") or {}
    assert st.get("ok") is True and st.get("algo_id") == "EXISTING1" and st.get("px") == 2580.0
    assert st.get("source") == "recovered"
    assert not oms.sync_calls(), "恢复过程不得下单"


def test_paper_venue_does_not_sync_exchange_sl(fresh_db):
    """paper venue 没有交易所侧止损 → 不做任何同步（也不报错）。"""
    ap, data = _paper_ap(fresh_db, last=2640.0, entry=2600.0)
    pos = _mk_pos(entry=2600.0, sz=0.5, sl=2580.0, risk=20.0)
    pos["venue"] = "paper"
    ap.positions["ETH-USDT"] = pos

    _sync(ap._manage_position("ETH-USDT", 2640.0))
    assert pos["sl_px"] == 2600.0
    assert "exch_sl" not in pos, "paper 不应产生交易所止损状态"


# =========================================================================
# 三、§6 行为确认：trailing 把 SL 抬到现价上方时，当 tick 即平仓
#    （只加测试、不改代码；结论见报告）
# =========================================================================
def test_trailing_above_current_price_closes_same_tick(fresh_db):
    """LONG：本 tick trailing 把 SL 抬到现价之上 → 同一 tick 内必须平仓，不留悬挂状态。"""
    ap, data = _paper_ap(fresh_db, last=2664.52, entry=2621.53)
    pos = _mk_pos(entry=2621.53, sz=0.076306, sl=2621.534202, risk=12.204, atr_pct=0.388)
    pos["tp1_done"] = pos["tp2_done"] = True
    pos["high_water"] = 2707.68
    ap.positions["ETH-USDT"] = pos
    ap._close_position = _spy_close(ap)                  # 记录平仓调用（不真正执行）

    _sync(ap._manage_position("ETH-USDT", 2664.52))

    trail = 2707.68 - TRAIL_ATR * (0.388 / 100 * 2621.53)
    assert pos["sl_px"] == max(2621.534202, trail), "本 tick 内 SL 被抬到现价上方"
    assert pos["sl_px"] > 2664.52, "本 tick 复现出「SL 高于现价」的状态"
    assert getattr(ap, "_closed_same_tick", False), \
        "同一 tick 内必须触发平仓（结论：不需要同 tick 再检查）"


def _spy_close(ap):
    async def _close(inst_id=None, reason="", sz=None):
        ap._closed_same_tick = True
        ap._close_reason = reason
        return {"ok": True, "fully": True}
    return _close
