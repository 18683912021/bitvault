"""V2 纯规则决策链回测：复用 factor_engine + signal_engine（与实盘 autopilot 同一决策链）。

时序纪律（严格无未来函数 / Look-ahead）：
- bar i 收盘 → 用 bars[i-warmup+1..i] 计算因子/评分/守卫 → 产生开仓/退出意图
- 开仓成交 = bar i+1 开盘价 ×(1±slippage)（taker 市价）
- Signal Exit / Time Exit 在 bar i 收盘判定 → bar i+1 开盘价成交
- 持仓管理逐 bar 保守模拟：SL 优先于 TP（同一根 bar 双触发按止损计）
- ATR trailing 本根更新、下一根生效（消除 bar 内高低点顺序的乐观假设）
- HTF(4H/1H) 只用"当前 bar 收盘时刻之前已收盘"的 HTF bar 计算 trend

成本模型（SPOT，与 signal_engine 常量一致）：
- taker 手续费 0.10%/边；滑点 0.02%/边；SPOT 无资金费率（funding=0）
- 单位成本含买入费（与 paper 引擎现金法一致），realized/expectancy 天然含费

参数：BTParams 可覆盖 signal_engine 常量（Walk-Forward 参数稳定区扫描用）。
输出：metrics + 每笔交易全字段 journal（MFE/MAE/exit_reason/regime/…）+ equity curve。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, asdict, field

from app.brain import signal_engine as se
from app.brain.factor_engine import compute_factors, trend_of

WARMUP = 120          # 与 autopilot LIMIT 120 一致
PERIOD_MS = {"5m": 300_000, "15m": 900_000, "1H": 3_600_000, "4H": 14_400_000, "1D": 86_400_000}


def mid_htf_period(period: str) -> str:
    """交易周期的中期 HTF 周期。

    1H 交易用 1D（真正的更高周期，避免与交易周期同频自证 → HTF 评分虚高）；
    5m/15m 交易用 1H。4H 交易的 mid HTF 此处不涉及（生产不用 4H 交易）。
    """
    return "1D" if period == "1H" else "1H"


@dataclass
class BTParams:
    """回测参数（Walk-Forward 扫描对象；默认值 = V2 生产值）。"""
    score_min: int = 70
    rr_min: float = 2.0
    cost_cover_x: float = 4.0
    sl_atr_trend: float = 1.8
    sl_atr_range: float = 1.2
    tp1_r: float = 1.0
    tp1_pct: float = 0.25
    tp2_r: float = 2.0
    tp2_pct: float = 0.35
    tp3_r: float = 3.0
    trail_atr: float = 2.5
    time_stop_bars: int = 120
    risk_pct: float = 0.005
    cooldown_bars: int = 3         # 15min / 5m
    max_opens_per_day: int = 20
    loss_pause_n: int = 3
    loss_pause_bars: int = 12      # 60min / 5m
    allow_short: bool = True       # SPOT 实盘只执行 long；short 信号仅统计评估
    require_setup: bool = False    # True: 无形态（none）禁止开仓（实验 A）
    signal_exit_only_loss: bool = False  # True: Signal Exit 仅在浮亏时触发（实验 B）
    signal_exit_bars: int = 2   # Signal Exit 需连续 N 根收盘<EMA21（过滤噪音；=1 旧逻辑）
    setup_filter: str = ""     # 非空则只允许该 setup（"breakout_retest"|"pullback"）
    fee_taker: float = 0.001
    slippage: float = 0.0002
    initial_equity: float = 10_000.0
    max_order_usdt: float = 200.0


def _apply_params(p: BTParams) -> dict:
    """把 BTParams 覆盖到 signal_engine 模块常量，返回原始值供恢复。"""
    orig = {
        "SCORE_MIN": se.SCORE_MIN, "RR_MIN": se.RR_MIN,
        "COST_COVER_X": se.COST_COVER_X, "SL_ATR_TREND": se.SL_ATR_TREND,
        "SL_ATR_RANGE": se.SL_ATR_RANGE, "RISK_PER_TRADE_PCT": se.RISK_PER_TRADE_PCT,
    }
    se.SCORE_MIN = int(p.score_min)
    se.RR_MIN = float(p.rr_min)
    se.COST_COVER_X = float(p.cost_cover_x)
    se.SL_ATR_TREND = float(p.sl_atr_trend)
    se.SL_ATR_RANGE = float(p.sl_atr_range)
    se.RISK_PER_TRADE_PCT = float(p.risk_pct)
    return orig


def _restore_params(orig: dict) -> None:
    """回测结束后恢复 signal_engine 模块常量（防止污染实盘）。"""
    se.SCORE_MIN = orig["SCORE_MIN"]
    se.RR_MIN = orig["RR_MIN"]
    se.COST_COVER_X = orig["COST_COVER_X"]
    se.SL_ATR_TREND = orig["SL_ATR_TREND"]
    se.SL_ATR_RANGE = orig["SL_ATR_RANGE"]
    se.RISK_PER_TRADE_PCT = orig["RISK_PER_TRADE_PCT"]


def _macd_hist_lite(closes: list[float]) -> float:
    """轻量 MACD hist（12/26/9），与 factor_engine._macd 同参数。"""
    if len(closes) < 40:
        return 0.0
    src = closes[-60:]
    ema12 = ema26 = src[0]
    diffs = []
    for v in src[1:]:
        ema12 = v * 2 / 13 + ema12 * 11 / 13
        ema26 = v * 2 / 27 + ema26 * 25 / 27
        diffs.append(ema12 - ema26)
    sig = diffs[0]
    for d in diffs[1:]:
        sig = d * 2 / 10 + sig * 8 / 10
    return diffs[-1] - sig


@dataclass
class _Pos:
    round_id: int
    qty: float
    entry: float          # 实际成交价（含滑点）
    unit_cost: float      # entry × (1+fee)，现金法单位成本
    sl: float
    risk_dist: float
    tp1: float
    tp2: float
    tp3: float
    side: str = "long"    # "long" 或 "short"
    tp1_done: bool = False
    tp2_done: bool = False
    entry_i: int = 0
    entry_ts: int = 0
    high_water: float = 0.0
    low_water: float = 0.0
    mfe_px: float = 0.0
    mae_px: float = 0.0
    atr_abs: float = 0.0
    meta: dict = field(default_factory=dict)
    fees_paid: float = 0.0


class _Sim:
    """回测状态机：cash / 持仓 / 节流 / journal 全部集中在实例上。"""

    def __init__(self, p: BTParams):
        self.p = p
        self.cash = p.initial_equity
        self.pos: _Pos | None = None
        self.journal: list[dict] = []
        self.fee_total = 0.0
        self.slip_total = 0.0
        self.loss_streak = 0
        self.last_close_i = -10**9
        self.daily_key = ""
        self.daily_opens = 0
        self.sleep_until_i = -1
        self._i = 0
        self._round_seq = 0

    # ---- 平仓成交（px 已含滑点）----
    # long: realized = 卖出净得 - 单位成本×qty（含开仓费）
    # short: realized = 开仓所得 - 买回成本（含双边费）
    def _fill_sell(self, qty: float, px: float, reason: str, ts: int, close_pos: bool,
                   pos: _Pos) -> float:
        is_short = pos.side == "short"
        if is_short:
            cost = qty * px                          # 买回花费
            fee = cost * self.p.fee_taker
            self.fee_total += fee
            pos.fees_paid += fee
            realized = pos.unit_cost * qty - cost - fee   # (entry*(1+fee) - close - fee) * qty
            self.cash -= cost + fee                       # 买回付出
        else:
            proceeds = qty * px
            fee = proceeds * self.p.fee_taker
            self.fee_total += fee
            pos.fees_paid += fee
            realized = proceeds - fee - pos.unit_cost * qty
            self.cash += proceeds - fee
        pos.qty -= qty
        m = pos.meta
        rd = pos.risk_dist
        rec = {
            "timestamp": ts, "symbol": "BTC-USDT",
            "side": "SHORT" if is_short else "LONG",
            "round_id": pos.round_id,
            "regime": m.get("regime"), "entry": round(pos.entry, 2),
            "stopLoss": round(pos.sl, 2),
            "takeProfit": round(pos.tp3, 2),
            "positionSize": round(qty * px, 2), "leverage": 1,
            "riskReward": m.get("rr"), "entryScore": m.get("score"),
            "atr_pct": m.get("atr_pct"), "adx": m.get("adx"),
            "volumeRatio": m.get("volume_ratio"), "rsi": m.get("rsi"),
            "htf_4h": m.get("htf_4h"), "htf_1h": m.get("htf_1h"),
            "setup": m.get("setup"), "sl_basis": m.get("sl_basis"),
            "exitReason": reason, "exitPrice": round(px, 2),
            "pnl": round(realized, 4),
            "fee_close": round(fee, 4), "fee_open_share": round(m.get("fee_open", 0.0), 4),
            "funding": 0.0,
            "slippage_est": round((qty * px + qty * pos.entry) * self.p.slippage, 4),
            "mfe_r": round((pos.mfe_px - pos.entry) / rd, 3) if rd > 0 else 0,
            "mae_r": round((pos.mae_px - pos.entry) / rd, 3) if rd > 0 else 0,
            "holdingBars": self._i - pos.entry_i,
            "holdingTimeMin": round((ts - pos.entry_ts) / 60_000, 1),
        }
        self.journal.append(rec)
        if close_pos:
            self.loss_streak = self.loss_streak + 1 if realized < 0 else 0
            self.last_close_i = self._i
            n = int(self.p.loss_pause_n)
            if n > 0 and self.loss_streak >= n:
                self.sleep_until_i = self._i + int(self.p.loss_pause_bars)
                self.loss_streak = 0
            self.pos = None
        return realized

    def open_pos(self, notional: float, fill: float, plan: dict, score: int,
                 setup: dict, factors: dict, htf4h, htf1h, ts: int, i: int,
                 side: str = "long") -> None:
        is_short = side == "short"
        fee = notional * self.p.fee_taker
        qty = (notional - fee) / fill
        self.fee_total += fee
        sign = -1 if is_short else 1
        risk_dist = abs(fill - plan["sl"])
        self._round_seq += 1
        if is_short:
            self.cash += notional - fee        # 做空收到资金
            unit_cost = fill * (1 - self.p.fee_taker)   # 净收入/BTC
        else:
            self.cash -= notional              # 做多付出资金
            unit_cost = fill * (1 + self.p.fee_taker)   # 含费成本/BTC
        self.pos = _Pos(
            round_id=self._round_seq, qty=qty, entry=fill,
            unit_cost=unit_cost,
            sl=plan["sl"], risk_dist=risk_dist, side=side,
            tp1=fill + sign * self.p.tp1_r * risk_dist,
            tp2=fill + sign * self.p.tp2_r * risk_dist,
            tp3=fill + sign * self.p.tp3_r * risk_dist,
            entry_i=i, entry_ts=ts,
            high_water=fill, low_water=fill,
            mfe_px=fill, mae_px=fill,
            atr_abs=factors["atr_pct"] / 100 * fill,
            meta={
                "qty0": qty, "score": score, "regime": factors.get("regime"),
                "setup": setup.get("setup"), "rr": plan["rr"],
                "atr_pct": factors.get("atr_pct"), "adx": factors.get("adx_14"),
                "volume_ratio": factors.get("volume_ratio"), "rsi": factors.get("rsi_14"),
                "htf_4h": htf4h, "htf_1h": htf1h,
                "sl_basis": plan.get("sl_basis"),
                "ema21": factors.get("ema_slow"),
                "fee_open": fee,
            },
        )
        self.slip_total += notional * self.p.slippage

    def close_part(self, qty: float, px: float, reason: str, ts: int) -> None:
        self._fill_sell(qty, px, reason, ts, close_pos=False, pos=self.pos)

    def close_full(self, px: float, reason: str, ts: int) -> None:
        self._fill_sell(self.pos.qty, px, reason, ts, close_pos=True, pos=self.pos)


def run_rules_backtest(bars: list[dict], bars_4h: list[dict], bars_1h: list[dict],
                       params: BTParams | None = None, period: str = "5m",
                       start_ms: int | None = None,
                       end_ms: int | None = None) -> dict:
    """主入口。bars 为升序 [{ts,o,h,l,c,vol}]。

    start_ms：交易/净值统计起点（之前的数据只作指标 warmup，不计收益）。
    end_ms  ：交易/净值统计终点（达到即停止交易并强平尾仓）。
              Holdout 守卫：开发脚本传 dev_end_ms(period) 截断，确保不触达 Holdout 段。
    返回 {metrics, journal, equity, params}。
    """
    p = params or BTParams()
    _apply_params(p)
    pm = PERIOD_MS[period]

    # ---- HTF 趋势预计算：trend 只依赖已收盘 HTF bar（收盘时刻 = open_time + period_ms） ----
    def _htf_series(rows: list[dict], htf_ms: int) -> list[tuple[int, str]]:
        out = []
        for j in range(len(rows)):
            if j < 23:
                continue
            t = trend_of(rows[max(0, j - 59):j + 1])       # 与 _htf_context 的 LIMIT 60 一致
            out.append((rows[j]["ts"] + htf_ms, t))
        return out

    htf4 = _htf_series(bars_4h, PERIOD_MS["4H"])
    # mid HTF：1H 交易用 1D（避免同频自证），其余用 1H。调用方须加载对应周期的 bars 到 bars_1h 槽位
    htf1 = _htf_series(bars_1h, PERIOD_MS[mid_htf_period(period)])

    def _htf_at(series: list[tuple[int, str]], close_ms: int) -> str | None:
        lo, hi, ans = 0, len(series) - 1, None
        while lo <= hi:
            mid = (lo + hi) // 2
            if series[mid][0] <= close_ms:
                ans = series[mid][1]
                lo = mid + 1
            else:
                hi = mid - 1
        return ans

    sim = _Sim(p)
    equity_curve: list[dict] = []
    peak_eq = p.initial_equity
    pending_exit: str | None = None
    trailing_apply: float | None = None
    bars_since_open = 0
    last_dev_i = -1                  # 最后一个开发窗口 bar 的索引（尾仓强平用）

    n = len(bars)
    for i in range(WARMUP, n):
        bar = bars[i]
        o, h, l, c = bar["o"], bar["h"], bar["l"], bar["c"]
        ts = bar["ts"]
        sim._i = i
        trading = (start_ms is None or ts >= start_ms) and (end_ms is None or ts < end_ms)
        pos = sim.pos

        if not trading:
            if end_ms is not None and ts >= end_ms:
                break                  # 开发窗口结束：停止迭代（Holdout 守卫）
            continue                       # warmup 区间：不做交易、不记净值
        last_dev_i = i

        # ============ 1. 开盘：执行上一根收盘判定的 signal/time exit ============
        if pos and pending_exit:
            if pos.side == "short":
                sim.close_full(o * (1 + p.slippage), pending_exit, ts)   # 买回，滑点加价
            else:
                sim.close_full(o * (1 - p.slippage), pending_exit, ts)
            pos, pending_exit = None, None

        # ============ 2. bar 内持仓管理（保守顺序：SL 优先于 TP） ============
        if pos:
            is_short = pos.side == "short"
            pos.high_water = max(pos.high_water, h)
            pos.low_water = min(pos.low_water, l)
            # MFE/MAE: 多头 MFE=最高价 MAE=最低价；空头 MFE=最低价 MAE=最高价
            if is_short:
                pos.mfe_px = min(pos.mfe_px, l)
                pos.mae_px = max(pos.mae_px, h)
            else:
                pos.mfe_px = max(pos.mfe_px, h)
                pos.mae_px = min(pos.mae_px, l)

            if trailing_apply is not None:              # 上一根更新的 trailing 生效
                pos.sl = min(pos.sl, trailing_apply) if is_short else max(pos.sl, trailing_apply)
                trailing_apply = None

            sl_hit = (h >= pos.sl) if is_short else (l <= pos.sl)
            if sl_hit:                                  # 止损（含开盘 gap）
                px = (max(o, pos.sl) if is_short else min(o, pos.sl))
                px *= (1 + p.slippage) if is_short else (1 - p.slippage)
                reason = "保本止损" if (pos.sl <= pos.entry if is_short else pos.sl >= pos.entry) else "止损"
                sim.close_full(px, reason, ts)
                pos = None
            elif not pos.tp1_done and ((l <= pos.tp1) if is_short else (h >= pos.tp1)):
                tp1_px = pos.tp1 * (1 + p.slippage) if is_short else pos.tp1 * (1 - p.slippage)
                sim.close_part(pos.meta["qty0"] * p.tp1_pct, tp1_px, "TP1 1R 平25%", ts)
                pos.tp1_done = True
                pos.sl = pos.entry                     # 推保本（多空一致）
            elif not pos.tp2_done and ((l <= pos.tp2) if is_short else (h >= pos.tp2)):
                tp2_px = pos.tp2 * (1 + p.slippage) if is_short else pos.tp2 * (1 - p.slippage)
                sim.close_part(pos.meta["qty0"] * p.tp2_pct, tp2_px, "TP2 2R 平35%", ts)
                pos.tp2_done = True
            elif pos.tp1_done and ((l <= pos.tp3) if is_short else (h >= pos.tp3)):
                tp3_px = pos.tp3 * (1 + p.slippage) if is_short else pos.tp3 * (1 - p.slippage)
                sim.close_full(tp3_px, "TP3 3R 清仓", ts)
                pos = None

        # ============ 3. 收盘级：signal exit / time exit（下一根开盘执行） ============
        if pos and pending_exit is None:
            bars_since_open += 1
            hist = _macd_hist_lite([b["c"] for b in bars[i - 59: i + 1]])
            ema21 = pos.meta["ema21"]
            is_short = pos.side == "short"
            if is_short:
                above_n = all(bars[i - k]["c"] > ema21 * 1.001 for k in range(p.signal_exit_bars))
                broken = not pos.tp2_done and above_n and hist > 0
                floating_loss = c > pos.entry * 1.001
            else:
                below_n = all(bars[i - k]["c"] < ema21 * 0.999 for k in range(p.signal_exit_bars))
                broken = not pos.tp2_done and below_n and hist < 0
                floating_loss = c < pos.entry * 0.999
            if broken and (not p.signal_exit_only_loss or floating_loss):
                pending_exit = "Signal Exit 趋势破坏"
            elif (not pos.tp1_done and bars_since_open >= p.time_stop_bars and floating_loss):
                pending_exit = "时间止损未达TP1"

        # ============ 4. trailing 更新（本根 → 下一根生效） ============
        if pos and pos.tp2_done:
            is_short = pos.side == "short"
            if is_short:
                pos.low_water = min(pos.low_water, l)
                trail = min(pos.entry, pos.low_water + p.trail_atr * pos.atr_abs)
                trailing_apply = trail if trail < pos.sl else None   # 空头止损只下移
            else:
                pos.high_water = max(pos.high_water, h)
                trail = max(pos.entry, pos.high_water - p.trail_atr * pos.atr_abs)
                trailing_apply = trail if trail > pos.sl else None

        # ============ 5. 收盘级：开仓决策（无持仓、无 pending exit） ============
        if sim.pos is None and pending_exit is None and i < n - 1:
            close_ms = ts + pm
            htf4h = _htf_at(htf4, close_ms)
            htf1h = _htf_at(htf1, close_ms)
            equity = sim.cash
            dd = (peak_eq - equity) / peak_eq * 100 if peak_eq > 0 else 0.0

            blocked = None
            if i < sim.sleep_until_i:
                blocked = "连亏休眠"
            elif i - sim.last_close_i < p.cooldown_bars:
                blocked = "平仓冷却"
            else:
                day = time.strftime("%Y-%m-%d", time.gmtime(ts / 1000))
                if day != sim.daily_key:
                    sim.daily_key, sim.daily_opens = day, 0
                if sim.daily_opens >= p.max_opens_per_day:
                    blocked = "日开仓上限"

            best = None
            if blocked is None:
                candles = bars[i - WARMUP + 1: i + 1]
                factors = compute_factors(candles)
                if not factors.get("error"):
                    ctx = {"htf_4h": htf4h, "htf_1h": htf1h, "halted": False,
                           "drawdown_pct": dd, "loss_streak": sim.loss_streak}
                    sides = ("long", "short") if p.allow_short else ("long",)
                    for side in sides:
                        setup = se.detect_setup(candles, factors, side)
                        if p.require_setup and setup.get("setup") == "none":
                            continue          # 实验 A：无形态不开仓
                        if p.setup_filter and setup.get("setup") != p.setup_filter:
                            continue          # 只允许指定 setup
                        score, _bd = se.score_side(factors, side, setup,
                                                   htf_4h=htf4h, htf_1h=htf1h)
                        plan = se.plan_trade(candles, factors, side)
                        ok, _checks = se.check_gate(candles, factors, side, score, plan, ctx)
                        if ok and (best is None or score > best[1]):
                            best = (side, score, plan, setup, factors)

            if best:                                   # 多空均可执行
                _side, score, plan, setup, factors = best
                notional, _note = se.position_size(
                    equity, equity, plan, dd, sim.loss_streak, p.max_order_usdt,
                    fee_taker=p.fee_taker)
                if notional >= 1.0:
                    is_short = _side == "short"
                    fill = bars[i + 1]["o"] * (1 - p.slippage if is_short else 1 + p.slippage)
                    sim.open_pos(notional, fill, plan, score, setup, factors,
                                 htf4h, htf1h, ts, i, side=_side)
                    bars_since_open = 0
                    sim.daily_opens += 1

        # ============ 6. equity ============
        if sim.pos:
            if sim.pos.side == "short":
                eq = sim.cash - sim.pos.qty * c       # 空头：欠 qty BTC
            else:
                eq = sim.cash + sim.pos.qty * c        # 多头：持有 qty BTC
        else:
            eq = sim.cash
        peak_eq = max(peak_eq, eq)
        equity_curve.append({"ts": ts, "equity": round(eq, 2)})

    # 尾仓强平
    if sim.pos:
        _is_short = sim.pos.side == "short"
        if end_ms is not None and last_dev_i >= 0:
            tail = bars[last_dev_i]
            tail_px = tail["c"] * (1 + p.slippage if _is_short else 1 - p.slippage)
            sim.close_full(tail_px, "开发窗口结束强平", tail["ts"])
            equity_curve.append({"ts": tail["ts"], "equity": round(sim.cash, 2)})
        else:
            tail_px = bars[-1]["c"] * (1 + p.slippage if _is_short else 1 - p.slippage)
            sim.close_full(tail_px, "回测结束强平", bars[-1]["ts"])
            equity_curve.append({"ts": bars[-1]["ts"] + pm, "equity": round(sim.cash, 2)})

    return {
        "metrics": _metrics(sim, equity_curve, p, period),
        "journal": sim.journal,
        "equity": equity_curve,
        "params": asdict(p),
    }


# ================= 指标（口径与 SimBroker.metrics 对齐 + V2 扩展） =================
def _metrics(sim: _Sim, equity_curve: list[dict], p: BTParams, period: str) -> dict:
    if not equity_curve:
        return {}
    eq = [e["equity"] for e in equity_curve]
    ppy = {"5m": 105_120, "15m": 35_040, "1H": 8_760}.get(period, 8760)
    total_return = (eq[-1] / p.initial_equity - 1) * 100
    years = len(eq) / ppy
    annual = ((eq[-1] / p.initial_equity) ** (1 / years) - 1) * 100 if years > 0 and eq[-1] > 0 else 0
    peak_v, max_dd = eq[0], 0.0
    for v in eq:
        peak_v = max(peak_v, v)
        max_dd = max(max_dd, (peak_v - v) / peak_v * 100)
    rets = [(eq[i] - eq[i - 1]) / eq[i - 1] for i in range(1, len(eq)) if eq[i - 1] > 0]
    mean = sum(rets) / len(rets) if rets else 0
    var = sum((r - mean) ** 2 for r in rets) / len(rets) if rets else 0
    std = var ** 0.5 or 1e-12
    sharpe = mean / std * (ppy ** 0.5) if rets else 0
    downside = [r for r in rets if r < 0]
    dstd = (sum(r ** 2 for r in downside) / len(downside)) ** 0.5 if downside else 1e-12
    sortino = mean / dstd * (ppy ** 0.5) if rets else 0

    # journal 是分批平仓记录；按 round_id 聚合成完整仓
    rounds: dict[int, list[dict]] = {}
    for rec in sim.journal:
        rounds.setdefault(rec["round_id"], []).append(rec)

    closed = []
    for recs in rounds.values():
        pnl = sum(r["pnl"] for r in recs)
        closed.append({
            "ts": recs[-1]["timestamp"], "pnl": pnl,
            "exit": recs[-1]["exitReason"], "regime": recs[-1]["regime"],
            "score": recs[-1]["entryScore"], "setup": recs[-1]["setup"],
            "mfe_r": recs[-1]["mfe_r"], "mae_r": recs[-1]["mae_r"],
            "hold_bars": recs[-1].get("holdingBars", 0),
        })
    closed.sort(key=lambda x: x["ts"])

    wins = [r for r in closed if r["pnl"] > 0]
    losses = [r for r in closed if r["pnl"] <= 0]
    gross_win = sum(r["pnl"] for r in wins)
    gross_loss = abs(sum(r["pnl"] for r in losses))
    net_pnl = gross_win - gross_loss
    expectancy = net_pnl / len(closed) if closed else 0.0
    streak = worst_streak = 0
    for r in closed:
        streak = streak + 1 if r["pnl"] < 0 else 0
        worst_streak = max(worst_streak, streak)

    def _bucket(key_fn) -> dict:
        out: dict[str, dict] = {}
        for r in closed:
            k = key_fn(r)
            b = out.setdefault(k, {"n": 0, "wins": 0, "pnl": 0.0})
            b["n"] += 1
            b["wins"] += r["pnl"] > 0
            b["pnl"] += r["pnl"]
        return {k: {"n": v["n"], "win_rate": round(v["wins"] / v["n"] * 100, 1),
                    "pnl": round(v["pnl"], 2)} for k, v in sorted(out.items())}

    mfe_all = sorted(r["mfe_r"] for r in closed)
    mae_win = sorted(r["mae_r"] for r in wins)

    def _pct(seq, q):
        return round(seq[min(len(seq) - 1, int(q * len(seq)))], 3) if seq else 0

    mc = _monte_carlo(closed, p.initial_equity)

    return {
        "total_return_pct": round(total_return, 2),
        "annual_pct": round(annual, 2),
        "max_dd_pct": round(max_dd, 2),
        "calmar": round(annual / max_dd, 2) if max_dd > 0 else None,
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "round_trips": len(closed),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "expectancy": round(expectancy, 4),
        "expectancy_pct": round(expectancy / p.initial_equity * 100, 4),
        "avg_win": round(gross_win / len(wins), 2) if wins else 0,
        "avg_loss": round(gross_loss / len(losses), 2) if losses else 0,
        "payoff_ratio": round((gross_win / len(wins)) / (gross_loss / len(losses)), 2)
                        if wins and losses else None,
        "max_consec_losses": worst_streak,
        "fee_total": round(sim.fee_total, 2),
        "slippage_total": round(sim.slip_total, 2),
        "net_pnl": round(net_pnl, 2),
        "final_equity": round(eq[-1], 2),
        "avg_hold_bars": round(sum(r["hold_bars"] for r in closed if r["hold_bars"])
                               / max(1, len([r for r in closed if r["hold_bars"]])), 1),
        "mfe_r_median": _pct(mfe_all, 0.5),
        "mae_r_p90_winners": _pct(mae_win, 0.9) if mae_win else None,
        "monte_carlo": mc,
        "by_exit": _bucket(lambda r: r["exit"].split(" ")[0]),
        "by_regime": _bucket(lambda r: str(r["regime"])),
        "by_setup": _bucket(lambda r: str(r["setup"])),
    }


def _monte_carlo(closed: list[dict], initial_equity: float, runs: int = 2000) -> dict | None:
    """Monte Carlo：重排完整仓净盈亏序列，评估回撤/连亏分布（防"顺序幸运"）。"""
    if len(closed) < 5:
        return None
    import random
    net = [r["pnl"] for r in closed]
    rng = random.Random(42)
    dds, streaks = [], []
    for _ in range(runs):
        seq = net[:]
        rng.shuffle(seq)
        eq = initial_equity
        peak = eq
        maxdd = 0.0
        streak = worst = 0
        for r in seq:
            eq += r
            peak = max(peak, eq)
            dd = (peak - eq) / peak * 100 if peak > 0 else 0
            maxdd = max(maxdd, dd)
            if r < 0:
                streak += 1
                worst = max(worst, streak)
            else:
                streak = 0
        dds.append(maxdd)
        streaks.append(worst)
    dds.sort()
    streaks.sort()
    return {
        "runs": runs,
        "median_max_dd_pct": round(dds[len(dds) // 2], 2),
        "p95_max_dd_pct": round(dds[int(0.95 * len(dds))], 2),
        "p95_max_consec_losses": streaks[int(0.95 * len(streaks))],
    }
