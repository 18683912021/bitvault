"""REST API 路由。所有服务经 request.app.state.svc 获取。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import config, db
from app.core import security
from app.exchange.okx_client import OkxApiError
from app.risk.risk_engine import RiskBlocked
from app.trading.roundtrips import compute_round_trips

router = APIRouter(prefix="/api")


def svc(request: Request):
    return request.app.state.svc


# ================= 系统 =================
@router.get("/status")
async def status(request: Request):
    s = svc(request)
    return {
        "env": s.env,
        "has_key": s.has_key(),
        "venue": s.venue,
        "time_offset_ms": round(s.public_client._time_offset_ms) if s.public_client else 0,
        "market": s.data.status if s.data else {},
        "private_ws": s.private_ws.status if s.private_ws else "no_key",
        "public_ws": s.public_ws.status if s.public_ws else "disconnected",
        "business_ws": s.business_ws.status if s.business_ws else "disconnected",
        "risk": s.risk.status(),
        "account_config": s.account.account_config if s.account else {},
        "whitelist": config.INSTRUMENT_WHITELIST,
        "strategy_types": [
            {"type": t, "label": c.label, "params_schema": c.params_schema}
            for t, c in s.strategy_registry().items()
        ],
    }


# ================= 系统级模式（venue） =================
@router.get("/venue")
async def get_venue(request: Request):
    s = svc(request)
    return {"venue": s.venue, "has_key": s.has_key(),
            "live_ready": s.has_key() and s.oms is not None}


@router.post("/venue")
async def set_venue(request: Request, body: dict):
    """切换系统级模式：paper 模拟虚拟资金 / okx 实盘真实资金。
    各 venue 配置独立（模式/杠杆/开关），切换时清仓位追踪 + 重置节流。"""
    want = (body or {}).get("venue", "paper")
    s = svc(request)
    actual = s.set_venue(want)
    # 切换后：autopilot 读取新 venue 的独立配置，清旧仓位追踪
    if s.brain:
        s.brain.on_venue_switch()
    return {"ok": True, "venue": actual, "requested": want,
            "fallback": want == "okx" and actual == "paper"}


# ================= API Key 管理 =================
class ApiKeyIn(BaseModel):
    name: str
    api_key: str
    secret: str
    passphrase: str
    env: str = "demo"


@router.get("/keys")
async def list_keys(request: Request):
    rows = db.query("SELECT id, name, env, key_masked, permission, is_active, created_at FROM api_keys ORDER BY id DESC")
    return rows


@router.post("/keys")
async def add_key(body: ApiKeyIn, request: Request):
    if body.env not in ("demo", "live"):
        raise HTTPException(400, "env 必须为 demo 或 live")
    kid = db.execute(
        "INSERT INTO api_keys (name, env, key_enc, secret_enc, passphrase_enc, key_masked, is_active, created_at)"
        " VALUES (?,?,?,?,?,?,0,?)",
        (body.name, body.env, security.encrypt(body.api_key), security.encrypt(body.secret),
         security.encrypt(body.passphrase), security.mask(body.api_key), int(time.time() * 1000)),
    )
    db.add_audit("user", "api_key_add", {"name": body.name, "env": body.env})
    return {"id": kid}


@router.post("/keys/{kid}/activate")
async def activate_key(kid: int, request: Request):
    row = db.query_one("SELECT * FROM api_keys WHERE id=?", (kid,))
    if not row:
        raise HTTPException(404, "Key 不存在")
    db.execute("UPDATE api_keys SET is_active=0")
    db.execute("UPDATE api_keys SET is_active=1 WHERE id=?", (kid,))
    await svc(request).rebuild_connection(row)
    db.add_audit("user", "api_key_activate", {"id": kid, "env": row["env"]})
    return {"ok": True}


@router.delete("/keys/{kid}")
async def delete_key(kid: int, request: Request):
    row = db.query_one("SELECT env FROM api_keys WHERE id=?", (kid,))
    if not row:
        raise HTTPException(404, "Key 不存在")
    db.execute("DELETE FROM api_keys WHERE id=?", (kid,))
    if row["is_active"] if "is_active" in row else False:
        await svc(request).disconnect_private()
    return {"ok": True}


@router.post("/keys/test")
async def test_key(body: ApiKeyIn, request: Request):
    """保存前测试连通性：用临时 Key 调账户配置接口。"""
    from app.exchange.okx_client import OkxClient

    client = OkxClient(body.api_key, body.secret, body.passphrase, demo=(body.env == "demo"))
    try:
        await client.sync_time()
        rows = await client.get_account_config()
        c = rows[0] if rows else {}
        return {"ok": True, "acctLv": c.get("acctLv"), "posMode": c.get("posMode"), "uid": c.get("uid")}
    except OkxApiError as e:
        return {"ok": False, "code": e.code, "msg": e.msg}
    finally:
        await client.aclose()


# ================= 行情 =================
@router.get("/market/ticker")
async def ticker(request: Request, instId: str):
    return svc(request).data.tickers.get(instId, {})


@router.get("/market/instruments")
async def instruments(request: Request):
    return list(svc(request).data.instruments.values())


@router.get("/market/candles")
async def candles(request: Request, instId: str, period: str = "1m", limit: int = 300):
    s = svc(request)
    rows = db.query(
        "SELECT open_time, o,h,l,c,vol FROM bars WHERE inst_id=? AND period=? ORDER BY open_time DESC LIMIT ?",
        (instId, period, min(limit, 1000)),
    )
    rows.reverse()
    return [
        {"ts": r["open_time"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "vol": r["vol"]}
        for r in rows
    ]


@router.post("/market/download-history")
async def download_history(request: Request, body: dict):
    s = svc(request)
    inst_id = body.get("inst_id", "BTC-USDT")
    period = body.get("period", "1m")
    days = int(body.get("days", 90))
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400_000

    async def run():
        try:
            await s.data.download_history(inst_id, period, start_ms, end_ms)
            db.set_setting(f"dl_{inst_id}_{period}", {"days": days, "ts": time.time()})
        except Exception as e:
            db.add_audit("system", "download_history", body, str(e))

    asyncio.create_task(run())
    return {"ok": True, "msg": f"开始后台下载 {inst_id} {period} 最近 {days} 天"}


@router.get("/market/depth")
async def depth(request: Request, instId: str):
    return svc(request).data.books.get(instId, {})


@router.get("/market/trades")
async def market_trades(request: Request, instId: str):
    return list(reversed(svc(request).data.trades.get(instId, [])))


@router.get("/market/funding-rate")
async def funding_rate(request: Request, instId: str = "BTC-USDT-SWAP"):
    try:
        rows = await svc(request).public_client.get_funding_rate(instId)
        return rows[0] if rows else {}
    except Exception as e:
        raise HTTPException(502, str(e))


@router.get("/market/forecast10")
async def forecast10(request: Request, instId: str = "BTC-USDT"):
    """10 分钟涨跌预测（事件合约参考），10 秒缓存，确定性多因子打分。"""
    from app.brain import forecast10

    return forecast10.compute(svc(request).data, instId)


# ================= 账户 =================
@router.get("/account/summary")
async def account_summary(request: Request):
    s = svc(request)
    if not s.has_key():
        raise HTTPException(400, "未配置 API Key")
    return {"summary": s.account.summary, "positions": s.account.positions,
            "config": s.account.account_config}


@router.get("/account/curve")
async def account_curve(request: Request, days: int = 30):
    return svc(request).account.curve(days)


# ================= 本地模拟盘（Paper Trading） =================
@router.get("/paper/account")
async def paper_account(request: Request):
    s = svc(request)
    if not s.paper:
        raise HTTPException(400, "本地模拟引擎未就绪")
    return s.paper.summary()


@router.get("/roundtrips")
async def roundtrips(request: Request, venue: str = "paper", inst_id: str | None = None,
                     limit: int = 50):
    """交易闭环列表（FIFO 开平配对）：开/平仓价、资费、净收入。模拟盘与实盘通用。"""
    if venue not in ("paper", "okx"):
        raise HTTPException(400, "venue 仅支持 paper/okx")
    return compute_round_trips(venue=venue, inst_id=inst_id, limit=min(limit, 200))


@router.post("/paper/reset")
async def paper_reset(request: Request, body: dict):
    s = svc(request)
    if not s.paper:
        raise HTTPException(400, "本地模拟引擎未就绪")
    initial = float(body.get("initial") or 10000)
    acct = s.paper.reset(initial)
    if s.brain:
        s.brain.reset_throttle()  # 节流状态（连亏/冷却/每日计数）一并从零开始
    return {"ok": True, "account": acct}


# ================= 决策大脑 + Autopilot =================
@router.get("/brain/status")
async def brain_status(request: Request):
    s = svc(request)
    if not s.brain:
        raise HTTPException(400, "决策大脑未就绪")
    return s.brain.status()


@router.post("/brain/autopilot/start")
async def autopilot_start(request: Request, body: dict | None = None):
    s = svc(request)
    if not s.brain:
        raise HTTPException(400, "决策大脑未就绪")
    overrides = body or {}
    cfg = s.brain.enable(overrides=overrides if overrides else None)
    await s.brain.start()
    return {"ok": True, "config": cfg}


@router.post("/brain/autopilot/stop")
async def autopilot_stop(request: Request):
    s = svc(request)
    if not s.brain:
        raise HTTPException(400, "决策大脑未就绪")
    cfg = s.brain.disable()
    await s.brain.stop()
    return {"ok": True, "config": cfg}


@router.post("/brain/config")
async def brain_update_config(request: Request, body: dict):
    """更新 autopilot 配置（mode/leverage 等）。按当前 venue 独立存储。"""
    s = svc(request)
    if not s.brain:
        raise HTTPException(400, "决策大脑未就绪")
    old_cfg = s.brain.config()
    new_cfg = {**old_cfg}
    allowed = {"mode", "leverage", "period", "max_order_usdt", "require_setup",
               "setup_filter", "cooldown_min", "max_opens_per_day",
               "loss_pause_n", "loss_pause_min", "daily_loss_limit_usdt"}
    for k in allowed:
        if k in body:
            new_cfg[k] = body[k]
    lev = int(new_cfg.get("leverage", 2) or 2)
    if lev < 1:
        lev = 1
    elif lev > config.LEVERAGE_CAP:
        lev = config.LEVERAGE_CAP
    new_cfg["leverage"] = lev
    db.set_setting(f"autopilot_config_{s.venue}", new_cfg)
    return {"ok": True, "config": new_cfg, "venue": s.venue}


@router.post("/brain/decide")
async def brain_decide(request: Request, body: dict):
    """手动触发一次 LLM 决策（不下单，只返回建议）。"""
    s = svc(request)
    if not s.brain:
        raise HTTPException(400, "决策大脑未就绪")
    from app.brain.llm_client import LLMClient
    from app.brain.decision_brain import DecisionBrain
    inst_id = body.get("inst_id", "BTC-USDT")
    period = body.get("period", "5m")
    candles = db.query(
        "SELECT open_time as ts, o, h, l, c, vol FROM bars WHERE inst_id=? AND period=? ORDER BY open_time ASC LIMIT 100",
        (inst_id, period),
    )
    if len(candles) < 10:
        raise HTTPException(400, "K 线数据不足，请先等待行情采集")
    acct = s.paper.summary() if s.paper else {"equity": 0, "usdt": 0}
    position = None
    if s.paper:
        positions = acct.get("positions", [])
        for p in positions:
            if p.get("inst_id") == inst_id and p.get("sz", 0) > 0:
                position = {"side": "buy", "sz": p["sz"], "entry_px": 0, "last": p.get("last", 0)}
    # 临时 brain 实例（不影响 autopilot 状态）
    tmp_brain = DecisionBrain(s.llm or LLMClient())
    decision = await tmp_brain.analyze(inst_id, candles, position, acct)
    return decision


@router.get("/brain/history")
async def brain_history(request: Request, limit: int = 20):
    s = svc(request)
    if not s.brain:
        raise HTTPException(400, "决策大脑未就绪")
    return s.brain.history(limit)


# ================= 交易 =================
class OrderIn(BaseModel):
    inst_id: str
    side: str
    ord_type: str = "market"
    px: float | None = None
    sz_base: float
    reduce_only: bool = False
    td_mode: str | None = None
    venue: str = "okx"   # okx=真实交易所 / paper=本地模拟


@router.post("/order/place")
async def place_order(body: OrderIn, request: Request):
    s = svc(request)
    if body.venue == "paper":
        if not s.paper:
            raise HTTPException(400, "本地模拟引擎未就绪")
        try:
            row = await s.paper.place_intent({
                "inst_id": body.inst_id, "side": body.side, "ord_type": body.ord_type,
                "px": body.px, "sz_base": body.sz_base, "reduce_only": body.reduce_only,
                "source": "manual",
            })
            return {"ok": True, "order": row}
        except RiskBlocked as e:
            raise HTTPException(403, str(e))
    if not s.has_key():
        raise HTTPException(400, "未配置 API Key")
    try:
        row = await s.oms.place_intent({
            "inst_id": body.inst_id, "side": body.side, "ord_type": body.ord_type,
            "px": body.px, "sz_base": body.sz_base, "reduce_only": body.reduce_only,
            "source": "manual", "td_mode": body.td_mode,
        })
        return {"ok": True, "order": row}
    except RiskBlocked as e:
        raise HTTPException(403, str(e))


@router.post("/order/cancel")
async def cancel_order(request: Request, body: dict):
    cl = body.get("cl_ord_id", "")
    row = db.query_one("SELECT venue FROM orders WHERE cl_ord_id=?", (cl,))
    if row and row["venue"] == "paper":
        try:
            return await svc(request).paper.cancel_order(cl)
        except RiskBlocked as e:
            raise HTTPException(403, str(e))
    try:
        return await svc(request).oms.cancel_order(cl)
    except RiskBlocked as e:
        raise HTTPException(403, str(e))


@router.get("/orders")
async def orders(request: Request, state: str = "open", limit: int = 100, venue: str | None = None):
    """订单列表。venue 传 'paper'/'okx' 时只返回该通道订单（系统级模式过滤）。"""
    vclause = "AND venue=?" if venue else ""
    params: list = ([venue] if venue else []) + [min(limit, 500)]
    if state == "open":
        return db.query(
            f"SELECT * FROM orders WHERE state IN ('pending_submit','live','partially_filled') {vclause} ORDER BY id DESC LIMIT ?",
            tuple(params),
        )
    return db.query(
        f"SELECT * FROM orders WHERE state NOT IN ('pending_submit','live','partially_filled') {vclause} ORDER BY id DESC LIMIT ?",
        tuple(params),
    )


@router.get("/trades")
async def trades(request: Request, limit: int = 100, venue: str | None = None):
    """成交流水。trades 表无 venue 列，JOIN orders 取 venue（顺带修复前端通道列）；
    传 venue 时仅返回该通道成交。"""
    if venue:
        return db.query(
            "SELECT t.*, o.venue FROM trades t JOIN orders o ON t.cl_ord_id=o.cl_ord_id"
            " WHERE o.venue=? ORDER BY t.id DESC LIMIT ?",
            (venue, min(limit, 500)),
        )
    return db.query(
        "SELECT t.*, o.venue FROM trades t LEFT JOIN orders o ON t.cl_ord_id=o.cl_ord_id"
        " ORDER BY t.id DESC LIMIT ?",
        (min(limit, 500),),
    )


@router.post("/account/close-position")
async def close_position(request: Request, body: dict):
    s = svc(request)
    inst_id = body.get("inst_id")
    pos = next((p for p in s.account.positions if p["instId"] == inst_id), None)
    if not pos:
        raise HTTPException(404, "未找到该持仓")
    ok = await s.oms.close_position(pos)
    return {"ok": ok}


# ================= 策略 =================
@router.get("/strategies/templates")
async def templates(request: Request):
    reg = svc(request).strategy_registry()
    return [
        {"type": t, "label": c.label, "params_schema": c.params_schema}
        for t, c in reg.items()
    ]


@router.get("/strategies/instances")
async def instances(request: Request):
    return svc(request).manager.list_instances()


class InstanceIn(BaseModel):
    strategy_type: str
    name: str
    inst_id: str
    mode: str = "demo"
    params: dict = {}
    period: str = "1m"


@router.post("/strategies/instances")
async def create_instance(body: InstanceIn, request: Request):
    try:
        iid = svc(request).manager.create(body.strategy_type, body.name, body.inst_id, body.mode, body.params, body.period)
        return {"id": iid}
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/strategies/instances/{iid}/start")
async def start_instance(iid: int, request: Request, body: dict = None):
    body = body or {}
    try:
        return await svc(request).manager.start_instance(iid, confirm=bool(body.get("confirm")))
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/strategies/instances/{iid}/stop")
async def stop_instance(iid: int, request: Request, body: dict = None):
    body = body or {}
    try:
        return await svc(request).manager.stop_instance(iid, close_position=bool(body.get("close_position")))
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/strategies/instances/{iid}/pause")
async def pause_instance(iid: int, request: Request):
    try:
        await svc(request).manager.pause_instance(iid)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/strategies/instances/{iid}/resume")
async def resume_instance(iid: int, request: Request):
    try:
        await svc(request).manager.resume_instance(iid)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, str(e))


@router.delete("/strategies/instances/{iid}")
async def delete_instance(iid: int, request: Request):
    try:
        svc(request).manager.delete(iid)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, str(e))


@router.get("/strategies/instances/{iid}/logs")
async def instance_logs(iid: int, request: Request):
    return svc(request).manager.logs.get(iid, [])


# ================= 回测 =================
@router.post("/backtest/run")
async def backtest_run(request: Request, body: dict):
    s = svc(request)
    end_ms = int(time.time() * 1000)
    days = int(body.get("days", 90))
    job = {
        "strategy_type": body["strategy_type"],
        "params": body.get("params", {}),
        "inst_id": body.get("inst_id", "BTC-USDT"),
        "period": body.get("period", "1m"),
        "start_ms": end_ms - days * 86400_000,
        "end_ms": end_ms,
        "fee_rate": float(body.get("fee_rate", 0.0005)),
        "slippage": float(body.get("slippage", 0.0005)),
        "initial_equity": float(body.get("initial_equity", 10000)),
    }
    n_bars = db.query_one(
        "SELECT COUNT(*) AS n FROM bars WHERE inst_id=? AND period=? AND open_time>=?",
        (job["inst_id"], job["period"], job["start_ms"]),
    )["n"]
    if n_bars < 50:
        raise HTTPException(400, f"本地历史数据不足（{n_bars} 根），请先在行情中心下载历史")
    bid = db.execute(
        "INSERT INTO backtests (strategy_type, params_json, inst_id, period, start_ts, end_ts, fee_rate, slippage,"
        " status, created_at) VALUES (?,?,?,?,?,?,?,?, 'running', ?)",
        (job["strategy_type"], json.dumps(job["params"]), job["inst_id"], job["period"],
         job["start_ms"], job["end_ms"], job["fee_rate"], job["slippage"], int(time.time() * 1000)),
    )
    job_path = os.path.join(config.DATA_DIR, f"bt_job_{bid}.json")
    out_path = os.path.join(config.DATA_DIR, f"bt_out_{bid}.json")
    with open(job_path, "w", encoding="utf-8") as f:
        json.dump(job, f)

    async def run():
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, os.path.join(config.BASE_DIR, "run_backtest.py"), job_path, out_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            await asyncio.wait_for(proc.wait(), timeout=600)
            with open(out_path, "r", encoding="utf-8") as f:
                result = json.load(f)
            if result.get("ok"):
                db.execute(
                    "UPDATE backtests SET status='done', metrics_json=?, equity_json=?, trades_json=?, finished_at=? WHERE id=?",
                    (json.dumps(result["metrics"]), json.dumps(result["equity"]),
                     json.dumps(result["trades"]), int(time.time() * 1000), bid),
                )
            else:
                db.execute("UPDATE backtests SET status='failed', error=? WHERE id=?",
                           (result.get("error", "")[:500], bid))
        except Exception as e:
            db.execute("UPDATE backtests SET status='failed', error=? WHERE id=?", (str(e)[:500], bid))

    asyncio.create_task(run())
    return {"id": bid, "status": "running"}


@router.get("/backtest/list")
async def backtest_list(request: Request):
    return db.query(
        "SELECT id, strategy_type, inst_id, period, start_ts, end_ts, status, error, created_at, finished_at,"
        " metrics_json FROM backtests ORDER BY id DESC LIMIT 50"
    )


@router.get("/backtest/{bid}")
async def backtest_detail(bid: int, request: Request):
    row = db.query_one("SELECT * FROM backtests WHERE id=?", (bid,))
    if not row:
        raise HTTPException(404, "回测不存在")
    row["metrics"] = json.loads(row["metrics_json"] or "{}")
    row["equity"] = json.loads(row["equity_json"] or "[]")
    row["trades"] = json.loads(row["trades_json"] or "[]")
    return row


# ================= 风控 =================
@router.get("/risk/status")
async def risk_status(request: Request):
    return svc(request).risk.status()


@router.get("/risk/rules")
async def risk_rules(request: Request):
    return svc(request).risk.rules


@router.post("/risk/rules")
async def update_rule(request: Request, body: dict):
    rule_type = body.get("type")
    params = body.get("params", {})
    enabled = bool(body.get("enabled", True))
    svc(request).risk.update_rule(rule_type, params, enabled)
    return {"ok": True}


@router.get("/risk/events")
async def risk_events(request: Request, limit: int = 100):
    return db.query("SELECT * FROM risk_events ORDER BY id DESC LIMIT ?", (min(limit, 500),))


@router.post("/risk/kill-switch")
async def kill_switch(request: Request):
    report = await svc(request).risk.kill_switch()
    return {"ok": True, "report": report}


@router.post("/risk/resume")
async def risk_resume(request: Request):
    await svc(request).risk.resume()
    return {"ok": True}


# ================= 通知 / 审计 =================
@router.get("/notifications")
async def notifications(request: Request, limit: int = 50):
    return db.query("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (min(limit, 200),))


@router.get("/audit-logs")
async def audit_logs(request: Request, limit: int = 100):
    return db.query("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (min(limit, 500),))
