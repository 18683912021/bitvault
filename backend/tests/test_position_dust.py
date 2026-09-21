# 仓位残仓(dust) / 平仓失败 / 本地-venue 数量不一致 回归测试（P0-1 ~ P0-7）。
#
# 背景（线上 ETH-USDT 事故）：
#   分批止盈按比例平仓经 normalize_sz 的 int() 截断——
#   开仓 0.076306 → TP1 平 0.0190765 被截成 0.019076、剩余 0.0572295 被截成 0.057229，
#   两笔合计只平掉 0.076305，venue 残留 1e-06；而 ETH-USDT minSz=0.0001，
#   之后每一次止损/止盈下单都被"数量低于最小下单量"拒绝，
#   仓位永久卡死、止损彻底失效，并长期占用 has_position 阻塞新开仓。
#
# 覆盖：
#   A 分批平仓产生浮点/规格化残差   B venue 剩余 < minSz   C 本地与 venue 不一致
#   D 平仓失败 retry/sync           E dust 不阻塞 has_position
#   F _discover_position 不收养 dustG 正常仓位行为不变
import asyncio
import json
import time
from types import SimpleNamespace

from app.brain.autopilot import (MAX_CLOSE_RETRIES, TP1_PCT, TP1_R, TP2_PCT, TP2_R,
                                 TRAIL_ATR, Autopilot)
from app.market.data_service import DataService
from app.paper.paper_engine import PaperEngine
from app.risk.risk_engine import RiskBlocked
from conftest import FakeRisk

ETH = {"instId": "ETH-USDT", "instType": "SPOT", "tickSz": 0.01, "lotSz": 1e-06,
       "minSz": 0.0001, "ctVal": 0.0, "baseCcy": "ETH", "quoteCcy": "USDT", "settleCcy": ""}
BTC = {"instId": "BTC-USDT", "instType": "SPOT", "tickSz": 0.01, "lotSz": 0.0001,
       "minSz": 0.0001, "ctVal": 0.0, "baseCcy": "BTC", "quoteCcy": "USDT", "settleCcy": ""}
ETH_SWAP = {"instId": "ETH-USDT-SWAP", "instType": "SWAP", "tickSz": 0.01, "lotSz": 0.01,
            "minSz": 0.01, "ctVal": 0.1, "baseCcy": "", "quoteCcy": "",
            "settleCcy": "USDT"}
SOL = {"instId": "SOL-USDT", "instType": "SPOT", "tickSz": 0.01, "lotSz": 1e-06,
       "minSz": 0.001, "ctVal": 0.0, "baseCcy": "SOL", "quoteCcy": "USDT", "settleCcy": ""}


class FakeData(DataService):
    """最小 DataService 替身：规格化/规划逻辑用真实实现（被测代码本身）。"""

    def __init__(self, insts, last=2621.01):
        self.instruments = {i["instId"]: dict(i) for i in insts}
        self.last = last
        self.delegated = set()

    def set_delegated_insts(self, insts):
        self.delegated = set(insts)

    def is_supported(self, inst_id):
        return inst_id in self.instruments

    def last_price(self, inst_id, max_age_s=30):
        return self.last


class FailingExecutor:
    """平仓必定被拒的下单引擎替身（模拟"数量低于最小下单量"）。"""

    def __init__(self, err="数量低于最小下单量 0.0001"):
        self.err = err
        self.calls = []

    async def place_intent(self, intent):
        self.calls.append(intent)
        raise RiskBlocked(self.err)


