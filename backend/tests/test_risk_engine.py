# 风控引擎回归：自动熔断接通(on_equity) / 占比规则 / reduce 豁免 / lever cap。
import asyncio

from app.risk.risk_engine import RiskEngine, RiskBlocked


def test_on_equity_daily_loss_triggers(tmp_path, monkeypatch):
    from app import config as cfg, db
    monkeypatch.setattr(cfg, "DB_PATH", str(tmp_path / "r.db"))
    if getattr(db, "_conn", None):
        db._conn.close()
        db._conn = None
    db.init()
    rb = RiskEngine()
    # 更新日内亏损熔断阈值 3%
    rb.update_rule("max_daily_loss_pct", {"pct": 3.0}, True)
    rb.on_equity(10_000.0, avail=10_000.0)
    assert not rb.state.get("halted")
    rb.on_equity(9_600.0, avail=9_600.0)   # 日内 -4% → 熔断
    assert rb.state.get("halted") is True
    assert "日内亏损" in rb.state.get("halt_reason", "")
    # 熔断中开仓被拦
    try:
        rb.check_pretrade(inst_id="BTC-USDT", side="buy", sz=1, px=100,
                          notional=100, reduce_only=False, source="t", leverage=1)
        assert False, "应被熔断拦截"
    except RiskBlocked:
        pass
    # 减仓放行
    rb.check_pretrade(inst_id="BTC-USDT", side="sell", sz=1, px=100,
                      notional=100, reduce_only=True, source="t", leverage=1)


def test_on_equity_drawdown_triggers(tmp_path, monkeypatch):
    from app import config as cfg, db
    monkeypatch.setattr(cfg, "DB_PATH", str(tmp_path / "r2.db"))
    if getattr(db, "_conn", None):
        db._conn.close()
        db._conn = None
    db.init()
    rb = RiskEngine()
    rb.update_rule("max_drawdown_pct", {"pct": 10.0}, True)
    rb.on_equity(10_000.0, avail=10_000.0)
    rb.on_equity(8_500.0, avail=8_500.0)   # 回撤 15% → 熔断
    assert rb.state.get("halted") is True


def test_auto_reduce_hint(tmp_path, monkeypatch):
    from app import config as cfg, db
    monkeypatch.setattr(cfg, "DB_PATH", str(tmp_path / "r3.db"))
    if getattr(db, "_conn", None):
        db._conn.close()
        db._conn = None
    db.init()
    rb = RiskEngine()
    rb.update_rule("auto_reduce_liq", {"enabled": True, "margin_ratio_warn": 0.10,
                                       "margin_ratio_danger": 0.05}, True)
    rb.on_equity(10_000.0, avail=5_000.0)   # ratio 0.5 > warn → 无 hint
    assert rb.state.get("reduce_hint") is False
    rb.on_equity(10_000.0, avail=800.0)     # ratio 0.08 → REDUCE_HINT
    assert rb.state.get("reduce_hint") is True


def test_leverage_cap_block():
    rb = RiskEngine()
    assert rb.lever_cap <= 13
    rb.update_rule("leverage_cap", {"lever": 5}, True)
    assert rb.lever_cap == 5
    try:
        rb.check_pretrade(inst_id="BTC-USDT", side="buy", sz=1, px=100,
                          notional=100, reduce_only=False, source="t", leverage=10)
        assert False
    except RiskBlocked:
        pass
    # 减仓不限杠杆
    rb.check_pretrade(inst_id="BTC-USDT", side="sell", sz=1, px=100,
                      notional=100, reduce_only=True, source="t", leverage=10)


def test_max_position_pct_okx(tmp_path, monkeypatch):
    from app import config as cfg, db
    monkeypatch.setattr(cfg, "DB_PATH", str(tmp_path / "r4.db"))
    if getattr(db, "_conn", None):
        db._conn.close()
        db._conn = None
    db.init()
    rb = RiskEngine()
    rb.update_rule("max_position_pct", {"pct": 20}, True)

    class Acct:
        def equity(self):
            return 10_000.0
        positions = [{"notionalUsd": 1_500.0}]

    rb.account = Acct()   # P0-4：账户注入后规则真正生效
    try:
        rb.check_pretrade(inst_id="BTC-USDT", side="buy", sz=1, px=100,
                          notional=1_000, reduce_only=False, source="t", leverage=1)
        assert False, "1500+1000=2500 > 20%×10000 应拦截"
    except RiskBlocked:
        pass
    # 减仓不过占比
    rb.check_pretrade(inst_id="BTC-USDT", side="sell", sz=1, px=100,
                      notional=1_000, reduce_only=True, source="t", leverage=1)


def test_reduce_only_exemptions():
    rb = RiskEngine()
    rb.update_rule("order_rate_limit", {"per_sec": 1}, True)
    rb.update_rule("max_order_notional", {"max_usdt": 100}, True)
    # 开仓受频率+单笔限制
    try:
        rb.check_pretrade(inst_id="BTC-USDT", side="buy", sz=1, px=100,
                          notional=200, reduce_only=False, source="t", leverage=1)
        assert False
    except RiskBlocked:
        pass
    # 减仓豁免频率与单笔（但仍走支持集）
    rb.check_pretrade(inst_id="BTC-USDT", side="sell", sz=1, px=100,
                      notional=200, reduce_only=True, source="t", leverage=1)
    try:
        rb.check_pretrade(inst_id="NOT-EXISTS", side="sell", sz=1, px=100,
                          notional=1, reduce_only=True, source="t", leverage=1)
        assert False
    except RiskBlocked:
        pass


def test_venue_separated_baseline(tmp_path, monkeypatch):
    """核心回归：paper 大额权益不得污染 okx 基线（整改误熔断的根因）。"""
    from app import config as cfg, db
    monkeypatch.setattr(cfg, "DB_PATH", str(tmp_path / "rv.db"))
    if getattr(db, "_conn", None):
        db._conn.close()
        db._conn = None
    db.init()
    rb = RiskEngine()
    rb.update_rule("max_drawdown_pct", {"pct": 10.0}, True)
    # paper 先到（10000）→ okx 后到（0.003）：okx 基线独立，绝不 100% 回撤
    rb.on_equity(10_000.0, avail=10_000.0, venue="paper")
    rb.on_equity(0.003, avail=0.003, venue="okx")
    assert rb.state.get("halted") in (None, False), "空 okx 账户不得因 paper 基线被熔断"
    # 回撤判定基于各自基线：okx 基线=0.003，无回撤
    assert abs(rb.state["okx_peak_equity"] - 0.003) < 1e-6
    assert abs(rb.state["paper_peak_equity"] - 10_000.0) < 1e-6


def test_deposit_withdrawal_resets_baseline(tmp_path, monkeypatch):
    """大额出金（单次骤降 >20%）按资金调整处理，不触发熔断。"""
    from app import config as cfg, db
    monkeypatch.setattr(cfg, "DB_PATH", str(tmp_path / "rw.db"))
    if getattr(db, "_conn", None):
        db._conn.close()
        db._conn = None
    db.init()
    rb = RiskEngine()
    rb.update_rule("max_drawdown_pct", {"pct": 10.0}, True)
    rb.on_equity(10_000.0, avail=10_000.0, venue="okx")
    rb.on_equity(7_000.0, avail=7_000.0, venue="okx")   # -30% 单次 → 视为出金，重置
    assert rb.state.get("halted") in (None, False), "出金不应触发回撤熔断"
    assert abs(rb.state["okx_peak_equity"] - 7_000.0) < 1e-6, "基线应重置为出金后权益"
