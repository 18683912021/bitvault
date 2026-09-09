"""未来 24 小时涨跌预测（纯规则、无 AI）：确定性多因子打分 + 建议开仓/止盈/止损价。

口径：窗口 = 当前时刻 → +24h（非事件合约对齐）；主时间框架 1H + 4H 趋势确认。
方向判定要素（24 小时级别）：
  1. 近 24h 动量（收盘价对比 24h 前）
  2. 1H EMA9/21 排列 + ADX 趋势强度
  3. 4H 趋势一致性（跨周期确认）
  4. RSI14 均值回归（1H 级别超买超卖）
  5. 布林位置 + 量能确认
  6. 市场状态门控（classify_regime 七态置信缩放）

建议价复用 signal_engine.plan_trade（与 autopilot 决策同源口径）：
  开仓价=现价参考；止损=结构/ATR 位；止盈 1R/2R 分批目标 + 计划目标位。
仅作参考展示，不自动下单；现货策略（leverage=1）看跌时不给做空建议。

纯确定性打分（无 LLM），每 10 秒刷新；只给参考概率，不给承诺。
"""
from __future__ import annotations

import math
import time
from typing import Any

from app.brain import signal_engine as se
from app.brain.factor_engine import _bollinger, _rsi, classify_regime, compute_factors, trend_of

WINDOW_MS = 86_400_000      # 24 小时
REFRESH_SEC = 10            # 10 秒刷新


def _tanh_scale(x: float, scale: float) -> float:
    return math.tanh(x / scale) if scale > 0 else 0.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _is_spot(data: Any, inst_id: str) -> bool:
    ins = data.instrument(inst_id)
    return bool(ins and ins.get("instType") == "SPOT")


def _round_px(data: Any, inst_id: str, px: float) -> float:
    return data.round_px(inst_id, px) if px else px


