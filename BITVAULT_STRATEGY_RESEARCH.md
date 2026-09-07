# BitVault 策略研究说明（Pullback 实验开关）

> 状态：**研究阶段原型**。默认正式策略（official）与生产行为 100% 一致；
> 本实验仅用于收集样本，**不构成参数/策略调整建议**。

---

## 1. 实验开关使用方式

- 配置字段：`strategy_mode`
  - `official`（**默认**）：pullback 被准入层拦截（与生产完全一致）；
  - `research_pullback`：仅允许 **pullback** 进入后续完整决策链（Score→11 Guards→Risk→Size→Execution）。
- 运行时切换（服务器/本地皆可，BCL/A 受 auth+confirm 保护）：

```bash
# 开启实验
curl -X POST https://bitvault.iyouzi.cc/api/brain/config \
  -H "Authorization: Bearer <BV_API_TOKEN>" -H "Content-Type: application/json" \
  -d '{"strategy_mode":"research_pullback","confirm":true}'

# 切回正式
curl -X POST .../api/brain/config -d '{"strategy_mode":"official","confirm":true}'
```

- venue（paper/okx）独立配置（`autopilot_config_{venue}`），实验在 paper 或 okx 均可运行；
- 状态查看：`GET /api/brain/status` → `strategy_mode`；
- 开关为**运行时切换**：改的是配置持久化值，下一次决策立即生效；不重启服务。

## 2. 当前正式策略基线（不允许更改的部分）

| 项 | 约束 |
|---|---|
| 准入 | require_setup=true + setup_filter="breakout_retest"（official 下 pullback 拦截） |
| Score | SCORE_MIN=70，权重 20/15/15/15/10/10/15 |
| Guards | 11 项全过（regime/htf_conflict/score/rr/chase/divergence/sl_range/structure_room/cost/risk_state/liq_safety） |
| RR | ≥2.0 |
| Regime | classify_regime 七态（阈值不变） |
| 仓位 | 1% 风险预算 × _risk_tier(1/0.5/0.25/0) × reduce_hint(0.5) |
| SL/TP | 结构止损 0.4~2.5%；TP1=1R25%→TP2=2R35%→trail 2.5ATR→TP3=3R；时间止损 120bar |
| 杠杆 | normal=2x / sprint=min(10, cap)（cap=13 上限） |
| 风控 | check_pretrade（支持集/熔断/杠杆上限/频率/单笔/占比）+ 自动熔断（venue 分账）+ kill_switch |
| SHORT 额外 | leverage>1 且形态=breakout_retest（实验同样适用，不因 research 放开） |

## 3. 实验策略定义（research_pullback）

- **唯一变化**：`_setup_gate()` 准入层对 `pullback` 返回放行；
- 之后 **detect_setup → score_side → plan_trade → check_gate(11) → position_size → risk.check_pretrade → execution** 全部为现有正式实现，一个参数都不改；
- 因此 pullback 通过全部 11 守卫的概率由真实规则决定——这正是要收集的样本；
- 不绕过：HTF conflict、chase、divergence、sl_range、structure room、cost、risk_state、liq_safety、杠杆、清算距离。

## 4. 数据字段（日志 & trade_journal）

每次决策（signals, instance_id=0）已含：
`action`（decide:wait / gate:long|short:pass|fail）+ `payload`：
- `strategy_mode`、`regime`、`regime_reason`、`htf_4h/htf_1h`、`eval_details`（side/setup/skip/score/gate/detail）
- 开仓质量：`signal_score`、`rr`、`checks`（十一项每项 ok/why）、`plan`(entry/sl/tp…)、`block_layer`（wait 卡层：data_insufficient/stale_data/live_not_ready/has_position/throttle/setup_or_gate）

每笔已平仓（trade_journal 表）已含：
`ts/venue/inst_id/side/entry_px/exit_px/sz/sl_px/tp1_px/tp2_px/mfe_px/mae_px/mfe_r/mae_r/pnl/fees/leverage/risk_tier/regime/setup/score/htf_4h/htf_1h/exit_reason/hold_bars/open_ts/strategy_mode/guards_json`

## 5. 统计口径与工具

```bash
python backend/scripts/stats_strategy_mode.py            # 服务器库
# 或本地： cd backend && python scripts/stats_strategy_mode.py
```
输出：卡层分布、mode×regime×setup×结果矩阵、gate pass/fail、score 分布（min/avg/max）、守卫失败 TOP、trade_journal 胜率/均值R/PF/MFE/MAE（按 mode 分组）。

## 6. 研究问题 ↔ 所需样本

1. pullback 各 regime 表现 → 矩阵 `(strateg_mode, regime, setup, result)`
2. Score 分布 → `score` 组（缺样本即说明被守卫/准入拦截）
3. 拦截 pullback 的 Guards → `guard_fail` 计数
4. 最终通过率 → gate pass / (gate pass+gate fail+准入外) ；注意区分"卡准入"与"卡守卫"
5. 胜率/平均 R/PF/MaxDD → trade_journal（需至少 20+ 笔 close 才有意义）
6. MFE/MAE → journal mfe_r/mae_r（同 setup 分组）
7. trend_up/range/trend_down 差异 → 矩阵按 regime 切开
8. breakout_retest vs pullback 差异 → 两 mode 同表对比

## 7. 结果评估纪律（防过拟合）

- **严禁**根据短期样本（<30 笔平仓或 <2 周）直接调整任何正式参数；
- 评估采用**时间顺序**（不 shuffle）：先做 OOS 切分/随后的 Walk-Forward（见 scripts/run_walk_forward.py 等既有工具），规则口径与 `rules_backtest.py` 共享；
- 转正判据（供后续决策，本轮不执行）建议：在 paper 上积累 ≥50 笔平仓且（时序 OOS 上）满足：PF≥1.15、胜率×均值R 优于官方同窗口、最大回撤无恶化、MFE/MAE 分布尾部可接受；再走一次 holdout 复核后，由人工决策是否提交为正式策略变更；
- 任何阶段都不允许把 research_pullback 自动转为正式、不自动调参、不根据历史结果自动改参数。

## 8. 安全影响（不变）

- research 模式运行在 paper 或 okx 均**不绕过**：venue 隔离、Bearer/confirm 鉴权、risk.check_pretrade、set_leverage、private_ws 健康门、自动熔断；
- Journal/日志记录 strategy_mode，统计可区分 official vs research（字段为每笔/每次决策的 payload/列）。
