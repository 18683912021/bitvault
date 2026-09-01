"""确定性信号评分引擎 + 交易守卫链（Trading Engine 的决策核心）。

设计原则：
- AI（DeepSeek）只是评分输入之一（占 20/100），不是最终决策者。
- 只有 LONG_SCORE/SHORT_SCORE >= SCORE_MIN 且通过全部硬守卫才允许开仓。
- 所有规则在代码里可审计，不依赖 LLM 遵守 prompt。
"""
from __future__ import annotations

from typing import Any

SCORE_MIN = 70          # 评分门槛
RR_MIN = 2.0            # 最低风险回报比
CHASE_EMA_DIST = 2.0    # 价格偏离 EMA21 超过 max(1.5%, 2×atr_pct) 视为追涨/杀跌
CHASE_VOL_RATIO = 3.5   # 量比异常放大禁止追单
RISK_PER_TRADE_PCT = 0.005   # 单笔风险占权益 0.5%
DD_THROTTLE_PCT = 3.0        # 回撤 > 3% 仓位减半
DD_PAUSE_PCT = 6.0           # 回撤 > 6% 暂停开仓（需人工 resume 或条件恢复）


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def score_side(factors: dict, ai: dict | None, side: str,
               htf_trend: str | None = None) -> tuple[int, list[dict]]:
    """对指定方向打分（0-100）。返回 (总分, 明细)。

    side: "long" | "short"。htf_trend: 高周期（1H）趋势 up/down/range，可选。
    """
    f = factors
    rsi = float(f.get("rsi_14", 50))
    bb = float(f.get("bb_pos", 0.5))
    hist = float(f.get("macd_hist", 0))
    adx = float(f.get("adx_14", 0))
    atr_pct = float(f.get("atr_pct", 0))        # %
    vr = float(f.get("volume_ratio", 1))
    ema_dist = float(f.get("ema21_dist_pct", 0))  # %
    regime = f.get("regime", "range")
    ai_conf = float((ai or {}).get("confidence", 0))  # 0-1
    ai_dir = (ai or {}).get("direction", "NONE")

    long_side = side == "long"
    items: list[dict] = []

    # ---- 趋势 20 ----
    if long_side:
        if regime == "bull_trend":
            t = 20 if hist > 0 else 14
        elif regime == "range":
            t = 12 if (rsi < 32 and bb < 0.2) else 3   # 震荡只给极端反转少量分
        else:
            t = 0
    else:
        if regime == "bear_trend":
            t = 20 if hist < 0 else 14
        elif regime == "range":
            t = 12 if (rsi > 68 and bb > 0.8) else 3
        else:
            t = 0
    items.append({"k": "趋势", "v": t, "max": 20, "why": f"regime={regime} hist={hist:.2f}"})

    # ---- 动量 15 ----
    if long_side:
        if 48 <= rsi < 70 and hist > 0:
            m = 15
        elif 40 <= rsi < 48:
            m = 8
        elif rsi < 28 and bb < 0.15:
            m = 12       # 超卖反转
        else:
            m = 4 if hist > 0 else 0
    else:
        if 30 < rsi <= 52 and hist < 0:
            m = 15
        elif 52 < rsi <= 60:
            m = 8
        elif rsi > 72 and bb > 0.85:
            m = 12       # 超买反转
        else:
            m = 4 if hist < 0 else 0
    items.append({"k": "动量", "v": m, "max": 15, "why": f"RSI={rsi:.0f}"})

    # ---- 成交量 10：温和放大加分，异常放量反而扣分（追单风险） ----
    if 1.1 <= vr <= 3.0:
        v = 10
    elif 0.8 <= vr < 1.1:
        v = 6
    elif 3.0 < vr <= CHASE_VOL_RATIO:
        v = 4
    elif vr > CHASE_VOL_RATIO:
        v = 0
    else:
        v = 2
    items.append({"k": "成交量", "v": v, "max": 10, "why": f"量比={vr:.2f}"})

    # ---- 波动率 10：太死撑不起成本，太狂风险大 ----
    if 0.15 <= atr_pct <= 1.2:
        vol_s = 10
    elif 1.2 < atr_pct <= 2.0:
        vol_s = 6
    elif 0.08 <= atr_pct < 0.15:
        vol_s = 4
    else:
        vol_s = 0
    items.append({"k": "波动率", "v": vol_s, "max": 10, "why": f"ATR%={atr_pct:.2f}"})

    # ---- 价格结构 15：不追高/不杀低，位置决定赔率 ----
    if long_side:
        if 0.2 <= bb <= 0.6:
            s = 15
        elif 0.6 < bb <= 0.8:
            s = 10
        elif bb < 0.2:
            s = 8        # 超卖区有反弹赔率
        elif 0.8 < bb <= 0.9:
            s = 4
        else:
            s = 0        # >0.9 追高
    else:
        if 0.4 <= bb <= 0.8:
            s = 15
        elif 0.2 <= bb < 0.4:
            s = 10
        elif bb > 0.8:
            s = 8
        elif 0.1 <= bb < 0.2:
            s = 4
        else:
            s = 0
    items.append({"k": "价格结构", "v": s, "max": 15, "why": f"bb_pos={bb:.2f}"})

    # ---- 多周期一致 10（1H 趋势可选输入）----
    if htf_trend is None:
        h = 5
        why = "无 1H 数据（中性处理）"
    elif (long_side and htf_trend == "up") or (not long_side and htf_trend == "down"):
        h, why = 10, "1H 同向"
    elif htf_trend == "range":
        h, why = 5, "1H 震荡"
    else:
        h, why = 0, "1H 逆向"
    items.append({"k": "多周期一致", "v": h, "max": 10, "why": why})

    # ---- AI 评分 20：AI 反对该方向则为 0（AI 有否决权，无批准权）----
    ai_ok = (long_side and ai_dir == "LONG") or (not long_side and ai_dir == "SHORT")
    a = round(ai_conf * 20) if ai_ok else 0
    items.append({"k": "AI 评分", "v": a, "max": 20, "why": f"AI conf={ai_conf:.2f} dir={ai_dir}"})

    # ---- ADX 强度修正：趋势市 ADX 不足时从趋势分里回收 ----
    if (long_side and regime == "bull_trend") or (not long_side and regime == "bear_trend"):
        if adx < 25:
            items[0]["v"] = max(0, items[0]["v"] - (8 if adx < 22 else 4))
            items[0]["why"] += f"（ADX={adx:.0f} 扣弱趋势分）"

    total = sum(i["v"] for i in items)
    return total, items


