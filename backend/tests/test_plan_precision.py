# plan_trade 价格距离精度回归：微价币（DOOD ~0.001）的 risk_dist/reward_dist 不得被 round(,2) 归零。
# 背景：signal_engine.plan_trade 曾执行 round(risk,2)/round(reward,2)，
# DOOD 的 risk≈4e-6 被归零 → 污染 cost/sl_range/liq_safety/position_size 守卫。
from app.brain import signal_engine as se


def _factors(**over):
    f = {
        "last_px": None, "atr_14": None, "atr_pct": 1.0, "atr_pctile": 0.5,
        "rsi_14": 55.0, "bb_pos": 0.5, "macd_hist": 0.5,
        "adx_14": 30.0, "volume_ratio": 1.5, "ema21_dist_pct": 0.5,
        "trend": "up", "trend_strength": 0.5, "regime": "trend_up",
        "ema_fast": None, "ema_slow": None,
    }
    f.update(over)
    return f


def _candles(px, n=60, step=None, base_range=0.005):
    """微价 K 线：价格中枢 px，每根波动 base_range。"""
    if step is None:
        step = px * 0.001  # 微增幅
    out = []
    for i in range(n):
        c = px + i * step
        out.append({"ts": i * 300_000, "o": c, "h": c * (1 + base_range),
                    "l": c * (1 - base_range), "c": c, "vol": 1.0})
    return out


def test_microprice_risk_dist_not_zero():
    """DOOD 量级（~0.0017）：risk_dist/reward_dist 必须 > 0（曾经 round(,2) 归零）。"""
    px = 0.0017
    f = _factors(last_px=px, atr_14=px * 0.01, ema_fast=px, ema_slow=px * 0.999)
    plan = se.plan_trade(_candles(px), f, "long")
    assert plan["risk_dist"] > 0, f"risk_dist 被归零: {plan['risk_dist']!r}"
    assert plan["reward_dist"] > 0, f"reward_dist 被归零: {plan['reward_dist']!r}"
    # 量级合理：风险距离应约等于价格×1%~2.5%（即 2e-5 ~ 4e-5 附近，远大于 0）
    assert plan["risk_dist"] > 1e-6, f"risk_dist 量级异常: {plan['risk_dist']!r}"


def test_microprice_rr_correct():
    """RR 是比值（round(,2) 保留），修复后仍正确：rl/reward 均非零时 RR 合理。"""
    px = 0.0017
    f = _factors(last_px=px, atr_14=px * 0.01, ema_fast=px, ema_slow=px * 0.999)
    plan = se.plan_trade(_candles(px), f, "long")
    # rr = reward/risk（round 2 位）；修复后 risk/reward 非零 → rr 应为正值
    assert plan["rr"] > 0, f"rr 异常: {plan['rr']!r}"
    # rr 应是合理值（2.0 左右，因 plan_trade 用 rr_target 设定）
    assert 1.5 <= plan["rr"] <= 3.0, f"rr 异常: {plan['rr']!r}"


def test_highprice_unchanged():
    """BTC/ETH 类高价格：risk_dist/reward_dist 行为不因修复变化（仍非零且量级合理）。"""
    px = 79477.0  # BTC
    f = _factors(last_px=px, atr_14=px * 0.01, ema_fast=px, ema_slow=px * 0.999)
    plan = se.plan_trade(_candles(px), f, "long")
    assert plan["risk_dist"] > 0
    assert plan["reward_dist"] > 0
    # 高价格币风险距离约 1%~2.5%：几十~两千 USDT
    assert 100 < plan["risk_dist"] < 3000, f"BTC risk_dist 异常: {plan['risk_dist']!r}"
    # rr 同样合理
    assert 1.5 <= plan["rr"] <= 3.0


def test_position_size_uses_real_risk_dist():
    """position_size 使用非零 risk_dist 时可正常计算（不受归零污染）。"""
    px = 0.0017
    f = _factors(last_px=px, atr_14=px * 0.01, ema_fast=px, ema_slow=px * 0.999)
    plan = se.plan_trade(_candles(px), f, "long")
    sz, note = se.position_size(10000.0, 10000.0, plan, 0.0, 0, 200.0, leverage=10)
    assert sz > 0, f"position_size 异常：{sz} {note}"
