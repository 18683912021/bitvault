# 观望报告（NO TRADE「下一步等待」）回归测试。
#
# 背景（线上既有 bug，09-16 起触发 392 次）：
#   autopilot._next_wait_cn 里 wants 初始只有 1 个元素（多头句），却执行
#       wants[0], wants[1] = wants[0], "空头：等待有效突破 + 回踩 + 确认（当前仅普通回踩）"
#   → IndexError: list assignment index out of range。
# 触发条件：eval_details 中出现「空头方向 pullback（普通回踩）」且没有任何方向通过 Gate。
# 后果：整个 decide:wait 报告构建中断 → signals 不落库 → /api/brain/history 与
#      /api/brain/last_decision_at 停滞（可观测性丧失）。
#      该异常只发生在「观望」分支，不影响真实开仓路径（有信号时走 _open_position）。
#
# 本测试锁定：① 不再越界；② 观望语义不变（多头句 + 空头句两条）；
#             ③ 空头 pullback 时确实写出 wait/no-trade 决策记录。
import asyncio
import json
import time

from app.brain import signal_engine as se
from app.brain.autopilot import Autopilot
from app.market.data_service import DataService
from app.paper.paper_engine import PaperEngine
from conftest import FakeRisk

ETH = {"instId": "ETH-USDT", "instType": "SPOT", "tickSz": 0.01, "lotSz": 1e-06,
       "minSz": 0.0001, "ctVal": 0.0, "baseCcy": "ETH", "quoteCcy": "USDT", "settleCcy": ""}
P_MS = {"5m": 300_000, "1H": 3_600_000, "4H": 14_400_000, "1D": 86_400_000}
LONG_LINE = "多头：等待现有多头入场形态（优先突破回踩）"
SHORT_DEFAULT = "空头：等待有效突破回踩（突破 + 回踩 + 确认）"
SHORT_PULLBACK = "空头：等待有效突破 + 回踩 + 确认（当前仅普通回踩）"


class FakeData(DataService):
    def __init__(self, insts, last=2620.0):
        self.instruments = {i["instId"]: dict(i) for i in insts}
        self.last = last
        self.delegated = set()

    def set_delegated_insts(self, insts):
        self.delegated = set(insts)

    def is_supported(self, inst_id):
        return inst_id in self.instruments

    def last_price(self, inst_id, max_age_s=30):
        return self.last


