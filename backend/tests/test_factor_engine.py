# 指标/regime 回归（P2-7 golden + 七态阈值）。
from app.brain.factor_engine import classify_regime, trend_of, _atr_pct_series


def _candles(closes, high=None, low=None, vol=None):
    n = len(closes)
    out = []
    for i, c in enumerate(closes):
        h = high[i] if high else c * 1.005
        l = low[i] if low else c * 0.995
        out.append({"ts": i * 60_000, "o": c, "h": h, "l": l, "c": c,
                    "vol": vol[i] if vol else 1.0})
    return out


def test_regime_unknown_insufficient():
    r = classify_regime(_candles([100.0] * 29))
    assert r["regime"] == "unknown"


def test_regime_unknown_nan():
    closes = [100.0] * 29 + [float("nan")]
    assert classify_regime(_candles(closes))["regime"] == "unknown"


def test_regime_trend_up():
    closes = [100 + i * 0.5 for i in range(40)]
    assert classify_regime(_candles(closes))["regime"] == "trend_up"


def test_regime_trend_down():
    closes = [120 - i * 0.5 for i in range(40)]
    r = classify_regime(_candles(closes))
    assert r["regime"] == "trend_down"


def test_regime_extreme_spike():
    # 前段零波动 → ATR 基线≈0，最后单根巨震 → atr_spike → extreme
    closes = [100.0] * 34 + [105.0]
    highs = [100.0] * 34 + [110.0]
    lows = [100.0] * 34 + [90.0]
    r = classify_regime(_candles(closes, highs, lows))
    assert r["regime"] == "extreme"


def test_trend_of_dead_zone():
    # 平数据 → range；上升 → up；不足 23 → unknown
    assert trend_of(_candles([100.0] * 30)) == "range"
    assert trend_of(_candles([100 + i for i in range(30)])) == "up"
    assert trend_of(_candles([100.0] * 22)) == "unknown"


def test_atr_pct_series_golden_o_n():
    """P2-7：优化后的滑动窗口与原始 O(n²) 实现数学结果完全一致。"""
    highs = [100 + (i % 7) * 0.3 for i in range(60)]
    lows = [99.5 + (i % 5) * 0.2 for i in range(60)]
    closes = [100 + i * 0.1 for i in range(60)]

    def legacy(hs, ls, cs, n=14):
        trs = []
        for i in range(1, len(cs)):
            trs.append(max(hs[i] - ls[i], abs(hs[i] - cs[i - 1]), abs(ls[i] - cs[i - 1])))
        out = []
        for i in range(n, len(trs) + 1):
            window = trs[i - n:i]
            px = cs[i]
            out.append((sum(window) / n) / px if px > 0 else 0.0)
        return out

    assert _atr_pct_series(highs, lows, closes) == legacy(highs, lows, closes)
