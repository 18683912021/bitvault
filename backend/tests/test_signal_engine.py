# 信号引擎回归：score 权重 / 十一项守卫 / position_size 公式 / setup detail。
from app.brain import signal_engine as se


def _factors(**over):
    f = {
        "last_px": 100.0, "atr_14": 1.0, "atr_pct": 1.0, "atr_pctile": 0.5,
        "rsi_14": 55.0, "bb_pos": 0.5, "macd_hist": 0.5,
        "adx_14": 30.0, "volume_ratio": 1.5, "ema21_dist_pct": 0.5,
        "trend": "up", "trend_strength": 0.5, "regime": "trend_up",
        "ema_fast": 101.0, "ema_slow": 100.0,
    }
    f["last_px"] = 100.0 + 59 * 0.2 if "last_px" not in over else f["last_px"]
    f.update(over)
    return f


def _candles(n=60, px=100.0):
    """上升 K 线（当前价高于近 40 根高点 → 无上方阻力；趋势市）。"""
    out = []
    for i in range(n):
        c = px + i * 0.2
        out.append({"ts": i * 300_000, "o": c - 0.1, "h": c + 0.05, "l": c - 0.15,
                    "c": c, "vol": 1.0})
    return out


def test_score_weights_trend_up_long():
    f = _factors()
    setup = {"setup": "pullback", "why": "x", "detail": {"kind": "pullback"}}
    score, breakdown = se.score_side(f, "long", setup, htf_4h="up", htf_1h="up")
    assert 0 <= score <= 100
    assert breakdown  # 明细非空
    # trend_up & hist>0 & adx>=25 → 趋势 20 分；结构 pullback 15 分；成交量 10；波动率 10
    mapping = {b["k"]: b["v"] for b in breakdown}
    assert mapping["趋势"] == 20
    assert mapping["结构"] == 15
    assert mapping["成交量"] == 10


def test_gate_eleven_items():
    ctx = {"htf_4h": "up", "htf_1h": "up", "halted": False,
           "drawdown_pct": 0.0, "loss_streak": 0, "leverage": 2}
    plan = se.plan_trade(_candles(), _factors(), "long")
    ok, checks = se.check_gate(_candles(), _factors(), "long", 99, plan, ctx)
    assert len(checks) == 11, "守卫必须是 11 项（含 liq_safety）"
    assert ok  # 全部通过
    names = [c["check"] for c in checks]
    for expect in ("regime", "htf_conflict", "score", "rr", "chase",
                   "divergence", "sl_range", "structure_room", "cost",
                   "risk_state", "liq_safety"):
        assert expect in names, f"缺守卫 {expect}"


def test_gate_blocks_extreme_regime():
    ctx = {"htf_4h": "up", "htf_1h": "up", "halted": False,
           "drawdown_pct": 0.0, "loss_streak": 0, "leverage": 2}
    plan = se.plan_trade(_candles(), _factors(), "long")
    ok, checks = se.check_gate(_candles(), _factors(regime="extreme"), "long", 99, plan, ctx)
    assert not ok
    assert next(c for c in checks if c["check"] == "regime")["ok"] is False


def test_gate_htf_conflict_4h():
    ctx = {"htf_4h": "down", "htf_1h": "up", "halted": False,
           "drawdown_pct": 0.0, "loss_streak": 0, "leverage": 2}
    plan = se.plan_trade(_candles(), _factors(), "long")
    ok, checks = se.check_gate(_candles(), _factors(), "long", 99, plan, ctx)
    assert not ok
    conflict = next(c for c in checks if c["check"] == "htf_conflict")
    assert conflict["ok"] is False


def test_detect_setup_insufficient():
    r = se.detect_setup(_candles(20), _factors(), "long")
    assert r["setup"] == "none"


def test_detect_setup_detail_present():
    r = se.detect_setup(_candles(60), _factors(), "long")
    assert "detail" in r
    assert isinstance(r["detail"], dict)


def test_position_size_formula():
    # risk = 权益 1%；notional = risk / (dist/px)
    plan = {"risk_dist": 2.0, "entry": 100.0}
    n, note = se.position_size(10_000, 10_000, plan, 0.0, 0, 200.0, leverage=1)
    risk_usdt = 10_000 * 0.01
    expected = risk_usdt / (2.0 / 100.0)
    expected = min(expected, 200.0)
    assert round(n, 4) == round(expected, 4)


def test_position_size_leverage_cap_vs_risk():
    # 杠杆提升只影响可用资金上限，不放大风险预算
    plan = {"risk_dist": 2.0, "entry": 100.0}
    n1, _ = se.position_size(10_000, 10_000, plan, 0.0, 0, 10_000, leverage=1)
    n2, _ = se.position_size(10_000, 10_000, plan, 0.0, 0, 10_000, leverage=13)
    assert n1 == n2  # 可用资金充足时风险预算决定仓位，与杠杆无关
