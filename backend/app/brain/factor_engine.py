"""因子引擎：纯函数，无状态。输入 K 线序列（按时间升序），输出结构化因子字典。

只实现最常用的 8 个因子：RSI / MACD / 布林位置 / ATR / 波动率分位 / EMA 趋势 / 量比 / 收益率。
不追求完备，LLM 负责综合解读。
"""
from __future__ import annotations

from typing import Any


def _sma(values: list[float], n: int) -> float:
    if len(values) < n or n <= 0:
        return 0.0
    return sum(values[-n:]) / n


def _ema(values: list[float], n: int) -> float:
    if len(values) < n or n <= 0:
        return 0.0
    k = 2 / (n + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema


def _rsi(closes: list[float], n: int = 14) -> float:
    if len(closes) < n + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    gains, losses = gains[-(n):], losses[-(n):]
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _macd(closes: list[float]) -> tuple[float, float, float]:
    if len(closes) < 35:
        return 0.0, 0.0, 0.0
    ema12 = closes[0]
    ema26 = closes[0]
    k12, k26 = 2 / 13, 2 / 27
    diffs = []
    for v in closes[1:]:
        ema12 = v * k12 + ema12 * (1 - k12)
        ema26 = v * k26 + ema26 * (1 - k26)
        diffs.append(ema12 - ema26)
    # signal = EMA9 of MACD
    macd_now = diffs[-1]
    if len(diffs) < 9:
        return macd_now, 0.0, macd_now
    k9 = 2 / 10
    signal = diffs[0]
    for v in diffs[1:]:
        signal = v * k9 + signal * (1 - k9)
    return macd_now, signal, macd_now - signal


def _bollinger(closes: list[float], n: int = 20) -> tuple[float, float, float, float]:
    """返回 (mid, upper, lower, pos)；pos ∈ [0,1]，0=下轨 1=上轨。"""
    if len(closes) < n:
        last = closes[-1] if closes else 0
        return last, last, last, 0.5
    window = closes[-n:]
    mid = sum(window) / n
    var = sum((x - mid) ** 2 for x in window) / n
    std = var ** 0.5
    upper = mid + 2 * std
    lower = mid - 2 * std
    last = closes[-1]
    if upper == lower:
        pos = 0.5
    else:
        pos = (last - lower) / (upper - lower)
    return mid, upper, lower, max(0, min(1, pos))


def _atr(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> float:
    if len(closes) < n + 1:
        return 0.0
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return sum(trs[-n:]) / n


def _adx(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> float:
    """ADX(n)：趋势强度（Wilder）。<20 无趋势，20-25 弱趋势，>=25 趋势确立。"""
    if len(closes) < 2 * n + 2:
        return 0.0
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    trs: list[float] = []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))

    def _wilder(seq: list[float]) -> list[float]:
        s = sum(seq[:n])
        out = [s]
        for v in seq[n:]:
            s = s - s / n + v
            out.append(s)
        return out

    trs_s, pdm_s, mdm_s = _wilder(trs), _wilder(plus_dm), _wilder(minus_dm)
    dxs = []
    for tr, p, m in zip(trs_s, pdm_s, mdm_s):
        if tr <= 0:
            dxs.append(0.0)
            continue
        pdi, mdi = 100 * p / tr, 100 * m / tr
        s = pdi + mdi
        dxs.append(100 * abs(pdi - mdi) / s if s > 0 else 0.0)
    if not dxs:
        return 0.0
    adx = sum(dxs[:n]) / n
    for v in dxs[n:]:
        adx = (adx * (n - 1) + v) / n
    return adx


def classify_regime(candles: list[dict]) -> dict[str, Any]:
    """确定性市场状态识别（不依赖 LLM）。

    extreme_vol 优先：ATR 突增 / 单根异常振幅 / 放量暴涨暴跌——降低仓位或禁止新开仓。
    bull/bear_trend：ADX>=22 且 EMA 排列成立且价格在 EMA21 正确一侧。
    其余一律 range（震荡：不追涨杀跌，只考虑极端反转或观望）。
    """
    closes = [c["c"] for c in candles]
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    vols = [c.get("vol", 0) for c in candles]
    last = closes[-1]
    atr = _atr(highs, lows, closes, 14)
    adx = _adx(highs, lows, closes, 14)
    ema_fast = _ema(closes, 9)
    ema_slow = _ema(closes, 21)
    atr_pct = atr / last if last > 0 else 0

    # 正常波动基线：最近 100 根 TR 均值（不含突增窗口的粗略基线）
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
           for i in range(max(1, len(closes) - 100), len(closes))]
    atr_base = sum(trs) / len(trs) if trs else atr
    atr_spike = atr / atr_base if atr_base > 0 else 1.0

    # 单根异常振幅：最后一根实体波幅 >= 2.5×ATR
    last_range = (highs[-1] - lows[-1]) / last if last > 0 else 0
    # 近 3 根累计收益（暴涨暴跌检测）
    ret_3 = (closes[-1] - closes[-4]) / closes[-4] if len(closes) >= 4 else 0
    avg_vol = sum(vols[-20:]) / 20 if len(vols) >= 20 and sum(vols[-20:]) > 0 else 0
    vol_ratio = vols[-1] / avg_vol if avg_vol > 0 else 1.0

    extreme_reason = ""
    if atr_spike >= 1.8:
        extreme_reason = f"ATR 突增 {atr_spike:.1f} 倍于常态"
    elif last_range >= atr * 2.5 and atr > 0:
        extreme_reason = f"单根振幅 {last_range * 100:.2f}% 异常（>=2.5×ATR）"
    elif vol_ratio >= 4 and abs(ret_3) >= 0.012:
        extreme_reason = f"放量暴涨暴跌：3 根 {ret_3 * 100:+.2f}%、量比 {vol_ratio:.1f}"

    if extreme_reason:
        regime = "extreme_vol"
    elif adx >= 22 and ema_fast > ema_slow and last > ema_slow:
        regime = "bull_trend"
    elif adx >= 22 and ema_fast < ema_slow and last < ema_slow:
        regime = "bear_trend"
    else:
        regime = "range"

    return {
        "regime": regime,
        "regime_reason": extreme_reason or (
            f"ADX={adx:.0f}, EMA{'>' if ema_fast > ema_slow else '<'}排列" if regime != "range"
            else f"ADX={adx:.0f} 无有效趋势"),
        "adx_14": round(adx, 1),
        "atr_spike": round(atr_spike, 2),
        "atr_base_pct": round(atr_base / last * 100, 3) if last > 0 else 0,
        "ema21_dist_pct": round((last - ema_slow) / ema_slow * 100, 3) if ema_slow > 0 else 0,
        "volume_ratio": round(vol_ratio, 3),
    }


