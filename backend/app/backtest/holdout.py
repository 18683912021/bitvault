"""Holdout 数据段配置（任务书第二十九节：建立真正的 Holdout）。

原则（红线）：
- 预留每周期最近一段历史数据作为 Holdout，**冻结**。
- 任何参数优化 / Walk-Forward / 网格搜索 / 手动调参 **禁止**使用 Holdout 段。
- Holdout 仅在全部开发完成后进行 **一次** 最终验证（run_holdout.py）。
- 一旦在 Holdout 上验证过，不得再回头调参（否则等于把 Holdout 纳入 IS，丧失 OOS 意义）。

边界选取（兼顾样本量与开发窗口）：
- 1H/4H：数据 3.7 年 → Holdout = 最近 6 个月（~4400/900 bars，统计充足）
- 5m：数据 1 年     → Holdout = 最近 3 个月（~26k bars）
- 15m：数据 2 年    → Holdout = 最近 3 个月（~8.7k bars）

调用方：
  dev_end_ms(period)     → 开发窗口上界（= Holdout 起点），优化脚本默认 --end 取此值
  holdout_start_ms(period) → 同上（语义别名）
  holdout_end_ms(period)   → Holdout 终点（= 数据最新点）
  assert_not_in_holdout(ts, period) → 守卫：开发脚本误用 Holdout 段时抛错
"""
from __future__ import annotations

from datetime import datetime, timezone

# 各周期 Holdout 起点（UTC 0:00）。冻结，不得随意改动——改了等于污染 Holdout。
_HOLDOUT_START = {
    "5m":  "2026-06-01",
    "15m": "2026-06-01",
    "1H":  "2026-03-01",
    "4H":  "2026-03-01",
    "1D":  "2026-03-01",
}

# Holdout 终点 = 数据最新点（下载到 2026-09-01）
_HOLDOUT_END = "2026-09-01"


def _ms(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def holdout_start_ms(period: str) -> int:
    """Holdout 段起点（= 开发窗口上界）。"""
    if period not in _HOLDOUT_START:
        raise ValueError(f"Holdout 未定义周期 {period}（已定义: {list(_HOLDOUT_START)})")
    return _ms(_HOLDOUT_START[period])


def dev_end_ms(period: str) -> int:
    """开发窗口上界（参数优化 / WF 的默认 --end）。= holdout_start_ms。"""
    return holdout_start_ms(period)


def holdout_end_ms(period: str) -> int:
    """Holdout 段终点。"""
    return _ms(_HOLDOUT_END)


def holdout_span(period: str) -> tuple[int, int]:
    """返回 (holdout_start_ms, holdout_end_ms)。"""
    return holdout_start_ms(period), holdout_end_ms(period)


def assert_not_in_holdout(ts: int, period: str) -> None:
    """守卫：开发脚本若误用 Holdout 段数据（ts >= holdout_start）则抛错。"""
    if ts >= holdout_start_ms(period):
        raise RuntimeError(
            f"数据泄漏：ts={ts} 落入 Holdout 段（起点 {holdout_start_ms(period)}）。"
            f"参数优化禁止使用 Holdout，请用 dev_end_ms('{period}') 截断。"
        )


def describe(period: str) -> str:
    s = datetime.fromtimestamp(holdout_start_ms(period) / 1000, tz=timezone.utc)
    e = datetime.fromtimestamp(holdout_end_ms(period) / 1000, tz=timezone.utc)
    return (f"{period}: 开发窗口截止 {s:%Y-%m-%d}，Holdout "
            f"{s:%Y-%m-%d}→{e:%Y-%m-%d}（冻结，仅最终验证一次）")
