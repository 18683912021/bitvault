"""Autopilot V3：纯规则自动驾驶（无 AI/LLM），跟随系统级模式（venue）执行。

决策链 = 因子 → Regime(六态) → HTF(4H/1H) 确认 → Entry Setup → No-Trade Filter(十项)
        → Entry Score(≥70) → 风险预算仓位 → 执行器派发(paper|okx) → 本地持仓管理。

系统级模式（venue）：autopilot 跟随顶栏切换的模式执行——
- venue=paper：本地模拟盘虚拟资金（PaperEngine）
- venue=okx：OKX 实盘真实资金（OMS，需已连接 API Key）
"踩油门就走、踩刹车就停"：单一 autopilot 开关，无需策略实例配置。

约束（红线不变）：
- 每次开仓必过 risk.check_pretrade（红线 R4，paper/OMS 各自内置）
- 实盘模式需已连接 Key；未连 Key 自动落回 paper（不报错、不阻断）
- 熔断中只允许平仓；同时只持 1 个仓位
- 止损/分批止盈/trailing/signal-exit/time-exit 全部本地执行，确定性规则
- 每次开仓决策写 signals 表（含交易质量字段，供 MFE/MAE 与亏损归因分析）
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from app import db
from app.brain import signal_engine as se
from app.brain.factor_engine import compute_factors, trend_of
from app.core import event_bus
from app.risk.risk_engine import RiskBlocked
from app.trading.roundtrips import compute_round_trips

log = logging.getLogger("bitvault.brain.autopilot")

INST_ID = "BTC-USDT"

MIN_NOTIONAL_USDT = 1.0
PX_DRIFT_LIMIT = 0.0015          # 决策价与下单价偏差 >0.15% 时重算计划

# 分批止盈（R = |entry - sl|）
TP1_R, TP1_PCT = 1.0, 0.25   # 1R 平 25%，止损推保本
TP2_R, TP2_PCT = 2.0, 0.35   # 2R 平 35%，激活 ATR trailing
TRAIL_ATR = 2.5              # trailing 距离 = 2.5×ATR（不低于保本价）
TIME_STOP_BARS = 120         # 时间止损：120 根 bar 未到 TP1 且浮亏 → 离场


class Autopilot:
    def __init__(self, data, paper, brain=None, risk=None):
        self.data = data
        self.paper = paper
        self.brain = brain          # 保留引用（手动分析端点用），决策链不再依赖
        self.risk = risk
        self.svc = None            # Services 容器引用（bind_svc 注入）：用于读取 venue/oms/account
        self._task: asyncio.Task | None = None
        self._tick_task: asyncio.Task | None = None
        self._deciding = False      # 重入守卫：决策期间丢弃新事件（修复同 bar 双开）
        self.position: dict | None = None
        self.last_action: str = "flat"
        self.last_factors: dict = {}
        self._paper_peak_equity: float = 0.0
        self._last_close_ts: int = 0
        self._daily_key: str = ""
        self._daily_opens: int = 0
        self._loss_streak: int = 0
        self._sleep_until: int = 0
        self._bars_since_open: int = 0
        self._daily_realized_pnl: float = 0.0   # 当日已实现净盈亏（USDT）
        self._dd_pct: float = 0.0              # 缓存当前回撤供风控降档用

    def bind_svc(self, svc) -> None:
        """注入 Services 容器：autopilot 据此读取系统级 venue 与实盘 oms/account。"""
        self.svc = svc

    # ---------- 系统级模式（venue）派发 ----------
    @property
    def venue(self) -> str:
        """当前系统级模式：'paper' 模拟虚拟资金 / 'okx' 实盘真实资金。"""
        return getattr(self.svc, "venue", "paper") if self.svc else "paper"

    def _live_ready(self) -> bool:
        """实盘模式是否就绪（已连 Key + OMS 已建）。未就绪时调用方应落回 paper 或观望。"""
        return bool(self.svc and self.svc.has_key() and getattr(self.svc, "oms", None))

    def _executor(self):
        """返回当前模式下的下单引擎：okx 模式且 Key 就绪 → OMS（真实下单）；否则 PaperEngine。
        接口对齐（place_intent 同签名），autopilot 无需感知差异。"""
        if self.venue == "okx" and self._live_ready():
            return self.svc.oms
        return self.paper

    def _account_summary(self) -> dict:
        """返回当前模式下的账户摘要（统一口径 {equity, usdt(可用), initial, positions}）。
        okx：totalEq 为权益，可用取现货 USDT 余额；paper：原样返回。"""
        if self.venue == "okx" and self._live_ready():
            acct = self.svc.account.summary or {}
            equity = float(acct.get("totalEq") or 0)
            avail = 0.0
            for d in acct.get("details") or []:
                if (d.get("ccy") or "").upper() == "USDT":
                    avail += float(d.get("availBal") or d.get("cashBal") or 0)
            if avail <= 0:
                avail = equity
            # OKX 持仓
            positions = []
            for p in (self.svc.account.positions or []):
                inst_id = p.get("instId", "")
                if inst_id != "BTC-USDT":
                    continue
                pos = float(p.get("pos") or 0)
                if pos <= 0:
                    continue
                last = self.data.last_price(inst_id)
                positions.append({
                    "inst_id": inst_id, "sz": pos, "last": last,
                    "entry_px": float(p.get("avgPx") or 0), "leverage": 1,
                    "notional": round(pos * last, 2),
                    "upl": float(p.get("upl") or 0),
                })
            return {"equity": equity, "usdt": avail, "initial": equity, "positions": positions}
        return self.paper.summary()

    def _discover_position(self, acct: dict) -> None:
        """发现并收养当前 venue 已有但 autopilot 未追踪的 BTC-USDT 持仓。

        场景：venue 切换后 self.position 被清空，但实际持仓仍在。
        收养时用当前因子重算 SL/TP（不知道原始计划，保守处理）。
        支持多空双向：paper lev_positions 有 side 字段；OKX SWAP pos 正=多 负=空。
        """
        if self.position:
            return
        positions = acct.get("positions") or []
        for p in positions:
            if p.get("inst_id") != "BTC-USDT":
                continue
            sz = float(p.get("sz") or 0)
            if sz <= 0:
                continue
            pos_side = p.get("side", "long")   # paper: "long"/"short"; OKX: 默认 long
            is_short = pos_side == "short"
            entry_px = float(p.get("entry_px") or 0) or self.data.last_price("BTC-USDT")
            last_px = self.data.last_price("BTC-USDT") or entry_px
            cfg = self.config()
            lev = int(cfg.get("leverage", 1) or 1)
            candles = self._recent_candles(cfg.get("period", "1H"))
            if len(candles) < 60:
                return
            factors = compute_factors(candles)
            if factors.get("error"):
                return
            side_str = "short" if is_short else "long"
            plan = se.plan_trade(candles, factors, side_str)
            risk_dist = plan["risk_dist"] or entry_px * 0.008
            sign = -1 if is_short else 1
            self.position = {
                "cl_ord_id": "discovered",
                "side": side_str,
                "entry_px": entry_px,
                "sl_px": plan["sl"],
                "tp1_px": round(entry_px + sign * risk_dist * 1.0, 2),
                "tp2_px": round(entry_px + sign * risk_dist * 2.0, 2),
                "tp3_px": round(entry_px + sign * risk_dist * 3.0, 2),
                "sz": sz, "orig_sz": sz,
                "high_water": max(entry_px, last_px),
                "low_water": min(entry_px, last_px),
                "mfe_px": max(entry_px, last_px) if not is_short else min(entry_px, last_px),
                "mae_px": min(entry_px, last_px) if not is_short else max(entry_px, last_px),
                "tp1_done": False, "tp2_done": False,
                "atr_pct": float(factors.get("atr_pct", 0)) or 1.0,
                "risk_dist": risk_dist,
                "open_ts": int(time.time() * 1000),
                "leverage": lev,
                "margin": float(p.get("margin") or 0) or (sz * entry_px / lev if lev > 1 else sz * entry_px),
                "liq_px": p.get("liq_px"),
                "setup": "discovered",
            }
            dir_tag = "空" if is_short else "多"
            log.info("发现已有 %s 持仓 %s entry=%.2f sz=%.6f lev=%dx → 收养 SL=%.2f TP1=%.2f",
                     self.venue, dir_tag, entry_px, sz, lev, plan["sl"],
                     entry_px + sign * risk_dist)
            return

    def config(self) -> dict:
        """按 venue 独立存储配置：autopilot_config_paper / autopilot_config_okx。"""
        key = f"autopilot_config_{self.venue}"
        saved = db.get_setting(key)
        if saved is None and self.venue == "paper":
            old = db.get_setting("autopilot_config")
            if old:
                saved = old
        cfg = {"enabled": False, "period": "1H", "max_order_usdt": 200,
               "cooldown_min": 15, "max_opens_per_day": 20,
               "loss_pause_n": 3, "loss_pause_min": 60,
               "daily_loss_limit_usdt": 100,
               "require_setup": True, "setup_filter": "breakout_retest",
               "mode": "normal", "leverage": 2}
        cfg.update(saved or {})
        return cfg

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._bar_loop())
            self._tick_task = asyncio.create_task(self._tick_loop())
            log.info("autopilot(纯规则) 任务已启动")

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
        db.set_setting(f"autopilot_config_{self.venue}", cfg)
        return cfg

    def disable(self) -> dict:
        cfg = self.config()
        cfg["enabled"] = False
        db.set_setting(f"autopilot_config_{self.venue}", cfg)
        return cfg

    # ---------- 主循环：bar 收盘后决策（确定性，无 LLM 延迟） ----------
    async def _bar_loop(self) -> None:
        q = event_bus.subscribe(["bar"])
        while True:
            try:
                topic, data, _ = await q.get()
                cfg = self.config()
                if not cfg.get("enabled") or self._deciding:
                    continue
                period = cfg.get("period", "5m")
                if data.get("period") != period or data.get("inst_id") != INST_ID:
                    continue
                if data.get("confirm") == "0":
                    continue                     # 只在 K 线收盘确认后决策
                self._deciding = True
                try:
                    if self.position:
                        self._bars_since_open += 1
                        await self._signal_exit_check(data.get("ts", 0))
                    await self._decide_and_act()
                finally:
                    self._deciding = False
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

    # ================= 决策链 V3（纯规则） =================
    async def _decide_and_act(self) -> None:
        cfg = self.config()
        candles = self._recent_candles(cfg.get("period", "5m"))
        if len(candles) < 60:
            return
        # 数据新鲜度守卫（V3 §33/§48：数据源故障致 bar 陈旧→禁止交易，防用旧数据误下单）
        _p_ms = {"5m": 300_000, "15m": 900_000, "1H": 3_600_000, "4H": 14_400_000}.get(cfg.get("period", "5m"), 300_000)
        _bar_age_s = (time.time() * 1000 - candles[-1].get("ts", 0)) / 1000
        if _bar_age_s > 2 * _p_ms / 1000:   # 容忍 2 个周期（含收盘延迟+轮询间隔），超出=数据源故障
            log.warning("数据陈旧：最新 %s bar 距今 %.0fs > 容忍 %.0fs，跳过决策（防数据源故障误交易）",
                        cfg.get("period"), _bar_age_s, 2 * _p_ms / 1000)
            return
        acct = self._account_summary()
        equity = float(acct.get("equity", 0))
        # venue 切换（paper↔okx）时权益口径跳变，重置峰值避免误触回撤降档
        if self.venue != getattr(self, "_peak_venue", None):
            self._peak_venue = self.venue
            self._paper_peak_equity = equity
        if equity > self._paper_peak_equity:
            self._paper_peak_equity = equity
        dd_pct = ((self._paper_peak_equity - equity) / self._paper_peak_equity * 100
                  if self._paper_peak_equity > 0 else 0.0)
        self._dd_pct = dd_pct

        # 实盘模式未就绪（未连 Key/OMS 未建）：明确观望，绝不偷偷落回 paper 误下真单
        if self.venue == "okx" and not self._live_ready():
            log.warning("实盘模式但未连接 OKX Key，autopilot 观望（不下单）")
            return

        factors = compute_factors(candles)
        if factors.get("error"):
            return
        self.last_factors = factors
        regime = factors.get("regime", "range")

        # 发现已有持仓（venue 切换后收养，防重复开仓）
        self._discover_position(acct)

        # 持仓中：持仓管理走 tick + signal-exit，不开新仓
        if self.position:
            return

        # 开仓节流
        block = self._open_block_reason(int(time.time() * 1000))
        if block:
            log.info("开仓节流：%s", block)
            return

        # ---- 两个方向都跑完整决策链，取分高且过守卫的方向 ----
        htf4h, htf1h = self._htf_context(cfg.get("period", "5m"))
        ctx = {"htf_4h": htf4h, "htf_1h": htf1h, "halted": bool(self.risk.state.get("halted")) if self.risk else False,
               "drawdown_pct": dd_pct, "loss_streak": self._loss_streak,
               "leverage": int(cfg.get("leverage", 1) or 1)}
        best = None
        require_setup = cfg.get("require_setup", False)
        setup_filter = cfg.get("setup_filter", "")
        eval_details = []
        for side in ("long", "short"):
            setup = se.detect_setup(candles, factors, side)
            if require_setup and setup.get("setup") == "none":
                eval_details.append({"side": side, "setup": "none", "skip": "无形态(require_setup)"})
                continue
            if setup_filter and setup.get("setup") != setup_filter:
                eval_details.append({"side": side, "setup": setup.get("setup"), "skip": f"非指定形态(需{setup_filter})"})
                continue
            score, breakdown = se.score_side(factors, side, setup, htf_4h=htf4h, htf_1h=htf1h)
            plan = se.plan_trade(candles, factors, side)
            ok, checks = se.check_gate(candles, factors, side, score, plan, ctx)
            quality = {
                "regime": regime, "regime_reason": factors.get("regime_reason"),
                "signal_score": score, "rr": plan["rr"],
                "atr_pct": factors.get("atr_pct"), "atr_pctile": factors.get("atr_pctile"),
                "volume_ratio": factors.get("volume_ratio"),
                "htf_4h": htf4h, "htf_1h": htf1h,
                "setup": setup.get("setup"), "setup_why": setup.get("why"),
                "checks": checks, "breakdown": breakdown,
                "plan": {k: plan.get(k) for k in ("entry", "sl", "tp", "risk_dist", "rr", "sl_basis")},
            }
            db.execute(
                "INSERT INTO signals (instance_id, ts, action, reason, payload_json) VALUES (?,?,?,?,?)",
                (0, int(time.time() * 1000),
                 f"gate:{side}:{'pass' if ok else 'fail'}",
                 f"score={score} rr={plan['rr']} regime={regime} setup={setup.get('setup')}",
                 json.dumps(quality, ensure_ascii=False)),
            )
            eval_details.append({"side": side, "setup": setup.get("setup"), "score": score, "gate": "pass" if ok else "fail"})
            if ok and (best is None or score > best[1]):
                best = (side, score, plan, quality)

        if best is None:
            # 观望也是有效决策——写一条 wait 记录供健康监控（1H 稀疏策略需确认存活+决策频率）
            eval_summary = " ".join(
                f"{d['side']}={d.get('setup','none')}" + (f"({d['skip']})" if d.get("skip") else f"score={d.get('score',0)}/{d.get('gate','')}") 
                for d in eval_details
            )
            db.execute(
                "INSERT INTO signals (instance_id, ts, action, reason, payload_json) VALUES (?,?,?,?,?)",
                (0, int(time.time() * 1000), "decide:wait",
                 f"regime={regime} {eval_summary} htf4h={htf4h} htf_mid={htf1h}",
                 json.dumps({"regime": regime, "regime_reason": factors.get("regime_reason"),
                             "htf_4h": htf4h, "htf_1h": htf1h,
                             "atr_pct": factors.get("atr_pct"), "adx": factors.get("adx_14"),
                             "evaluated": len(eval_details), "eval_details": eval_details}, ensure_ascii=False)),
            )
            return   # 观望是有效决策
        side, score, plan, quality = best
        leverage = int(cfg.get("leverage", 1) or 1)
        if side == "short" and leverage <= 1:
            log.info("最高分方向 short（score=%d），现货模式(leverage=1)不支持裸做空，跳过", score)
            return
        await self._open_position(side, plan, quality, acct, dd_pct, cfg)

    def _save_wait(self, factors: dict, reason: str) -> None:
        event_bus.publish("log", {"ts": int(time.time() * 1000), "level": "info",
                                  "msg": f"自动驾驶观望：{reason}"})

    def _htf_context(self, period: str = "5m") -> tuple[str | None, str | None]:
        """4H 大趋势 + mid HTF 中期趋势（多周期确认）。取最近 60 根已收盘 HTF bar。

        1H 交易时 mid 用 1D（真正的更高周期，避免与交易周期同频自证 → HTF 评分虚高）；
        5m/15m 交易时 mid 用 1H。与 rules_backtest.mid_htf_period 同口径。
        """
        rows4h = db.query(
            "SELECT open_time as ts, o, h, l, c, vol FROM bars WHERE inst_id=? AND period='4H' "
            "ORDER BY open_time DESC LIMIT 60", (INST_ID,))
        mid_period = "1D" if period == "1H" else "1H"
        rows_mid = db.query(
            "SELECT open_time as ts, o, h, l, c, vol FROM bars WHERE inst_id=? AND period=? "
            "ORDER BY open_time DESC LIMIT 60", (INST_ID, mid_period))
        rows4h = list(reversed(rows4h))   # DESC 取最近，再反转为升序供 trend_of
        rows_mid = list(reversed(rows_mid))
        return (trend_of(rows4h) if len(rows4h) >= 23 else None,
                trend_of(rows_mid) if len(rows_mid) >= 23 else None)

    def _recent_candles(self, period: str) -> list[dict]:
        rows = db.query(
            "SELECT open_time as ts, o, h, l, c, vol FROM bars WHERE inst_id=? AND period=? "
            "ORDER BY open_time DESC LIMIT 120",
            (INST_ID, period),
        )
        return list(reversed(rows))   # DESC 取最近 120 根，反转为升序供 compute_factors

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
            self._daily_realized_pnl = 0.0     # 新日重置当日盈亏
        # V2 风控降档（任务书第三十四）：paused 档停止开仓
        tier, _mult = self._risk_tier(self._dd_pct, self._daily_realized_pnl, cfg)
        if tier == "paused":
            return f"风控降档 paused（回撤{self._dd_pct:.1f}%/连亏{self._loss_streak}/当日{self._daily_realized_pnl:.1f}U）"
        if cap > 0 and self._daily_opens >= cap:
            return f"今日开仓已达上限 {cap} 次，明日再战"
        return None

    def _update_loss_streak(self) -> None:
        try:
            rt = compute_round_trips(venue=self.venue, inst_id=INST_ID, limit=1)
            rows = rt.get("closed") or []
            if not rows:
                return
            pnl = float(rows[-1].get("pnl") or 0)
            self._daily_realized_pnl += pnl     # V2 累加当日已实现盈亏（供每日风险限制）
            if pnl < 0:
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
                    f"连续亏损 {n} 笔，自动驾驶强制休眠 {mins:.0f} 分钟",
                    {"sleep_until": self._sleep_until},
                )
        except Exception as e:
            log.warning("连亏统计失败: %s", e)

    def _risk_tier(self, dd_pct: float, daily_pnl: float, cfg: dict) -> tuple[str, float]:
        """策略风控降档（任务书第三十四）：normal/reduced/minimum/paused。
        基于：回撤 + 连亏 + 当日亏损。返回 (档位, 仓位倍率)。paused=0 停止开仓。"""
        daily_limit = float(cfg.get("daily_loss_limit_usdt", 100))
        n = self._loss_streak
        # paused：极端回撤/连亏/当日亏损达限
        if dd_pct >= 6.0 or n >= 5 or (daily_limit > 0 and daily_pnl <= -daily_limit):
            return "paused", 0.0
        # minimum：严重恶化
        if dd_pct >= 4.0 or n >= 3:
            return "minimum", 0.25
        # reduced：轻度恶化
        if dd_pct >= 3.0 or n >= 2:
            return "reduced", 0.5
        return "normal", 1.0

    # ---------- 开仓（风险预算 + 决策价偏差守卫） ----------
    async def _open_position(self, side: str, plan: dict, quality: dict, acct: dict,
                             dd_pct: float, cfg: dict) -> None:
        last_px = self.data.last_price(INST_ID)
        if last_px <= 0:
            return
        # 安全网：已有持仓时不重复开仓（_discover_position 遗漏时兜底）
        if self.position:
            log.warning("已有持仓，跳过开仓（安全网）")
            return
        # 价格漂移守卫：决策后价格移动明显时以最新价重算（纯规则无 LLM 延迟，通常极小）
        if abs(last_px - plan["entry"]) / plan["entry"] > PX_DRIFT_LIMIT:
            candles = self._recent_candles(self.config().get("period", "5m"))
            f2 = compute_factors(candles) if len(candles) >= 60 else None
            if f2 and not f2.get("error"):
                plan = se.plan_trade(candles, f2, side)
                if plan["rr"] < se.RR_MIN:
                    log.info("价格漂移 %.2f%% 后 RR=%.2f 不达标，放弃",
                             abs(last_px - plan["entry"]) / plan["entry"] * 100, plan["rr"])
                    return
            plan = dict(plan, entry=last_px)

        equity = float(acct.get("equity", 0))
        avail = float(acct.get("usdt", 0))
        leverage = int(cfg.get("leverage", 1) or 1)
        notional, size_note = se.position_size(
            equity, avail, plan, dd_pct, self._loss_streak,
            float(cfg.get("max_order_usdt", 200)),
            leverage=leverage)
        # V2 风控降档（任务书第三十四）：策略级仓位倍率（normal1.0/reduced0.5/minimum0.25/paused停）
        _tier, mult = self._risk_tier(dd_pct, self._daily_realized_pnl, cfg)
        if mult == 0.0:
            self._notify("autopilot_skip", "warning",
                         f"风控降档 {_tier}，放弃开仓",
                         {"dd_pct": dd_pct, "loss_streak": self._loss_streak,
                          "daily_pnl": self._daily_realized_pnl})
            return
        notional *= mult
        if notional < MIN_NOTIONAL_USDT:
            self._notify("autopilot_skip", "warning",
                         f"仓位计算不足最小下单额：{size_note}", {"plan": plan})
            return
        sz_base = notional / last_px
        is_short = side == "short"
        order_side = "sell" if is_short else "buy"

        try:
            row = await self._executor().place_intent({
                "inst_id": INST_ID, "side": order_side, "ord_type": "market",
                "sz_base": sz_base, "reduce_only": False, "source": "autopilot:open",
                "leverage": leverage,
            })
            fill_px = last_px
            if row.get("cl_ord_id"):
                tr = db.query("SELECT px FROM trades WHERE cl_ord_id=? ORDER BY id DESC LIMIT 1",
                              (row["cl_ord_id"],))
                if tr:
                    fill_px = float(tr[0]["px"])
            entry = fill_px
            risk_dist = abs(entry - plan["sl"])
            if risk_dist <= 0:
                return
            sign = -1 if is_short else 1
            self.position = {
                "side": side, "sz": float(row.get("sz") or sz_base),
                "orig_sz": float(row.get("sz") or sz_base),
                "entry_px": entry, "sl_px": plan["sl"],
                "risk_dist": risk_dist,
                "tp1_px": round(entry + sign * TP1_R * risk_dist, 2),
                "tp2_px": round(entry + sign * TP2_R * risk_dist, 2),
                "tp3_px": round(entry + sign * 3.0 * risk_dist, 2),
                "tp1_done": False, "tp2_done": False,
                "high_water": entry, "low_water": entry,
                "atr_pct": float(quality.get("atr_pct", 0)) or 0,
                "cl_ord_id": row.get("cl_ord_id"),
                "regime": quality.get("regime"), "signal_score": quality.get("signal_score"),
                "setup": quality.get("setup"), "open_ts": int(time.time() * 1000),
                "mfe_px": entry, "mae_px": entry,
                "venue": self.venue,
                "leverage": leverage,
                "margin": round(notional * mult, 4),
                "liq_px": round(entry * (1 - sign * (1.0 / leverage - 0.005)), 2) if leverage > 1 else None,
            }
            self._bars_since_open = 0
            self.last_action = side
            day = time.strftime("%Y-%m-%d")
            if day != self._daily_key:
                self._daily_key = day
                self._daily_opens = 0
            self._daily_opens += 1
            venue_tag = "实盘" if self.venue == "okx" else "模拟"
            lev_tag = f" {leverage}x杠杆" if leverage > 1 else ""
            dir_tag = "开空" if is_short else "开多"
            self._notify(
                "autopilot_open", "info",
                f"[{venue_tag}{lev_tag}] {dir_tag} {sz_base:.6f} @ {entry:.2f} sl={plan['sl']:.2f} "
                f"tp1={self.position['tp1_px']:.2f} tp2={self.position['tp2_px']:.2f} "
                f"score={quality.get('signal_score')} rr={plan['rr']} setup={quality.get('setup')}",
                {"plan": plan, "quality": quality, "size_note": size_note, "order": row,
                 "venue": self.venue, "leverage": leverage, "side": side},
            )
        except RiskBlocked as e:
            self._notify("autopilot_blocked", "warning",
                         f"开仓被风控拦截：{e}", {"plan": plan})
        except Exception as e:
            log.error("autopilot 开仓失败: %s", e)

    # ---------- Signal Exit（bar 收盘级，规则版趋势反转退出） ----------
    async def _signal_exit_check(self, _ts: int) -> None:
        """趋势反转退出：多单收盘跌破 EMA21+MACD 转负 / 空头收盘涨破 EMA21+MACD 转正。

        只在未到达 TP1 前生效（到达 TP2 后由 trailing 接管，不重复干预）。
        """
        if not self.position or self.position.get("tp2_done"):
            return
        candles = self._recent_candles(self.config().get("period", "5m"))
        if len(candles) < 40:
            return
        f = compute_factors(candles)
        if f.get("error"):
            return
        pos = self.position
        last_close = float(candles[-1]["c"])
        ema21 = float(f.get("ema_slow", 0))
        hist = float(f.get("macd_hist", 0))
        is_short = pos.get("side") == "short"
        if not is_short and last_close < ema21 * 0.999 and hist < 0:
            await self._close_position(
                reason=f"Signal Exit：收盘 {last_close:.0f} 跌破 EMA21 {ema21:.0f} 且 MACD 转负")
        elif is_short and last_close > ema21 * 1.001 and hist > 0:
            await self._close_position(
                reason=f"Signal Exit：收盘 {last_close:.0f} 涨破 EMA21 {ema21:.0f} 且 MACD 转正")

    # ---------- 平仓 ----------
    async def _close_position(self, reason: str = "", sz: float | None = None) -> None:
        if not self.position:
            return
        pos = self.position
        sell_sz = min(sz if sz is not None else pos["sz"], pos["sz"])
        if sell_sz <= 1e-12:
            return
        is_short = pos.get("side") == "short"
        close_side = "buy" if is_short else "sell"
        try:
            await self._executor().place_intent({
                "inst_id": INST_ID, "side": close_side, "ord_type": "market",
                "sz_base": sell_sz, "reduce_only": True, "source": "autopilot:close",
                "leverage": int(pos.get("leverage", 1) or 1),
            })
            pos["sz"] -= sell_sz
            fully = pos["sz"] <= 1e-12
            if fully:
                self._last_close_ts = int(time.time() * 1000)
                self._update_loss_streak()
                self.position = None
                self.last_action = "flat"
            dir_tag = "平空" if is_short else "平多"
            self._notify("autopilot_close", "info",
                         f"{dir_tag} {sell_sz:.6f}（{'全部' if fully else '部分'}，{reason}）",
                         {"reason": reason, "sz": sell_sz, "fully": fully, "side": pos.get("side")})
            log.info("autopilot %s sz=%.6f reason=%s fully=%s", dir_tag, sell_sz, reason, fully)
        except Exception as e:
            log.error("autopilot 平仓失败: %s", e)

    # ---------- 持仓管理（tick 级，优先级从高到低，全本地确定性） ----------
    async def _manage_position(self, px: float) -> None:
        if not self.position or not px:
            return
        pos = self.position
        is_short = pos.get("side") == "short"

        # 水位追踪：多头 high_water（最高价）/ 空头 low_water（最低价）
        if "high_water" not in pos:
            pos["high_water"] = pos["entry_px"]
        if "low_water" not in pos:
            pos["low_water"] = pos["entry_px"]
        if is_short:
            if px < pos["low_water"]:
                pos["low_water"] = px
                pos["mfe_px"] = min(pos.get("mfe_px", px), px)   # 空头 MFE = 最低价
            pos["mae_px"] = max(pos.get("mae_px", px), px)        # 空头 MAE = 最高价
        else:
            if px > pos["high_water"]:
                pos["high_water"] = px
                pos["mfe_px"] = max(pos.get("mfe_px", px), px)   # 多头 MFE = 最高价
            pos["mae_px"] = min(pos.get("mae_px", px), px)        # 多头 MAE = 最低价

        # 1. 强制止损
        sl_hit = (px >= pos["sl_px"]) if is_short else (px <= pos["sl_px"])
        if sl_hit:
            reason = "止损触发" if (pos["sl_px"] > pos["entry_px"] if is_short else pos["sl_px"] < pos["entry_px"]) else "保本止损触发"
            await self._close_position(reason=f"{reason} @ {px:.2f}")
            return
        # 2. TP1：1R 平 25% + 止损推保本
        tp1_hit = (px <= pos["tp1_px"]) if is_short else (px >= pos["tp1_px"])
        if not pos["tp1_done"] and tp1_hit:
            pos["tp1_done"] = True
            pos["sl_px"] = pos["entry_px"]  # 保本
            await self._close_position(reason=f"TP1 到达 1R 平 25% @ {px:.2f}",
                                       sz=pos["orig_sz"] * TP1_PCT)
            return
        # 3. TP2：2R 平 35% + 激活 trailing
        tp2_hit = (px <= pos["tp2_px"]) if is_short else (px >= pos["tp2_px"])
        if not pos["tp2_done"] and tp2_hit:
            pos["tp2_done"] = True
            await self._close_position(reason=f"TP2 到达 2R 平 35% @ {px:.2f}",
                                       sz=pos["orig_sz"] * TP2_PCT)
            return
        # 4. trailing（TP2 后）
        if pos["tp2_done"]:
            atr_abs = pos["atr_pct"] / 100 * pos["entry_px"] if pos["atr_pct"] else pos["risk_dist"] / 1.8
            if is_short:
                trail = min(pos["entry_px"], pos["low_water"] + TRAIL_ATR * atr_abs)
                pos["sl_px"] = min(pos["sl_px"], trail)  # 空头止损只下移
                if px >= trail:
                    await self._close_position(reason=f"ATR trailing @ {px:.2f} ≥ {trail:.2f}")
                    return
            else:
                trail = max(pos["entry_px"], pos["high_water"] - TRAIL_ATR * atr_abs)
                pos["sl_px"] = max(pos["sl_px"], trail)  # 多头止损只上移
                if px <= trail:
                    await self._close_position(reason=f"ATR trailing @ {px:.2f} ≤ {trail:.2f}")
                    return
        # 5. 3R 全平
        tp3_hit = (px <= pos["tp3_px"]) if is_short else (px >= pos["tp3_px"])
        if pos["tp1_done"] and tp3_hit:
            await self._close_position(reason=f"TP3 到达 3R 清仓 @ {px:.2f}")
            return
        # 6. 时间止损
        floating_loss = (px > pos["entry_px"] * 1.001) if is_short else (px < pos["entry_px"] * 0.999)
        if not pos["tp1_done"] and self._bars_since_open >= TIME_STOP_BARS and floating_loss:
            await self._close_position(reason=f"时间止损：{self._bars_since_open} 根 bar 未达 TP1 且浮亏")

    # ---------- 持久化 / 通知 ----------
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
        self._peak_venue = None
        self._bars_since_open = 0
        self.position = None

    def on_venue_switch(self) -> None:
        """切换 venue 时调用：清仓位追踪 + 重置节流，各 venue 独立。
        然后立即发现新 venue 已有持仓（不等下一个 bar）。
        按新 venue 的 enabled 状态启动/停止 autopilot 任务。"""
        self.position = None
        self._last_close_ts = 0
        self._daily_key = ""
        self._daily_opens = 0
        self._loss_streak = 0
        self._sleep_until = 0
        self._paper_peak_equity = 0.0
        self._peak_venue = None
        self._bars_since_open = 0
        self._open_block_reason = None
        # 立即发现新 venue 已有持仓
        try:
            acct = self._account_summary()
            self._discover_position(acct)
            # 重置峰值到当前权益（防跳变误触回撤）
            equity = float(acct.get("equity", 0))
            self._paper_peak_equity = equity
            self._peak_venue = self.venue
        except Exception:
            log.debug("on_venue_switch: 持仓发现延迟到 _decide_and_act")
        # 按新 venue 的 enabled 状态启动/停止 autopilot 任务
        cfg = self.config()
        if cfg.get("enabled"):
            if self._task is None:
                asyncio.ensure_future(self.start())
                log.info("on_venue_switch: 新 venue %s autopilot enabled → 启动任务", self.venue)
        else:
            if self._task is not None:
                asyncio.ensure_future(self.stop())
                log.info("on_venue_switch: 新 venue %s autopilot disabled → 停止任务", self.venue)

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
        regime = self.last_factors.get("regime")
        return {
            "enabled": bool(cfg.get("enabled")),
            "running": self.is_running(),
            "period": cfg.get("period", "5m"),
            "engine": "rules_v3",
            "venue": self.venue,
            "live_ready": self.venue != "okx" or self._live_ready(),
            "last_action": self.last_action,
            "regime": regime,
            "regime_reason": self.last_factors.get("regime_reason", ""),
            "last_error": "",
            "position": self.position,
            "throttle": self.throttle_status(),
            "mode": cfg.get("mode", "normal"),
            "leverage": int(cfg.get("leverage", 1) or 1),
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
