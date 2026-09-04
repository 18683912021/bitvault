"""10 分钟涨跌预测（事件合约参考）。

口径对齐 OKX 事件合约（涨跌事件）：
  - 结算价 = 结算窗口内秒级指数 K 线收盘价的算术平均（防插针）；
  - 结果 = 窗口结算价 vs 窗口基准价（开仓时的价格）。
本模块预测「下一个 10 分钟窗口 涨 / 跌」，作为事件合约买入方向的参考。

大厂短周期方向判定要素（5~15 分钟级别）：
  1. 短期动量自相关（近几分钟收益惯性）
  2. 盘口买卖压力失衡（books5 挂单量比）
  3. 极值均值回归（RSI / 布林位置）
  4. 跨周期趋势一致性（1m EMA + 5m EMA 排列）
  5. 量能确认（放量方向跟随）
  6. 波动状态门控（极端波动/数据异常 → 置信度大幅折减，事件合约近似掷硬币）

纯确定性打分（无 LLM），10 秒刷新一次；只给参考概率，不给承诺。
"""
from __future__ import annotations

import math
import time
from typing import Any

from app.brain.factor_engine import _bollinger, _ema, _rsi, classify_regime, trend_of

WINDOW_MS = 600_000           # 10 分钟
REFRESH_SEC = 10              # 10 秒刷新

_cache: dict[str, tuple[int, dict]] = {}   # inst_id -> (cache_bucket, result)


def _tanh_scale(x: float, scale: float) -> float:
    return math.tanh(x / scale) if scale > 0 else 0.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def compute(data: Any, inst_id: str = "BTC-USDT") -> dict:
    """基于 1m/5m 收盘 K 线 + 盘口 + ticker 计算 10 分钟方向预测。同步、纯读。"""
    now_ms = int(time.time() * 1000)
    bucket = now_ms // (REFRESH_SEC * 1000)
    hit = _cache.get(inst_id)
    if hit and hit[0] == bucket:
        return hit[1]

    bars_1m = data.bars_from_db(inst_id, "1m", now_ms - 150 * 60_000, now_ms)
    bars_5m = data.bars_from_db(inst_id, "5m", now_ms - 20 * 3_600_000, now_ms)

    base = {
        "inst_id": inst_id,
        "ts": now_ms,
        "direction": "unknown",
        "p_up": 50.0,
        "score": 0.0,
        "conf_scale": 0.0,
        "regime": "unknown",
        "regime_reason": "",
        "window_start_ts": (now_ms // WINDOW_MS + 1) * WINDOW_MS,   # 下一整点窗口（对齐事件合约节奏）
        "window_end_ts": (now_ms // WINDOW_MS + 2) * WINDOW_MS,
        "ref_price": 0.0,
        "factors": [],
        "note": "",
    }

    if len(bars_1m) < 35:
        base["note"] = f"1 分钟 K 线仅 {len(bars_1m)} 根（需 ≥35），数据积累中，暂不预测"
        _cache[inst_id] = (bucket, base)
        return base

    closes = [b["c"] for b in bars_1m]
    ref_price = data.last_price(inst_id) or closes[-1]
    base["ref_price"] = round(ref_price, 2)

    # 市场状态门控（复用因子引擎的七态识别，只看已收盘 K 线，无未来函数）
    regime_info = classify_regime(bars_1m)
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

    # 1. 短期动量（近 5 根收盘 1m 收益惯性）
    if len(closes) >= 6:
        ret5 = (closes[-1] - closes[-6]) / closes[-6]
        add("mom_5m", "5分钟动量", f"{ret5 * 100:+.3f}%",
            _tanh_scale(ret5, 0.0025), 0.26)

    # 2. 1m EMA 排列强度
    ema_f, ema_s = _ema(closes, 9), _ema(closes, 21)
    if ema_s > 0:
        add("ema_1m", "1m均线排列", f"EMA9 {ema_f:.1f} vs EMA21 {ema_s:.1f}",
            _tanh_scale((ema_f - ema_s) / ema_s, 0.0012), 0.16)

    # 3. 5m 趋势一致性（跨周期确认）
    t5 = trend_of(bars_5m, 9, 21)
    add("ema_5m", "5m趋势", {"up": "多头", "down": "空头", "range": "横盘", "unknown": "数据不足"}[t5],
        {"up": 1.0, "down": -1.0}.get(t5, 0.0), 0.14)

    # 4. 盘口失衡（books5 买卖挂单量）
    book = data.books.get(inst_id)
    if book and book.get("bids") and book.get("asks"):
        bid_qty = sum(q for _, q in book["bids"])
        ask_qty = sum(q for _, q in book["asks"])
        if bid_qty + ask_qty > 0:
            imb = (bid_qty - ask_qty) / (bid_qty + ask_qty)
            add("book_imb", "盘口买卖压力", f"买/卖 {bid_qty:.2f}/{ask_qty:.2f}", imb, 0.20)

    # 5. RSI 均值回归（10 分钟级别超买超卖易回摆）
    rsi = _rsi(closes, 14)
    add("rsi_rev", "RSI超买超卖", f"RSI14={rsi:.1f}", _tanh_scale((50 - rsi) / 14, 1.0), 0.11)

    # 6. 布林位置回归
    _, _, _, bb_pos = _bollinger(closes, 20)
    add("bb_rev", "布林带位置", f"{bb_pos * 100:.0f}%位", (0.5 - bb_pos) * 2, 0.07)

    # 7. 量能确认（放量方向跟随）
    vols = [b.get("vol", 0) for b in bars_1m]
    if len(vols) >= 21 and sum(vols[-21:-1]) > 0:
        vol_ratio = vols[-1] / (sum(vols[-21:-1]) / 20)
        ret5_sign = 1 if closes[-1] >= closes[-6] else -1
        add("vol_conf", "量能确认", f"量比 {vol_ratio:.1f}x",
            ret5_sign * _clamp((vol_ratio - 1) / 2, -1, 1), 0.06)

    total_w = sum(f["weight"] for f in factors) or 1.0
    score = sum(f["contrib"] for f in factors) / total_w
    score = _clamp(score, -1, 1)

    # 置信度门控：极端波动 / 数据异常时大幅折减（事件合约此时接近掷硬币）
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
    notes = []
    if regime in ("extreme", "unknown"):
        notes.append(f"市场状态异常（{regime}），置信度已折减至 {conf_scale:.0%}")
    if direction == "flat":
        notes.append("多空信号抵消，方向不明，事件合约建议观望")
    notes.append("预测仅供参考，10 分钟涨跌接近随机博弈，请严格控仓")
    base["note"] = "；".join(notes)

    _cache[inst_id] = (bucket, base)
    return base