def chase_blocked(factors: dict, side: str) -> str:
    """禁止追涨杀跌检查：返回拦截原因（空串=放行）。"""
    atr_pct = float(factors.get("atr_pct", 0)) or 0.01
    dist = float(factors.get("ema21_dist_pct", 0))
    vr = float(factors.get("volume_ratio", 1))
    bb = float(factors.get("bb_pos", 0.5))
    rsi = float(factors.get("rsi_14", 50))
    hot = max(1.5, CHASE_EMA_DIST * atr_pct)
    if side == "long":
        if dist > hot:
            return f"价格高于 EMA21 达 {dist:.2f}%（>{hot:.2f}%），禁止追多，等回踩"
        if bb > 0.92 or rsi > 78:
            return f"超买过热（bb={bb:.2f} RSI={rsi:.0f}），禁止追多"
        if vr > CHASE_VOL_RATIO:
            return f"量比 {vr:.1f} 异常放大，禁止追多"
    else:
        if -dist > hot:
            return f"价格低于 EMA21 达 {-dist:.2f}%（>{hot:.2f}%），禁止追空，等反抽"
        if bb < 0.08 or rsi < 22:
            return f"超卖过热（bb={bb:.2f} RSI={rsi:.0f}），禁止追空"
        if vr > CHASE_VOL_RATIO:
            return f"量比 {vr:.1f} 异常放大，禁止追空"
    return ""


def plan_trade(factors: dict, side: str) -> dict:
    """按 regime 自适应生成止损距离 / 止盈目标 / RR。

    趋势市：止损 1.8×ATR、目标 2.5R（让利润跑，trailing 接管）
    震荡反转：止损 1.2×ATR（窄）、目标 2R（快进快出）
    同时保证止盈距离 >= MIN_TP_PCT（0.9%）、止损距离 >= 0.4%（费用约束）。
    """
    px = float(factors["last_px"])
    atr = float(factors.get("atr_14", 0)) or px * 0.002
    regime = factors.get("regime", "range")
    trend_regime = regime in ("bull_trend", "bear_trend")
    sl_mult = 1.8 if trend_regime else 1.2
    rr_target = 2.5 if trend_regime else 2.0

    min_sl = px * 0.004
    min_tp = px * 0.009
    if side == "long":
        sl = px - max(sl_mult * atr, min_sl)
        tp = px + max(rr_target * (px - sl), min_tp)
    else:
        sl = px + max(sl_mult * atr, min_sl)
        tp = px - max(rr_target * (sl - px), min_tp)
    risk = abs(px - sl)
    reward = abs(tp - px)
    return {
        "entry": px, "sl": round(sl, 2), "tp": round(tp, 2),
        "risk_dist": round(risk, 2), "reward_dist": round(reward, 2),
        "rr": round(reward / risk, 2) if risk > 0 else 0,
        "sl_atr_mult": sl_mult, "rr_target": rr_target,
        "regime": regime,
    }


