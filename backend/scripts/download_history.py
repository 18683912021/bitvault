"""深历史数据下载（BTC-USDT SPOT）：OKX history-candles 分页 + 断点续传。

回测数据计划（与 paper 引擎同标的 BTC-USDT）：
  5m  : 近 12 个月      15m : 近 24 个月
  1H  : 2023-01-01 起   4H / 1D : 2021-01-01 起

用法：
  python scripts/download_history.py            # 按计划下载（可反复运行，自动续传）
  python scripts/download_history.py --status   # 仅查看当前覆盖
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.exchange.okx_client import OkxClient  # noqa: E402

INST_ID = "BTC-USDT"
SOURCE = "rest_history_dl"

PLAN = {  # period -> 起始 UTC 日期
    "5m": "2025-09-01",
    "15m": "2024-09-01",
    "1H": "2023-01-01",
    "4H": "2021-01-01",
    "1D": "2021-01-01",
}
PERIOD_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1H": 3_600_000,
             "4H": 14_400_000, "1D": 86_400_000}


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _start_ms(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def coverage() -> None:
    print(f"{'inst':<16}{'period':>6}{'bars':>10}  {'first':<17}{'last':<17}")
    for row in db.query(
        "SELECT inst_id, period, COUNT(*) n, MIN(open_time) t0, MAX(open_time) t1 "
        "FROM bars WHERE inst_id=? GROUP BY period ORDER BY period", (INST_ID,)
    ):
        print(f"{row['inst_id']:<16}{row['period']:>6}{row['n']:>10}  "
              f"{_iso(row['t0']):<17}{_iso(row['t1']):<17}")


def _store(inst_id: str, period: str, rows: list) -> list[tuple]:
    out = []
    for r in rows:
        if str(r[8] if len(r) > 8 else "1") != "1":
            continue  # 未收盘的不落库
        out.append((inst_id, period, int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                    float(r[4]), float(r[5]), float(r[6]), SOURCE))
    return out


async def download_period(client: OkxClient, period: str, start_ms: int) -> int:
    """断点续传：从 db 中该周期最早 bar 向前回溯下载到 start_ms。"""
    row = db.query_one(
        "SELECT MIN(open_time) t0, COUNT(*) n FROM bars WHERE inst_id=? AND period=?",
        (INST_ID, period))
    cursor = (int(row["t0"]) - 1) if row and row["t0"] else int(time.time() * 1000)
    stored, dup_guard = 0, 0
    t0 = time.time()
    while cursor > start_ms and dup_guard < 50:
        try:
            rows = await client.get_history_candles(INST_ID, period, after=cursor)
        except Exception as e:
            print(f"  [{period}] 请求异常 {e}，退避 3s 重试")
            await asyncio.sleep(3)
            continue
        batch = _store(INST_ID, period, rows)
        if not batch:
            break  # 到达 OKX 保留边界
        db.upsert_bars(batch)
        oldest = min(b[2] for b in batch)
        if oldest >= cursor - 1:
            dup_guard += 1
        cursor = oldest
        stored += len(batch)
        if stored % 2000 < 100:
            rate = stored / max(1e-9, time.time() - t0)
            print(f"  [{period}] +{stored} bars, cursor={_iso(cursor)}, {rate:.0f}/s",
                  flush=True)
        await asyncio.sleep(0.12)
    return stored


async def main() -> None:
    if "--status" in sys.argv:
        db.init()
        coverage()
        return
    db.init()
    client = OkxClient()
    try:
        for period, start in PLAN.items():
            target = _start_ms(start)
            row = db.query_one(
                "SELECT MIN(open_time) t0, COUNT(*) n FROM bars WHERE inst_id=? AND period=?",
                (INST_ID, period))
            have_min = int(row["t0"]) if row and row["t0"] else None
            if have_min and have_min <= target + PERIOD_MS[period]:
                print(f"[{period}] 已覆盖至 {start}（{row['n']} 根），跳过")
                continue
            print(f"[{period}] 下载 {start} -> now ...", flush=True)
            t0 = time.time()
            n = await download_period(client, period, target)
            print(f"[{period}] 新增 {n} 根，用时 {time.time() - t0:.0f}s", flush=True)
    finally:
        await client.aclose()
    print("\n== 完成，当前覆盖 ==")
    coverage()


if __name__ == "__main__":
    asyncio.run(main())