def _sync(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _paper(fresh_db, data):
    return PaperEngine(data, FakeRisk())


def _fund(fresh_db, usdt=10000.0):
    fresh_db.set_setting("paper_account", {"usdt": usdt, "coins": {}, "initial": usdt,
                                           "lev_positions": []})


def _ap(data, paper, venue="paper", inst_id="ETH-USDT", okx_positions=None):
    """构造 autopilot；venue=okx 时账户持仓取自 okx_positions（原始 OKX 字段）。"""
    ap = Autopilot(data, paper, FakeRisk())
    is_okx = venue == "okx"
    ap.bind_svc(SimpleNamespace(
        venue=venue, inst_id=inst_id,
        has_key=lambda: is_okx, oms=object() if is_okx else None,
        account=SimpleNamespace(positions=list(okx_positions or []), summary={"totalEq": 0}),
        private_ws=None,
    ))
    return ap


def _venue_sz(fresh_db, inst_id):
    """paper 账户里该标的的实际持仓量（合约=张数；现货=币量）；无持仓返回 0。"""
    acct = fresh_db.get_setting("paper_account") or {}
    for lp in acct.get("lev_positions", []):
        if lp["inst_id"] == inst_id:
            return float(lp["sz"])
    coin = inst_id.replace("-USDT", "")
    return float((acct.get("coins") or {}).get(coin, 0.0))


def _mk_pos(inst_id="ETH-USDT", side="long", entry=2621.534202, sz=0.076306,
            sl=2608.93, risk=12.204, atr_pct=0.388, lev=10):
    sign = -1 if side == "short" else 1
    return {
        "inst_id": inst_id, "cl_ord_id": "discovered", "side": side,
        "entry_px": entry, "sl_px": sl, "sz": sz, "orig_sz": sz,
        "tp1_px": round(entry + sign * risk, 2),
        "tp2_px": round(entry + sign * risk * 2, 2),
        "tp3_px": round(entry + sign * risk * 3, 2),
        "tp1_done": False, "tp2_done": False,
        "high_water": entry, "low_water": entry, "mfe_px": entry, "mae_px": entry,
        "atr_pct": atr_pct, "risk_dist": risk, "open_ts": int(time.time() * 1000),
        "leverage": lev, "margin": 0.0, "liq_px": None, "setup": "discovered",
        "venue": "paper", "bars_since_open": 0,
    }


def _open_venue(fresh_db, paper, inst_id, sz_base, leverage, side="buy"):
    """在 venue（paper 引擎）真实开一笔仓。"""
    return _sync(paper.place_intent({
        "inst_id": inst_id, "side": side, "ord_type": "market",
        "sz_base": sz_base, "reduce_only": False, "source": "t", "leverage": leverage}))


def _seed_dust(fresh_db, inst_id="ETH-USDT", sz=0.000001, entry=2621.534202, lev=10,
               side="long"):
    """直接写入模拟盘账户，复刻"截断平仓后留下的残仓"。

    注意：这种残值**不可能通过下单开出来**（paper 引擎同样按 minSz 拒绝，
    见 test_e4），它只能由分批平仓的截断残值产生——所以测试里直接构造账户状态。
    """
    acct = fresh_db.get_setting("paper_account") or {}
    acct.setdefault("usdt", 10000.0)
    acct.setdefault("coins", {})
    acct["lev_positions"] = list(acct.get("lev_positions") or [])
    acct["lev_positions"].append({
        "inst_id": inst_id, "coin": inst_id.replace("-USDT", ""), "sz": sz, "side": side,
        "entry_px": entry, "leverage": lev, "margin": round(sz * entry / lev, 8),
        "liq_px": round(entry * (1 - 1.0 / lev + 0.005), 2),
        "open_ts": int(time.time() * 1000), "cl_ord_id": "seed",
    })
    fresh_db.set_setting("paper_account", acct)


def _notifications(fresh_db, like=""):
    if like:
        return fresh_db.query(
            "SELECT title, body FROM notifications WHERE title LIKE ? ORDER BY id", (like,))
    return fresh_db.query("SELECT title, body FROM notifications ORDER BY id")


# ========================= A. 分批平仓不得留下残差 =========================
def test_a1_normalize_sz_no_float_truncation():
    """0.05723/1e-06 在 IEEE754 下是 57229.99999…，不得被 int() 截成 0.057229。"""
    d = FakeData([ETH])
    assert d.normalize_sz("ETH-USDT", 0.05723)[0] == 0.05723
    assert d.normalize_sz("ETH-USDT", 0.076306)[0] == 0.076306


def test_a2_plan_close_sz_eth_incident_no_dust():
    """复现事故数量：25% 分批 + 全平后残留必须为 0（修复前残留 1e-06）。"""
    d = FakeData([ETH])
    orig = 0.076306
    p1 = d.plan_close_sz("ETH-USDT", orig, orig * TP1_PCT)
    assert p1["submit"] == 0.019076 and p1["full"] is False
    left = orig - p1["submit"]
    p2 = d.plan_close_sz("ETH-USDT", left, None)
    assert p2["full"] is True and p2["submit"] == left
    assert left - p2["submit"] == 0.0, "全平后不得残留"


def test_a3_end_to_end_partial_close_then_full_no_venue_dust(fresh_db):
    """端到端：paper 真实撮合下，分批 + 全平后 venue 必须干净归零。"""
    data = FakeData([ETH], last=2621.01)
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper)
    _fund(fresh_db)
    _open_venue(fresh_db, paper, "ETH-USDT", 0.076306, leverage=10)
    assert _venue_sz(fresh_db, "ETH-USDT") == 0.076306

    ap.positions["ETH-USDT"] = _mk_pos(sz=0.076306)
    r1 = _sync(ap._close_position("ETH-USDT", reason="TP1 到达 1R 平 25%",
                                  sz=0.076306 * TP1_PCT))
    assert r1["ok"] and r1["fully"] is False
    assert _venue_sz(fresh_db, "ETH-USDT") == 0.05723, "TP1 后 venue 不得出现 0.057229 截断"
    assert ap.positions["ETH-USDT"]["sz"] == 0.05723, "本地数量必须等于 venue 实际剩余"

    r2 = _sync(ap._close_position("ETH-USDT", reason="保本止损触发"))
    assert r2["ok"] and r2["fully"] is True
    assert _venue_sz(fresh_db, "ETH-USDT") == 0.0, "全平后 venue 必须归零（不得残留 dust）"
    assert "ETH-USDT" not in ap.positions
    j = fresh_db.query("SELECT exit_reason FROM trade_journal WHERE inst_id='ETH-USDT'")
    assert j and j[-1]["exit_reason"] == "保本止损触发"


# ========================= B. 剩余低于 minSz =========================
def test_b1_plan_snaps_to_full_when_remainder_is_dust():
    """平仓后会留下低于 minSz 的残值 → 改为整仓平（防 dust）。"""
    d = FakeData([ETH])
    p = d.plan_close_sz("ETH-USDT", 0.00015, 0.0001)
    assert p["full"] is True and p["submit"] == 0.00015 and p["dust_after"] == 0.0
    assert "整仓平" in p["note"]


def test_b2_plan_marks_untradable_position_as_dust():
    """整仓低于 minSz：任何下单都会被拒 → 必须标记 untradable。"""
    d = FakeData([ETH])
    p = d.plan_close_sz("ETH-USDT", 0.000001, None)
    assert p["untradable"] is True and p["submit"] == 0.0


def test_b3_plan_skips_too_small_partial_but_keeps_position():
    """分批量低于 minSz（且不会制造 dust）→ 跳过本次分批，仓位保留。"""
    d = FakeData([ETH])
    p = d.plan_close_sz("ETH-USDT", 0.0003, 0.00005)
    assert p["submit"] == 0.0 and p["untradable"] is False and p["dust_after"] == 0.0003


