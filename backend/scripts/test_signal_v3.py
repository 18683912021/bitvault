"""Phase 3-5 模块自检：六态 regime / setup / 纯规则评分 / 十项守卫 / 仓位衰减 / 多周期。"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.brain.factor_engine import compute_factors, classify_regime, trend_of
from app.brain import signal_engine as se

# --- 场景1：上升趋势 + 回调（应识别 pullback 并可能通过） ---
random.seed(11)
candles = []
px = 100000.0
for i in range(120):
    o = px
    drift = 35 if i % 12 < 8 else -30
    c = px + drift + random.uniform(-80, 80)
    h = max(o, c) + abs(random.uniform(0, 60))
    l = min(o, c) - abs(random.uniform(0, 60))
    candles.append({'ts': i*300000, 'o': o, 'h': h, 'l': l, 'c': c,
                    'vol': 1000 + (200 if drift > 0 else -100) + random.uniform(-80, 80)})
    px = c

f = compute_factors(candles)
print('S1 regime:', f['regime'], '|', f['regime_reason'], '| atr_pctile:', f['atr_pctile'])
setup = se.detect_setup(candles, f, 'long')
print('S1 setup:', setup['setup'], '-', setup['why'])
score, det = se.score_side(f, 'long', setup, htf_4h='up', htf_1h='up')
print('S1 long score:', score, [f"{d['k']}:{d['v']}" for d in det])
plan = se.plan_trade(candles, f, 'long')
print('S1 plan:', {k: plan[k] for k in ('entry', 'sl', 'tp', 'rr', 'sl_basis', 'risk_atr_x')})
ok, checks = se.check_gate(candles, f, 'long', score, plan,
                           {'htf_4h': 'up', 'htf_1h': 'up', 'halted': False,
                            'drawdown_pct': 0, 'loss_streak': 0})
for c in checks:
    print('  ', c['check'], 'ok' if c['ok'] else 'FAIL', '-', c['why'][:60])
print('S1 gate:', ok)

# --- 场景2：4H 反向 + 低周期反弹做多（应被 htf_conflict 拦截） ---
ok2, checks2 = se.check_gate(candles, f, 'long', score, plan,
                             {'htf_4h': 'down', 'htf_1h': 'up', 'halted': False,
                              'drawdown_pct': 0, 'loss_streak': 0})
htf_check = [c for c in checks2 if c['check'] == 'htf_conflict'][0]
print('S2 4H-down long blocked:', not ok2, '|', htf_check['why'][:50])

# --- 场景3：仓位衰减档 ---
n0, _ = se.position_size(10000, 10000, plan, 0, 0, 5000)
n2, _ = se.position_size(10000, 10000, plan, 0, 2, 5000)
n3, _ = se.position_size(10000, 10000, plan, 0, 3, 5000)
n4, _ = se.position_size(10000, 10000, plan, 4, 0, 5000)
print('S3 sizing: normal', n0, '| streak2', n2, '| streak3', n3, '| dd4%', n4)

# --- 场景4：trend_of 多周期 ---
print('S4 trend_of(uptrend):', trend_of(candles), '| unknown:', trend_of(candles[:10]))

# --- 场景5：极端波动识别 ---
spike = [{'ts': i*300000, 'o': 100000, 'h': 100050, 'l': 99950, 'c': 100000, 'vol': 1000}
         for i in range(100)]
spike[-1] = {'ts': 99*300000, 'o': 100000, 'h': 104000, 'l': 99000, 'c': 103500, 'vol': 6000}
print('S5 extreme:', classify_regime(spike)['regime'], '-', classify_regime(spike)['regime_reason'][:40])
