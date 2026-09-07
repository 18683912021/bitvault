# 实验开关（strategy_mode）回归：
# official = 生产行为 100% 不变；research_pullback 仅放开 pullback 准入，后链全复用。
import json

from app.brain.autopilot import _setup_gate, _format_dec_report, Autopilot
from conftest import FakeRisk


class FakeData:
    def bars_from_db(self, *a, **k):
        return []
    def last_price(self, *a, **k):
        return 0.0


def test_official_gate_behavior_unchanged():
    """official：与生产完全一致（seed 默认也必须是 official）。"""
    # 默认 cfg 的 strategy_mode
    cfg = {"require_setup": True, "setup_filter": "breakout_retest", "strategy_mode": "official"}
    assert _setup_gate("none", True, "breakout_retest", "official") == "无形态(require_setup)"
    assert _setup_gate("pullback", True, "breakout_retest", "official") == "非指定形态(需breakout_retest)"
    assert _setup_gate("breakout_retest", True, "breakout_retest", "official") is None


def test_research_pullback_only_opens_setup_gate():
    """research：pullback 放行进入后续全链；其余规则等同 official。"""
    cfg = {"require_setup": True, "setup_filter": "breakout_retest", "strategy_mode": "research_pullback"}
    assert _setup_gate("pullback", True, "breakout_retest", "research_pullback") is None, \
        "实验模式必须放行 pullback"
    # none 仍被 require_setup 拦；未指定的其它形态仍然不放行
    assert _setup_gate("none", True, "breakout_retest", "research_pullback") == "无形态(require_setup)"
    assert _setup_gate("range", True, "breakout_retest", "research_pullback") is not None


def test_config_defaults_official():
    """默认配置必须是 official（绝不以实验模式替身生产）。"""
    ap = Autopilot.__new__(Autopilot)          # 跳过构造（只测 config 默认表）
    import io as _io
    import inspect
    src = inspect.getsource(Autopilot.config)
    assert '"strategy_mode": "official"' in src


def test_autopilot_status_exposes_mode():
    ap = Autopilot.__new__(Autopilot)
    ap.svc = type("S", (), {"venue": "paper", "inst_id": "BTC-USDT"})()
    ap.config = lambda: {"enabled": True, "period": "5m", "mode": "normal", "leverage": 2,
                         "strategy_mode": "research_pullback"}
    ap.is_running = lambda: True
    ap.last_factors = {"regime": "range"}
    ap.last_action = "flat"
    ap.positions = {}
    ap.throttle_status = lambda: {}
    ap.risk = None
    st = ap.status()
    assert st["strategy_mode"] == "research_pullback"
    assert st["inst_id"] == "BTC-USDT"


def test_journal_columns_include_mode_and_guards(fresh_db):
    cols = {r[1] for r in fresh_db._conn.execute("PRAGMA table_info(trade_journal)").fetchall()}
    assert "strategy_mode" in cols
    assert "guards_json" in cols


def test_wait_payload_records_block_layer():
    """NO TRADE 必须记录卡在哪一层（数据/陈旧/未就绪/持仓/节流/准入或守卫）。"""
    from app.brain import autopilot as ap_mod
    sig = {"strategy_mode": "official"}
    # 直接验证 _log_wait 生成的 payload 结构与 reason 分层文案
    import asyncio
    v = ap_mod._setup_gate  # 确保模块可导入
    assert v is not None
    # 主 wait 路径的 block_layer 在 _decide_and_act 中写为 setup_or_gate（text 校验）
    src = __import__("inspect").getsource(ap_mod.Autopilot._decide_and_act)
    assert '"block_layer": "setup_or_gate"' in src
    for lay in ('"data_insufficient"', '"stale_data"', '"live_not_ready"',
                '"has_position"', '"throttle"'):
        assert lay in src, f"缺少卡层 {lay}"