def test_b4_end_to_end_dust_remainder_closes_whole(fresh_db):
    """端到端：0.00015 持仓想平 0.0001 会留 dust → 实际整仓平掉，venue 归零。"""
    data = FakeData([ETH], last=2621.01)
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper)
    _fund(fresh_db)
    _open_venue(fresh_db, paper, "ETH-USDT", 0.00015, leverage=10)
    ap.positions["ETH-USDT"] = _mk_pos(sz=0.00015)
    r = _sync(ap._close_position("ETH-USDT", reason="分批", sz=0.0001))
    assert r["fully"] is True and _venue_sz(fresh_db, "ETH-USDT") == 0.0


def test_b5_swap_partial_close_uses_base_coin_units(fresh_db):
    """SWAP 平仓：持仓口径是张数，提交口径是币量——不得错位（否则平仓量放大 1/ctVal 倍）。"""
    data = FakeData([ETH_SWAP], last=2621.01)
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper, inst_id="ETH-USDT-SWAP")
    _fund(fresh_db)
    _open_venue(fresh_db, paper, "ETH-USDT-SWAP", 0.5, leverage=2)   # 5 张（ctVal=0.1）
    assert _venue_sz(fresh_db, "ETH-USDT-SWAP") == 5.0
    ap.positions["ETH-USDT-SWAP"] = _mk_pos(inst_id="ETH-USDT-SWAP", sz=5.0, lev=2)
    r = _sync(ap._close_position("ETH-USDT-SWAP", reason="全平"))
    assert r["ok"] and r["fully"] is True
    assert _venue_sz(fresh_db, "ETH-USDT-SWAP") == 0.0





# ========================= C. 本地与 venue 不一致 =========================
def test_c_local_size_is_reconciled_from_venue(fresh_db):
    """本地记账偏小/偏大都不算数：平仓后本地必须等于 venue 实际剩余。"""
    data = FakeData([ETH], last=2621.01)
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper)
    _fund(fresh_db)
    _open_venue(fresh_db, paper, "ETH-USDT", 0.06, leverage=10)
    ap.positions["ETH-USDT"] = _mk_pos(sz=0.05)          # 本地账故意写错
    _sync(ap._close_position("ETH-USDT", reason="TP1", sz=0.05 * TP1_PCT))
    assert _venue_sz(fresh_db, "ETH-USDT") == 0.0475      # 0.06 - 0.0125
    assert ap.positions["ETH-USDT"]["sz"] == 0.0475, "本地必须以 venue 为准回填"


def test_c2_venue_zero_after_close_clears_local(fresh_db):
    """venue 已无持仓而本地仍记着 → 平仓后本地必须摘除（不得留幽灵持仓）。"""
    data = FakeData([ETH], last=2621.01)
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper)
    _fund(fresh_db)
    _open_venue(fresh_db, paper, "ETH-USDT", 0.02, leverage=10)
    ap.positions["ETH-USDT"] = _mk_pos(sz=0.02)
    _sync(ap._close_position("ETH-USDT", reason="全平"))
    assert _venue_sz(fresh_db, "ETH-USDT") == 0.0
    assert "ETH-USDT" not in ap.positions




def test_c3_venue_snapshot_lag_falls_back_to_actual_fill(fresh_db):
    """venue 快照还没反映本次成交（okx 私有 WS 有延迟）→ 按实际成交量扣减，不得假装未成交。"""
    data = FakeData([ETH], last=2621.01)
    paper = _paper(fresh_db, data)
    _fund(fresh_db)
    stale = [{"instId": "ETH-USDT", "pos": "0.05", "avgPx": "2621.53"}]   # 平仓前的旧快照
    ap = _ap(data, paper, venue="okx", inst_id="ETH-USDT", okx_positions=stale)
    assert ap._venue_pos_sz("ETH-USDT") == 0.05
    # cur=0.05、快照未更新、实际成交 0.0125 → 剩余 0.0375（按成交量扣减，P0-3）
    assert abs(ap._post_close_size("ETH-USDT", 0.05, 0.0125, 0.0125) - 0.0375) < 1e-12



def test_c4_venue_snapshot_authoritative_when_smaller(fresh_db):
    """venue 快照小于本地记账时以 venue 为准（本地多记的仓位不得残留）。"""
    data = FakeData([ETH], last=2621.01)
    paper = _paper(fresh_db, data)
    _fund(fresh_db)
    okx_positions = [{"instId": "ETH-USDT", "pos": "0.03", "avgPx": "2621.53"}]
    ap = _ap(data, paper, venue="okx", inst_id="ETH-USDT", okx_positions=okx_positions)
    # 快照 0.03 < 本地 0.05 → 以 0.03 为准（而不是 0.05-0.0125=0.0375）
    assert ap._post_close_size("ETH-USDT", 0.05, 0.0125, 0.0125) == 0.03


# ========================= D. 平仓失败 retry / 告警 / 重同步 =========================
def test_d1_close_failure_is_retried_and_notified(fresh_db):
    """平仓异常不再被静默吞掉：计入重试 + 告警 + 仓位保留待重试。"""
    data = FakeData([ETH])
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper)
    _fund(fresh_db)
    ap.positions["ETH-USDT"] = _mk_pos()
    failing = FailingExecutor()
    ap._executor = lambda: failing
    r = _sync(ap._close_position("ETH-USDT", reason="止损触发"))
    assert r["ok"] is False and r["reason"] == "exception"
    assert ap._close_retries["ETH-USDT"] == 1, "失败必须计数"
    assert "ETH-USDT" in ap.positions, "失败后仓位必须保留（不能假装已平）"
    notes = _notifications(fresh_db, "自动驾驶：autopilot_close_failed%")
    assert len(notes) == 1 and notes[0]["title"].endswith("autopilot_close_failed")
    assert "平仓失败" in notes[0]["body"]


