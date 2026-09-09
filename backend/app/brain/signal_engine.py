"""确定性信号引擎 V2（纯规则，无 AI/LLM）。

决策链：Regime → HTF 确认 → Entry Setup → No-Trade Filter → Entry Score →
       RR/成本检查 → 仓位计算 → 执行。

设计原则：
- 所有规则可审计、可回测、确定性输出。
- 评分是入场质量度量，守卫链才有否决权；两者都通过才允许开仓。
- 宁可少交易：WAIT（无 setup / 守卫拦截）是常态。
"""
from __future__ import annotations

from typing import Any

from app.brain.factor_engine import _atr, _swing_levels

# ================= 可调参数（Phase 10-12 用 Walk-Forward 寻稳定区，不追单点最优） =================
SCORE_MIN = 70          # 评分门槛
RR_MIN = 2.0            # 最低风险回报比
COST_COVER_X = 4.0      # 预期毛利必须 ≥ 往返成本 × 4
CHASE_EMA_DIST = 2.0    # 价格偏离 EMA21 超过 max(1.5%, 2×atr_pct) 视为追涨/杀跌
CHASE_VOL_RATIO = 3.5   # 量比异常放大禁止追单
SL_MAX_PCT = 0.025      # 止损距离上限 2.5%（过远 → 风险不合理，拒绝）
SL_MIN_PCT = 0.004      # 止损距离下限 0.4%（费用约束）
SL_ATR_TREND = 1.8      # 趋势市 ATR 倍数（无结构点时兜底）
SL_ATR_RANGE = 1.2      # 震荡市 ATR 倍数
SL_BUFFER_ATR = 0.3     # 结构止损 ATR buffer
RR_TREND = 2.5          # 趋势市目标 R 倍数
RR_RANGE = 2.0          # 震荡市目标 R 倍数
RISK_PER_TRADE_PCT = 0.01    # 单笔风险占权益 1%
DD_THROTTLE_PCT = 3.0        # 回撤 > 3% 仓位减半
DD_PAUSE_PCT = 6.0           # 回撤 > 6% 暂停开仓
LOSS_STREAK_THROTTLE = 2     # 连亏 ≥2 仓位 ×0.75（≥3 ×0.5，暂停走 throttle）
MAX_SL_ATR_X = 3.0           # 止损距离 > 3×ATR 视为"距止损太远"

