# P2-6 回归：结构化决策报告层不变式（八层 + 大周期/方向语义 + 设置行三态）。
from app.brain.autopilot import _format_dec_report, _setup_line_cn, _htf_bias


def _rep(**over):
    r = {
        "symbol": "BTC-USDT", "period": "5m",
        "htf": {"1D": "多头", "4H": "多头", "bias": "多头"},
        "regime": "震荡区间", "regime_reason": "ADX=16 均值区间",
        "direction": "空头回调（大周期多头背景下的回调）",
        "structure": "多头大周期背景下，当前为空头回调；空头方向出现普通回踩（未突破关键结构）",
        "long_setup": _setup_line_cn(True, {"setup": "none", "detail": {"kind": "none"}}),
        "short_setup": _setup_line_cn(True, {"setup": "pullback",
                                            "detail": {"kind": "pullback", "touched": True}}),
        "rr": "N/A（未产生入场计划）",
        "final": "NO TRADE / 观望",
        "reason": "多头背景未变；空头回调；以上条件未齐，继续观望",
        "next": "多头：等待现有多头入场形态；空头：等待有效突破 + 回踩 + 确认",
    }
    r.update(over)
    return r


def test_report_eight_layers():
    text = _format_dec_report(_rep())
    lines = [l for l in text.splitlines() if l.strip()]
    tags = ["【大周期趋势 HTF】", "【当前市场状态】", "【当前周期方向】",
            "【市场结构】", "【多头 Setup】", "【空头 Setup】",
            "【风险收益】", "【最终决策】", "【原因】", "【下一步等待】"]
    assert lines[0].startswith("【自动驾驶决策】")
    for t in tags:
        assert any(l.startswith(t) for l in lines), f"缺层 {t}"


def test_report_never_calls_market_bearish_when_htf_bull():
    """核心语义不变式：大周期多头 + 短周期空头 → 只表述为"回调"，绝不输出"市场空头趋势"。"""
    text = _format_dec_report(_rep())
    assert "市场空头趋势" not in text
    assert "空头回调" in text


def test_htf_bias_rules():
    assert _htf_bias("up", "up") == "多头"
    assert _htf_bias("down", "down") == "空头"
    assert _htf_bias("up", "down") == "混合"     # 不强行归一向
    assert _htf_bias(None, "up") == "未确认"


def test_setup_line_three_states():
    assert "未出现" in _setup_line_cn(True, {"setup": "none", "detail": {"kind": "none"}})
    assert "普通回踩" in _setup_line_cn(True, {"setup": "pullback",
                                              "detail": {"kind": "pullback", "touched": True}})
    assert "突破尝试" in _setup_line_cn(True, {"setup": "none",
                                              "detail": {"kind": "breakout_attempt",
                                                         "broke": True, "retest": False}})
    assert "突破回踩(完整)" in _setup_line_cn(True, {"setup": "breakout_retest",
                                                    "detail": {"kind": "breakout_retest"}})