def test_d2_close_failure_escalates_to_sync_after_max_retries(fresh_db):
    """连续失败超过上限 → 升级为 error 告警并触发交易所持仓重同步。"""
    data = FakeData([ETH])
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper)
    _fund(fresh_db)
    ap.positions["ETH-USDT"] = _mk_pos()
    failing = FailingExecutor()
    ap._executor = lambda: failing
    synced: list[str] = []

    async def _fake_sync(inst_id):
        synced.append(inst_id)

    ap._sync_from_exchange = _fake_sync
    for _ in range(MAX_CLOSE_RETRIES + 1):
        _sync(ap._close_position("ETH-USDT", reason="止损触发"))
    assert synced == ["ETH-USDT"], "超过重试上限必须触发重同步"
    notes = _notifications(fresh_db, "自动驾驶：autopilot_close_failed%")
    assert any("连续" in n["body"] for n in notes), "必须有升级告警"


def test_d3_close_failure_notify_is_rate_limited(fresh_db):
    """同一标的的重复失败告警限流 60s，避免 tick 级刷爆通知表。"""
    data = FakeData([ETH])
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper)
    _fund(fresh_db)
    ap.positions["ETH-USDT"] = _mk_pos()
    ap._executor = lambda: FailingExecutor()
    for _ in range(3):
        _sync(ap._close_position("ETH-USDT", reason="止损触发"))
    assert ap._close_retries["ETH-USDT"] == 3
    assert len(_notifications(fresh_db, "自动驾驶：autopilot_close_failed%")) == 1


def test_d4_successful_close_resets_retry_counter(fresh_db):
    """失败后再成功 → 重试计数清零、告警限流时间戳清空。"""
    data = FakeData([ETH], last=2621.01)
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper)
    _fund(fresh_db)
    _open_venue(fresh_db, paper, "ETH-USDT", 0.02, leverage=10)
    ap.positions["ETH-USDT"] = _mk_pos(sz=0.02)
    ap._executor = lambda: FailingExecutor()
    _sync(ap._close_position("ETH-USDT", reason="止损触发"))
    assert ap._close_retries["ETH-USDT"] == 1
    ap._executor = _executor_of(ap, paper)              # 恢复真实 paper 引擎
    r = _sync(ap._close_position("ETH-USDT", reason="全平"))
    assert r["ok"] is True
    assert "ETH-USDT" not in ap._close_retries and "ETH-USDT" not in ap._close_fail_notify_ts


def _executor_of(ap, paper):
    return lambda: paper


# ========================= E. dust 不得阻塞 has_position =========================
def test_e_stuck_dust_position_is_released_and_purged(fresh_db):
    """线上事故复刻：venue 只剩 1e-06 残仓 + 本地仍托管 → 必须释放槽位并清理。

    修复前：每个 tick 触发止损 → 下单被 minSz 拒绝 → 仓位永久占用 has_position。
    """
    data = FakeData([ETH], last=2664.52)
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper)
    _fund(fresh_db)
    # venue 只剩 1e-06（模拟两笔截断平仓后的残值）
    _seed_dust(fresh_db, "ETH-USDT", sz=0.000001)
    pos = _mk_pos(sz=0.000001, sl=2682.25)      # 止损已在现价上方（线上实况）
    pos["tp1_done"] = pos["tp2_done"] = True
    ap.positions["ETH-USDT"] = pos

    _sync(ap._manage_position("ETH-USDT", 2664.52))

    assert ap.position is None, "dust 不得继续占用 has_position（否则永远开不了新仓）"
    assert _venue_sz(fresh_db, "ETH-USDT") == 0.0, "模拟盘 dust 必须被清理"
    assert not ap._close_retries, "不得因为 dust 累计平仓重试"
    dust = ap.status()["dust"]
    assert len(dust) == 1 and dust[0]["inst_id"] == "ETH-USDT" and dust[0]["cleaned"] is True
    notes = _notifications(fresh_db, "自动驾驶：autopilot_dust%")
    assert len(notes) == 1 and "低于最小下单量" in notes[0]["body"]


def test_e2_purge_dust_never_creates_orders(fresh_db):
    """P0-7：paper dust 清理必须靠改账，绝不允许通过下单解决（下单必然被 minSz 拒绝）。"""
    data = FakeData([ETH], last=2664.52)
    paper = _paper(fresh_db, data)
    _fund(fresh_db)
    _seed_dust(fresh_db, "ETH-USDT", sz=0.000001)
    before = len(fresh_db.query("SELECT id FROM orders"))
    removed = paper.purge_dust("ETH-USDT")
    assert len(removed) == 1 and removed[0]["sz"] == 0.000001
    assert len(fresh_db.query("SELECT id FROM orders")) == before, "purge_dust 不得产生任何订单"
    assert _venue_sz(fresh_db, "ETH-USDT") == 0.0
    audits = fresh_db.query("SELECT action FROM audit_logs WHERE action='paper_purge_dust'")
    assert len(audits) == 1, "清理必须留审计痕迹"


def test_e3_purge_dust_keeps_normal_positions(fresh_db):
    """清理只针对不可交易残值：正常仓位必须原样保留。"""
    data = FakeData([ETH, BTC, SOL], last=2621.01)
    paper = _paper(fresh_db, data)
    _fund(fresh_db)
    _open_venue(fresh_db, paper, "ETH-USDT", 0.076306, leverage=10)   # 正常
    _open_venue(fresh_db, paper, "BTC-USDT", 0.01, leverage=2)        # 正常
    _seed_dust(fresh_db, "SOL-USDT", sz=0.000001, entry=100.0, lev=10)  # 不可交易残值
    removed = paper.purge_dust()
    assert [r["inst_id"] for r in removed] == ["SOL-USDT"], "只应清理不可交易的残值"
    assert _venue_sz(fresh_db, "ETH-USDT") > 0.07 and _venue_sz(fresh_db, "BTC-USDT") > 0.009