FEE_TAKER = 0.001
SLIP_PCT = 0.0002
ROUNDCOST_PCT = FEE_TAKER * 2 + SLIP_PCT * 2   # 往返成本 ≈ 0.24%


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ================= Entry Setup 识别（模式 A 趋势回调 / 模式 B 突破回踩） =================
def detect_setup(candles: list[dict], factors: dict, side: str) -> dict:
    """识别入场形态。返回 {"setup": ..., "why": str, "detail": {...}}。

    detail 为结构判定明细（broke/retest/level 等），纯信息暴露，不参与任何判定；
    只用已收盘 K 线；形态识别为加分项（结构分），硬性入场由守卫决定。
    """
    n = len(candles)
    if n < 25:
        return {"setup": "none", "why": "K线不足", "detail": {"kind": "none"}}
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    closes = [c["c"] for c in candles]
    vols = [c.get("vol", 0) for c in candles]
    last = closes[-1]
    ema21 = float(factors.get("ema_slow", 0)) or last
    atr = float(factors.get("atr_14", 0)) or last * 0.002
    long_side = side == "long"

    setup, why, detail = "none", "", {"kind": "none"}

    # ---- 模式 A：趋势回调（价格回踩 EMA21 附近 + 反转确认） ----
    # 回踩：最近 6 根内 low 触及 EMA21 ± 0.5×ATR（多单）；反抽：high 触及（空单）
    touched = False
    for i in range(max(0, n - 6), n):
        if long_side and lows[i] <= ema21 + 0.5 * atr:
            touched = True
        elif not long_side and highs[i] >= ema21 - 0.5 * atr:
            touched = True
    # 反转确认：最后一根收盘收在 EMA21 正确一侧且为方向 K 线
    confirm = (last > ema21 and closes[-1] > closes[-2]) if long_side else \
              (last < ema21 and closes[-1] < closes[-2])
    if touched and confirm:
        setup, why = "pullback", "回踩 EMA21 后反转确认"
        detail = {"kind": "pullback", "touched": touched, "confirm": confirm,
                  "level": round(ema21, 2)}
    else:
        detail = {"kind": "attempt" if touched else "none", "touched": touched,
                  "confirm": confirm, "level": round(ema21, 2)}

    # ---- 模式 B：突破回踩（突破近 40 根极值 + 突破强度 + 回踩到 level 附近不破） ----
    if setup == "none" and n >= 40:
        base = highs[-40:-3] if long_side else lows[-40:-3]
        level = max(base) if long_side else min(base)
        broke = False
        # 突破强度：近 3 根内有收盘突破 level ± 0.3×ATR（非刚好越过，过滤弱突破/假突破）
        for i in range(n - 3, n):
            if long_side and closes[i] > level + 0.3 * atr:
                broke = True
            if not long_side and closes[i] < level - 0.3 * atr:
                broke = True
        # 回踩确认：近 2 根 low 回到 level 附近（level-0.2×ATR ~ level+0.8×ATR）且不破
        # 要求真正回踩到 level 测试支撑，而非突破后高位震荡（过滤"假回踩"）
        retest_ok = False
        if broke:
            if long_side:
                retest_ok = all(level - 0.2 * atr <= lows[i] <= level + 0.8 * atr
                                for i in range(n - 2, n))
            else:
                retest_ok = all(level - 0.8 * atr <= highs[i] <= level + 0.2 * atr
                                for i in range(n - 2, n))
        if broke and retest_ok:
            setup, why = "breakout_retest", f"突破 {'高点' if long_side else '低点'} {level:.0f}+强度确认 回踩不破"
            detail = {"kind": "breakout_retest", "broke": True, "retest": True,
                      "level": round(level, 2), "atr": round(atr, 2),
                      "long_side": long_side}
        else:
            # 暴露"只突破未回踩/未突破"，供报告区分普通回踩 vs 突破回踩
            detail = {"kind": "breakout_attempt" if broke else "no_breakout",
                      "broke": broke, "retest": retest_ok,
                      "level": round(level, 2), "atr": round(atr, 2),
                      "long_side": long_side}

    return {"setup": setup, "why": why or "无明确形态（观望）", "detail": detail}


