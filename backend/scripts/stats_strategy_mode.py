"""策略实验统计（只读）：按 strategy_mode × regime × setup × 结果聚合决策日志与 trade_journal。

用法（独立进程）：
    python scripts/stats_strategy_mode.py [--venue paper --limit 500]

口径：
- signals(instance_id=0)：decide:wait（含 block_layer / strategy_mode / eval_details）
  + gate:long|short:pass|fail（quality 含 score/checks/plan）。
- trade_journal：胜率 / 均值 R / Profit Factor / MFEr / MAEr / 最大连亏 / 回撤。
时间序列不重排；防过拟合请按 order 顺序查看与 holdout 切分（见 BITVAULT_STRATEGY_RESEARCH.md）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app import db  # noqa: E402  （打开同库，仅读）


def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--venue", default=None)
    a = ap.parse_args()
    db.init()

    rows = db.query(
        "SELECT ts, action, reason, payload_json FROM signals WHERE instance_id=0 "
        "ORDER BY ts ASC"
    )[-a.limit:]
    agg = {
        "wait_layer": Counter(),
        "mode_regime": Counter(),
        "mode_setup": Counter(),
        "mode_regime_setup_result": Counter(),
        "score": defaultdict(list),
        "guard_fail": Counter(),
        "gate_result": Counter(),
    }
    for r in rows:
        try:
            p = json.loads(r["payload_json"] or "{}")
        except Exception:
            continue
        mode = p.get("strategy_mode") or "official"
        act = r["action"] or ""
        if act.startswith("decide:wait"):
            agg["wait_layer"][p.get("block_layer", "setup_or_gate")] += 1
            for d in p.get("eval_details") or []:
                agg["mode_regime_setup_result"][(mode, p.get("regime"), d.get("setup"),
                                                 (d.get("skip") or "z")[:12])] += 1
        elif act.startswith("gate:"):
            reg = (p.get("regime") or "?") if not p.get("regime") else p.get("regime")
            agg["gate_result"][(mode, reg, p.get("setup"), act.split(":")[-1])] += 1
            sc = p.get("signal_score")
            if sc is not None:
                agg["score"][(mode, reg, p.get("setup"))].append(sc)
            for c in p.get("checks") or []:
                if not c.get("ok"):
                    agg["guard_fail"][c.get("check")] += 1

    print("=== 卡层 (NO TRADE 停在哪) ===")
    for k, v in agg["wait_layer"].most_common(12):
        print(f"{k}: {v}")

    print("\n=== mode × regime × setup × 结果 ===")
    for k in sorted(agg["mode_regime_setup_result"]):
        print(k, agg["mode_regime_setup_result"][k])
    for k in sorted(agg["gate_result"]):
        print("gate:", k, agg["gate_result"][k])

    print("\n=== Score 分布（有样本时） ===")
    for (mode, reg, st), scs in sorted(agg["score"].items()):
        if scs:
            print(f"{mode}/{reg}/{st}: n={len(scs)} min={min(scs)} avg={sum(scs)/len(scs):.1f} max={max(scs)}")

    print("\n=== 守卫失败 TOP ===")
    for k, v in agg["guard_fail"].most_common(10):
        print(k, v)

    # trade_journal
    jrows = db.query("SELECT * FROM trade_journal ORDER BY ts ASC")
    if a.venue:
        jrows = [j for j in jrows if j.get("venue") == a.venue]
    if jrows:
        print(f"\n=== trade_journal ({len(jrows)} 笔) ===")
        wins = [j for j in jrows if float(j.get("pnl") or 0) > 0]
        losses = [j for j in jrows if float(j.get("pnl") or 0) < 0]
        gw = sum(float(j["pnl"]) for j in wins)
        gl = -sum(float(j["pnl"]) for j in losses)
        mres = [float(j.get("mfe_r") or 0) for j in jrows]
        mars = [float(j.get("mae_r") or 0) for j in jrows]
        fres = []
        for j in jrows:
            entry = float(j.get("entry_px") or 0)
            sl = float(j.get("sl_px") or 0)
            exit_px = float(j.get("exit_px") or 0)
            sign = -1.0 if j.get("side") == "short" else 1.0
            risk = abs(entry - sl) if entry and sl else 0.0
            fres.append((exit_px - entry) * sign / risk if risk > 0 else 0.0)
        print(f"胜率: {len(wins) / len(jrows):.1%} | 均值FinalR: {sum(fres) / len(fres):.2f}"
              f" | PF: {gw / gl if gl else 0:.2f} | 平均MFEr: {sum(mres) / len(mres):.2f}"
              f" | 平均MAEr: {sum(mars) / len(mars):.2f}")
        by_mode = defaultdict(list)
        for j in jrows:
            by_mode[j.get("strategy_mode") or "official"].append(j)
        for mode, js in by_mode.items():
            w = [x for x in js if float(x.get("pnl") or 0) > 0]
            l = [x for x in js if float(x.get("pnl") or 0) < 0]
            gww = sum(float(x["pnl"]) for x in w)
            gll = -sum(float(x["pnl"]) for x in l)
            print(f"  [{mode}] n={len(js)} 胜率={len(w)/len(js):.1%} 均值R(pnl)="
                  f"{sum(float(x.get('pnl') or 0) for x in js)/len(js):.2f} PF={gww/gll if gll else 0:.2f}")
    else:
        print("\n=== trade_journal: 暂无样本（有开仓后自动统计） ===")
    print(f"\n(时间范围: {time.strftime('%m-%d %H:%M', time.localtime(rows[0]['ts'] / 1000))}"
          f" → {time.strftime('%m-%d %H:%M', time.localtime(rows[-1]['ts'] / 1000))})")


if __name__ == "__main__":
    _main()
