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

from app import config, db
from app.brain import signal_engine as se
from app.brain.factor_engine import compute_factors, trend_of
from app.core import event_bus
from app.risk.risk_engine import RiskBlocked
from app.trading.roundtrips import compute_round_trips

log = logging.getLogger("bitvault.brain.autopilot")

# ---------- 人类可读文案（决策记录/通知面向用户） ----------
_REGIME_CN = {"trend_up": "多头趋势", "trend_down": "空头趋势", "range": "震荡区间",
              "low_vol": "低波动", "high_vol": "高波动", "extreme": "异常波动", "unknown": "状态未知"}
_SETUP_CN = {"none": "无", "pullback": "回踩", "breakout_retest": "突破回踩"}
_HTF_CN = {"up": "多头", "down": "空头", "range": "震荡", "unknown": "未知", None: "未确认"}


def _setup_gate(setup: str, require_setup: bool, setup_filter: str, strategy_mode: str) -> str | None:
    """准入层（唯一实验点）：返回跳过原因（str）或 None（放行进入 Score/Guard 全链）。

    - official：与生产完全一致（只放行 setup_filter；require_setup 拦截 none）。
    - research_pullback：额外放行 pullback（仍须 require_setup 拦截 none、
      仍受 setup_filter 约束——实验只多加一个准入形态，其余层层复用）。"""
    if require_setup and setup == "none":
        return "无形态(require_setup)"
    if setup != setup_filter:
        if strategy_mode == "research_pullback" and setup == "pullback":
            return None
        return f"非指定形态(需{setup_filter})"
    return None


def _d_or_none(eval_details: list, side: str) -> dict:
    """取某方向的评估明细（含 setup/detail）；无则视为无形态。"""
    for d in eval_details:
        if d.get("side") == side:
            return {"setup": d.get("setup", "none"), "detail": d.get("detail") or {"kind": "none"}}
    return {"setup": "none", "detail": {"kind": "none"}}


_HTF_BIAS_CN = {"up": "多头", "down": "空头", "range": "震荡", "unknown": "未确认", None: "未确认"}
_TREND_DIR_CN = {"up": "多头", "down": "空头", "range": "横盘震荡", "unknown": "待确认"}


def _htf_bias(t4h: str | None, t1d: str | None) -> str:
    """大周期偏置：1D 与 4H 同向→该向；冲突→混合；任一未知→未确认。不强行归一。"""
    h, d = _HTF_BIAS_CN.get(t4h, "未确认"), _HTF_BIAS_CN.get(t1d, "未确认")
    if h == d and h != "未确认":
        return h
    if "未确认" in (h, d):
        return "未确认"
    return "混合"


def _setup_line_cn(allow: bool, sr: dict) -> str:
    """单方向 Setup 一行：允许 / 形态 / 突破 / 确认 / 结果。
    注意优先看结构 detail：突破尝试(kind=breakout_attempt)时 setup 名仍为 none，
    不能误判为"形态:未出现"——要把"已突破未回踩"讲清楚。"""
    setup = sr.get("setup", "none")
    detail = sr.get("detail") or {}
    kind = detail.get("kind", "none")
    base = "允许:" + ("是" if allow else "否")
    if kind == "breakout_retest":
        return f"{base} 形态:突破回踩(完整) 突破:已确认 回踩:未破 确认:完成 结果:PASS"
    if kind == "breakout_attempt":
        return (f"{base} 形态:突破尝试 突破:已发生 回踩:未完成确认 确认:未完成 结果:FAIL")
    if kind == "pullback":
        touched = "已触线" if detail.get("touched") else "未触线"
        return f"{base} 形态:普通回踩({touched}) 突破:无 确认:未完成 结果:FAIL"
    return f"{base} 形态:未出现 确认:未完成 结果:FAIL"


def _format_dec_report(rep: dict) -> str:
    """结构化决策日志（八层，每层一行）：大周期→状态→方向→结构→多/空 Setup→风险→决策→原因→下一步。"""
    lines = [
        f"【自动驾驶决策】{rep.get('symbol', '')} · {rep.get('period', '')} 周期",
        f"【大周期趋势 HTF】1D：{rep['htf']['1D']} ｜ 4H：{rep['htf']['4H']} ｜ HTF偏置：{rep['htf']['bias']}",
        f"【当前市场状态】{rep['regime']}（{rep.get('regime_reason', '')}）",
        f"【当前周期方向】{rep['direction']}",
        f"【市场结构】{rep['structure']}",
        f"【多头 Setup】{rep['long_setup']}",
        f"【空头 Setup】{rep['short_setup']}",
        f"【风险收益】RR：{rep.get('rr', 'N/A')}",
        f"【最终决策】{rep['final']}",
        f"【原因】{rep['reason']}",
        f"【下一步等待】{rep['next']}",
    ]
    return "\n".join(lines)


def _gate_reason_cn(side: str, score: int, setup: str, plan: dict, checks: list, ok: bool) -> str:
    """gate 记录 → 人话：方向 + 评分 + 形态 + 第一项未过的守卫。"""
    side_cn = "多头" if side == "long" else "空头"
    failed = next((c for c in (checks or []) if not c.get("ok")), None)
    verdict = failed.get("why", "全部守卫通过") if failed else ("全部守卫通过" if ok else "未通过守卫")
    return (f"{side_cn}方向：评分 {score}，形态 {_SETUP_CN.get(setup, setup)}，"
            f"风险收益比 {plan.get('rr', 0)}；{verdict}")


def _human_eval(d: dict, setup_filter: str = "") -> str:
    """单方向评估结论 → 一句人话。"""
    side_cn = "多头" if d.get("side") == "long" else "空头"
    if d.get("skip"):
        skip = d["skip"]
        if skip.startswith("无形态"):
            return f"{side_cn}：没有出现入场形态"
        setup_cn = _SETUP_CN.get(d.get("setup"), d.get("setup") or "—")
        want_cn = _SETUP_CN.get(setup_filter, setup_filter or "任意指定形态")
        return f"{side_cn}：出现 {setup_cn} 形态，但策略只认 {want_cn}"
    gate = "全部守卫通过" if d.get("gate") == "pass" else "未通过守卫"
    return f"{side_cn}：评分 {d.get('score')}，{gate}"


MIN_NOTIONAL_USDT = 1.0
PX_DRIFT_LIMIT = 0.0015          # 决策价与下单价偏差 >0.15% 时重算计划

# 分批止盈（R = |entry - sl|）
TP1_R, TP1_PCT = 1.0, 0.25   # 1R 平 25%，止损推保本
TP2_R, TP2_PCT = 2.0, 0.35   # 2R 平 35%，激活 ATR trailing
TRAIL_ATR = 2.5              # trailing 距离 = 2.5×ATR（不低于保本价）
TIME_STOP_BARS = 120         # 时间止损：120 根 bar 未到 TP1 且浮亏 → 离场
MAX_CLOSE_RETRIES = 3        # P0-4：平仓最大重试次数，超出则触发 exchange 重同步
SL_SYNC_RETRY_SEC = 30       # 交易所侧止损同步失败后的重试间隔（限流，防每 tick 重复下单）