# ================= 评分体系（纯规则 0-100） =================
def score_side(factors: dict, side: str, setup: dict,
               htf_4h: str | None = None, htf_1h: str | None = None) -> tuple[int, list[dict]]:
    """纯规则打分。AI 评分项移除，权重分配：趋势20 + HTF15 + 动量15 + 结构15 + 量10 + 波动10 + 位置15。"""
    f = factors
    rsi = float(f.get("rsi_14", 50))
    bb = float(f.get("bb_pos", 0.5))
    hist = float(f.get("macd_hist", 0))
    adx = float(f.get("adx_14", 0))
    atr_pct = float(f.get("atr_pct", 0))        # %
    vr = float(f.get("volume_ratio", 1))
    regime = f.get("regime", "range")
    long_side = side == "long"
    items: list[dict] = []

    def add(k: str, v: int, mx: int, why: str) -> None:
        items.append({"k": k, "v": v, "max": mx, "why": why})

    # ---- 趋势 20：regime 对齐 ----
    if long_side:
        if regime == "trend_up":
            t = 20 if hist > 0 else 14
        elif regime == "range":
            t = 8        # 震荡只允许极端反转，评分保守
        else:
            t = 0
    else:
        if regime == "trend_down":
            t = 20 if hist < 0 else 14
        elif regime == "range":
            t = 8
        else:
            t = 0
    # ADX 修正：趋势市 ADX 不足回收趋势分
    if regime in ("trend_up", "trend_down") and adx < 25:
        t = max(0, t - (8 if adx < 22 else 4))
    add("趋势", t, 20, f"regime={regime} ADX={adx:.0f} hist={hist:.2f}")

    # ---- 高周期确认 15：4H 权重 10 + 1H 权重 5（不机械要求全一致） ----
    want = "up" if long_side else "down"
    h = 0
    if htf_4h == want:
        h += 10
    elif htf_4h == "range" or htf_4h is None:
        h += 5 if htf_1h == want else 2
    # 4H 反向 → 0（守卫会直接拦截）
    if htf_1h == want:
        h += 5
    elif htf_1h == "range" or htf_1h is None:
        h += 2
    add("高周期", h, 15, f"4H={htf_4h} 1H={htf_1h}")

    # ---- 动量 15 ----
    if long_side:
        if 48 <= rsi < 70 and hist > 0:
            m = 15
        elif 40 <= rsi < 48:
            m = 8
        elif rsi < 30 and bb < 0.15:
            m = 12       # 超卖反转（仅震荡市可用，守卫控制）
        else:
            m = 4 if hist > 0 else 0
    else:
        if 30 < rsi <= 52 and hist < 0:
            m = 15
        elif 52 < rsi <= 60:
            m = 8
        elif rsi > 70 and bb > 0.85:
            m = 12
        else:
            m = 4 if hist < 0 else 0
    add("动量", m, 15, f"RSI={rsi:.0f}")

    # ---- 价格结构/形态 15：setup 是核心结构分 ----
    s_name = setup.get("setup", "none")
    if s_name == "pullback":
        s = 15
    elif s_name == "breakout_retest":
        s = 13
    elif long_side and bb < 0.2:
        s = 8
    elif not long_side and bb > 0.8:
        s = 8
    else:
        s = 0
    add("结构", s, 15, setup.get("why", ""))

    # ---- 成交量 10：温和放大加分，异常放量扣分 ----
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
    add("成交量", v, 10, f"量比={vr:.2f}")

    # ---- 波动率 10：ATR% 区间 + 分位 ----
    pctile = float(f.get("atr_pctile", 0.5))
    if 0.15 <= atr_pct <= 1.2 and 0.2 <= pctile <= 0.85:
        vol_s = 10
    elif 1.2 < atr_pct <= 2.0:
        vol_s = 6
    elif 0.08 <= atr_pct < 0.15:
        vol_s = 4
    else:
        vol_s = 0
    add("波动率", vol_s, 10, f"ATR%={atr_pct:.2f} 分位={pctile:.0%}")

    # ---- 位置 15：不追高/不杀低，赔率来自位置 ----
    ema_dist = float(f.get("ema21_dist_pct", 0))
    if long_side:
        if -0.5 <= ema_dist <= 1.0:
            p = 15
        elif 1.0 < ema_dist <= 2.0:
            p = 8
        elif ema_dist < -0.5:
            p = 10       # 下方有回归赔率
        elif 2.0 < ema_dist <= 3.0:
            p = 3
        else:
            p = 0        # >3% 严重追高
    else:
        if -1.0 <= ema_dist <= 0.5:
            p = 15
        elif -2.0 <= ema_dist < -1.0:
            p = 8
        elif ema_dist > 0.5:
            p = 10
        elif -3.0 <= ema_dist < -2.0:
            p = 3
        else:
            p = 0
    add("位置", p, 15, f"偏离EMA21 {ema_dist:.2f}%")

    return sum(i["v"] for i in items), items


# ================= 追涨杀跌（Overextension Filter） =================
def chase_blocked(factors: dict, side: str) -> str:
    """返回拦截原因（空串=放行）。"""
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


def divergence_blocked(candles: list[dict], side: str) -> str:
    """量价背离：近 5 根价格新高/新低但量能萎缩（<0.8×20均量）→ 拦截。"""
    if len(candles) < 20:
        return ""
    closes = [c["c"] for c in candles]
    vols = [c.get("vol", 0) for c in candles]
    avg_vol = sum(vols[-20:]) / 20
    if avg_vol <= 0:
        return ""
    recent = closes[-5:]
    if side == "long":
        if closes[-1] >= max(recent) and vols[-1] < 0.8 * avg_vol:
            return f"量价背离：价新高但量比 {vols[-1] / avg_vol:.2f} 萎缩，禁止追多"
    else:
        if closes[-1] <= min(recent) and vols[-1] < 0.8 * avg_vol:
            return f"量价背离：价新低但量比 {vols[-1] / avg_vol:.2f} 萎缩，禁止追空"
    return ""


