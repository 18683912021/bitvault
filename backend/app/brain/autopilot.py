"""Autopilot V2：常驻自动驾驶。决策链 = 因子 → Regime → 快评 → AI 分析 → 评分守卫 → 风险预算仓位 → paper 执行。

约束（红线不变）：
- 每次开仓必过 risk.check_pretrade(venue='paper')（红线 R4）
- autopilot 固定 venue=paper，严禁接入 OKX 实盘
- 熔断中只允许平仓；同时只持 1 个仓位
- 止损/分批止盈/trailing 由本地 tick 循环实时执行，不依赖 AI
- 每次分析 + 每笔成交写 signals + notifications 表（含交易质量字段）
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from app import db
from app.brain import signal_engine as se
from app.brain.decision_brain import DecisionBrain
from app.brain.factor_engine import _ema
from app.core import event_bus
from app.risk.risk_engine import RiskBlocked
from app.trading.roundtrips import compute_round_trips

log = logging.getLogger("bitvault.brain.autopilot")

INST_ID = "BTC-USDT"

# ---------- 费用模型（与 paper/OKX 现货 VIP0 一致） ----------
FEE_TAKER = 0.001
ROUNDCOST_PCT = FEE_TAKER * 2 + 0.0002   # 往返成本 ≈ 0.22%（双边 taker + 滑点余量）
MIN_TP_PCT = ROUNDCOST_PCT * 4           # 止盈距离下限 ≈ 0.9%
MIN_SL_PCT = 0.004                       # 止损距离下限 0.4%
MIN_NOTIONAL_USDT = 1.0
PX_DRIFT_LIMIT = 0.0015                  # 决策价与下单价偏差 >0.15% 时重算计划

# 分批止盈（R = |entry - sl|）
TP1_R, TP1_PCT = 1.0, 0.25   # 1R 平 25%，止损推保本
TP2_R, TP2_PCT = 2.0, 0.35   # 2R 平 35%，激活 ATR trailing
TRAIL_ATR = 2.5              # trailing 距离 = 2.5×ATR（不低于保本价）
TIME_STOP_BARS = 120         # 时间止损：120 根 bar 未到 TP1 且浮亏 → 离场


class Autopilot:
    def __init__(self, data, paper, brain: DecisionBrain, risk):
        self.data = data
        self.paper = paper
        self.brain = brain
        self.risk = risk
        self._task: asyncio.Task | None = None
        self._tick_task: asyncio.Task | None = None
        # 持仓跟踪（内存为准；重启后遗留 paper 持仓不自动管理，建议重置）
        self.position: dict | None = None
        self.last_action: str = "flat"
        # paper 权益峰值（回撤分档降仓用）
        self._paper_peak_equity: float = 0.0
        # 节流状态（内存）：防无休止做单
        self._last_close_ts: int = 0
        self._daily_key: str = ""
        self._daily_opens: int = 0
        self._loss_streak: int = 0
        self._sleep_until: int = 0
        self._bars_since_open: int = 0
        self._last_analyzed_bar_ts: int = 0  # 同一根 bar 只分析一次（防 LLM 期间重复事件）

    def config(self) -> dict:
        return db.get_setting("autopilot_config") or {
            "enabled": False, "period": "5m", "min_confidence": 0.6,
            "max_position_pct": 10, "max_order_usdt": 200,
            "cooldown_min": 15, "max_opens_per_day": 20,
            "loss_pause_n": 3, "loss_pause_min": 60,
        }

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._bar_loop())
            self._tick_task = asyncio.create_task(self._tick_loop())
            log.info("autopilot 任务已启动")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        if self._tick_task:
            self._tick_task.cancel()
            self._tick_task = None

    def enable(self, overrides: dict | None = None) -> dict:
        cfg = self.config()
        cfg["enabled"] = True
        if overrides:
            for k, v in overrides.items():
                if k in cfg:
                    cfg[k] = v
        db.set_setting("autopilot_config", cfg)
        log.info("autopilot 已启用 cfg=%s", cfg)
        return cfg

    def disable(self) -> dict:
        cfg = self.config()
        cfg["enabled"] = False
        db.set_setting("autopilot_config", cfg)
        log.info("autopilot 已停用（不平仓）")
        return cfg

    # ---------- 主循环：bar 收盘后决策 ----------
    async def _bar_loop(self) -> None:
        q = event_bus.subscribe(["bar"])
        while True:
            try:
                topic, data, _ = await q.get()
                cfg = self.config()
                if not cfg.get("enabled"):
                    continue
                period = cfg.get("period", "5m")
                if data.get("period") != period or data.get("inst_id") != INST_ID:
                    continue
                if data.get("confirm") == "0":
                    continue                     # 只在 K 线收盘确认后决策
                if data.get("ts") == self._last_analyzed_bar_ts and self.position:
                    continue                     # 持仓期间同一根 bar 不重复分析
                self._last_analyzed_bar_ts = data.get("ts", 0)
                if self.position:
                    self._bars_since_open += 1
                await self._decide_and_act()
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("autopilot bar 循环异常: %s", e)
                await asyncio.sleep(2)

    # ---------- 副循环：tick 实时止损/分批止盈/trailing ----------
    async def _tick_loop(self) -> None:
        q = event_bus.subscribe(["tick"])
        while True:
            try:
                topic, data, _ = await q.get()
                if not data.get("instId") or data["instId"] != INST_ID:
                    continue
                await self._manage_position(data.get("last", 0))
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("autopilot tick 循环异常: %s", e)
                await asyncio.sleep(1)

    # ================= 决策链 V2 =================
    async def _decide_and_act(self) -> None:
        cfg = self.config()
        candles = self._recent_candles(cfg.get("period", "5m"))
        if len(candles) < 30:
            return
        acct = self.paper.summary()
        equity = float(acct.get("equity", 0))
        if equity > self._paper_peak_equity:
            self._paper_peak_equity = equity
        dd_pct = ((self._paper_peak_equity - equity) / self._paper_peak_equity * 100
                  if self._paper_peak_equity > 0 else 0.0)

        from app.brain.factor_engine import compute_factors
        factors = compute_factors(candles)
        if factors.get("error"):
            return
        regime = factors.get("regime", "range")

        # ---- 快评分支：明确不值得交易的场景不调 LLM（省费用、省延迟） ----
        if not self.position:
            block = self._open_block_reason(int(time.time() * 1000))
            if block:
                log.info("开仓节流：%s（跳过 LLM）", block)
                return
            if regime == "extreme_vol":
                self._save_wait(factors, f"极端波动：{factors.get('regime_reason', '')}，禁止新开仓")
                return
            rsi, bb = float(factors.get("rsi_14", 50)), float(factors.get("bb_pos", 0.5))
            if regime == "range" and 28 <= rsi <= 72 and 0.08 <= bb <= 0.92:
                self._save_wait(factors, f"震荡市因子中性（RSI={rsi:.0f} bb={bb:.2f}），观望")
                return

        # ---- AI 分析（Analyst 定位） ----
        ai = await self.brain.analyze(INST_ID, candles, self.position, acct)
        self._save_decision(ai)

        # ---- 持仓中：AI 判断风险恶化 → 平仓（优先级低于本地止损） ----
        if self.position:
            if ai.get("action") == "close" and float(ai.get("confidence", 0) or 0) >= 0.6:
                await self._close_position(reason=f"AI 判断风险恶化：{ai.get('reason', '')[:60]}")
            return

        # ---- 无持仓：AI 有最终否决权 ----
        ai_dir = ai.get("direction", "NONE")
        if ai_dir not in ("LONG", "SHORT") or not ai.get("trade_allowed"):
            return   # wait / 中性 / 拒绝——等待是有效决策

        side = "long" if ai_dir == "LONG" else "short"
        # paper 现货只支持做多；short 评分仍计算（记录质量），但不执行
        if side == "short":
            self._save_wait(factors, f"AI 倾向做空但 paper 现货不支持裸做空，跳过（score 见下）")

        # ---- 确定性评分（两个方向都打，取 AI 方向） ----
        htf = self._htf_trend()
        score, breakdown = se.score_side(factors, ai, side, htf_trend=htf)
        plan = se.plan_trade(factors, side)
        halted = bool(self.risk.state.get("halted"))
        ok, checks = se.check_gate(factors, ai, side, score, plan, halted, dd_pct, htf)

        # 交易质量记录（用户第十九条：什么情况下赚钱）
        quality = {
            "regime": regime, "signal_score": score, "ai_confidence": ai.get("confidence", 0),
            "rr": plan["rr"], "atr_pct": factors.get("atr_pct"),
            "volume_ratio": factors.get("volume_ratio"), "htf_trend": htf,
            "checks": checks, "breakdown": breakdown,
            "ai_action": ai.get("action"), "ai_reason": (ai.get("reason") or "")[:200],
        }
        db.execute(
            "INSERT INTO signals (instance_id, ts, action, reason, payload_json) VALUES (?,?,?,?,?)",
            (0, int(time.time() * 1000),
             f"gate:{side}:{'pass' if ok else 'fail'}",
             f"score={score} rr={plan['rr']} regime={regime}",
             json.dumps(quality, ensure_ascii=False)),
        )
        if not ok:
            fails = "；".join(c["why"] for c in checks if not c["ok"])
            log.info("守卫拦截 %s：%s", side, fails)
            return

        # paper 现货只执行 long
        if side == "short":
            return
        await self._open_position(plan, quality, acct, dd_pct, cfg)

    def _save_wait(self, factors: dict, reason: str) -> None:
        """快评观望：只记日志不入 signals（避免表膨胀），发轻量事件。"""
        event_bus.publish("log", {"ts": int(time.time() * 1000), "level": "info",
                                  "msg": f"自动驾驶观望：{reason}"})
        log.info("快评观望：%s", reason)

    def _htf_trend(self) -> str | None:
        """1H 高周期趋势（多周期一致性因子）。"""
        rows = db.query(
            "SELECT c FROM bars WHERE inst_id=? AND period='1H' ORDER BY open_time ASC LIMIT 60",
            (INST_ID,))
        if len(rows) < 25:
            return None
        closes = [r["c"] for r in rows]
        f, s = _ema(closes, 9), _ema(closes, 21)
        if s <= 0:
            return None
        if f > s * 1.0005:
            return "up"
        if f < s * 0.9995:
            return "down"
        return "range"

    def _recent_candles(self, period: str) -> list[dict]:
        return db.query(
            "SELECT open_time as ts, o, h, l, c, vol FROM bars WHERE inst_id=? AND period=? "
            "ORDER BY open_time ASC LIMIT 120",
            (INST_ID, period),
        )

    # ---------- 节流：防无休止做单 ----------
    def _open_block_reason(self, now_ms: int) -> str | None:
        cfg = self.config()
        if self._sleep_until and now_ms < self._sleep_until:
            remain = (self._sleep_until - now_ms) / 60000
            return f"连亏休眠中（剩余 {remain:.0f} 分钟）"
        cooldown_ms = float(cfg.get("cooldown_min", 15)) * 60_000
        if cooldown_ms > 0 and self._last_close_ts and now_ms - self._last_close_ts < cooldown_ms:
            return f"平仓冷却中（{cfg.get('cooldown_min', 15):.0f} 分钟内不开新仓）"
        cap = int(cfg.get("max_opens_per_day", 20))
        day = time.strftime("%Y-%m-%d")
        if day != self._daily_key:
            self._daily_key = day
            self._daily_opens = 0
        if cap > 0 and self._daily_opens >= cap:
            return f"今日开仓已达上限 {cap} 次，明日再战"
        return None

    def _update_loss_streak(self) -> None:
        try:
            rt = compute_round_trips(venue="paper", inst_id=INST_ID, limit=1)
            rows = rt.get("closed") or []
            if not rows:
                return
            if float(rows[-1].get("pnl") or 0) < 0:
                self._loss_streak += 1
            else:
                self._loss_streak = 0
                return
            cfg = self.config()
            n = int(cfg.get("loss_pause_n", 3))
            if n > 0 and self._loss_streak >= n:
                mins = float(cfg.get("loss_pause_min", 60))
                self._sleep_until = int(time.time() * 1000) + mins * 60_000
                self._loss_streak = 0
                self._notify(
                    "autopilot_sleep", "warning",
                    f"连续亏损 {n} 笔，自动驾驶强制休眠 {mins:.0f} 分钟（防止持续磨损资费）",
                    {"sleep_until": self._sleep_until},
                )
        except Exception as e:
            log.warning("连亏统计失败: %s", e)

    # ---------- 开仓（风险预算 + 决策价偏差守卫） ----------
    async def _open_position(self, plan: dict, quality: dict, acct: dict,
                             dd_pct: float, cfg: dict) -> None:
        # 红线：autopilot 只允许本地模拟（paper），严禁接入 OKX 实盘。
        last_px = self.data.last_price(INST_ID)
        if last_px <= 0:
            return
        # 决策价偏差守卫：LLM 延迟期间价格移动明显时，以最新价重算计划
        if abs(last_px - plan["entry"]) / plan["entry"] > PX_DRIFT_LIMIT:
            factors = dict(quality and {})
            f2 = self._factors_snapshot()
            if f2:
                plan = se.plan_trade(f2, "long")
                if plan["rr"] < se.RR_MIN:
                    log.info("价格漂移 %.2f%% 后 RR=%.2f 不达标，放弃",
                             abs(last_px - plan["entry"]) / plan["entry"] * 100, plan["rr"])
                    return
            plan["entry"] = last_px

        equity = float(acct.get("equity", 0))
        avail = float(acct.get("usdt", 0))
        notional, size_note = se.position_size(
            equity, avail, plan, dd_pct, float(cfg.get("max_order_usdt", 200)), FEE_TAKER)
        if notional < MIN_NOTIONAL_USDT:
            self._notify("autopilot_skip", "warning",
                         f"仓位计算不足最小下单额：{size_note}", {"plan": plan})
            return
        sz_base = notional / last_px

        try:
            row = await self.paper.place_intent({
                "inst_id": INST_ID, "side": "buy", "ord_type": "market",
                "sz_base": sz_base, "reduce_only": False, "source": "autopilot:open",
            })
            # 实际成交价（paper 撮合含滑点）
            fill_px = last_px
            if row.get("cl_ord_id"):
                tr = db.query("SELECT px FROM trades WHERE cl_ord_id=? ORDER BY id DESC LIMIT 1",
                              (row["cl_ord_id"],))
                if tr:
                    fill_px = float(tr[0]["px"])
            entry = fill_px
            risk_dist = entry - plan["sl"]          # long
            if risk_dist <= 0:
                return
            self.position = {
                "side": "buy", "sz": float(row.get("sz") or sz_base),
                "orig_sz": float(row.get("sz") or sz_base),
                "entry_px": entry, "sl_px": plan["sl"],
                "risk_dist": risk_dist,
                "tp1_px": entry + TP1_R * risk_dist,
                "tp2_px": entry + TP2_R * risk_dist,
                "tp3_px": entry + se.RR_MIN * 1.5 * risk_dist,   # 3R 全平目标
                "tp1_done": False, "tp2_done": False,
                "high_water": entry, "atr": float(quality.get("atr_pct", 0)) or 0,
                "cl_ord_id": row.get("cl_ord_id"),
                "regime": quality.get("regime"), "signal_score": quality.get("signal_score"),
                "ai_confidence": quality.get("ai_confidence"),
            }
            self._bars_since_open = 0
            self.last_action = "long"
            day = time.strftime("%Y-%m-%d")
            if day != self._daily_key:
                self._daily_key = day
                self._daily_opens = 0
            self._daily_opens += 1
            self._notify(
                "autopilot_open", "info",
                f"开多 {sz_base:.6f} @ {entry:.2f} sl={plan['sl']:.2f} "
                f"tp1={self.position['tp1_px']:.2f} tp2={self.position['tp2_px']:.2f} "
                f"score={quality.get('signal_score')} rr={plan['rr']}",
                {"plan": plan, "quality": quality, "size_note": size_note, "order": row},
            )
        except RiskBlocked as e:
            self._notify("autopilot_blocked", "warning",
                         f"开仓被风控拦截：{e}", {"plan": plan})
        except Exception as e:
            log.error("autopilot 开仓失败: %s", e)

    def _factors_snapshot(self) -> dict | None:
        candles = self._recent_candles(self.config().get("period", "5m"))
        if len(candles) < 30:
            return None
        from app.brain.factor_engine import compute_factors
        f = compute_factors(candles)
        return None if f.get("error") else f

    # ---------- 平仓 ----------
    async def _close_position(self, reason: str = "", sz: float | None = None) -> None:
        if not self.position:
            return
        pos = self.position
        sell_sz = min(sz if sz is not None else pos["sz"], pos["sz"])
        if sell_sz <= 1e-12:
            return
        try:
            await self.paper.place_intent({
                "inst_id": INST_ID, "side": "sell", "ord_type": "market",
                "sz_base": sell_sz, "reduce_only": False, "source": "autopilot:close",
            })
            pos["sz"] -= sell_sz
            fully = pos["sz"] <= 1e-12
            if fully:
                self._last_close_ts = int(time.time() * 1000)
                self._update_loss_streak()
                self.position = None
                self.last_action = "flat"
            self._notify("autopilot_close", "info",
                         f"平仓 {sell_sz:.6f}（{'全部' if fully else '部分'}，{reason}）",
                         {"reason": reason, "sz": sell_sz, "fully": fully})
            log.info("autopilot 平仓 sz=%.6f reason=%s fully=%s", sell_sz, reason, fully)
        except Exception as e:
            log.error("autopilot 平仓失败: %s", e)

    # ---------- 持仓管理（tick 级，优先级从高到低，全本地不依赖 AI） ----------
    async def _manage_position(self, px: float) -> None:
        if not self.position or not px:
            return
        pos = self.position
        if px > pos["high_water"]:
            pos["high_water"] = px

        # 1. 强制止损（含保本后的止损推升）
        if px <= pos["sl_px"]:
            reason = "止损触发" if pos["sl_px"] < pos["entry_px"] else "保本止损触发"
            await self._close_position(reason=f"{reason} @ {px:.2f} ≤ {pos['sl_px']:.2f}")
            return
        # 2. TP1：1R 平 25% + 止损推保本
        if not pos["tp1_done"] and px >= pos["tp1_px"]:
            pos["tp1_done"] = True
            pos["sl_px"] = max(pos["sl_px"], pos["entry_px"])
            await self._close_position(reason=f"TP1 到达 1R 平 25% @ {px:.2f}",
                                       sz=pos["orig_sz"] * TP1_PCT)
            return
        # 3. TP2：2R 平 35% + 激活 trailing
        if not pos["tp2_done"] and px >= pos["tp2_px"]:
            pos["tp2_done"] = True
            await self._close_position(reason=f"TP2 到达 2R 平 35% @ {px:.2f}",
                                       sz=pos["orig_sz"] * TP2_PCT)
            return
        # 4. trailing（TP2 后）：high_water - 2.5×ATR，不低于保本
        if pos["tp2_done"]:
            atr_abs = pos["atr"] / 100 * pos["entry_px"] if pos["atr"] else pos["risk_dist"] / 1.8
            trail = max(pos["entry_px"], pos["high_water"] - TRAIL_ATR * atr_abs)
            pos["sl_px"] = max(pos["sl_px"], trail)   # 只上移
            if px <= trail:
                await self._close_position(reason=f"ATR trailing @ {px:.2f} ≤ {trail:.2f}")
                return
        # 5. 3R 全平（让利润奔跑的上限保护）
        if pos["tp1_done"] and px >= pos["tp3_px"]:
            await self._close_position(reason=f"TP3 到达 3R 清仓 @ {px:.2f}")
            return
        # 6. 时间止损：持仓 120 根 bar 仍未到 TP1 且浮亏 → 离场（资金效率）
        if (not pos["tp1_done"] and self._bars_since_open >= TIME_STOP_BARS
                and px < pos["entry_px"] * 0.999):
            await self._close_position(reason=f"时间止损：{self._bars_since_open} 根 bar 未达 TP1 且浮亏")

    # ---------- 持久化 / 通知 ----------
    def _save_decision(self, ai: dict) -> None:
        ts = ai.get("ts") or int(time.time() * 1000)
        action = ai.get("action", "wait")
        reason = (ai.get("reason") or "")[:300]
        db.execute(
            "INSERT INTO signals (instance_id, ts, action, reason, payload_json) VALUES (?,?,?,?,?)",
            (0, ts, action, reason, json.dumps(ai, ensure_ascii=False)),  # instance_id=0
        )
        self._notify(
            "autopilot_decision", "info",
            f"AI 分析：{ai.get('direction', 'NONE')}（置信度 {ai.get('confidence', 0):.0%}，"
            f"regime={ai.get('factors', {}).get('regime', '?')}）",
            {"reason": reason, "scores": {k: ai.get(k) for k in
             ("trend_score", "momentum_score", "volatility_score", "liquidity_score", "risk_score")}},
        )

    def _notify(self, rule_type: str, level: str, body: str, detail: dict) -> None:
        ts = int(time.time() * 1000)
        db.execute(
            "INSERT INTO notifications (ts, level, title, body) VALUES (?,?,?,?)",
            (ts, level, f"自动驾驶：{rule_type}", json.dumps(detail, ensure_ascii=False)),
        )
        event_bus.publish("log", {"ts": ts, "level": level, "msg": body})

    # ---------- 状态查询 ----------
    def reset_throttle(self) -> None:
        self._last_close_ts = 0
        self._daily_key = ""
        self._daily_opens = 0
        self._loss_streak = 0
        self._sleep_until = 0
        self._paper_peak_equity = 0.0
        self._bars_since_open = 0
        self.position = None

    def throttle_status(self) -> dict:
        now_ms = int(time.time() * 1000)
        cfg = self.config()
        cd_ms = float(cfg.get("cooldown_min", 15)) * 60_000
        cooldown_until = (self._last_close_ts + cd_ms) if self._last_close_ts else 0
        return {
            "sleeping": now_ms < self._sleep_until,
            "sleep_until": self._sleep_until,
            "cooldown_until": cooldown_until if now_ms < cooldown_until else 0,
            "opens_today": self._daily_opens if self._daily_key == time.strftime("%Y-%m-%d") else 0,
            "max_opens_per_day": int(cfg.get("max_opens_per_day", 20)),
            "loss_streak": self._loss_streak,
            "cooldown_min": cfg.get("cooldown_min", 15),
            "loss_pause_n": cfg.get("loss_pause_n", 3),
            "loss_pause_min": cfg.get("loss_pause_min", 60),
        }

    def status(self) -> dict:
        cfg = self.config()
        return {
            "enabled": bool(cfg.get("enabled")),
            "running": self.is_running(),
            "period": cfg.get("period", "5m"),
            "venue": "paper",  # autopilot 固定本地模拟，不支持实盘
            "last_action": self.last_action,
            "last_decision": self.brain.last_decision,
            "last_error": self.brain.last_error,
            "position": self.position,
            "throttle": self.throttle_status(),
        }

    def history(self, limit: int = 20) -> list[dict]:
        rows = db.query(
            "SELECT ts, action, reason, payload_json FROM signals WHERE instance_id=0 "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        for r in rows:
            try:
                r["payload"] = json.loads(r.pop("payload_json") or "{}")
            except Exception:
                r["payload"] = {}
        return rows
