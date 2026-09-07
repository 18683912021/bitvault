"""统一交易意图（TradeIntent）与合约规格口径（P2-4 一致性）。

- TradeIntent：所有下单入口（Autopilot / StrategyManager / PaperEngine / OMS）共用同一份
  字段契约；place_intent 首行校验，防止字段缺失/类型错误造成语义漂移。
- contract_value：现货=1、合约=ctVal，全系统名义金额/保证金/PnL 口径统一来源。
"""
from __future__ import annotations

# 统一意图字段（sz_base 为交易量：现货=币数量，合约=张数，下单前经 normalize_sz）
REQUIRED_INTENT_FIELDS = ("inst_id", "side", "sz_base")
OPTIONAL_INTENT_FIELDS = (
    "ord_type", "px", "reduce_only", "leverage", "td_mode", "venue",
    "source", "instance_id", "sl_trigger_px", "sl", "tp",
)


def validate_intent(intent: dict) -> None:
    """校验并规范化意图；非法输入直接抛 ValueError（fail-closed）。"""
    if not isinstance(intent, dict):
        raise ValueError("TradeIntent 必须是 dict")
    missing = [k for k in REQUIRED_INTENT_FIELDS if not intent.get(k)]
    if missing:
        raise ValueError(f"TradeIntent 缺少必填字段: {missing}")
    if intent["side"] not in ("buy", "sell"):
        raise ValueError(f"side 非法: {intent['side']}")
    try:
        float(intent["sz_base"])
    except (TypeError, ValueError):
        raise ValueError(f"sz_base 非法: {intent['sz_base']}") from None
    if intent.get("ord_type") not in (None, "market", "limit", "post_only"):
        raise ValueError(f"ord_type 非法: {intent.get('ord_type')}")


def contract_value(inst_spec: dict) -> float:
    """现货=1；合约=ctVal（0/缺失视为规格异常，raise）。"""
    if not inst_spec:
        raise ValueError("InstrumentSpec 缺失")
    if inst_spec.get("instType") != "SWAP":
        return 1.0
    ct = float(inst_spec.get("ctVal") or 0)
    if ct <= 0:
        raise ValueError(f"{inst_spec.get('instId')} 合约规格缺失 ctVal")
    return ct