# ================= 交易计划（结构止损 + 结构阻力/支撑复核） =================
def plan_trade(candles: list[dict], factors: dict, side: str, round_px=None) -> dict:
    """结构优先止损：最近有效 Swing Low/High ± 0.3×ATR；无结构用 ATR 兜底。

    同时输出 next_resistance / next_support 供"前方空间不足"守卫复核。
    round_px: 可选价格取整函数（按标的 tickSz）；传入时 sl/tp 按交易所规格取整，
    不传则保留原始浮点（供回测等无需下单规格的场景）。
    """
    px = float(factors["last_px"])
    atr = float(factors.get("atr_14", 0)) or px * 0.002
    regime = factors.get("regime", "range")
    trend_regime = regime in ("trend_up", "trend_down")
    rr_target = RR_TREND if trend_regime else RR_RANGE
    long_side = side == "long"

    sw_h, sw_l = _swing_levels([c["h"] for c in candles], [c["l"] for c in candles],
                               left=2, right=2, max_points=12)
    min_sl = px * SL_MIN_PCT
    if long_side:
        # 低于 entry 的最近 swing low（取最高的一个=最近有效支撑）
        valid = [v for v in sw_l if v < px]
        struct_sl = (max(valid) - SL_BUFFER_ATR * atr) if valid else None
        atr_sl = px - max(SL_ATR_TREND if trend_regime else SL_ATR_RANGE, 0) * atr
        sl = max(min(x for x in [struct_sl, atr_sl] if x is not None), px - SL_MAX_PCT * px)
        sl = min(sl, px - min_sl)                       # 不低于最小止损距离
        sl = max(sl, px - SL_MAX_PCT * px)              # 不超过最大止损距离
        # 前方阻力：entry 上方最近的 swing high
        res = [v for v in sw_h if v > px]
        next_res = min(res) if res else None
        tp = px + max(rr_target * (px - sl), px * 0.009)
    else:
        valid = [v for v in sw_h if v > px]
        struct_sl = (max(valid) + SL_BUFFER_ATR * atr) if valid else None
        atr_sl = px + (SL_ATR_TREND if trend_regime else SL_ATR_RANGE) * atr
        sl = min(x for x in [struct_sl, atr_sl] if x is not None)
        sl = max(sl, px + min_sl)
        sl = min(sl, px + SL_MAX_PCT * px)
        sup = [v for v in sw_l if v < px]
        next_sup = max(sup) if sup else None
        tp = px - max(rr_target * (sl - px), px * 0.009)

    risk = abs(px - sl)
    reward = abs(tp - px)
    # round_px 传入时按标的规格取整；否则保留原始精度（round(x,2) 会把微价币种归零）
    # risk_dist/reward_dist 是价格量（dist 距离），必须保留原始精度——
    # DOOD 等微价币（~0.001）的 risk≈4e-6，round(,2) 归零会污染 cost/sl_range/
    # liq_safety/position_size 守卫；比值类（rr/risk_atr_x）保持 round 不变。
    out = {
        "entry": px, "sl": round_px(sl) if round_px else sl,
        "tp": round_px(tp) if round_px else tp,
        "risk_dist": risk, "reward_dist": reward,
        "rr": round(reward / risk, 2) if risk > 0 else 0,
        "rr_target": rr_target, "regime": regime,
        "sl_basis": "structure" if struct_sl is not None else "atr",
        "risk_atr_x": round(risk / atr, 2) if atr > 0 else 0,
    }
    if long_side:
        out["next_resistance"] = round(min(res), 2) if res else None
    else:
        out["next_support"] = round(next_sup, 2) if sup else None
    return out