def test_e4_venue_rejects_sub_minsz_order(fresh_db):
    """前置说明：dust 无法通过下单产出（引擎按 minSz 拒单）→ 只能由平仓截断残留。"""
    data = FakeData([ETH], last=2664.52)
    paper = _paper(fresh_db, data)
    _fund(fresh_db)
    try:
        _open_venue(fresh_db, paper, "ETH-USDT", 0.000001, leverage=10)
        raise AssertionError("低于 minSz 的开仓单必须被拒")
    except RiskBlocked as e:
        assert "最小下单量" in str(e)




def test_e5_close_shortcuts_to_dust_when_venue_is_below_minsz(fresh_db):
    """本地账还记着 0.0001、但 venue 实际只剩 1e-06 → 平仓入口必须直接转 dust 收尾。"""
    data = FakeData([ETH], last=2664.52)
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper)
    _fund(fresh_db)
    _seed_dust(fresh_db, "ETH-USDT", sz=0.000001)
    ap.positions["ETH-USDT"] = _mk_pos(sz=0.0001)        # 本地记账偏大
    r = _sync(ap._close_position("ETH-USDT", reason="止损触发"))
    assert r["reason"] == "untradable"
    assert "ETH-USDT" not in ap.positions
    assert _venue_sz(fresh_db, "ETH-USDT") == 0.0
    assert not ap._close_retries, "dust 走专用通道，不应计入平仓重试"
    assert ap.status()["dust"][0]["inst_id"] == "ETH-USDT"


# ========================= F. _discover_position 不得收养 dust =========================
def test_f1_discover_skips_and_purges_paper_dust(fresh_db):
    """paper：dust 不收养，且被清理；反复调用也不会被收养成正常持仓。"""
    data = FakeData([ETH], last=2664.52)
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper)
    _fund(fresh_db)
    _seed_dust(fresh_db, "ETH-USDT", sz=0.000001)
    for _ in range(3):
        ap._discover_position(ap._account_summary())
    assert ap.positions == {}, "dust 绝不能被收养成托管持仓"
    assert _venue_sz(fresh_db, "ETH-USDT") == 0.0
    assert len(_notifications(fresh_db, "自动驾驶：autopilot_dust%")) == 1, "同一残仓只告警一次"


def test_f2_discover_adopts_normal_position(fresh_db):
    """正常仓位仍必须被收养（不得因为防 dust 而漏管）。"""
    data = FakeData([ETH], last=2664.52)
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper)
    _fund(fresh_db)
    _open_venue(fresh_db, paper, "ETH-USDT", 0.076306, leverage=10)
    ap._discover_position(ap._account_summary())
    assert "ETH-USDT" in ap.positions and ap.positions["ETH-USDT"]["setup"] == "discovered"


def test_f3_discover_okx_dust_recorded_not_adopted(fresh_db):
    """okx：交易所残值无法用下单清掉 → 只记录+告警，绝不收养（不阻塞 has_position）。"""
    data = FakeData([ETH], last=2664.52)
    paper = _paper(fresh_db, data)
    _fund(fresh_db)
    okx_positions = [{"instId": "ETH-USDT", "pos": "0.000001", "avgPx": "2621.53"}]
    ap = _ap(data, paper, venue="okx", inst_id="ETH-USDT", okx_positions=okx_positions)
    ap._discover_position(ap._account_summary())
    assert ap.positions == {}, "okx dust 不得被托管"
    dust = ap.status()["dust"]
    assert len(dust) == 1 and dust[0]["cleaned"] is False and dust[0]["remaining"] == 0.000001
    assert len(_notifications(fresh_db, "自动驾驶：autopilot_dust%")) == 1


def test_f4_discover_okx_normal_position_adopted(fresh_db):
    """okx 正常仓位（张数口径）仍必须被收养。"""
    data = FakeData([ETH_SWAP], last=2664.52)
    paper = _paper(fresh_db, data)
    _fund(fresh_db)
    okx_positions = [{"instId": "ETH-USDT-SWAP", "pos": "5", "avgPx": "2621.53"}]
    ap = _ap(data, paper, venue="okx", inst_id="ETH-USDT-SWAP", okx_positions=okx_positions)
    ap._discover_position(ap._account_summary())
    assert ap.positions["ETH-USDT-SWAP"]["sz"] == 5.0


# ========================= G. 正常仓位行为不变 =========================
def test_g1_normal_partial_close_flow_unchanged(fresh_db):
    """正常仓位：TP1 25% / TP2 35% / 全平 的成交数量与分档语义保持不变。"""
    data = FakeData([BTC], last=2621.01)
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper, inst_id="BTC-USDT")
    _fund(fresh_db)
    _open_venue(fresh_db, paper, "BTC-USDT", 0.01, leverage=2)
    ap.positions["BTC-USDT"] = _mk_pos(inst_id="BTC-USDT", sz=0.01, lev=2)

    r1 = _sync(ap._close_position("BTC-USDT", reason="TP1 到达 1R 平 25%", sz=0.01 * TP1_PCT))
    assert r1["fully"] is False and _venue_sz(fresh_db, "BTC-USDT") == 0.0075
    r2 = _sync(ap._close_position("BTC-USDT", reason="TP2 到达 2R 平 35%", sz=0.01 * TP2_PCT))
    assert r2["fully"] is False and _venue_sz(fresh_db, "BTC-USDT") == 0.004
    r3 = _sync(ap._close_position("BTC-USDT", reason="TP3 到达 3R 清仓"))
    assert r3["fully"] is True and _venue_sz(fresh_db, "BTC-USDT") == 0.0
    assert "BTC-USDT" not in ap.positions
    bodies = [n["body"] for n in _notifications(fresh_db, "自动驾驶：autopilot_close%")]
    assert any("0.002500（部分，TP1" in b for b in bodies), f"部分平仓文案不应改变: {bodies}"
    assert any("（全部，TP3" in b for b in bodies)


