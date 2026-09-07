# P1-2 回归：roundtrip 配对严格按 inst_id 隔离，绝不跨币种配对。
from app.trading.roundtrips import compute_round_trips


def _seed(fresh_db, rows):
    for i, (inst, side, px, sz, pos_side, fee) in enumerate(rows):
        fresh_db.execute(
            "INSERT INTO orders (cl_ord_id, inst_id, side, pos_side, ord_type, px, sz, state,"
            " source, venue, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"c{i}", inst, side, pos_side, "market", px, sz, "filled",
             "autopilot:open" if not i else "autopilot:close", "paper", 1000 + i, 1000 + i))
        fresh_db.execute(
            "INSERT INTO trades (ord_id, cl_ord_id, inst_id, side, px, sz, fee, instance_id, ts)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (f"o{i}", f"c{i}", inst, side, px, sz, fee, 0, 1000 + i))


def test_no_cross_instrument_pairing(fresh_db):
    """故意交错：BTC 开/平、ETH 开/平 —— 两条 roundtrip 各归其主。"""
    _seed(fresh_db, [
        ("BTC-USDT", "buy", 100.0, 1.0, "long", 0.1),
        ("ETH-USDT", "buy", 10.0, 5.0, "long", 0.05),
        ("BTC-USDT", "sell", 110.0, 1.0, "long", 0.11),
        ("ETH-USDT", "sell", 12.0, 5.0, "long", 0.06),
    ])
    r = compute_round_trips(venue="paper")
    closed = r["closed"]
    assert len(closed) == 2
    by_inst = {c["inst_id"] for c in closed}
    assert by_inst == {"BTC-USDT", "ETH-USDT"}, "必须各按标的配对"
    btc = next(c for c in closed if c["inst_id"] == "BTC-USDT")
    eth = next(c for c in closed if c["inst_id"] == "ETH-USDT")
    assert btc["open_px"] == 100.0 and btc["close_px"] == 110.0
    assert eth["open_px"] == 10.0 and eth["close_px"] == 12.0


def test_venue_isolation(fresh_db):
    """okx 数据不得进入 paper roundtrip（venue JOIN 过滤）。"""
    fresh_db.execute(
        "INSERT INTO orders (cl_ord_id, inst_id, side, pos_side, ord_type, px, sz, state,"
        " source, venue, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("c_okx", "BTC-USDT", "buy", "long", "market", 100.0, 1.0, "filled",
         "okx_open", "okx", 1000, 1000))
    fresh_db.execute(
        "INSERT INTO trades (ord_id, cl_ord_id, inst_id, side, px, sz, fee, instance_id, ts)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        ("o_okx", "c_okx", "BTC-USDT", "buy", 100.0, 1.0, 0.1, 0, 1000))
    r_paper = compute_round_trips(venue="paper")
    r_okx = compute_round_trips(venue="okx")
    assert r_paper["open_qty"] == 0 and r_paper["total"] == 0, "paper 视图不含 okx 成交"
    assert r_okx["open_qty"] == 1.0, "okx 未平仓多单 1 个单元（单边开仓无 closed 属正确行为）"
    assert r_okx["closed"] == []
