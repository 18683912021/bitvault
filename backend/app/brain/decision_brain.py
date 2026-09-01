"""决策大脑 V2：DeepSeek 定位为 AI Market Analyst（分析师），不是 Trader。

AI 输出结构化分析（regime / 方向倾向 / 各维度子评分 / 置信度 / 是否允许交易），
最终是否下单由确定性 signal_engine 评分 + 守卫链决定——AI 拥有一票否决权，没有批准权。

输出 schema（AI Analyst）:
{
  "regime": "bull_trend" | "bear_trend" | "range" | "extreme_vol",
  "direction": "LONG" | "SHORT" | "NONE",
  "action": "long" | "short" | "wait" | "close",
  "confidence": 0-100,
  "trend_score": 0-100, "momentum_score": 0-100, "volatility_score": 0-100,
  "liquidity_score": 0-100, "risk_score": 0-100（越高越危险）,
  "trade_allowed": true|false,
  "reason": "中文分析"
}

错误降级：LLM 失败 → {direction NONE, confidence 0, trade_allowed false}（守卫链会拒绝）。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.brain.factor_engine import compute_factors
from app.brain.llm_client import LLMClient

log = logging.getLogger("bitvault.brain")

SYSTEM_PROMPT = """你是一名 BTC/USDT 交易分析师（Analyst），不是交易员。你的分析只是交易引擎
评分体系的一部分（占 20%），最终是否下单由确定性风控与评分守卫决定。你的职责是客观评估行情。

**输出 JSON 字段（严格按此结构）：**
{
  "regime": "bull_trend" | "bear_trend" | "range" | "extreme_vol",
  "direction": "LONG" | "SHORT" | "NONE",
  "action": "long" | "short" | "wait" | "close",
  "confidence": 0-100 整数（你对方向判断的置信度）,
  "trend_score": 0-100（趋势质量）,
  "momentum_score": 0-100（动量质量）,
  "volatility_score": 0-100（波动是否处于适合交易的区间）,
  "liquidity_score": 0-100（量能是否健康配合）,
  "risk_score": 0-100（当前环境风险，越高越危险）,
  "trade_allowed": true|false（以你的分析是否支持此刻开新仓）,
  "reason": "中文 3 句以内说明"
}

**分析原则：**
1. WAIT/wait 是完全正常且优先的输出——震荡、因子中性、多空矛盾、风险评分高时必须 wait。
   宁可少交易，绝不为交易而交易。
2. 输入已含程序判定的 regime（含 ADX/极端波动检测）。若你不同意，说明理由并按你的判断输出，
   但 trade_allowed 需相应调整。
3. confidence < 60 时 trade_allowed 必须为 false（<60 禁止交易，60-70 仅观察级信号）。
4. 极端波动（程序已标注 atr_spike/单根异常/放量暴涨暴跌）时：regime=extreme_vol、
   trade_allowed=false、risk_score 给高值。
5. 费用现实：现货 taker 0.10%/边，往返约 0.22%。波动撑不起 0.9% 止盈距离的行情一律 wait。
6. 只有趋势明确（ADX 高 + EMA 排列 + 量能配合）或极端反转（超买超卖+轨道外）才给
   direction=LONG/SHORT 且 confidence >= 70。

只返回 JSON，不要任何其他文字。"""


class DecisionBrain:
    def __init__(self, llm: LLMClient):
        self.llm = llm
        self.last_decision: dict[str, Any] = {}
        self.last_error: str = ""

    async def analyze(
        self,
        inst_id: str,
        candles: list[dict],
        position: dict | None,
        account: dict,
    ) -> dict[str, Any]:
        """产出 AI 分析（非决策）。失败时返回 trade_allowed=false 的中性结果。"""
        factors = compute_factors(candles)
        if factors.get("error"):
            return {"direction": "NONE", "action": "wait", "confidence": 0,
                    "trade_allowed": False, "reason": factors["error"], "factors": factors}

        pos_text = "无持仓" if not position else (
            f"持仓 {position.get('sz', 0)} {inst_id}，入场 {position.get('entry_px', 0)}，"
            f"止损 {position.get('sl_px', 0)}，已部分止盈 tp1={position.get('tp1_done', False)}"
        )
        user_prompt = f"""当前标的：{inst_id}
最近价格：{factors['last_px']}
账户权益：{account.get('equity', 0)} USDT（可用 {account.get('usdt', 0)}）

程序判定市场状态：{factors.get('regime')}（{factors.get('regime_reason', '')}）

技术因子：
- ADX(14)={factors['adx_14']} ATR(14)={factors['atr_14']}（占价 {factors['atr_pct']}%，突增 {factors['atr_spike']} 倍）
- 收益率 1m={factors['return_1m']}% 5m={factors['return_5m']}% 15m={factors['return_15m']}%
- RSI(14)={factors['rsi_14']}
- MACD={factors['macd']} signal={factors['macd_signal']} hist={factors['macd_hist']}
- 布林位置={factors['bb_pos']} (0=下轨 1=上轨)
- EMA9={factors['ema_fast']} EMA21={factors['ema_slow']}（价格偏离 EMA21 {factors['ema21_dist_pct']}%）
- 量比={factors['volume_ratio']}

当前持仓：{pos_text}

请输出分析 JSON。"""

        try:
            t0 = time.time()
            result = await self.llm.chat_json(SYSTEM_PROMPT, user_prompt, temperature=0.3)
            latency = time.time() - t0
            # ---- 解析 + 归一化 ----
            direction = str(result.get("direction", "NONE")).upper()
            if direction not in ("LONG", "SHORT", "NONE"):
                direction = "NONE"
            action = str(result.get("action", "wait")).lower()
            if action not in ("long", "short", "wait", "close"):
                action = "wait"
            conf = float(result.get("confidence", 0) or 0)
            if conf > 1:            # 兼容 0-100
                conf = conf / 100
            result["direction"] = direction
            result["action"] = action
            result["confidence"] = max(0, min(1, conf))
            result["trade_allowed"] = bool(result.get("trade_allowed", False)) and conf >= 0.6
            for k in ("trend_score", "momentum_score", "volatility_score",
                      "liquidity_score", "risk_score"):
                try:
                    result[k] = max(0, min(100, float(result.get(k, 50) or 50)))
                except (TypeError, ValueError):
                    result[k] = 50
            result["latency_ms"] = int(latency * 1000)
            result["inst_id"] = inst_id
            result["factors"] = factors
            result["ts"] = int(time.time() * 1000)
            self.last_decision = result
            self.last_error = ""
            log.info("AI 分析完成 %s dir=%s conf=%.2f allowed=%s lat=%dms",
                     inst_id, direction, result["confidence"],
                     result["trade_allowed"], result["latency_ms"])
            return result
        except Exception as e:
            self.last_error = str(e)[:200]
            log.warning("LLM 分析失败 %s: %s", inst_id, self.last_error)
            return {
                "direction": "NONE", "action": "wait", "confidence": 0,
                "trade_allowed": False, "reason": f"LLM 调用失败：{self.last_error}",
                "inst_id": inst_id, "factors": factors, "ts": int(time.time() * 1000),
            }