def test_g2_strategy_constants_and_trailing_formula_unchanged():
    """策略参数与 trailing 公式不得被本次修复改动：SL/TP 仍为 entry±R、trailing=2.5×ATR。"""
    from app.brain import signal_engine as se
    assert (TP1_R, TP1_PCT) == (1.0, 0.25) and (TP2_R, TP2_PCT) == (2.0, 0.35)
    assert TRAIL_ATR == 2.5
    assert getattr(se, "SL_ATR_TREND") == 1.8 and getattr(se, "SL_ATR_RANGE") == 1.2
    assert getattr(se, "RR_TREND") == 2.5 and getattr(se, "RR_RANGE") == 2.0
    assert getattr(se, "SL_MIN_PCT") == 0.004 and getattr(se, "SL_MAX_PCT") == 0.025


def test_g3_trailing_stop_value_unchanged(fresh_db):
    """多头 trailing 止损值必须仍是 high_water - 2.5×ATR（线上 2682.25 的那个公式）。"""
    data = FakeData([ETH], last=2707.68)
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper)
    _fund(fresh_db)
    _open_venue(fresh_db, paper, "ETH-USDT", 0.076306, leverage=10)
    pos = _mk_pos(sz=0.076306, atr_pct=0.388, entry=2621.534202)
    pos["tp1_done"] = pos["tp2_done"] = True
    pos["high_water"] = 2707.68
    pos["sl_px"] = 2621.534202
    pos["tp3_px"] = 3000.0          # 抬高 TP3，隔离出「只观察 trailing」的场景
    ap.positions["ETH-USDT"] = pos

    _sync(ap._manage_position("ETH-USDT", 2707.68))
    expect = 2707.68 - TRAIL_ATR * (0.388 / 100 * 2621.534202)
    assert abs(ap.positions["ETH-USDT"]["sl_px"] - expect) < 1e-9, "trailing 公式被改动了"


def test_g4_adopted_position_sl_tp_anchoring_unchanged(fresh_db):
    """收养路径的 SL/TP 锚点与取值口径不变（SL 用 plan、TP 用 entry±kR）。"""
    data = FakeData([ETH], last=2621.01)
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper)
    _fund(fresh_db)
    _open_venue(fresh_db, paper, "ETH-USDT", 0.076306, leverage=10)
    ap._discover_position(ap._account_summary())
    pos = ap.positions["ETH-USDT"]
    risk = pos["risk_dist"]
    assert risk > 0
    assert pos["sl_px"] < pos["entry_px"], "多头 SL 必须在开仓价下方（数据不足走 ATR 兜底）"
    assert abs(pos["tp1_px"] - round(pos["entry_px"] + risk, 2)) < 1e-6
    assert abs(pos["tp2_px"] - round(pos["entry_px"] + 2 * risk, 2)) < 1e-6


def test_g5_sl_hit_still_closes_normal_position(fresh_db):
    """止损命中仍能正常平掉正常仓位（修复不得影响正常止损）。"""
    data = FakeData([ETH], last=2600.0)
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper)
    _fund(fresh_db)
    _open_venue(fresh_db, paper, "ETH-USDT", 0.076306, leverage=10)
    ap.positions["ETH-USDT"] = _mk_pos(sz=0.076306, sl=2608.93)
    _sync(ap._manage_position("ETH-USDT", 2600.0))          # 现价跌破止损
    assert "ETH-USDT" not in ap.positions
    assert _venue_sz(fresh_db, "ETH-USDT") == 0.0
    notes = _notifications(fresh_db, "自动驾驶：autopilot_close%")
    assert any("止损触发" in n["body"] for n in notes)


# =====================================================================================
# H. 完整生命周期回归：平仓 → 清仓 → cooldown → 重新进入信号评估 → 再次开仓
#    对应验收点 A(正常完全平仓) B(剩余<minSz) C(浮点残差) D(清仓) E(cooldown)
#             F(cooldown 后重新进入信号评估) G(第二次开仓 daily_opens +1)
#    注意：本组测试**不改变**任何策略语义（has_position 设计、单标的单仓位、
#    加仓/反向、max_opens_per_day=20、cooldown=15、评分与 Gate 规则全部保持原样）。
# =====================================================================================
P_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1H": 3_600_000,
        "4H": 14_400_000, "1D": 86_400_000}