# ================= No-Trade Filter + 硬守卫链（十一项） =================
def check_gate(candles: list[dict], factors: dict, side: str, score: int, plan: dict,
               ctx: dict) -> tuple[bool, list[dict]]:
    """十一项守卫（P1-9 文档修正：实为 11 项，含 liq_safety）。全部通过才放行。ctx 需含：htf_4h / htf_1h / halted / drawdown_pct / loss_streak。

    返回 (是否放行, 检查明细)。
    """
    f = factors
    regime = f.get("regime", "range")
    rsi = float(f.get("rsi_14", 50))
    bb = float(f.get("bb_pos", 0.5))
    atr_pct = float(f.get("atr_pct", 0)) or 0.01
    htf_4h = ctx.get("htf_4h")
    htf_1h = ctx.get("htf_1h")
    halted = bool(ctx.get("halted"))
    dd = float(ctx.get("drawdown_pct", 0))
    loss_streak = int(ctx.get("loss_streak", 0))

    checks: list[dict] = []

    def add(name: str, ok: bool, why: str) -> None:
        checks.append({"check": name, "ok": ok, "why": why})

    # ---- 1. regime 检查（趋势不明确/极端/异常波动禁止） ----
    if regime == "extreme":
        add("regime", False, f"极端市场：{f.get('regime_reason', '')}，禁止新开仓")
    elif regime == "high_vol":
        add("regime", False, f"高波动无趋势：{f.get('regime_reason', '')}，禁止新开仓")
    elif regime == "low_vol":
        add("regime", False, f"低波动：{f.get('regime_reason', '')}，观望")
    elif side == "long" and regime == "trend_down":
        add("regime", False, "下跌趋势中禁止做多")
    elif side == "short" and regime == "trend_up":
        add("regime", False, "上涨趋势中禁止做空")
    elif regime == "range":
        # 震荡市仅允许极端反转区域
        if side == "long" and not (rsi < 30 and bb < 0.15):
            add("regime", False, f"震荡市仅允许极端超卖做多（RSI={rsi:.0f} bb={bb:.2f}）")
        elif side == "short" and not (rsi > 70 and bb > 0.85):
            add("regime", False, f"震荡市仅允许极端超买做空（RSI={rsi:.0f} bb={bb:.2f}）")
        else:
            add("regime", True, "震荡市极端反转区域")
    elif regime == "unknown":
        # V3 §3/§33：数据不足/异常 → 禁止开仓（防 glitch/缺数据误交易）
        add("regime", False, f"市场状态未知：{f.get('regime_reason', '数据不足/异常')}，禁止开仓")
    else:
        add("regime", True, f.get("regime_reason", regime))

    # ---- 2. 高周期冲突（4H 明显反向 → 禁止） ----
    want = "up" if side == "long" else "down"
    if htf_4h is not None and htf_4h != "unknown" and htf_4h != want and htf_4h != "range":
        add("htf_conflict", False, f"4H 大周期{'下跌' if htf_4h == 'down' else '上涨'}，禁止低周期{'做多' if side == 'long' else '做空'}")
    else:
        add("htf_conflict", True, f"4H={htf_4h} 1H={htf_1h}")

    # ---- 3. 评分门槛 ----
    add("score", score >= SCORE_MIN, f"SCORE={score}（门槛 {SCORE_MIN}）")

    # ---- 4. RR ----
    add("rr", plan["rr"] >= RR_MIN, f"RR={plan['rr']}（门槛 {RR_MIN}）")

    # ---- 5. 追涨杀跌 ----
    chase = chase_blocked(f, side)
    add("chase", not chase, chase or "非追单状态")

    # ---- 6. 量价背离 ----
    div = divergence_blocked(candles, side)
    add("divergence", not div, div or "无量价背离")

    # ---- 7. 止损距离合理性（太远=风险失控；太近=噪音） ----
    risk_pct = plan["risk_dist"] / plan["entry"]
    if risk_pct > SL_MAX_PCT:
        add("sl_range", False, f"止损距离 {risk_pct:.2%} > {SL_MAX_PCT:.1%}，风险不合理")
    elif float(plan.get("risk_atr_x", 0)) > MAX_SL_ATR_X:
        add("sl_range", False, f"止损 {plan['risk_atr_x']}×ATR > {MAX_SL_ATR_X}×ATR，结构点过远")
    else:
        add("sl_range", True, f"止损距离 {risk_pct:.2%}（{plan.get('risk_atr_x')}×ATR，{plan.get('sl_basis')}）")

    # ---- 8. 前方结构空间（阻力/支撑太近 → RR 名义达标实际不达） ----
    reward_dist = plan["reward_dist"]
    if side == "long" and plan.get("next_resistance"):
        room = plan["next_resistance"] - plan["entry"]
        if 0 < room < reward_dist * 0.8:
            add("structure_room", False,
                f"前方阻力 {plan['next_resistance']:.0f} 距 entry 仅 {room:.0f} < 0.8×目标，空间不足")
        else:
            add("structure_room", True, f"前方阻力空间 {room:.0f}")
    elif side == "short" and plan.get("next_support"):
        room = plan["entry"] - plan["next_support"]
        if 0 < room < reward_dist * 0.8:
            add("structure_room", False,
                f"前方支撑 {plan['next_support']:.0f} 距 entry 仅 {room:.0f} < 0.8×目标，空间不足")
        else:
            add("structure_room", True, f"前方支撑空间 {room:.0f}")
    else:
        add("structure_room", True, "无近距结构位")

    # ---- 9. 成本覆盖（毛利必须 ≥ 4×往返成本） ----
    gross_profit_pct = reward_dist / plan["entry"]
    if gross_profit_pct < ROUNDCOST_PCT * COST_COVER_X:
        add("cost", False, f"预期毛利 {gross_profit_pct:.2%} < {ROUNDCOST_PCT * COST_COVER_X:.2%}（{COST_COVER_X}×往返成本），经济性不足")
    else:
        add("cost", True, f"毛利 {gross_profit_pct:.2%} 覆盖成本 {ROUNDCOST_PCT * COST_COVER_X:.2%}")

    # ---- 10. 连亏 / 熔断 / 回撤 ----
    if halted:
        add("risk_state", False, "风控熔断中禁止开仓")
    elif dd >= DD_PAUSE_PCT:
        add("risk_state", False, f"回撤 {dd:.1f}% >= {DD_PAUSE_PCT}%，暂停开仓")
    elif loss_streak >= 5:
        add("risk_state", False, f"连亏 {loss_streak} 次，策略暂停")
    else:
        note = f"回撤 {dd:.1f}%，连亏 {loss_streak}"
        if loss_streak >= 3:
            note += "（仓位×0.5）"
        elif loss_streak >= LOSS_STREAK_THROTTLE:
            note += "（仓位×0.75）"
        if dd >= DD_THROTTLE_PCT:
            note += "（回撤减半）"
        add("risk_state", True, note)

    # ---- 11. 清算安全（合约模式：止损必须明显早于清算） ----
    leverage = int(ctx.get("leverage", 1) or 1)
    if leverage > 1:
        liq_dist_pct = 1.0 / leverage - 0.005   # 与 autopilot liq_px 公式一致
        sl_dist_pct = plan["risk_dist"] / plan["entry"]
        if sl_dist_pct >= liq_dist_pct:
            add("liq_safety", False,
                f"止损距离 {sl_dist_pct:.2%} >= 清算距离 {liq_dist_pct:.2%}，止损晚于清算，NO-TRADE")
        else:
            add("liq_safety", True,
                f"止损距离 {sl_dist_pct:.2%} < 清算距离 {liq_dist_pct:.2%}（安全缓冲 {(liq_dist_pct - sl_dist_pct):.2%}）")
    else:
        add("liq_safety", True, "现货模式无清算风险")

    return all(c["ok"] for c in checks), checks