def _sync(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _seed_bars(fresh_db, inst_id="ETH-USDT", period="5m", n=80, px0=2600.0, step=0.8):
    """写入足够且新鲜的 K 线（通过数据充足性 + 新鲜度守卫）。"""
    p_ms = P_MS.get(period, 300_000)
    now = int(time.time() * 1000)
    last_open = now - (now % p_ms)
    rows = []
    for i in range(n):
        c = px0 + i * step
        rows.append((inst_id, period, last_open - (n - 1 - i) * p_ms,
                     c, c * 1.002, c * 0.998, c, 100.0 + (i % 7) * 10.0, c * 100, "test"))
    fresh_db.executemany(
        "INSERT OR REPLACE INTO bars (inst_id, period, open_time, o,h,l,c,vol,vol_ccy,source)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


def _ap(fresh_db):
    """构造 autopilot（paper venue，无持仓、无冷却、配置为 official 默认）。"""
    data = FakeData([ETH])
    paper = PaperEngine(data, FakeRisk())
    ap = Autopilot(data, paper, FakeRisk())
    from types import SimpleNamespace
    ap.bind_svc(SimpleNamespace(venue="paper", inst_id="ETH-USDT", has_key=lambda: False,
                                oms=None, account=SimpleNamespace(positions=[], summary={}),
                                private_ws=None))
    fresh_db.set_setting("paper_account", {"usdt": 10000.0, "coins": {}, "initial": 10000.0,
                                           "lev_positions": []})
    return ap


def _last_signal(fresh_db):
    rows = fresh_db.query(
        "SELECT action, reason, payload_json FROM signals ORDER BY id DESC LIMIT 1")
    if not rows:
        return {}
    r = rows[0]
    try:
        payload = json.loads(r.get("payload_json") or "{}")
    except Exception:
        payload = {}
    return {"action": r["action"], "reason": r["reason"] or "", "payload": payload}


def _patch_detect(monkeypatch, short_setup, long_setup="none"):
    """只替换形态识别结果，其余（评分/Gate/计划/报告）全跑真实代码。"""
    real = se.detect_setup

    def fake(candles, factors, side):
        out = dict(real(candles, factors, side) or {})
        out["setup"] = short_setup if side == "short" else long_setup
        out.setdefault("detail", {"kind": "test"})
        return out

    monkeypatch.setattr(se, "detect_setup", fake)


# ===================== ① 单元：_next_wait_cn 语义 + 不再越界 =====================
def test_next_wait_cn_short_pullback_does_not_raise():
    """空头 pullback：修复前此处 IndexError（wants 只有 1 个元素时写 wants[1]）。"""
    ap = Autopilot.__new__(Autopilot)
    out = ap._next_wait_cn(
        [{"side": "long", "setup": "none"}, {"side": "short", "setup": "pullback"}],
        "breakout_retest")
    assert isinstance(out, str) and out
    assert LONG_LINE in out, "多头等待句必须保留"
    assert SHORT_PULLBACK in out, "空头 pullback 应换成突破回踩确认句"
    assert len(out.split("；")) == 2, f"应恰好两条等待句: {out!r}"


def test_next_wait_cn_default_semantics_unchanged():
    """无空头 pullback（含空明细）：仍是「多头句 + 空头默认句」两条，语义不变。"""
    ap = Autopilot.__new__(Autopilot)
    for details in ([{"side": "long", "setup": "none"}, {"side": "short", "setup": "none"}],
                    [{"side": "short", "setup": "breakout_retest"}],
                    []):
        out = ap._next_wait_cn(details, "breakout_retest")
        assert LONG_LINE in out and SHORT_DEFAULT in out, f"默认语义被改动: {out!r}"
        assert len(out.split("；")) == 2


def test_next_wait_cn_uses_configured_setup_name():
    """等待句里的形态名跟随 setup_filter（保持原有文案口径）。"""
    ap = Autopilot.__new__(Autopilot)
    out = ap._next_wait_cn([], "breakout_retest")
    assert out.startswith("多头：等待现有多头入场形态（优先突破回踩）")


# ===================== ② 集成：空头 pullback + 无入场信号 → 正常写出 wait 决策 =====================
def test_wait_decision_written_with_short_pullback(fresh_db, monkeypatch):
    """端到端：空头 pullback、无任何方向通过 Gate → 不抛异常且写出 decide:wait。

    修复前：_decide_and_act 在构建观望报告时抛 IndexError → signals 无记录。
    """
    ap = _ap(fresh_db)
    _seed_bars(fresh_db, "ETH-USDT", "5m", n=80)
    _seed_bars(fresh_db, "ETH-USDT", "1H", n=40, step=12.0)
    _seed_bars(fresh_db, "ETH-USDT", "4H", n=40, step=40.0)
    _seed_bars(fresh_db, "ETH-USDT", "1D", n=40, step=90.0)
    assert ap.config().get("strategy_mode") == "official"
    _patch_detect(monkeypatch, short_setup="pullback")

    _sync(ap._decide_and_act())          # ← 修复前此处抛 IndexError

    sig = _last_signal(fresh_db)
    assert sig.get("action") == "decide:wait", f"必须写出 wait/no-trade 决策: {sig}"
    assert sig.get("payload", {}).get("block_layer") == "setup_or_gate"
    assert SHORT_PULLBACK in sig["reason"], "观望报告应包含空头突破回踩确认句"
    assert LONG_LINE in sig["reason"]
    details = sig["payload"].get("eval_details") or []
    assert any(d.get("side") == "short" and d.get("setup") == "pullback" for d in details), \
        "评估明细必须记录空头 pullback"
    # 没有信号通过 Gate → 不得建仓、不得下任何单
    assert ap.position is None and ap.positions == {}
    assert fresh_db.query("SELECT id FROM orders") == []


def test_wait_decision_written_without_any_setup(fresh_db, monkeypatch):
    """对照组：两方向都无形态（最普通的观望）→ 同样正常写出 wait 决策。"""
    ap = _ap(fresh_db)
    _seed_bars(fresh_db, "ETH-USDT", "5m", n=80)
    _patch_detect(monkeypatch, short_setup="none", long_setup="none")

    _sync(ap._decide_and_act())

    sig = _last_signal(fresh_db)
    assert sig.get("action") == "decide:wait"
    assert sig.get("payload", {}).get("block_layer") == "setup_or_gate"
    assert SHORT_DEFAULT in sig["reason"], "无 pullback 时应使用空头默认等待句"
    assert ap.position is None