def _seed_bars(fresh_db, inst_id="ETH-USDT", period="5m", n=80, px0=2600.0,
               step=0.8, now_ms=None):
    """写入足够且"新鲜"的 K 线：通过 _decide_and_act 的数据充足性与新鲜度守卫。"""
    p_ms = P_MS.get(period, 300_000)
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    last_open = now - (now % p_ms)          # 对齐周期起点 → 距今 < 1 个周期
    rows = []
    for i in range(n):
        ot = last_open - (n - 1 - i) * p_ms
        c = px0 + i * step
        rows.append((inst_id, period, ot, c, c * 1.002, c * 0.998, c,
                     100.0 + (i % 7) * 10.0, c * 100, "test"))
    fresh_db.executemany(
        "INSERT OR REPLACE INTO bars (inst_id, period, open_time, o,h,l,c,vol,vol_ccy,source)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return rows[-1][2]


def _last_signal(fresh_db):
    """最近一条 signals（含 payload 里的 block_layer）。"""
    rows = fresh_db.query(
        "SELECT action, reason, payload_json FROM signals ORDER BY id DESC LIMIT 1")
    if not rows:
        return {}
    r = rows[0]
    try:
        payload = json.loads(r.get("payload_json") or "{}")
    except Exception:
        payload = {}
    return {"action": r["action"], "block_layer": payload.get("block_layer"),
            "payload": payload, "reason": r["reason"] or ""}


def _signal_layers(fresh_db):
    out = []
    for r in fresh_db.query("SELECT payload_json FROM signals ORDER BY id"):
        try:
            out.append((json.loads(r.get("payload_json") or "{}") or {}).get("block_layer"))
        except Exception:
            out.append(None)
    return out


def _open_blocked_by_has_position(fresh_db) -> int:
    return sum(1 for lyr in _signal_layers(fresh_db) if lyr == "has_position")


def _plan_and_quality(fresh_db, inst_id="ETH-USDT", side="long"):
    """用真实 K 线跑出 plan_trade + 因子，供 _open_position 直接调用。"""
    from app.brain import signal_engine as se
    from app.brain.factor_engine import compute_factors
    rows = fresh_db.query(
        "SELECT open_time as ts, o,h,l,c,vol FROM bars WHERE inst_id=? AND period='5m'"
        " ORDER BY open_time ASC", (inst_id,))
    f = compute_factors(rows)
    assert not f.get("error"), f"因子计算失败: {f.get('error')}"
    plan = se.plan_trade(rows, f, side)
    quality = {"regime": f.get("regime"), "regime_reason": f.get("regime_reason"),
               "signal_score": 70, "setup": "breakout_retest", "atr_pct": f.get("atr_pct"),
               "htf_4h": None, "htf_1h": None, "checks": []}
    return plan, quality


# ---------- 验收点：不会反复提交低于 minSz 的平仓订单 ----------
def test_h1_no_sub_minsz_close_order_is_ever_submitted(fresh_db):
    """dust 仓位在多个 tick 上反复管理，必须 0 次下单（否则每 tick 一次注定被拒的单）。"""
    data = FakeData([ETH], last=2664.52)
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper)
    _fund(fresh_db)
    _seed_dust(fresh_db, "ETH-USDT", sz=0.000001)       # venue 侧真实残仓
    spy = FailingExecutor()                            # 任何下单都会抛错 → 若被调用即失败
    ap._executor = lambda: spy
    ap.positions["ETH-USDT"] = _mk_pos(sz=0.000001, sl=2682.25)
    for px in (2664.52, 2660.0, 2680.0, 2650.0):
        _sync(ap._manage_position("ETH-USDT", px))
    assert spy.calls == [], f"dust 仓位不得提交任何平仓单，实际提交 {len(spy.calls)} 次"
    assert fresh_db.query("SELECT id FROM orders") == [], "不得产生任何订单"
    assert ap.position is None


def test_h2_full_close_clears_position_and_starts_cooldown(fresh_db):
    """D+E：正常完全平仓后 position 必须清空，并进入 15 分钟平仓冷却（到期后放行）。"""
    data = FakeData([ETH], last=2600.0)
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper)
    _fund(fresh_db)
    _open_venue(fresh_db, paper, "ETH-USDT", 0.076306, leverage=10)
    ap.positions["ETH-USDT"] = _mk_pos(sz=0.076306, sl=2608.93)

    before = int(time.time() * 1000)
    _sync(ap._manage_position("ETH-USDT", 2600.0))     # 现价跌破止损 → 完全平仓

    # D：仓位清除
    assert ap.position is None, "平仓后 self.position 必须为 None"
    assert "ETH-USDT" not in ap.positions
    assert _venue_sz(fresh_db, "ETH-USDT") == 0.0

    # E：cooldown 生效（15 分钟内拦下开仓）
    assert ap._last_close_ts >= before, "完全平仓必须写入平仓时间戳"
    th = ap.throttle_status()
    assert th["cooldown_until"] >= before + 15 * 60_000
    assert abs((ap._last_close_ts + 15 * 60_000) - th["cooldown_until"]) < 5, "cooldown 必须是 15 分钟"
    now_ms = int(time.time() * 1000)
    assert "冷却" in (ap._open_block_reason(now_ms) or ""), "冷却期内必须拦下开仓"

    # 冷却到期 → 放行（节流不再拦）
    ap._last_close_ts = now_ms - 16 * 60_000
    assert ap._open_block_reason(int(time.time() * 1000)) is None, "冷却结束后节流必须放行"