def position_size(equity: float, avail_usdt: float, plan: dict, drawdown_pct: float,
                  loss_streak: int, max_order_usdt: float,
                  fee_taker: float = FEE_TAKER, leverage: int = 1) -> tuple[float, str]:
    """风险预算仓位：单笔最大亏损 = 权益 × 1.0%（RISK_PER_TRADE_PCT）。

    衰减档：连亏≥2 ×0.75；连亏≥3 ×0.5；回撤≥3% ×0.5（叠加）。
    禁止马丁格尔：无任何加仓/加倍路径，亏损后只降不升。

    notional = 全仓位价值（full position value），不是保证金。
    杠杆只影响保证金需求（margin = notional / leverage），不放大风险预算。
    可用资金约束：margin + fee ≤ avail → notional ≤ avail×lev / (1 + lev×fee)。
    """
    risk_usdt = equity * RISK_PER_TRADE_PCT
    mult = 1.0
    if loss_streak >= 3:
        mult *= 0.5
    elif loss_streak >= LOSS_STREAK_THROTTLE:
        mult *= 0.75
    if drawdown_pct >= DD_THROTTLE_PCT:
        mult *= 0.5
    risk_usdt *= mult

    risk_dist = float(plan["risk_dist"])
    if risk_dist <= 0:
        return 0.0, "止损距离为 0"
    notional = risk_usdt / (risk_dist / float(plan["entry"]))
    notes = [f"风险 {risk_usdt:.2f}U（1%%×权益{f'×衰减{mult:.2f}' if mult < 1 else ''}）÷止损距离 {risk_dist:.0f}"]
    notional = min(notional, max_order_usdt)
    notes.append(f"单笔上限 {max_order_usdt:.0f}U")
    notional = min(notional, avail_usdt * leverage / (1 + leverage * fee_taker) * 0.995)
    notes.append(f"可用 {avail_usdt:.2f}U" + (f" ×{leverage}杠杆" if leverage > 1 else ""))
    if notional < 1.0:
        return 0.0, "；".join(notes) + " → 不足最小下单额 1U"
    return round(notional, 2), "；".join(notes)
