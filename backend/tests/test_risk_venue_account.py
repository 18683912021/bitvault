# 回归：风控账户口径必须按 venue 隔离（模拟盘单不得用实盘权益校验）。
#
# 背景（线上实测）：max_position_pct 固定读 self.account（实盘 OKX 账户）做占比校验，
# 于是连着实盘 Key 时，任何模拟盘订单都会被"实盘权益 0.003U"判成占比超限而全部拦死；
# 且 RiskEngine.paper 从未赋值（无 Key 时走到 self.paper 直接 AttributeError）。
# 另外 status() 固定读 okx_* 基线与旧键 "day_key"，模拟盘模式下页面显示的
# 日内盈亏/回撤其实是实盘账户的数字，day_key 永远停在上一次写旧键的日期。
import asyncio

import pytest

from app.risk.risk_engine import RiskEngine, RiskBlocked

INST = "BTC-USDT"
# 生产实际规则量级：单笔上限 1000U（测试里放宽，确保占比规则才是拦截项）
BIG_NOTIONAL = {"max_usdt": 100_000}


class FakeAcct:
    """实盘账户替身（OKX AccountService 的用到的两个接口）。"""

    def __init__(self, equity: float, positions: list | None = None):
        self._eq = equity
        self.positions = positions or []

    def equity(self) -> float:
        return self._eq


class FakePaper:
    """模拟盘引擎替身（只用到 summary()）。"""

    def __init__(self, equity: float, positions: list | None = None):
        self._eq = equity
        self._pos = positions or []

    def summary(self) -> dict:
        return {"equity": self._eq, "usdt": self._eq, "initial": 10_000.0,
                "positions": self._pos, "pnl": 0.0}


def _rb(fresh_db) -> RiskEngine:
    rb = RiskEngine()
    rb.update_rule("max_order_notional", BIG_NOTIONAL, True)
    rb.update_rule("max_position_pct", {"pct": 20}, True)
    return rb


def _order(rb: RiskEngine, venue: str, notional: float, leverage: int = 1) -> None:
    rb.check_pretrade(inst_id=INST, side="buy", sz=1, px=100, notional=notional,
                      reduce_only=False, source="autopilot:open", venue=venue,
                      leverage=leverage)


def test_paper_order_not_blocked_by_empty_live_account(fresh_db):
    """核心回归：实盘账户只有 0.003U 时，模拟盘订单必须放行（旧实现恒被拦截）。"""
    rb = _rb(fresh_db)
    rb.paper = FakePaper(10_000.0)
    rb.account = FakeAcct(0.003)          # 连着实盘 Key、余额≈0（线上真实状态）
    _order(rb, "paper", 200.0, leverage=10)


def test_paper_order_still_blocked_by_paper_equity(fresh_db):
    """占比规则对模拟盘依然生效：2500+2000 > 20%×10000 → 拦截。"""
    rb = _rb(fresh_db)
    rb.paper = FakePaper(10_000.0, [{"inst_id": INST, "sz": 0.05, "notional": 2_500.0}])
    rb.account = FakeAcct(0.003)
    with pytest.raises(RiskBlocked):
        _order(rb, "paper", 2_000.0)


def test_paper_order_without_any_account_does_not_crash(fresh_db):
    """无 Key 且未注入 paper 时不得 AttributeError（旧实现走到 self.paper 即崩）。"""
    rb = _rb(fresh_db)
    _order(rb, "paper", 200.0)


def test_okx_order_uses_live_equity_not_paper(fresh_db):
    """反向隔离：实盘单只看实盘账户，模拟盘单不受实盘权益/持仓影响。"""
    rb = _rb(fresh_db)
    rb.paper = FakePaper(10_000.0)                                # 模拟盘：空仓、1 万权益
    rb.account = FakeAcct(10_000.0, [{"notionalUsd": 2_500.0}])   # 实盘：已有 2500U 仓位
    with pytest.raises(RiskBlocked):
        _order(rb, "okx", 2_000.0)        # 2500+2000 = 45%×10_000 → 拦截
    # 模拟盘按自己的空仓 + 自身权益算：15% → 放行（若误用实盘持仓会被拦成 45%）
    _order(rb, "paper", 1_500.0)


def test_status_reports_own_venue(fresh_db):
    """风控状态按 venue 取值：模拟盘不得显示实盘基线，day_key 不再是旧键。"""
    rb = _rb(fresh_db)
    rb.paper = FakePaper(10_000.0)
    rb.account = FakeAcct(0.003)
    rb.on_equity(10_000.0, avail=10_000.0, venue="paper")
    rb.on_equity(0.003, avail=0.003, venue="okx")

    sp, so = rb.status("paper"), rb.status("okx")
    assert sp["venue"] == "paper" and so["venue"] == "okx"
    assert abs(sp["peak_equity"] - 10_000.0) < 1e-6, "模拟盘峰值应来自 paper 基线"
    assert abs(so["peak_equity"] - 0.003) < 1e-9, "实盘峰值应来自 okx 基线"
    assert sp["day_key"] and so["day_key"], "day_key 应取 {venue}_day_key（旧实现读旧键恒空）"
    assert sp["daily_pnl_pct"] == 0 and sp["drawdown_pct"] == 0
    assert abs(sp["day_start_equity"] - 10_000.0) < 1e-6


def test_resume_reanchors_per_venue_baseline(fresh_db):
    """解除熔断：按 venue 重锚当日基线（不再写已废弃的旧键）。"""
    rb = _rb(fresh_db)
    rb.paper = FakePaper(9_000.0)
    rb.account = FakeAcct(500.0)
    rb.state["halted"] = True
    asyncio.run(rb.resume())
    assert rb.state["halted"] is False
    assert abs(rb.state["paper_day_start_equity"] - 9_000.0) < 1e-6
    assert abs(rb.state["okx_day_start_equity"] - 500.0) < 1e-6
    assert "day_start_equity" not in rb.state, "旧键不应再被写入"