def test_h3_after_cooldown_reenters_signal_evaluation(fresh_db):
    """F：冷却结束后必须重新走 signal evaluation（setup→score→gate→plan_trade），
       而不是继续卡在 has_position。"""
    data = FakeData([ETH], last=2621.01)
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper)
    _fund(fresh_db)
    _seed_bars(fresh_db, "ETH-USDT", "5m", n=80)
    _seed_bars(fresh_db, "ETH-USDT", "1H", n=40, step=12.0)
    _seed_bars(fresh_db, "ETH-USDT", "4H", n=40, step=40.0)

    # 情形 1：有持仓 → 卡在 has_position（设计如此，只确认现状）
    ap.positions["ETH-USDT"] = _mk_pos(sz=0.076306)
    _sync(ap._decide_and_act())
    assert _last_signal(fresh_db)["block_layer"] == "has_position"
    ap.positions.pop("ETH-USDT")

    # 情形 2：刚平仓、冷却中 → 卡在 throttle（证明已越过 has_position）
    ap._last_close_ts = int(time.time() * 1000)
    _sync(ap._decide_and_act())
    sig = _last_signal(fresh_db)
    assert sig["block_layer"] == "throttle", f"冷却期应卡在 throttle，实际 {sig['block_layer']}"
    assert "冷却" in sig["reason"]

    # 情形 3：冷却结束 → 必须进入信号评估（setup/score/gate 跑过）
    n_has_pos_before = _open_blocked_by_has_position(fresh_db)
    ap._last_close_ts = int(time.time() * 1000) - 16 * 60_000
    _sync(ap._decide_and_act())
    sig = _last_signal(fresh_db)
    assert sig["block_layer"] != "has_position", "冷却结束后不得再卡在 has_position"
    assert sig["block_layer"] != "throttle", "冷却结束后不得再卡在 throttle"
    # 要么产生入场计划（gate:xxx），要么走到 setup/gate 评估层
    assert sig["block_layer"] == "setup_or_gate" or sig["action"].startswith("gate:"), \
        f"必须进入信号评估，实际 action={sig['action']} layer={sig['block_layer']}"
    if sig["block_layer"] == "setup_or_gate":
        assert sig["payload"].get("eval_details") is not None, "评估明细必须存在（detect_setup/score 已跑）"
    assert _open_blocked_by_has_position(fresh_db) == n_has_pos_before, \
        "平仓后不得再新增 has_position 拦截记录"


def test_h4_second_open_increments_daily_opens_and_cap_blocks(fresh_db):
    """G：完整平仓后可以再次开仓，_daily_opens 正确累加；达上限后按现有规则拦下。"""
    data = FakeData([ETH], last=2621.01)
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper)
    _fund(fresh_db)
    _seed_bars(fresh_db, "ETH-USDT", "5m", n=80)
    plan, quality = _plan_and_quality(fresh_db)
    data.last = plan["entry"]                      # 与决策价一致 → 不触发漂移重算
    cfg = ap.config()
    acct = ap._account_summary()

    # 第一次开仓
    _sync(ap._open_position("long", plan, quality, acct, 0.0, cfg))
    assert "ETH-USDT" in ap.positions, "第一次开仓应建立托管持仓"
    assert ap._daily_opens == 1, f"开仓成功必须 +1，实际 {ap._daily_opens}"
    sz1 = _venue_sz(fresh_db, "ETH-USDT")
    assert sz1 > 0

    # 完全平仓（止损）→ 解除 has_position，并清掉冷却以模拟冷却已到期
    ap.positions["ETH-USDT"]["sl_px"] = plan["entry"] * 1.5      # 抬高止损便于触发
    _sync(ap._manage_position("ETH-USDT", plan["entry"] * 0.5))  # 现价跌破止损 → 全平
    assert ap.position is None and _venue_sz(fresh_db, "ETH-USDT") == 0.0
    ap._last_close_ts = 0                                        # 模拟 cooldown 已过

    # 第二次开仓（同一标的、同一设计：完全平仓后才能再开）
    acct2 = ap._account_summary()
    _sync(ap._open_position("long", plan, quality, acct2, 0.0, cfg))
    assert "ETH-USDT" in ap.positions, "完全平仓后必须可以再次开仓"
    assert ap._daily_opens == 2, f"第二次成功开仓后应为 2，实际 {ap._daily_opens}"

    # 每日上限仍然是 20，且仍然按原规则拦下
    ap._daily_opens = 20
    blocked = ap._open_block_reason(int(time.time() * 1000))
    assert blocked and f"上限 20 次" in blocked, f"达上限必须拦下，实际 {blocked!r}"
    assert ap.config().get("max_opens_per_day", 20) == 20, "max_opens_per_day 必须保持 20"


def test_h5_post_close_exchange_residual_dust_is_recognized_and_cleared(fresh_db):
    """交易所侧在平仓后仍留下极小残值（按手数/张数取整的尾差）→ 必须识别为 dust 并收尾，
       不得把它当成"还有持仓"继续卡住 has_position。"""
    data = FakeData([ETH], last=2621.01)
    paper = _paper(fresh_db, data)
    ap = _ap(data, paper)
    _fund(fresh_db)
    _open_venue(fresh_db, paper, "ETH-USDT", 0.02, leverage=10)
    ap.positions["ETH-USDT"] = _mk_pos(sz=0.02)

    def _post_close_with_exchange_residual(inst_id, cur_venue, submit_venue, filled_venue):
        """模拟交易所只平到剩 5e-05（< minSz=0.0001）的尾差。

        注意：真实场景里这个残值是交易所按手数取整留下的，本地完全不知情；
        此处平仓已按 0.02 成交，故直接重建一个 sz=5e-05 的残仓来复刻。"""
        acct = fresh_db.get_setting("paper_account") or {}
        acct["lev_positions"] = [{
            "inst_id": "ETH-USDT", "coin": "ETH", "sz": 0.00005, "side": "long",
            "entry_px": 2621.534202, "leverage": 10, "margin": 0.0000131,
            "liq_px": 2372.49, "open_ts": int(time.time() * 1000), "cl_ord_id": "residual",
        }]
        fresh_db.set_setting("paper_account", acct)
        return 0.00005

    ap._post_close_size = _post_close_with_exchange_residual
    r = _sync(ap._close_position("ETH-USDT", reason="全平"))

    assert r.get("dust") is True, "平仓后暴露的 <minSz 残值必须走 dust 收尾"
    assert ap.position is None, "残值不得继续占用 has_position"
    assert "ETH-USDT" not in ap.positions
    assert _venue_sz(fresh_db, "ETH-USDT") == 0.0, "模拟盘残值必须被清理"
    dust = ap.status()["dust"]
    assert len(dust) == 1 and dust[0]["sz"] == 0.00005 and dust[0]["cleaned"] is True
    assert len(_notifications(fresh_db, "自动驾驶：autopilot_dust%")) == 1