def check_gate(factors: dict, ai: dict | None, side: str, score: int,
               plan: dict, halted: bool, drawdown_pct: float,
               htf_trend: str | None = None) -> tuple[bool, list[dict]]:
    """硬守卫链。返回 (是否放行, 检查明细)。AI 拒绝/守卫拒绝都过不了。"""
    regime = factors.get("regime", "range")
    ai_conf = float((ai or {}).get("confidence", 0))
    ai_dir = (ai or {}).get("direction", "NONE")
    ai_allowed = bool((ai or {}).get("trade_allowed", False))

    checks: list[dict] = []

    def add(name: str, ok: bool, why: str) -> None:
        checks.append({"check": name, "ok": ok, "why": why})

    # 1. 市场状态
    if regime == "extreme_vol":
        add("regime", False, f"极端波动：{factors.get('regime_reason', '')}，禁止新开仓")
    elif side == "long" and regime == "bear_trend":
        add("regime", False, "空头趋势中禁止做多")
    elif side == "short" and regime == "bull_trend":
        add("regime", False, "多头趋势中禁止做空（现货也不支持）")
    elif regime == "range" and side == "long" and not (factors.get("rsi_14", 50) < 32 and factors.get("bb_pos", .5) < 0.2):
        add("regime", False, "震荡市仅允许极端超卖做多，当前非极端区域")
    elif regime == "range" and side == "short" and not (factors.get("rsi_14", 50) > 68 and factors.get("bb_pos", .5) > 0.8):
        add("regime", False, "震荡市仅允许极端超买做空，当前非极端区域")
    else:
        add("regime", True, factors.get("regime_reason", regime))

    # 2. 评分
    add("score", score >= SCORE_MIN, f"{side.upper()}_SCORE={score}（门槛 {SCORE_MIN}）")

    # 3. RR
    add("rr", plan["rr"] >= RR_MIN, f"RR={plan['rr']}（门槛 {RR_MIN}）")

    # 4. 追涨杀跌
    chase = chase_blocked(factors, side)
    add("chase", not chase, chase or "非追单状态")

    # 5. AI 一票否决（AI 说不允许 / 置信度 <60% / 方向相反）
    ai_ok = ai_dir == ("LONG" if side == "long" else "SHORT")
    if not ai_allowed:
        add("ai", False, "AI 分析 trade_allowed=false")
    elif ai_conf < 0.6:
        add("ai", False, f"AI 置信度 {ai_conf:.0%} < 60%，观察不交易")
    elif not ai_ok:
        add("ai", False, f"AI 方向 {ai_dir} 与 {side} 不一致")
    else:
        add("ai", True, f"AI 置信度 {ai_conf:.0%}")

    # 6. 熔断 / 回撤
    add("halted", not halted, "熔断中禁止开仓" if halted else "正常")
    if drawdown_pct >= DD_PAUSE_PCT:
        add("drawdown", False, f"回撤 {drawdown_pct:.1f}% >= {DD_PAUSE_PCT}%，暂停开仓")
    else:
        add("drawdown", True, f"回撤 {drawdown_pct:.1f}%（>{DD_THROTTLE_PCT}% 时仓位减半）")

    return all(c["ok"] for c in checks), checks


def position_size(equity: float, avail_usdt: float, plan: dict,
                  drawdown_pct: float, max_order_usdt: float,
                  fee_taker: float = 0.001) -> tuple[float, str]:
    """风险预算仓位：单笔最大亏损 = 权益 × 0.5%（回撤>3% 减半）。

    sz_base = 风险额 / 止损距离；同时受单笔上限、可用资金、最小下单额约束。
    返回 (名义 USDT, 说明)。
    """
    risk_usdt = equity * RISK_PER_TRADE_PCT
    if drawdown_pct >= DD_THROTTLE_PCT:
        risk_usdt *= 0.5
    risk_dist = float(plan["risk_dist"])
    if risk_dist <= 0:
        return 0.0, "止损距离为 0"
    notional = risk_usdt / (risk_dist / float(plan["entry"]))
    notes = [f"风险预算 {risk_usdt:.2f}U（权益×{RISK_PER_TRADE_PCT:.1%}"
             + (",回撤减半" if drawdown_pct >= DD_THROTTLE_PCT else "") + f"）÷ 止损距离 {risk_dist:.0f}"]
    notional = min(notional, max_order_usdt)
    notes.append(f"单笔上限 {max_order_usdt:.0f}U")
    # 买入手续费后不能透支可用资金
    notional = min(notional, avail_usdt / (1 + fee_taker) * 0.995)
    notes.append(f"可用资金 {avail_usdt:.2f}U")
    if notional < 1.0:
        return 0.0, "；".join(notes) + " → 不足最小下单额 1U"
    return round(notional, 2), "；".join(notes)