def compute_factors(candles: list[dict]) -> dict[str, Any]:
    """candles: K 线列表（按时间升序），字段 ts/o/h/l/c/vol。

    返回结构化因子字典，供 LLM 决策使用。
    """
    if not candles or len(candles) < 2:
        return {"last_px": 0, "error": "K 线不足"}

    closes = [c["c"] for c in candles]
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    vols = [c.get("vol", 0) for c in candles]

    last_px = closes[-1]
    # 收益率
    ret_1 = (closes[-1] - closes[-2]) / closes[-2] if len(closes) >= 2 else 0
    ret_5 = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 else 0
    ret_15 = (closes[-1] - closes[-16]) / closes[-16] if len(closes) >= 16 else 0

    # EMA 趋势
    ema_fast = _ema(closes, 9)
    ema_slow = _ema(closes, 21)
    if ema_slow > 0:
        trend_strength = (ema_fast - ema_slow) / ema_slow
    else:
        trend_strength = 0
    if abs(trend_strength) < 0.0005:
        trend = "range"
    elif trend_strength > 0:
        trend = "up"
    else:
        trend = "down"

    # 布林位置
    _, _, _, bb_pos = _bollinger(closes, 20)

    # 波动率分位（ATR 占价格百分比，相对最近 50 根的位置）
    atr = _atr(highs, lows, closes, 14)
    atr_pct = atr / last_px if last_px > 0 else 0
    if len(closes) >= 50:
        # 简化：用最近 50 根的最大最小收盘价 / 价格 衡量波动
        recent = closes[-50:]
        vol_recent = (max(recent) - min(recent)) / recent[-1] if recent[-1] > 0 else 0
        vol_pct = vol_recent * 100  # 转成百分比数字
    else:
        vol_pct = atr_pct * 100 * 5  # 退而求其次

    # 量比
    if len(vols) >= 20 and vols[-1] >= 0:
        avg_vol = sum(vols[-20:]) / 20
        volume_ratio = vols[-1] / avg_vol if avg_vol > 0 else 1.0
    else:
        volume_ratio = 1.0

    macd_v, macd_sig, macd_hist = _macd(closes)

    out = {
        "last_px": round(last_px, 2),
        "return_1m": round(ret_1 * 100, 3),
        "return_5m": round(ret_5 * 100, 3),
        "return_15m": round(ret_15 * 100, 3),
        "rsi_14": round(_rsi(closes, 14), 2),
        "macd": round(macd_v, 2),
        "macd_signal": round(macd_sig, 2),
        "macd_hist": round(macd_hist, 2),
        "bb_pos": round(bb_pos, 3),
        "atr_14": round(atr, 2),
        "atr_pct": round(atr_pct * 100, 3),
        "vol_pct": round(vol_pct, 3),
        "trend": trend,
        "trend_strength": round(trend_strength * 100, 3),
        "ema_fast": round(ema_fast, 2),
        "ema_slow": round(ema_slow, 2),
        "volume_ratio": round(volume_ratio, 3),
    }
    # 确定性市场状态（含 ADX / 极端波动 / 价格偏离）——评分与守卫的权威输入
    out.update(classify_regime(candles))
    return out