class Autopilot:
    def __init__(self, data, paper, risk=None):
        self.data = data
        self.paper = paper
        self.risk = risk
        self.svc = None            # Services 容器引用（bind_svc 注入）：用于读取 venue/oms/account
        self._task: asyncio.Task | None = None
        self._tick_task: asyncio.Task | None = None
        self._deciding = False      # 重入守卫：决策期间丢弃新事件（修复同 bar 双开）
        # 被托管的持仓（多标的）：inst_id -> pos。决策只对当前驾驶标的执行；
        # 但所有已托管标的的止损/止盈/trailing 持续生效（切换标的不影响旧仓位自动驾驶）。
        self.positions: dict[str, dict] = {}
        self.last_action: str = "flat"
        self.last_factors: dict = {}
        self._paper_peak_equity: float = 0.0
        self._last_close_ts: int = 0
        self._daily_key: str = ""
        self._daily_opens: int = 0
        self._loss_streak: int = 0
        self._sleep_until: int = 0
        self._daily_realized_pnl: float = 0.0   # 当日已实现净盈亏（USDT）
        self._dd_pct: float = 0.0              # 缓存当前回撤供风控降档用
        self._close_retries: dict[str, int] = {}  # P0-4：按 inst_id 累计平仓重试次数
        # 无法下单平掉的残仓（低于最小下单量）记录：inst_id -> 明细。
        # 这类残仓**不进入 self.positions**，因此不阻塞开仓（has_position）。
        self._dust_notes: dict[str, dict] = {}
        self._dust_notified: set[str] = set()
        self._close_fail_notify_ts: dict[str, int] = {}   # 平仓失败告警限流

    def bind_svc(self, svc) -> None:
        """注入 Services 容器：autopilot 据此读取系统级 venue 与实盘 oms/account。"""
        self.svc = svc

    def _sync_delegated(self) -> None:
        """P0-2：将当前被托管持仓的标的同步到行情中心，维持 ticker/candle 订阅。

        持仓的止损/止盈/trailing 依赖实时行情事件；切换回标的后再切走时，
        事件路由按 inst_id 分发——只要频道订阅了该标的，事件就能到达。
        """
        if self.data and hasattr(self.data, "set_delegated_insts"):
            self.data.set_delegated_insts(set(self.positions.keys()))

    # ---------- 系统级模式（venue）派发 ----------
    @property
    def venue(self) -> str:
        """当前系统级模式：'paper' 模拟虚拟资金 / 'okx' 实盘真实资金。"""
        return getattr(self.svc, "venue", "paper") if self.svc else "paper"

    @property
    def inst_id(self) -> str:
        """当前驾驶标的（币种+合约/现货）。决策只针对此标的。"""
        return getattr(self.svc, "inst_id", config.DEFAULT_INST_ID) if self.svc else config.DEFAULT_INST_ID

    @property
    def position(self) -> dict | None:
        """当前驾驶标的的托管持仓（兼容旧单仓接口）。"""
        return self.positions.get(self.inst_id)

    @position.setter
    def position(self, val: dict | None) -> None:
        if val is None:
            self.positions.pop(self.inst_id, None)
        else:
            self.positions[self.inst_id] = val

    def _live_ready(self) -> bool:
        """实盘模式是否就绪（已连 Key + OMS 已建 + 私有 WS 健康）。
        P1-6：private_ws 断开时视为未就绪——禁止新开仓（fail-closed），
        但持仓管理/减仓不受影响（_manage_position 独立于本检查）。"""
        ws_ok = True
        pws = getattr(self.svc, "private_ws", None)
        if pws is not None and pws.status not in ("connected", "connecting", ""):
            ws_ok = pws.status == "connected"
        return bool(self.svc and self.svc.has_key() and getattr(self.svc, "oms", None) and ws_ok)

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
            # OKX 持仓（全部标的计入：权益与托管口径按整个账户，不因切换标的丢仓位）
            positions = []
            for p in (self.svc.account.positions or []):
                inst_id = p.get("instId", "")
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
        """发现并收养当前 venue 已有但 autopilot 未追踪的持仓（支持集内全部标的）。

        场景：venue 切换/标的切换后 positions 中被清空/未收养，但实际持仓仍在。
        收养时用当前因子重算 SL/TP（不知道原始计划，保守处理）。
        支持多空双向：paper lev_positions 有 side 字段；OKX SWAP pos 正=多 负=空。
        P0-5：低于最小下单量的残仓（任何下单都平不掉）**不收养**，走 dust 处理——
        否则会被无限收养成"正常持仓"，把 has_position 永久占住（ETH 残仓事故）。
        """
        positions = acct.get("positions") or []
        for p in positions:
            inst_id = p.get("inst_id") or ""
            sz = float(p.get("sz") or 0)
            if sz <= 0 or inst_id in self.positions:
                continue
            if not self.data.is_supported(inst_id):
                continue
            _step, min_sz, unit = self.data.spec_step(inst_id)
            if min_sz > 0 and self._sz_venue_to_intent(inst_id, sz) < min_sz - 1e-12:
                # 不可交易残仓：不收养（paper 会被 purge_dust 清掉，okx 只记录+告警）
                self._handle_untradable(inst_id, sz, f"低于最小下单量 {min_sz:g} {unit}")
                continue
            pos_side = p.get("side", "long")   # paper: "long"/"short"; OKX: 默认 long
            is_short = pos_side == "short"
            entry_px = float(p.get("entry_px") or 0) or self.data.last_price(inst_id)
            last_px = self.data.last_price(inst_id) or entry_px
            cfg = self.config()
            lev = int(cfg.get("leverage", 1) or 1)
            candles = self._recent_candles(cfg.get("period", "1H"), inst_id)
            factors = compute_factors(candles) if len(candles) >= 60 else None
            sign = -1 if is_short else 1
            # 锚点一致性：SL 与 TP1/TP2/TP3 一律以「真实开仓价 entry_px」为基准。
            # 旧实现 SL 取 plan["sl"]（它锚定的是"收养时的市价"），而 TP 锚定 entry_px；
            # 当市价已明显偏离开仓价（如 P > entry + risk_dist）时，多头 SL 会落到开仓价之上。
            if factors and not factors.get("error"):
                plan = se.plan_trade(candles, factors, "short" if is_short else "long", round_px=lambda v: self.data.round_px(inst_id, v))
                risk_dist = plan["risk_dist"] or entry_px * 0.008
                sl_px = self.data.round_px(inst_id, entry_px - sign * risk_dist)
            else:
                # 数据不足：退化为 ATR 保守计划（1.5R 止损 / 1R-3R 分批止盈）
                risk_dist = entry_px * 0.008
                sl_px = self.data.round_px(inst_id, entry_px - sign * risk_dist * 1.5)
            self.positions[inst_id] = {
                "inst_id": inst_id,
                "cl_ord_id": "discovered",
                "side": "short" if is_short else "long",
                "entry_px": entry_px,
                "sl_px": sl_px,
                "tp1_px": self.data.round_px(inst_id, entry_px + sign * risk_dist * 1.0),
                "tp2_px": self.data.round_px(inst_id, entry_px + sign * risk_dist * 2.0),
                "tp3_px": self.data.round_px(inst_id, entry_px + sign * risk_dist * 3.0),
                "sz": sz, "orig_sz": sz,
                "high_water": max(entry_px, last_px),
                "low_water": min(entry_px, last_px),
                "mfe_px": max(entry_px, last_px) if not is_short else min(entry_px, last_px),
                "mae_px": min(entry_px, last_px) if not is_short else max(entry_px, last_px),
                "tp1_done": False, "tp2_done": False,
                "atr_pct": float(factors.get("atr_pct", 0)) if factors else 1.0,
                "risk_dist": risk_dist,
                "open_ts": int(time.time() * 1000),
                "leverage": lev,
                "margin": float(p.get("margin") or 0) or (sz * entry_px / lev if lev > 1 else sz * entry_px),
                "liq_px": p.get("liq_px"),
                "setup": "discovered",
            }
            dir_tag = "空" if is_short else "多"
            log.info("发现已有 %s 持仓 %s entry=%.2f sz=%.6f lev=%dx → 收养 SL=%.2f TP1=%.2f",
                     self.venue, dir_tag, entry_px, sz, lev, sl_px,
                     entry_px + sign * risk_dist)

    def config(self) -> dict:
        """按 venue 独立存储配置：autopilot_config_paper / autopilot_config_okx。"""
        key = f"autopilot_config_{self.venue}"
        saved = db.get_setting(key)
        if saved is None and self.venue == "paper":
            old = db.get_setting("autopilot_config")
            if old:
                saved = old
        cfg = {"enabled": False, "period": "5m", "max_order_usdt": 200,
               "cooldown_min": 15, "max_opens_per_day": 20,
               "loss_pause_n": 3, "loss_pause_min": 60,
               "daily_loss_limit_usdt": 100,
               "require_setup": True, "setup_filter": "breakout_retest",
               "mode": "normal", "leverage": 2,
               "strategy_mode": "official"}   # official | research_pullback（实验仅放开 pullback 准入）
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
    # 变更（多标的）：被托管持仓按 bar 事件 inst_id 路由——旧标的的 signal-exit/时间止损
    # 继续运作；只有当前驾驶标的且 enabled 时才跑新开仓决策。
    async def _bar_loop(self) -> None:
        q = event_bus.subscribe(["bar"])
        while True:
            try:
                topic, data, _ = await q.get()
                if data.get("confirm") == "0":
                    continue                     # 只在 K 线收盘确认后决策
                inst_id = data.get("inst_id") or ""
                period = data.get("period") or ""
                # 已托管持仓：bar 收盘驱动 signal-exit + 时间止损（独立于决策开关）
                if inst_id in self.positions and period == self.config().get("period", "5m"):
                    pos = self.positions[inst_id]
                    pos["bars_since_open"] = pos.get("bars_since_open", 0) + 1
                    await self._signal_exit_check(inst_id, data.get("ts", 0))
                cfg = self.config()
                if not cfg.get("enabled") or self._deciding:
                    continue
                if inst_id != self.inst_id or period != cfg.get("period", "5m"):
                    continue
                self._deciding = True
                try:
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
                inst_id = data.get("instId") or data.get("inst_id") or ""
                if inst_id not in self.positions:
                    continue
                await self._manage_position(inst_id, data.get("last", 0))
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
            self._log_wait("data_insufficient", f"K 线不足 60 根（{len(candles)}）")
            return
        # 数据新鲜度守卫（V3 §33/§48：数据源故障致 bar 陈旧→禁止交易，防用旧数据误下单）
        _p_ms = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
                 "1H": 3_600_000, "2H": 7_200_000, "4H": 14_400_000, "1D": 86_400_000}.get(
            cfg.get("period", "5m"), 300_000)
        _bar_age_s = (time.time() * 1000 - candles[-1].get("ts", 0)) / 1000
        if _bar_age_s > 2 * _p_ms / 1000:   # 容忍 2 个周期（含收盘延迟+轮询间隔），超出=数据源故障
            log.warning("数据陈旧：最新 %s bar 距今 %.0fs > 容忍 %.0fs，跳过决策（防数据源故障误交易）",
                        cfg.get("period"), _bar_age_s, 2 * _p_ms / 1000)
            self._log_wait("stale_data", f"最新 {cfg.get('period')} bar 已 {_bar_age_s:.0f}s > 容忍 {2 * _p_ms / 1000:.0f}s")
            return
        acct = self._account_summary()
        equity = float(acct.get("equity", 0))
        # P0-4：权益事件接入风控（Paper 也走与 OKX 相同的自动风控入口）
        if self.risk:
            try:
                self.risk.on_equity(equity, avail=float(acct.get("usdt", 0)) or None, venue=self.venue)
            except Exception as e:
                log.debug("risk.on_equity 异常: %s", e)
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
            self._log_wait("live_not_ready", "实盘未就绪（Key/OMS/private_ws 任一不健康）")
            return

        factors = compute_factors(candles)
        if factors.get("error"):
            return
        self.last_factors = factors
        regime = factors.get("regime", "range")

        # 发现已有持仓（venue 切换后收养，防重复开仓）
        self._discover_position(acct)
        self._sync_delegated()                     # P0-2：收养持仓后同步行情订阅

        # 重启/收养后：从交易所读回已有止损单写入 exch_sl（仅 okx，每仓位一次）
        for _iid, _p in list(self.positions.items()):
            await self._recover_exchange_sl(_iid, _p)

        # 持仓中：持仓管理走 tick + signal-exit，不开新仓
        if self.position:
            self._log_wait("has_position", f"当前 {self.inst_id} 已有持仓（{self.position.get('side')}），只管理不开新仓")
            return

        # 开仓节流
        block = self._open_block_reason(int(time.time() * 1000))
        if block:
            log.info("开仓节流：%s", block)
            self._log_wait("throttle", block)
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
            skip = _setup_gate(setup.get("setup", "none"), require_setup, setup_filter,
                               str(cfg.get("strategy_mode") or "official"))
            if skip:
                eval_details.append({"side": side, "setup": setup.get("setup"), "skip": skip,
                                     "detail": setup.get("detail")})
                continue
            score, breakdown = se.score_side(factors, side, setup, htf_4h=htf4h, htf_1h=htf1h)
            plan = se.plan_trade(candles, factors, side, round_px=lambda v: self.data.round_px(self.inst_id, v))
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
                 _gate_reason_cn(side, score, setup.get("setup"), plan, checks, ok)
                 + f"（{cfg.get('strategy_mode', 'official')}）",
                 json.dumps(quality, ensure_ascii=False)),
            )
            eval_details.append({"side": side, "setup": setup.get("setup"), "score": score,
                                 "gate": "pass" if ok else "fail", "detail": setup.get("detail")})
            if ok and (best is None or score > best[1]):
                best = (side, score, plan, quality)

        if best is None:
            # 观望也是有效决策——结构化报告（大周期/状态/方向/结构/多空Setup/风险/决策/原因/下一步）
            t4h = htf4h
            t1d = self._htf_d1(cfg.get("period", "5m"))
            bias = _htf_bias(t4h, t1d)
            current_dir = self._current_direction_cn(factors, bias)
            htf_disp = f"{_HTF_BIAS_CN.get(t4h, '未确认')}"
            rep = {
                "symbol": self.inst_id, "period": cfg.get("period", "5m"),
                "htf": {"1D": _HTF_BIAS_CN.get(t1d, "未确认"), "4H": htf_disp, "bias": bias},
                "regime": _REGIME_CN.get(regime, regime),
                "regime_reason": factors.get("regime_reason", ""),
                "direction": current_dir,
                # 结构句：哪个方向出现了"突破尝试"或"普通回踩"，未完成哪种结构
                "structure": self._structure_cn(current_dir, bias, eval_details),
                "long_setup": _setup_line_cn(True, _d_or_none(eval_details, "long")),
                "short_setup": _setup_line_cn(True, _d_or_none(eval_details, "short")),
                "rr": "N/A（未产生入场计划）",
                "final": ("LONG" if False else "NO TRADE") + " / 观望",
                "reason": self._wait_reason_cn(current_dir, bias, eval_details, setup_filter),
                "next": self._next_wait_cn(eval_details, setup_filter),
            }
            wait_reason = _format_dec_report(rep)
            db.execute(
                "INSERT INTO signals (instance_id, ts, action, reason, payload_json) VALUES (?,?,?,?,?)",
                (0, int(time.time() * 1000), "decide:wait",
                 wait_reason,
                 json.dumps({"regime": regime, "regime_reason": factors.get("regime_reason"),
                             "htf_4h": htf4h, "htf_1h": htf1h,
                             "atr_pct": factors.get("atr_pct"), "adx": factors.get("adx_14"),
                             "evaluated": len(eval_details), "eval_details": eval_details,
                             "strategy_mode": cfg.get("strategy_mode", "official"),
                             "block_layer": "setup_or_gate"},
                            ensure_ascii=False)),
            )
            return   # 观望是有效决策
        side, score, plan, quality = best
        leverage = int(cfg.get("leverage", 1) or 1)
        if side == "short" and leverage <= 1:
            log.info("最高分方向 short（score=%d），现货模式(leverage=1)不支持裸做空，跳过", score)
            return
        await self._open_position(side, plan, quality, acct, dd_pct, cfg)

    def _log_wait(self, layer: str, note: str = "") -> None:
        """未到达开仓决策即返回的路径：记录 NO TRADE 与卡在哪一层（不改变交易行为，仅日志）。"""
        try:
            cfg = self.config()
            db.execute(
                "INSERT INTO signals (instance_id, ts, action, reason, payload_json) VALUES (?,?,?,?,?)",
                 (0, int(time.time() * 1000), "decide:wait",
                 f"【自动驾驶决策】{self.inst_id} · {cfg.get('period')} 周期\n"
                 f"【最终决策】NO TRADE / 观望（卡在 {layer}）\n【原因】{note or layer}",
                 json.dumps({"block_layer": layer, "strategy_mode": cfg.get("strategy_mode", "official")},
                            ensure_ascii=False)),
            )
        except Exception as e:
            log.debug("_log_wait 写入失败: %s", e)

    def _save_wait(self, factors: dict, reason: str) -> None:
        event_bus.publish("log", {"ts": int(time.time() * 1000), "level": "info",
                                  "msg": f"自动驾驶观望：{reason}"})

    def _htf_context(self, period: str = "5m", inst_id: str | None = None) -> tuple[str | None, str | None]:
        """4H 大趋势 + mid HTF 中期趋势（多周期确认）。取最近 60 根已收盘 HTF bar。

        1H 交易时 mid 用 1D（真正的更高周期，避免与交易周期同频自证 → HTF 评分虚高）；
        5m/15m 交易时 mid 用 1H。与 rules_backtest.mid_htf_period 同口径。
        """
        inst_id = inst_id or self.inst_id
        rows4h = db.query(
            "SELECT open_time as ts, o, h, l, c, vol FROM bars WHERE inst_id=? AND period='4H' "
            "ORDER BY open_time DESC LIMIT 60", (inst_id,))
        mid_period = "1D" if period in ("1H", "2H", "4H") else "1H"
        rows_mid = db.query(
            "SELECT open_time as ts, o, h, l, c, vol FROM bars WHERE inst_id=? AND period=? "
            "ORDER BY open_time DESC LIMIT 60", (inst_id, mid_period))
        rows4h = list(reversed(rows4h))   # DESC 取最近，再反转为升序供 trend_of
        rows_mid = list(reversed(rows_mid))
        return (trend_of(rows4h) if len(rows4h) >= 23 else None,
                trend_of(rows_mid) if len(rows_mid) >= 23 else None)

    # ---------- 结构化决策报告：周期分层辅助（纯展示，不参与评分/守卫） ----------
    def _htf_d1(self, period: str = "5m") -> str | None:
        """报告层：日线趋势（1D）。策略决策仍用原 htf4h/mid，此函数仅用于"大周期"表述。"""
        rows = db.query(
            "SELECT open_time as ts, o, h, l, c, vol FROM bars WHERE inst_id=? AND period='1D' "
            "ORDER BY open_time DESC LIMIT 60", (self.inst_id,))
        rows = list(reversed(rows))
        return trend_of(rows) if len(rows) >= 23 else None

    def _current_direction_cn(self, factors: dict, bias: str) -> str:
        """当前周期方向：大周期多头背景下的"空头回调"≠"市场空头趋势"。"""
        trend = str(factors.get("trend", "unknown"))
        if bias == "多头" and trend == "down":
            return "空头回调（大周期多头背景下的回调）"
        if bias == "空头" and trend == "up":
            return "多头反弹（大周期空头背景下的反弹）"
        return _TREND_DIR_CN.get(trend, "待确认")

    def _structure_cn(self, direction: str, bias: str, eval_details: list) -> str:
        """市场结构：区分"普通回踩/突破尝试/突破回踩"，未完成的结构明说未完成。"""
        notes = []
        for d in eval_details:
            side_cn = "多头" if d.get("side") == "long" else "空头"
            detail = d.get("detail") or {}
            kind = detail.get("kind", "none")
            if kind == "pullback":
                notes.append(f"{side_cn}方向出现普通回踩（未突破关键结构）")
            elif kind == "breakout_attempt":
                notes.append(f"{side_cn}方向突破已发生但回踩未形成确认")
            elif kind == "breakout_retest":
                notes.append(f"{side_cn}方向突破回踩结构完成（等待评分与守卫）")
        if not notes:
            notes.append("无显著入场结构")
        # direction 可能带括号说明（如"空头回调（大周期多头背景下的回调）"），结构句用短形式避免重复
        return f"{bias}大周期背景下，当前为{direction.split('（')[0]}；" + "；".join(notes)

    def _wait_reason_cn(self, direction: str, bias: str, eval_details: list, setup_filter: str) -> str:
        """原因三段：大周期/当前结构/多空 Setup 结论。"""
        setup_notes = [_human_eval(d, setup_filter) for d in eval_details] or ["两个方向均无有效形态"]
        return (
            f"{bias}背景未变；{direction}；"
            + "；".join(setup_notes)
            + "。以上条件未齐，继续观望，不降低入场标准"
        )

    def _next_wait_cn(self, eval_details: list, setup_filter: str) -> str:
        """下一步各方向等待什么（问题3）。

        返回最多两条：多头等待句 + 空头等待句。空头若出现普通回踩（pullback），
        空头句换成"等待有效突破 + 回踩 + 确认"，否则用默认句（下方 len==1 分支补）。
        注意 wants 初始只有多头 1 条，追加空头句必须用 append——
        曾用 wants[1] = ... 直接索引赋值，在"空头 pullback 且无入场信号"时
        抛 IndexError 中断整个观望报告。
        """
        want_cn = _SETUP_CN.get(setup_filter, setup_filter or "指定形态")
        wants = ["多头：等待现有多头入场形态（优先" + want_cn + "）"]
        for d in eval_details:
            if d.get("side") == "short" and d.get("setup") == "pullback":
                wants.append("空头：等待有效突破 + 回踩 + 确认（当前仅普通回踩）")
                break
        if len(wants) == 1:
            wants.append("空头：等待有效突破回踩（突破 + 回踩 + 确认）")
        return "；".join(wants[:2])

    def _recent_candles(self, period: str, inst_id: str | None = None) -> list[dict]:
        inst_id = inst_id or self.inst_id
        rows = db.query(
            "SELECT open_time as ts, o, h, l, c, vol FROM bars WHERE inst_id=? AND period=? "
            "ORDER BY open_time DESC LIMIT 120",
            (inst_id, period),
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

    def _update_loss_streak(self, inst_id: str | None = None) -> None:
        try:
            rt = compute_round_trips(venue=self.venue, inst_id=inst_id or self.inst_id, limit=1)
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
        last_px = self.data.last_price(self.inst_id)
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
                plan = se.plan_trade(candles, f2, side, round_px=lambda v: self.data.round_px(self.inst_id, v))
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
        # P0-4：auto_reduce_liq 提示（可用资金占比低）→ 新开仓再降 50%（保守，不自动平仓）
        if self.risk and (self.risk.state.get("reduce_hint")):
            mult *= 0.5
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
                "inst_id": self.inst_id, "side": order_side, "ord_type": "market",
                "sz_base": sz_base, "reduce_only": False, "source": "autopilot:open",
                "leverage": leverage,
                # P0-1：交易所侧止损——即使本地进程崩溃，该止损也在 OKX 侧生效
                "sl_trigger_px": self.data.round_px(self.inst_id, plan["sl"]),
            })
            # P0-4：等待订单终态（最多 6s），按实际成交回填持仓，防止 IOC 不成交产生幽灵持仓
            cl_ord_id = row.get("cl_ord_id")
            filled_sz, fill_px = 0.0, last_px
            if cl_ord_id:
                for _attempt in range(30):       # 30×200ms = 6s 总超时
                    await asyncio.sleep(0.2)
                    o = db.query_one("SELECT state, filled_sz, avg_px FROM orders WHERE cl_ord_id=?",
                                     (cl_ord_id,))
                    if not o:
                        continue
                    state = o.get("state", "")
                    if state in ("filled", "canceled", "partially_canceled", "failed",
                                 "partially_filled"):
                        filled_sz = float(o.get("filled_sz") or 0)
                        fill_px = float(o.get("avg_px") or fill_px) or fill_px
                        break
                    if state == "live":
                        continue   # 仍活跃，继续等
                if filled_sz <= 1e-12:
                    log.warning("autopilot 开仓单 %s 未成交（state=%s filled=%.6f），放弃开仓",
                                cl_ord_id, o.get("state") if o else "unknown", filled_sz)
                    self._notify("autopilot_unfilled", "warning",
                                 f"开仓单 {cl_ord_id} 未成交（IOC保护价外），放弃开仓",
                                 {"state": o.get("state") if o else "unknown", "cl_ord_id": cl_ord_id})
                    return
            entry = fill_px
            sz = filled_sz if filled_sz > 0 else float(row.get("sz") or sz_base)
            risk_dist = abs(entry - plan["sl"])
            if risk_dist <= 0:
                return
            sign = -1 if is_short else 1
            self.position = {
                "inst_id": self.inst_id,
                "side": side, "sz": sz,
                "orig_sz": sz,
                "entry_px": entry, "sl_px": self.data.round_px(self.inst_id, plan["sl"]),
                "risk_dist": risk_dist,
                "tp1_px": self.data.round_px(self.inst_id, entry + sign * TP1_R * risk_dist),
                "tp2_px": self.data.round_px(self.inst_id, entry + sign * TP2_R * risk_dist),
                "tp3_px": self.data.round_px(self.inst_id, entry + sign * 3.0 * risk_dist),
                "tp1_done": False, "tp2_done": False,
                "high_water": entry, "low_water": entry,
                "atr_pct": float(quality.get("atr_pct", 0)) or 0,
                "cl_ord_id": row.get("cl_ord_id"),
                "regime": quality.get("regime"), "signal_score": quality.get("signal_score"),
                "strategy_mode": cfg.get("strategy_mode", "official"),
                "guards_json": json.dumps(
                    [{"check": c.get("check"), "ok": c.get("ok"), "why": c.get("why")}
                     for c in (quality.get("checks") or [])], ensure_ascii=False),
                "htf_4h": quality.get("htf_4h") or quality.get("htf_1h"),
                "htf_1h": quality.get("htf_1h"),
                "risk_tier": _tier,
                "setup": quality.get("setup"), "open_ts": int(time.time() * 1000),
                "mfe_px": entry, "mae_px": entry,
                "venue": self.venue,
                "leverage": leverage,
                "margin": round(notional * mult, 4),
                "liq_px": self.data.round_px(self.inst_id, entry * (1 - sign * (1.0 / leverage - 0.005))) if leverage > 1 else None,
            }
            self.positions[self.inst_id]["bars_since_open"] = 0
            self.last_action = side
            day = time.strftime("%Y-%m-%d")
            if day != self._daily_key:
                self._daily_key = day
                self._daily_opens = 0
            self._daily_opens += 1
            self._sync_delegated()                 # P0-2：新增持仓 → 行情中心追订 ticker/candle
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
    async def _signal_exit_check(self, inst_id: str, _ts: int) -> None:
        """趋势反转退出：多单收盘跌破 EMA21+MACD 转负 / 空头收盘涨破 EMA21+MACD 转正。

        只在未到达 TP1 前生效（到达 TP2 后由 trailing 接管，不重复干预）。
        """
        pos = self.positions.get(inst_id)
        if not pos or pos.get("tp2_done"):
            return
        candles = self._recent_candles(self.config().get("period", "5m"), inst_id)
        if len(candles) < 40:
            return
        f = compute_factors(candles)
        if f.get("error"):
            return
        last_close = float(candles[-1]["c"])
        ema21 = float(f.get("ema_slow", 0))
        hist = float(f.get("macd_hist", 0))
        is_short = pos.get("side") == "short"
        if not is_short and last_close < ema21 * 0.999 and hist < 0:
            await self._close_position(
                inst_id,
                reason=f"Signal Exit：收盘 {last_close:.0f} 跌破 EMA21 {ema21:.0f} 且 MACD 转负")
        elif is_short and last_close > ema21 * 1.001 and hist > 0:
            await self._close_position(
                inst_id,
                reason=f"Signal Exit：收盘 {last_close:.0f} 涨破 EMA21 {ema21:.0f} 且 MACD 转正")

    # ---------- 平仓 ----------
    def _sz_venue_to_intent(self, inst_id: str, venue_sz: float) -> float:
        """venue 持仓口径 → place_intent 的 sz_base 口径（现货=币量；SWAP=张数→币量）。

        持仓对象 pos["sz"] 始终是 venue 口径（OKX SWAP 为张数），而下单管道
        normalize_sz 期望币量（SWAP 内部再折张），平仓提交前必须换算一次。
        """
        ins = self.data.instrument(inst_id) or {}
        if ins.get("instType") == "SWAP":
            ct = float(ins.get("ctVal") or 0)
            if ct > 0:
                return float(venue_sz) * ct
        return float(venue_sz)

    def _sz_intent_to_venue(self, inst_id: str, base_sz: float) -> float:
        """place_intent 的 sz_base 口径 → venue 持仓口径（_sz_venue_to_intent 的逆）。"""
        ins = self.data.instrument(inst_id) or {}
        if ins.get("instType") == "SWAP":
            ct = float(ins.get("ctVal") or 0)
            if ct > 0:
                return float(base_sz) / ct
        return float(base_sz)

    def _venue_pos_sz(self, inst_id: str) -> float | None:
        """venue 侧实际持仓量（venue 口径）：无持仓返回 0.0，读不到返回 None。"""
        try:
            acct = self._account_summary()
        except Exception as e:
            log.debug("venue 持仓读取失败 %s: %s", inst_id, e)
            return None
        for p in (acct.get("positions") or []):
            if (p.get("inst_id") or "") == inst_id:
                return float(p.get("sz") or 0)
        return 0.0

    def _post_close_size(self, inst_id: str, cur_venue: float, submit_venue: float,
                         filled_venue: float) -> float:
        """平仓后剩余持仓量（venue 口径）。

        优先用 venue 实际持仓（P0-4）；venue 快照尚未反映本次成交（OKX 私有 WS 有延迟）时，
        退回"按实际成交量扣减"（P0-3：绝不按未规格化的意向量记账）。
        """
        venue_sz = self._venue_pos_sz(inst_id)
        if venue_sz is not None and venue_sz < cur_venue - 1e-12:
            return max(0.0, venue_sz)
        used = filled_venue if filled_venue > 0 else submit_venue
        return max(0.0, cur_venue - used)

    async def _on_close_failed(self, inst_id: str, reason: str, err: Exception) -> None:
        """平仓失败统一出口（P0-2）：计数 → 告警 → 超限升级并触发交易所重同步。

        旧实现只在 place_intent 返回空/state=dead 时才进入重试；异常（例如数量被
        normalize_sz 以"低于最小下单量"抛出的 RiskBlocked）被 except 吞掉，
        止损失效在 UI 上完全静默——这是 ETH 残仓仓位长期卡住未被发现的原因之一。
        """
        retry = self._close_retries.get(inst_id, 0) + 1
        self._close_retries[inst_id] = retry
        log.error("autopilot 平仓失败 %s（第 %d 次）：%s", inst_id, retry, err)
        now_ms = int(time.time() * 1000)
        escalated = retry > MAX_CLOSE_RETRIES
        if escalated:
            # 升级告警不受限流抑制（失败已持续，必须让人看到）
            self._close_fail_notify_ts[inst_id] = now_ms
            self._notify("autopilot_close_failed", "error",
                         f"{inst_id} 平仓连续 {retry - 1} 次失败（{reason}）：{err}；"
                         f"已触发交易所持仓重同步，请人工核实该仓位",
                         {"inst_id": inst_id, "reason": reason, "error": str(err),
                          "retry": retry, "escalated": True})
        elif retry == 1 and now_ms - self._close_fail_notify_ts.get(inst_id, 0) >= 60_000:
            # 首次失败告警按标的限流 60s，避免 tick 级重试刷爆通知表
            self._close_fail_notify_ts[inst_id] = now_ms
            self._notify("autopilot_close_failed", "warning",
                         f"{inst_id} 平仓失败（{reason}）：{err}；将继续重试",
                         {"inst_id": inst_id, "reason": reason, "error": str(err),
                          "retry": retry, "escalated": False})
        if escalated:
            self._close_retries.pop(inst_id, None)   # 交给重同步收尾，重新计数
            await self._sync_from_exchange(inst_id)

    def _handle_untradable(self, inst_id: str, venue_sz: float, note: str) -> dict:
        """venue 侧存在"低于最小下单量、任何下单都平不掉"的残仓（P0-5/P0-6/P0-7）。

        - paper：交给 PaperEngine.purge_dust 由模拟盘自行改账清理（**不产生任何订单**）
        - okx  ：交易所残值无法用下单清掉 → 只记录并告警，**绝不当成托管持仓**
        两种情况都把本地托管仓位摘掉，避免 tick 循环反复尝试注定失败的平仓，
        也避免 has_position 永久阻塞新开仓。
        """
        self.positions.pop(inst_id, None)
        self._close_retries.pop(inst_id, None)
        self._sync_delegated()
        cleaned: list[dict] = []
        if self.venue != "okx" and hasattr(self.paper, "purge_dust"):
            try:
                cleaned = self.paper.purge_dust(inst_id)
            except Exception as e:
                log.warning("paper purge_dust 失败 %s: %s", inst_id, e)
        left = self._venue_pos_sz(inst_id)
        record = {
            "inst_id": inst_id, "sz": float(venue_sz), "note": note, "venue": self.venue,
            "cleaned": bool(cleaned), "remaining": None if left is None else float(left),
            "ts": int(time.time() * 1000),
        }
        self._dust_notes[inst_id] = record
        if inst_id not in self._dust_notified:
            self._dust_notified.add(inst_id)
            self._notify("autopilot_dust", "warning",
                         f"{inst_id} 残仓 {venue_sz:g} 低于最小下单量（{note}）："
                         + ("模拟盘已自动清理，不再占用持仓槽位"
                            if cleaned else
                            "交易所残值无法通过下单平掉，已停止托管以免阻塞开仓，请人工处理"),
                         record)
        log.warning("dust 残仓 %s sz=%s cleaned=%s note=%s", inst_id, venue_sz, bool(cleaned), note)
        return record

    async def _sync_exchange_sl(self, inst_id: str, pos: dict, reason: str) -> None:
        """本地动态止损变更后，把交易所侧止损同步过去（仅 okx live）。

        - paper 无交易所侧止损，直接跳过
        - 同价不重复下单；只允许朝有利方向移动（多头只上移、空头只下移），绝不回退
        - 复用已知 algo_id 免去每次查询；失败记录失败次数与时间戳，交由限流重试兜底
        - 失败不静默：error 日志 + 告警 + 状态记录在 pos["exch_sl"]（status() 里可见）
        """
        if self.venue != "okx":
            return
        if inst_id not in self.positions or float(pos.get("sz") or 0) <= 0:
            return                                   # 已清仓则无需保护
        sl = float(pos.get("sl_px") or 0)
        if sl <= 0:
            return
        ex = self._executor()
        sync = getattr(ex, "sync_position_sl", None)
        if sync is None:
            log.warning("交易所止损同步跳过 %s：下单引擎不支持 sync_position_sl", inst_id)
            return
        is_short = pos.get("side") == "short"
        prev = pos.get("exch_sl") or {}
        prev_px = float(prev.get("px") or 0)
        if prev.get("ok") and abs(prev_px - sl) < 1e-12:
            return                                   # 已同步到该价位
        if prev.get("ok") and prev_px > 0:
            # 单调性：多头只允许上移、空头只允许下移
            if (not is_short and sl <= prev_px) or (is_short and sl >= prev_px):
                log.warning("交易所止损同步被拒 %s：新价 %s 非有利方向（上次 %s）", inst_id, sl, prev_px)
                return
        try:
            res = await sync(inst_id, sl, pos_side=pos.get("side", "long"),
                             sz=float(pos.get("sz") or 0),
                             algo_id=(prev.get("algo_id") or None) if prev.get("ok") else None)
        except Exception as e:
            res = {"ok": False, "mode": None, "err": str(e)}
        ok = bool(res.get("ok"))
        prev_fail = int(prev.get("fail_count") or 0)
        pos["exch_sl"] = {"px": sl, "ok": ok, "reason": reason,
                          "ts": int(time.time() * 1000),
                          "mode": res.get("mode") or "",
                          "algo_id": res.get("algo_id") or (prev.get("algo_id") or ""),
                          "canceled": res.get("canceled", 0),
                          "cancel_errors": res.get("cancel_errors", 0),
                          "fail_count": 0 if ok else prev_fail + 1,
                          "err": (res.get("err") or "")[:200]}
        if ok:
            log.info("交易所止损已同步 %s → %s（%s，mode=%s 撤销旧单 %s 张）",
                     inst_id, sl, reason, res.get("mode"), res.get("canceled", 0))
            if res.get("cancel_errors"):
                log.warning("交易所止损同步 %s：新止损已挂上，但有 %s 张旧单撤销失败（下次同步会再纠正）",
                            inst_id, res.get("cancel_errors"))
        else:
            log.error("交易所止损同步失败 %s → %s（%s）：%s", inst_id, sl, reason, res.get("err"))
            self._notify("autopilot_sl_sync", "error",
                         f"{inst_id} 交易所侧止损同步失败（{reason}）：{res.get('err')}；"
                         f"本地止损仍为 {sl:g}，将在 {SL_SYNC_RETRY_SEC}s 后重试，请留意交易所侧保护",
                         {"inst_id": inst_id, "sl_px": sl, "reason": reason,
                          "err": res.get("err"), "venue": self.venue,
                          "fail_count": prev_fail + 1})

    async def _maybe_retry_exchange_sl(self, inst_id: str, pos: dict) -> None:
        """失败后的限流重试：只在"上次同步失败、SL 未再变化、且距上次尝试 ≥ 30s"时重试一次。

        平时的 tick 直接返回（不查询、不下单），保证不会每 tick 重复创建订单。
        """
        if self.venue != "okx":
            return
        st = pos.get("exch_sl") or {}
        if st.get("ok") or not st.get("px"):
            return                                   # 未失败 / 无记录 → 无需重试
        now_ms = int(time.time() * 1000)
        if now_ms - int(st.get("ts") or 0) < SL_SYNC_RETRY_SEC * 1000:
            return                                   # 同一次失败限流窗口内不重试
        sl = float(pos.get("sl_px") or 0)
        if abs(float(st.get("px") or 0) - sl) > 1e-12:
            return                                   # SL 已变化 → 交给正常同步路径
        log.warning("交易所止损同步重试 %s（第 %s 次失败后）", inst_id, int(st.get("fail_count") or 0))
        await self._sync_exchange_sl(inst_id, pos, st.get("reason") or "重试")

    async def _recover_exchange_sl(self, inst_id: str, pos: dict) -> None:
        """重启/收养后：从交易所读回已有止损单，写入 pos["exch_sl"]。

        否则重启后本地不知道交易所已有止损 → 单调性守卫失效、且可能重复挂单。
        仅在本地没有 exch_sl 记录时执行（每个仓位生命周期一次）。
        """
        if self.venue != "okx" or pos.get("exch_sl"):
            return
        if float(pos.get("sz") or 0) <= 0:
            return
        ex = self._executor()
        find = getattr(ex, "find_position_sl", None)
        if find is None:
            return
        try:
            found = await find(inst_id, pos.get("side", "long"))
        except Exception as e:
            log.warning("读取交易所已有止损失败 %s：%s", inst_id, e)
            return
        if found and found.get("algo_id"):
            pos["exch_sl"] = {"px": float(found.get("sl_trigger_px") or 0), "ok": True,
                              "reason": "重启恢复", "source": "recovered",
                              "algo_id": found["algo_id"], "ts": int(time.time() * 1000),
                              "mode": "", "canceled": 0, "cancel_errors": 0, "fail_count": 0, "err": ""}
            log.info("已恢复交易所止损记录 %s：algo=%s slTriggerPx=%s",
                     inst_id, found["algo_id"], found.get("sl_trigger_px"))

    async def _close_position(self, inst_id: str | None = None, reason: str = "",
                              sz: float | None = None) -> dict:
        """平仓：数量规格化防 dust + 失败可见 + 平仓后与 venue 对账。

        1) 平仓量先经 plan_close_sz 规划：分批低于最小下单量则跳过；平仓后会留下
           不可交易残值则整仓平（尾部归零）——从源头杜绝 dust 残仓。
        2) 下单异常不再被静默吞掉，统一走 _on_close_failed（重试/告警/重同步）。
        3) 成交后以 venue 实际持仓为准回填本地 sz（对不上则说明本地记账有误，交由
           _sync_from_exchange 纠正）。
        """
        inst_id = inst_id or self.inst_id
        pos = self.positions.get(inst_id)
        if not pos:
            return {"ok": False, "reason": "no_position"}
        cur_venue = float(pos.get("sz") or 0)
        if cur_venue <= 0:
            self.positions.pop(inst_id, None)
            self._sync_delegated()
            return {"ok": False, "reason": "zero_size"}
        # P0-5：venue 实际持仓是"大于 0 但低于最小下单量"的残值 → 直接按 dust 收尾，
        # 不再试注定被拒的下单。注意 venue 读数为 0 不在此列：那属于"幽灵持仓"，
        # 语义不同（也可能是 okx 快照滞后），仍走正常平仓→失败重试→重同步链路。
        venue_now = self._venue_pos_sz(inst_id)
        if venue_now is not None and venue_now > 0:
            _s, min_now, unit_now = self.data.spec_step(inst_id)
            if min_now > 0 and self._sz_venue_to_intent(inst_id, venue_now) < min_now - 1e-12:
                self._handle_untradable(
                    inst_id, venue_now,
                    f"venue 实际持仓低于最小下单量 {min_now:g} {unit_now}")
                return {"ok": False, "reason": "untradable"}
        is_short = pos.get("side") == "short"
        close_side = "buy" if is_short else "sell"
        dir_tag = "平空" if is_short else "平多"

        cur_base = self._sz_venue_to_intent(inst_id, cur_venue)
        want_base = None if sz is None else self._sz_venue_to_intent(
            inst_id, min(float(sz), cur_venue))
        plan = self.data.plan_close_sz(inst_id, cur_base, want_base)
        if plan["untradable"]:
            # 整仓低于最小下单量：任何下单都会被拒 → 不再空转，转 dust 流程
            self._handle_untradable(inst_id, cur_venue, plan["note"])
            return {"ok": False, "reason": "untradable", "note": plan["note"]}
        if plan["submit"] <= 0:
            log.info("平仓跳过 %s（%s）：%s", inst_id, reason, plan["note"])
            return {"ok": False, "reason": "skipped", "note": plan["note"]}

        submit_base = float(plan["submit"])
        submit_venue = self._sz_intent_to_venue(inst_id, submit_base)
        try:
            row = await self._executor().place_intent({
                "inst_id": inst_id, "side": close_side, "ord_type": "market",
                "sz_base": submit_base, "reduce_only": True, "source": "autopilot:close",
                "leverage": int(pos.get("leverage", 1) or 1),
            })
        except Exception as e:
            await self._on_close_failed(inst_id, reason, e)
            return {"ok": False, "reason": "exception", "error": str(e)}
        if not row or row.get("state") == "dead":
            await self._on_close_failed(inst_id, reason, RuntimeError("平仓单未被接受（state=dead/空）"))
            return {"ok": False, "reason": "rejected"}

        # ---- 成交成功：重置失败计数，并按实际成交/venue 持仓回填本地数量 ----
        self._close_retries.pop(inst_id, None)
        self._close_fail_notify_ts.pop(inst_id, None)
        filled_venue = 0.0
        cl_ord_id = row.get("cl_ord_id")
        if cl_ord_id:
            o = db.query_one("SELECT filled_sz FROM orders WHERE cl_ord_id=?", (cl_ord_id,))
            if o:
                filled_venue = float(o.get("filled_sz") or 0)
        new_venue = self._post_close_size(inst_id, cur_venue, submit_venue, filled_venue)
        _step, min_sz, unit = self.data.spec_step(inst_id)
        new_base = self._sz_venue_to_intent(inst_id, new_venue)

        if new_venue <= 0 or (min_sz > 0 and new_base < min_sz - 1e-12):
            if new_venue > 0:
                # 平掉后才暴露出"剩余低于最小下单量"→ 立即按 dust 收尾，不留给下一轮
                self._handle_untradable(inst_id, new_venue,
                                        f"平仓后剩余 {new_venue:g} {unit} 低于最小下单量 {min_sz:g}")
                return {"ok": True, "fully": True, "filled": submit_venue, "dust": True}
            self._last_close_ts = int(time.time() * 1000)
            self._update_loss_streak(inst_id)
            self._journal_close(inst_id, pos, reason)   # 落库用的仍是平仓前的 sz/orig_sz
            self.positions.pop(inst_id, None)
            self._sync_delegated()                 # P0-2：持仓清空 → 退订该标的行情频道
            if inst_id == self.inst_id:
                self.last_action = "flat"
            self._notify("autopilot_close", "info",
                         f"{dir_tag} {submit_venue:.6f}（全部，{reason}）",
                         {"reason": reason, "sz": submit_venue, "fully": True, "side": pos.get("side")})
            log.info("autopilot %s sz=%.6f reason=%s fully=True", dir_tag, submit_venue, reason)
            return {"ok": True, "fully": True, "filled": submit_venue}

        pos["sz"] = new_venue                        # P0-3：本地数量=venue 实际剩余
        self._notify("autopilot_close", "info",
                     f"{dir_tag} {submit_venue:.6f}（部分，{reason}）",
                     {"reason": reason, "sz": submit_venue, "fully": False,
                      "side": pos.get("side"), "left": new_venue})
        log.info("autopilot %s sz=%.6f reason=%s fully=False left=%.6f",
                 dir_tag, submit_venue, reason, new_venue)
        return {"ok": True, "fully": False, "filled": submit_venue, "left": new_venue}

    async def _sync_from_exchange(self, inst_id: str) -> None:
        """P0-4：从交易所账户同步指定标的的持仓状态，纠正本地幽灵持仓。

        当平仓连续失败 MAX_CLOSE_RETRIES 次后调用此方法，以交易所真实状态
        覆盖本地 positions（或清仓后重新 discover）。"""
        log.warning("autopilot _sync_from_exchange: 开始同步 %s", inst_id)
        try:
            acct = await self._account_summary()
        except Exception as e:
            log.error("_sync_from_exchange 获取账户失败: %s", e)
            return
        # 查找交易所侧该标的的仓位
        exchange_pos = None
        for p in (acct.get("positions") or []):
            if p.get("inst_id") == inst_id and float(p.get("pos") or 0) != 0:
                exchange_pos = {
                    "inst_id": inst_id,
                    "side": p.get("pos_side", "long"),
                    "sz": float(p.get("pos") or 0),
                    "entry_px": float(p.get("avg_px") or 0),
                }
                break
        local_pos = self.positions.get(inst_id)
        if exchange_pos is None:
            if local_pos:
                log.warning("_sync_from_exchange: 交易所 %s 无仓位，清除本地", inst_id)
                self.positions.pop(inst_id, None)
                self._sync_delegated()
            return
        if local_pos:
            local_pos["sz"] = exchange_pos["sz"]
            local_pos["entry_px"] = exchange_pos.get("entry_px", local_pos["entry_px"])
            log.info("_sync_from_exchange: %s 已对齐 sz=%.6f", inst_id, exchange_pos["sz"])
        else:
            log.warning("_sync_from_exchange: 交易所 %s 有仓位但本地无，收养", inst_id)
            self._discover_position(acct)
            self._sync_delegated()

    # ---------- 持仓管理（tick 级，优先级从高到低，全本地确定性） ----------
    async def _manage_position(self, inst_id: str, px: float) -> None:
        pos = self.positions.get(inst_id)
        if not pos or not px:
            return
        is_short = pos.get("side") == "short"

        # P0-5/P0-6：持仓量已低于最小下单量 → 止损/止盈下单注定被拒，转 dust 收尾，
        # 不在 tick 上反复空转（否则每 tick 一次注定失败的下单 + 一条 error 日志）。
        _step, _min_sz, _unit = self.data.spec_step(inst_id)
        _venue_sz = float(pos.get("sz") or 0)
        if _min_sz > 0 and self._sz_venue_to_intent(inst_id, _venue_sz) < _min_sz - 1e-12:
            self._handle_untradable(inst_id, _venue_sz, f"低于最小下单量 {_min_sz:g} {_unit}")
            return

        # 交易所止损同步失败后的限流重试（平时立即返回，不查询也不下单）
        await self._maybe_retry_exchange_sl(inst_id, pos)

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
            await self._close_position(inst_id, reason=f"{reason} @ {px:.2f}")
            return
        # 2. TP1：1R 平 25% + 止损推保本
        tp1_hit = (px <= pos["tp1_px"]) if is_short else (px >= pos["tp1_px"])
        if not pos["tp1_done"] and tp1_hit:
            pos["tp1_done"] = True
            pos["sl_px"] = pos["entry_px"]  # 保本
            await self._close_position(inst_id, reason=f"TP1 到达 1R 平 25% @ {px:.2f}",
                                       sz=pos["orig_sz"] * TP1_PCT)
            await self._sync_exchange_sl(inst_id, pos, "TP1 保本")   # 交易所侧止损跟随到保本
            return
        # 3. TP2：2R 平 35% + 激活 trailing
        tp2_hit = (px <= pos["tp2_px"]) if is_short else (px >= pos["tp2_px"])
        if not pos["tp2_done"] and tp2_hit:
            pos["tp2_done"] = True
            await self._close_position(inst_id, reason=f"TP2 到达 2R 平 35% @ {px:.2f}",
                                       sz=pos["orig_sz"] * TP2_PCT)
            return
        # 4. trailing（TP2 后）
        if pos["tp2_done"]:
            atr_abs = pos["atr_pct"] / 100 * pos["entry_px"] if pos["atr_pct"] else pos["risk_dist"] / 1.8
            if is_short:
                trail = min(pos["entry_px"], pos["low_water"] + TRAIL_ATR * atr_abs)
                old_sl = pos["sl_px"]
                pos["sl_px"] = min(pos["sl_px"], trail)  # 空头止损只下移
                if px >= trail:
                    await self._close_position(inst_id, reason=f"ATR trailing @ {px:.2f} ≥ {trail:.2f}")
                    return
                if pos["sl_px"] != old_sl:               # 仅真正变化时同步交易所侧
                    await self._sync_exchange_sl(inst_id, pos, "ATR trailing")
            else:
                trail = max(pos["entry_px"], pos["high_water"] - TRAIL_ATR * atr_abs)
                old_sl = pos["sl_px"]
                pos["sl_px"] = max(pos["sl_px"], trail)  # 多头止损只上移
                if px <= trail:
                    await self._close_position(inst_id, reason=f"ATR trailing @ {px:.2f} ≤ {trail:.2f}")
                    return
                if pos["sl_px"] != old_sl:               # 仅真正变化时同步交易所侧
                    await self._sync_exchange_sl(inst_id, pos, "ATR trailing")
        # 5. 3R 全平
        tp3_hit = (px <= pos["tp3_px"]) if is_short else (px >= pos["tp3_px"])
        if pos["tp1_done"] and tp3_hit:
            await self._close_position(inst_id, reason=f"TP3 到达 3R 清仓 @ {px:.2f}")
            return
        # 6. 时间止损
        floating_loss = (px > pos["entry_px"] * 1.001) if is_short else (px < pos["entry_px"] * 0.999)
        if not pos["tp1_done"] and pos.get("bars_since_open", 0) >= TIME_STOP_BARS and floating_loss:
            await self._close_position(inst_id, reason=f"时间止损：{pos.get('bars_since_open', 0)} 根 bar 未达 TP1 且浮亏")

    # ---------- 交易归因落库（P2-10 MFE/MAE journal） ----------
    def _journal_close(self, inst_id: str, pos: dict, reason: str) -> None:
        """平仓后写入 trade_journal：每笔真实交易可解释"为什么开/为什么平/最大浮盈浮亏"。"""
        try:
            entry = float(pos.get("entry_px") or 0)
            exit_px = self.data.last_price(inst_id) or entry
            is_short = pos.get("side") == "short"
            risk_dist = float(pos.get("risk_dist") or 0)
            mfe_px = float(pos.get("mfe_px") or entry)
            mae_px = float(pos.get("mae_px") or entry)
            mfe_r = abs(mfe_px - entry) / risk_dist if risk_dist > 0 else 0.0
            mae_r = abs(mae_px - entry) / risk_dist if risk_dist > 0 else 0.0
            sign = -1 if is_short else 1
            pnl = (exit_px - entry) * sign * float(pos.get("sz") or 0)
            db.execute(
                "INSERT INTO trade_journal (ts, venue, inst_id, side, entry_px, exit_px, sz,"
                " sl_px, tp1_px, tp2_px, mfe_px, mae_px, mfe_r, mae_r, pnl, fees, leverage,"
                " risk_tier, regime, setup, score, htf_4h, htf_1h, exit_reason, hold_bars, open_ts,"
                " strategy_mode, guards_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(time.time() * 1000), self.venue, inst_id, pos.get("side"),
                 round(entry, 8), round(exit_px, 8), float(pos.get("orig_sz") or pos.get("sz") or 0),
                 pos.get("sl_px"), pos.get("tp1_px"), pos.get("tp2_px"),
                 round(mfe_px, 8), round(mae_px, 8), round(mfe_r, 3), round(mae_r, 3),
                 round(pnl, 6), 0.0, int(pos.get("leverage") or 1), pos.get("risk_tier", "-"),
                 pos.get("regime", "-"), pos.get("setup", "-"), pos.get("signal_score"),
                 pos.get("htf_4h"), pos.get("htf_1h"), reason,
                 int(pos.get("bars_since_open") or 0), int(pos.get("open_ts") or 0),
                 pos.get("strategy_mode", "official"), pos.get("guards_json")),
            )
        except Exception as e:
            log.debug("trade_journal 写入失败: %s", e)

    # ---------- 持久化 / 通知 ----------
    def _notify(self, rule_type: str, level: str, body: str, detail: dict) -> None:
        """通知落库：body 存中文文案（用户可读），结构化 detail 只在事件总线发布。"""
        ts = int(time.time() * 1000)
        db.execute(
            "INSERT INTO notifications (ts, level, title, body) VALUES (?,?,?,?)",
            (ts, level, f"自动驾驶：{rule_type}", body),
        )
        event_bus.publish("log", {"ts": ts, "level": level, "msg": body, "detail": detail})

    # ---------- 状态查询 ----------
    def reset_throttle(self) -> None:
        self._last_close_ts = 0
        self._daily_key = ""
        self._daily_opens = 0
        self._loss_streak = 0
        self._sleep_until = 0
        self._paper_peak_equity = 0.0
        self._peak_venue = None
        self.positions = {}
        self._close_retries.clear()
        self._close_fail_notify_ts.clear()
        self._sync_delegated()                     # P0-2：重置后同步退订行情

    def on_venue_switch(self) -> None:
        """切换 venue 时调用：清仓位追踪 + 重置节流，各 venue 独立。
        然后立即发现新 venue 已有持仓（不等下一个 bar）。
        按新 venue 的 enabled 状态启动/停止 autopilot 任务。
        注意：venue 切换会改变下单引擎（paper/OKX），旧 venue 持仓不在此托管，
        切回该 venue 时会再次发现收养。"""
        self.positions = {}
        self._sync_delegated()                     # P0-2：清仓后同步退订行情
        self._last_close_ts = 0
        self._daily_key = ""
        self._daily_opens = 0
        self._loss_streak = 0
        self._sleep_until = 0
        self._paper_peak_equity = 0.0
        self._peak_venue = None
        # dust 记录按 venue 隔离：切走时清掉旧 venue 的，避免误报/混淆
        self._dust_notes.clear()
        self._dust_notified.clear()
        self._close_retries.clear()
        self._close_fail_notify_ts.clear()
        # 立即发现新 venue 已有持仓
        try:
            acct = self._account_summary()
            self._discover_position(acct)
            self._sync_delegated()                 # P0-2：收养持仓后同步行情订阅
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

    def on_instrument_switch(self, prev_inst: str) -> None:
        """切换驾驶标的时调用（安全语义）：
        1. 旧标的已托管持仓【继续】由 tick/bar 循环止损止盈（循环按事件 inst 路由）；
        2. 切换后自动刹车：enabled=False（不开新仓），需顶栏人工确认开启；
        3. 立即收养新标的存在持仓（防切换后漏管/误重复开仓）；
        4. 清空因子缓存：status() 会即时重算（避免长时间"数据不足"）。"""
        self.last_factors = {}
        cfg = self.config()
        if cfg.get("enabled"):
            cfg["enabled"] = False
            db.set_setting(f"autopilot_config_{self.venue}", cfg)
            log.info("标的切换 %s → %s：autopilot 已自动刹车（需人工确认开启）", prev_inst, self.inst_id)
        try:
            acct = self._account_summary()
            self._discover_position(acct)
            self._sync_delegated()                 # P0-2：切换标的后同步行情订阅
        except Exception:
            log.debug("on_instrument_switch: 持仓发现延迟到 _decide_and_act")
        self._notify("autopilot_inst_switch", "info",
                     f"驾驶标的已切换：{prev_inst} → {self.inst_id}（自动驾驶已刹车，需手动开启；"
                     f"旧标的持仓由自动托管继续管理）",
                     {"prev_inst": prev_inst, "inst_id": self.inst_id})

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
        # 兜底：进程重启/切换标的后 last_factors 为空 → 即时按当前 K 线重算一次，
        # 避免前端长时间显示"数据不足"（真实数据不足时仍返回 unknown）。
        if regime is None:
            try:
                candles = self._recent_candles(cfg.get("period", "5m"))
                if len(candles) >= 30:
                    f2 = compute_factors(candles)
                    if not f2.get("error"):
                        self.last_factors = f2
                        regime = f2.get("regime")
            except Exception as e:
                log.debug("status regime 兜底失败: %s", e)
        return {
            "enabled": bool(cfg.get("enabled")),
            "running": self.is_running(),
            "inst_id": self.inst_id,
            "period": cfg.get("period", "5m"),
            "engine": "rules_v3",
            "venue": self.venue,
            "live_ready": self.venue != "okx" or self._live_ready(),
            "last_action": self.last_action,
            "regime": regime,
            "regime_reason": self.last_factors.get("regime_reason", ""),
            "last_error": "",
            "position": self.position,
            "positions": [
                {
                    "inst_id": k, "side": v.get("side"), "sz": v.get("sz"),
                    "entry_px": v.get("entry_px"), "sl_px": v.get("sl_px"),
                    "tp1_px": v.get("tp1_px"), "open_ts": v.get("open_ts"),
                    "setup": v.get("setup"),
                }
                for k, v in self.positions.items()
            ],
            # 低于最小下单量、无法下单平掉的残仓（不参与托管，仅供前端/排障可见）
            "dust": [dict(v) for v in self._dust_notes.values()],
            "throttle": self.throttle_status(),
            "mode": cfg.get("mode", "normal"),
            "strategy_mode": cfg.get("strategy_mode", "official"),
            "leverage": int(cfg.get("leverage", 1) or 1),
            "leverage_cap": getattr(self.risk, "lever_cap", config.LEVERAGE_CAP)
              if self.risk else config.LEVERAGE_CAP,
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