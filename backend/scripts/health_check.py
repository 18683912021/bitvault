"""策略健康度监控：forward paper trading 的每日 metrics + 历史区间比较。

读 DB（闭环交易 + 决策日志 + 通知）+ 尝试 API 取实时账户/节流。
输出红黄绿灯报告，回答："forward 表现是否在 dev 验证的预期区间内？"

基准（1H dev 修正后，4H+1D 真 HTF）：
  round_trips=4  net=+2.12U  PF=1.24  expectancy=0.53  win=50%
  max_consec_losses=2  avg_hold=18.5 bars  fee_total=1.6

用法：
  python scripts/health_check.py            # 控制台报告
  python scripts/health_check.py --json     # 机器可读
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db
from app.trading.roundtrips import compute_round_trips

INST_ID = "BTC-USDT"
INITIAL_EQUITY = 10000.0

# dev 1H 基准（修正自证 bug 后）——forward 指标与之对照
BENCH = {
    "period": "1H",
    "round_trips": 4, "net_pnl": 2.12, "pf": 1.24, "expectancy": 0.53,
    "win_rate": 50.0, "max_consec_losses": 2, "avg_hold_bars": 18.5,
    "fee_total": 1.6,
}

# 风控阈值（与 autopilot/signal_engine 常量一致）
DD_THROTTLE = 3.0     # 回撤 >3% 仓位减半（黄灯）
DD_PAUSE = 6.0        # 回撤 >6% 暂停开仓（红灯）
LOSS_PAUSE_N = 3      # 连亏 3 笔休眠（红灯）
DAILY_LOSS_LIMIT = 100  # 当日亏损 100U 停（红灯）


def _ms_age(ms: int) -> str:
    s = (time.time() * 1000 - ms) / 1000
    if s < 60: return f"{s:.0f}s"
    if s < 3600: return f"{s/60:.1f}min"
    if s < 86400: return f"{s/3600:.1f}h"
    return f"{s/86400:.1f}d"


def _api(path: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8000/api/{path}", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _closed_roundtrips() -> list[dict]:
    rt = compute_round_trips(venue="paper", inst_id=INST_ID, limit=200)
    return rt.get("closed") or []


def _metrics(closed: list[dict]) -> dict:
    if not closed:
        return {"round_trips": 0}
    wins = [r for r in closed if r["pnl"] > 0]
    losses = [r for r in closed if r["pnl"] <= 0]
    gross_win = sum(r["pnl"] for r in wins)
    gross_loss = abs(sum(r["pnl"] for r in losses))
    net = gross_win - gross_loss
    # 连亏序列（含部分平仓按 round 聚合：同 open_ts 视为一笔）
    streak = worst = 0
    for r in closed:
        streak = streak + 1 if r["pnl"] < 0 else 0
        worst = max(worst, streak)
    return {
        "round_trips": len(closed),
        "win_rate": round(len(wins) / len(closed) * 100, 1),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "expectancy": round(net / len(closed), 4),
        "net_pnl": round(net, 2),
        "avg_win": round(gross_win / len(wins), 2) if wins else 0,
        "avg_loss": round(gross_loss / len(losses), 2) if losses else 0,
        "max_consec_losses": worst,
        "fee_total": round(sum(r["fee"] for r in closed), 2),
        "avg_hold_s": round(sum(r["hold_s"] for r in closed) / len(closed)) if closed else 0,
    }


def _equity_curve(closed: list[dict], unrealized: float) -> list[float]:
    """从闭环 PnL + 当前浮盈重建权益曲线（近似，不含中间浮盈波动）。"""
    eq = INITIAL_EQUITY
    curve = [eq]
    for r in closed:
        eq += r["pnl"]
        curve.append(eq)
    curve[-1] += unrealized
    return curve


def _drawdown(curve: list[float]) -> float:
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak * 100 if peak > 0 else 0)
    return round(mdd, 2)


def _decision_health() -> dict:
    """最近 50 条 autopilot 决策记录（signals 表 instance_id=0）。"""
    rows = db.query(
        "SELECT ts, action, reason, payload_json FROM signals WHERE instance_id=0 "
        "ORDER BY ts DESC LIMIT 50")
    if not rows:
        return {"decisions": 0}
    n_pass = sum(1 for r in rows if "gate:" in r["action"] and r["action"].endswith(":pass"))
    n_fail = sum(1 for r in rows if "gate:" in r["action"] and r["action"].endswith(":fail"))
    n_wait = sum(1 for r in rows if r["action"] == "decide:wait")
    evaluated = n_pass + n_fail
    regimes, scores, setups = {}, [], {}
    for r in rows:
        try:
            p = json.loads(r["payload_json"] or "{}")
        except Exception:
            p = {}
        reg = p.get("regime") or "?"
        regimes[reg] = regimes.get(reg, 0) + 1
        sc = p.get("signal_score")
        if isinstance(sc, (int, float)):
            scores.append(sc)
        st = p.get("setup") or "none"
        setups[st] = setups.get(st, 0) + 1
    last_ts = rows[0]["ts"] if rows else 0
    return {
        "decisions": len(rows),
        "gate_pass": n_pass, "gate_fail": n_fail, "wait": n_wait,
        "evaluated": evaluated,
        "pass_rate": round(n_pass / evaluated * 100, 1) if evaluated else 0,
        "regimes": regimes, "setups": setups,
        "score_min": min(scores) if scores else None,
        "score_max": max(scores) if scores else None,
        "score_avg": round(sum(scores) / len(scores), 1) if scores else None,
        "last_decision_age": _ms_age(last_ts),
        "last_decision_age_s": (time.time() * 1000 - last_ts) / 1000 if last_ts else None,
    }


def _open_position() -> dict:
    """从 trades 表推算当前持仓（不依赖 in-memory paper engine）。"""
    rows = db.query(
        "SELECT t.side, t.sz, t.px FROM trades t JOIN orders o ON t.cl_ord_id=o.cl_ord_id "
        "WHERE o.venue='paper' AND t.inst_id=? ORDER BY t.ts ASC", (INST_ID,))
    net_btc = sum(float(r["sz"]) if r["side"] == "buy" else -float(r["sz"]) for r in rows)
    if net_btc <= 1e-10:
        return {"net_btc": 0, "unrealized": 0}
    # 最新价
    last = db.query(
        "SELECT c FROM bars WHERE inst_id=? AND period='1H' ORDER BY open_time DESC LIMIT 1",
        (INST_ID,))
    px = float(last[0]["c"]) if last else 0
    # 加权开仓均价
    cost = sum(float(r["px"]) * float(r["sz"]) for r in rows if r["side"] == "buy")
    buys = sum(float(r["sz"]) for r in rows if r["side"] == "buy")
    avg_buy = cost / buys if buys > 0 else 0
    unrealized = (px - avg_buy) * net_btc
    return {"net_btc": round(net_btc, 8), "entry_avg": round(avg_buy, 2),
            "last_px": px, "unrealized": round(unrealized, 2)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args()
    db.init()

    closed = _closed_roundtrips()
    m = _metrics(closed)
    pos = _open_position()
    eq_curve = _equity_curve(closed, pos["unrealized"])
    dd = _drawdown(eq_curve)
    dec = _decision_health()

    # 实时节流（API，可能不可用）
    brain = _api("brain/status")
    acct = _api("paper/account")
    throttle = brain.get("throttle", {}) if brain else {}
    loss_streak = throttle.get("loss_streak", 0)
    sleeping = throttle.get("sleeping", False)
    opens_today = throttle.get("opens_today", 0)

    # 红黄绿灯判定
    alerts: list[tuple[str, str]] = []  # (level, msg)
    if dd >= DD_PAUSE:
        alerts.append(("🔴", f"回撤 {dd}% ≥ {DD_PAUSE}% 暂停阈值（autopilot 应已 paused）"))
    elif dd >= DD_THROTTLE:
        alerts.append(("🟡", f"回撤 {dd}% ≥ {DD_THROTTLE}% 仓位减半档"))
    if loss_streak >= LOSS_PAUSE_N:
        alerts.append(("🔴", f"连亏 {loss_streak} 笔 ≥ {LOSS_PAUSE_N} 触发休眠"))
    elif loss_streak >= 2:
        alerts.append(("🟡", f"连亏 {loss_streak} 笔（≥2 仓位×0.5）"))
    if sleeping:
        alerts.append(("🟡", f"autopilot 休眠中（连亏保护）"))
    if closed:
        worst = min(r["pnl"] for r in closed)
        if worst < -INITIAL_EQUITY * 0.01:
            alerts.append(("🔴", f"单笔最差 {worst:.2f}U > 1% 权益，止损过大"))
    if dec.get("decisions", 0) == 0 and not closed:
        alerts.append(("🟡", "尚无决策记录（autopilot 可能刚启动或未到 1H 收盘）"))
    # 决策停滞：1H 策略应每小时一次决策，>3h 无新决策 → 检查 bar 采集/进程
    age_s = dec.get("last_decision_age_s")
    if age_s is not None and age_s > 3 * 3600:
        alerts.append(("🟡", f"决策停滞 {dec['last_decision_age']}（>3h 无新决策，检查 bar 采集/autopilot 进程）"))

    level = "🟢 健康"
    if any(a[0] == "🔴" for a in alerts):
        level = "🔴 异常（需介入）"
    elif any(a[0] == "🟡" for a in alerts):
        level = "🟡 警告"

    # 与 dev 基准对照
    bench_cmp = []
    if m.get("round_trips", 0) > 0:
        for k, fwd, bench_v, better in [
            ("PF", m.get("profit_factor"), BENCH["pf"], "higher"),
            ("expectancy", m.get("expectancy"), BENCH["expectancy"], "higher"),
            ("win_rate%", m.get("win_rate"), BENCH["win_rate"], "higher"),
            ("max_consec_losses", m.get("max_consec_losses"), BENCH["max_consec_losses"], "lower"),
        ]:
            if fwd is not None:
                bench_cmp.append(f"  {k}: forward={fwd}  dev基准={bench_v}")

    report = {
        "level": level,
        "timestamp": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "account": {
            "equity": acct.get("equity") if acct else eq_curve[-1],
            "usdt": acct.get("usdt") if acct else None,
            "initial": INITIAL_EQUITY,
            "drawdown_pct": dd,
        },
        "position": pos,
        "forward_metrics": m,
        "decision_health": dec,
        "throttle": throttle,
        "alerts": [{"level": a[0], "msg": a[1]} for a in alerts],
        "bench_comparison": bench_cmp,
        "backend_reachable": brain is not None,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    # 控制台报告
    print("=" * 64)
    print(f"  策略健康度报告  {level}")
    print(f"  {report['timestamp']}  周期=1H  venue=paper")
    print("=" * 64)
    a = report["account"]
    print(f"\n[账户]  权益={a.get('equity')}  初始={a['initial']}  "
          f"回撤={a['drawdown_pct']}%  后端={'✅' if report['backend_reachable'] else '❌ 不可达'}")
    p = report["position"]
    if p["net_btc"] > 0:
        print(f"[持仓]  BTC={p['net_btc']}  开仓均价={p.get('entry_avg')}  "
              f"最新价={p.get('last_px')}  浮盈={p['unrealized']}U")
    else:
        print(f"[持仓]  空仓（观望）")

    print(f"\n[forward 指标]  闭环={m.get('round_trips',0)} 笔")
    if m.get("round_trips", 0) > 0:
        print(f"  净盈亏={m['net_pnl']}U  胜率={m['win_rate']}%  PF={m['profit_factor']}  "
              f"expectancy={m['expectancy']}")
        print(f"  均赢={m['avg_win']}U  均亏={m['avg_loss']}U  最大连亏={m['max_consec_losses']}  "
              f"费={m['fee_total']}U")

    print(f"\n[决策健康]  近50条: 观望(wait)={dec.get('wait',0)} 评估={dec.get('evaluated',0)} "
          f"(pass={dec.get('gate_pass',0)} fail={dec.get('gate_fail',0)}, 通过率={dec.get('pass_rate',0)}%)")
    if dec.get("decisions", 0) > 0:
        print(f"  regime分布={dec['regimes']}  setup分布={dec['setups']}")
        print(f"  score 范围={dec.get('score_min')}~{dec.get('score_max')} 均值={dec.get('score_avg')}  "
              f"最近决策距今={dec['last_decision_age']}")

    print(f"\n[节流]  今日开仓={opens_today}/20  连亏={loss_streak}  "
          f"休眠={'是' if sleeping else '否'}")

    if bench_cmp:
        print(f"\n[dev 基准对照]  (1H dev 修正: {BENCH['round_trips']}笔 +{BENCH['net_pnl']}U PF{BENCH['pf']})")
        for c in bench_cmp:
            print(c)

    if alerts:
        print(f"\n[告警]")
        for al in alerts:
            print(f"  {al[0]} {al[1]}")
    else:
        print(f"\n[告警]  无")

    print("\n" + "=" * 64)
    print("注：1H 交易稀疏（dev 3年仅 4 笔），forward 短期无交易是常态。")
    print("    重点关注：回撤/连亏/决策停滞/单笔过大。每日或每 1H 收盘后运行。")
    print("=" * 64)


if __name__ == "__main__":
    main()