def compute(data: Any, inst_id: str = "BTC-USDT") -> dict:
    """基于 1H/4H 收盘 K 线 + ticker 计算未来 24 小时方向预测。同步、纯读。"""
    now_ms = int(time.time() * 1000)
    base = {
        "inst_id": inst_id,
        "ts": now_ms,
        "horizon": "24h",
        "direction": "unknown",
        "p_up": 50.0,
        "score": 0.0,
        "conf_scale": 0.0,
        "regime": "unknown",
        "regime_reason": "",
        "window_start_ts": now_ms,              # 当前时刻 → +24h
        "window_end_ts": now_ms + WINDOW_MS,
        "ref_price": 0.0,
        "factors": [],
        "suggestion": None,
        "note": "",
    }

    bars_1h = data.bars_from_db(inst_id, "1H", now_ms - 72 * 3_600_000, now_ms)
    bars_4h = data.bars_from_db(inst_id, "4H", now_ms - 7 * 86_400_000, now_ms)

    if len(bars_1h) < 30:
        base["note"] = f"1 小时 K 线仅 {len(bars_1h)} 根（需 ≥30），数据积累中，暂不预测"
        base["ref_price"] = _round_px(data, inst_id, data.last_price(inst_id) or 0) or 0.0
        return base

    closes = [b["c"] for b in bars_1h]
    ref_price = data.last_price(inst_id) or closes[-1]
    base["ref_price"] = _round_px(data, inst_id, ref_price) or 0.0
    closes_l = closes + [ref_price]   # 实时价并入动量/均线（仅已发生数据，无未来函数）

    # 市场状态门控（七态）
    regime_info = classify_regime(bars_1h)
    regime = regime_info.get("regime", "unknown")
    base["regime"] = regime
    base["regime_reason"] = regime_info.get("regime_reason", "")
    conf_scale = {
        "extreme": 0.35, "unknown": 0.35,
        "high_vol": 0.60, "low_vol": 0.70,
        "range": 0.85, "trend_up": 1.0, "trend_down": 1.0,
    }.get(regime, 0.85)
    base["conf_scale"] = conf_scale

    factors: list[dict] = []

    def add(key: str, label: str, value_text: str, score: float, weight: float) -> None:
        factors.append({
            "key": key, "label": label, "value_text": value_text,
            "score": round(score, 3), "weight": weight,
            "contrib": round(score * weight, 3),
        })

    # 1. 近 24h 动量（收益惯性的主成分，权重最高）
    if len(closes_l) >= 25:
        # 24h 前收盘 = 最后一根 24h 前的已收盘 1H bar；尾补实时价
        px_24h_ago = closes[-25] if len(closes) >= 25 else closes[0]
        ret_24h = (closes_l[-1] - px_24h_ago) / px_24h_ago if px_24h_ago > 0 else 0.0
        add("mom_24h", "近24小时动量", f"{ret_24h * 100:+.2f}%",
            _tanh_scale(ret_24h, 0.03), 0.28)

    # 2. 1H EMA 排列 + ADX 趋势强度（含实时价）
    f1 = compute_factors(bars_1h) if len(bars_1h) >= 60 else None
    ema_fast, ema_slow = None, None
    if f1 and not f1.get("error"):
        ema_fast, ema_slow = f1.get("ema_fast"), f1.get("ema_slow")
        adx = float(f1.get("adx_14", 0))
        ts_frac = float(f1.get("trend_strength", 0) or 0) / 100   # 百分比 → 小数
        add("h1_trend", "1H趋势强度", f"ADX={adx:.0f} {f1.get('trend', '')}",
            _tanh_scale(ts_frac, 0.004), 0.22)

    # 3. 4H 趋势一致性（跨周期确认）
    t4h = trend_of(bars_4h, 9, 21) if len(bars_4h) >= 23 else "unknown"
    add("h4_trend", "4H趋势", {"up": "多头", "down": "空头", "range": "横盘", "unknown": "数据不足"}[t4h],
        {"up": 0.9, "down": -0.9}.get(t4h, 0.0), 0.20)

    # 4. RSI 均值回归（1H 级别超买超卖易回摆）
    rsi = _rsi(closes, 14)
    add("rsi_rev", "RSI超买超卖", f"RSI14={rsi:.1f}", _tanh_scale((50 - rsi) / 18, 1.0), 0.12)

    # 5. 布林位置回归
    _, _, _, bb_pos = _bollinger(closes, 20)
    add("bb_rev", "布林带位置", f"{bb_pos * 100:.0f}%位", (0.5 - bb_pos) * 2, 0.08)

    # 6. 量能确认（1H 最近量比，方向随 24h 动量）
    vols = [b.get("vol", 0) for b in bars_1h]
    if len(vols) >= 21 and sum(vols[-21:-1]) > 0:
        vol_ratio = vols[-1] / (sum(vols[-21:-1]) / 20)
        ret24_sign = 1 if (len(closes_l) >= 25 and closes_l[-1] >= closes_l[-2]) else -1
        add("vol_conf", "量能确认", f"量比 {vol_ratio:.1f}x",
            ret24_sign * _clamp((vol_ratio - 1) / 2, -1, 1), 0.10)

    total_w = sum(f["weight"] for f in factors) or 1.0
    score = _clamp(sum(f["contrib"] for f in factors) / total_w, -1, 1)

    p_up = _clamp(50 + score * 50 * conf_scale, 12, 88)
    if score >= 0.08:
        direction = "up"
    elif score <= -0.08:
        direction = "down"
    else:
        direction = "flat"

    base.update({
        "direction": direction,
        "p_up": round(p_up, 1),
        "score": round(score, 3),
        "factors": factors,
    })

    # ---- 建议开仓/止盈/止损（复用 autopilot 的 plan_trade 口径） ----
    suggestion = None
    if direction == "flat" or not f1 or f1.get("error"):
        if direction == "flat":
            suggestion = {"side": "flat", "note": "多空信号抵消，建议观望"}
    else:
        side = "long" if direction == "up" else "short"
        if side == "short" and _is_spot(data, inst_id):
            suggestion = {"side": "flat", "note": "看跌但现货不可做空，建议观望"}
        else:
            plan = se.plan_trade(bars_1h, f1, side, round_px=lambda v: _round_px(data, inst_id, v))
            risk_dist = plan.get("risk_dist") or ref_price * 0.008
            sign = 1 if side == "long" else -1
            suggestion = {
                "side": side,
                "entry_px": _round_px(data, inst_id, plan["entry"]),
                "sl_px": _round_px(data, inst_id, plan["sl"]),
                "tp1_px": _round_px(data, inst_id, ref_price + sign * risk_dist),
                "tp2_px": _round_px(data, inst_id, ref_price + sign * risk_dist * 2),
                "tp_px": _round_px(data, inst_id, plan["tp"]),
                "rr": plan.get("rr", 0),
                "sl_basis": plan.get("sl_basis", "atr"),
                "note": "1R/2R 分批止盈，终极目标见 tp；仅参考，不自动下单",
            }
    base["suggestion"] = suggestion

    notes = []
    if regime in ("extreme", "unknown"):
        notes.append(f"市场状态异常（{regime}），置信度已折减至 {conf_scale:.0%}")
    if direction == "flat":
        notes.append("多空信号抵消，方向不明，建议观望")
    notes.append("预测仅供参考（未来24小时，非承诺），建议价来自策略同源规则")
    base["note"] = "；".join(notes)
    return base
