import asyncio
from app.brain.factor_engine import compute_factors
from app.exchange.okx_client import OkxClient

async def main():
    client = OkxClient(demo=False)
    await client.sync_time()
    raw = await client.get_candles('BTC-USDT', '1H', limit=150)
    candles = [{"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]),
                "l": float(r[3]), "c": float(r[4]), "vol": float(r[5])} for r in raw]
    factors = compute_factors(candles)

    last = candles[-1]['c']
    adx = factors.get('adx_14', 0)
    ema9 = factors.get('ema_fast', 0)
    ema21 = factors.get('ema_slow', 0)
    atr = factors.get('atr_14', 0)
    atr_pct = factors.get('atr_pct', 0)
    rsi = factors.get('rsi_14', 0)
    regime = factors.get('regime', '?')
    bb_pos = factors.get('bb_pos', 0)
    vol_ratio = factors.get('volume_ratio', 0)

    # 40-bar high/low (excluding last 3)
    highs_40 = [c['h'] for c in candles[-40:-3]]
    lows_40 = [c['l'] for c in candles[-40:-3]]
    level_high = max(highs_40)
    level_low = min(lows_40)

    breakout_up = level_high + 0.3 * atr
    breakout_down = level_low - 0.3 * atr

    # Recent 6 bars
    recent6 = candles[-6:]
    print(f'=== BTC 实时状态 ({recent6[-1]["ts"]}) ===')
    print(f'当前价: {last:.0f}')
    print(f'Regime: {regime}')
    print(f'ADX: {adx:.1f} (阈值 22)')
    print(f'EMA9: {ema9:.0f}  EMA21: {ema21:.0f}  {"多头" if ema9>ema21 else "空头"}排列')
    print(f'ATR(1H): {atr:.0f} ({atr_pct:.3f}%)')
    print(f'RSI: {rsi:.1f}')
    print(f'布林位置: {bb_pos:.3f} (0=下轨 1=上轨)')
    print(f'量比: {vol_ratio:.2f}')
    print()

    print(f'=== 突破位 ===')
    print(f'40根最高: {level_high:.0f}  做多突破需收 {breakout_up:.0f} (+{(breakout_up/last-1)*100:.2f}%)')
    print(f'40根最低: {level_low:.0f}  做空突破需收 {breakout_down:.0f} ({(breakout_down/last-1)*100:.2f}%)')
    print()

    print(f'=== 最近 6 根 1H K线 ===')
    for c in recent6:
        import datetime
        t = datetime.datetime.fromtimestamp(c['ts']/1000, tz=datetime.timezone.utc)
        chg = (c['c']/c['o']-1)*100
        print(f'  {t.strftime("%m-%d %H:%M")} O:{c["o"]:.0f} H:{c["h"]:.0f} L:{c["l"]:.0f} C:{c["c"]:.0f} {"+" if chg>=0 else ""}{chg:.2f}%')
    print()

    # ADX history (simulate by computing on subsets)
    print(f'=== ADX 距离 22 的差距 ===')
    print(f'当前 ADX={adx:.1f}，需 +{22-adx:.1f} 点')
    print(f'ATR={atr:.0f} ({atr_pct:.3f}%)')
    print(f'连续 4 根同向跌 ~1ATR: 价格 -{4*atr_pct:.2f}% → {last*(1-4*atr_pct/100):.0f}')
    print(f'连续 5 根同向跌 ~1ATR: 价格 -{5*atr_pct:.2f}% → {last*(1-5*atr_pct/100):.0f}')
    print(f'连续 6 根同向跌 ~1ATR: 价格 -{6*atr_pct:.2f}% → {last*(1-6*atr_pct/100):.0f}')
    print()

    # Check: how many conditions already met for short?
    print(f'=== 做空条件检查 ===')
    c1 = adx >= 22
    c2_ema = ema9 < ema21
    c2_px = last < ema21
    c3_break = last < breakout_down
    print(f'ADX>=22: {"YES" if c1 else f"NO ({adx:.1f}<22)"}')
    print(f'EMA空头排列: {"YES" if c2_ema else "NO"}')
    print(f'价格<EMA21: {"YES" if c2_px else "NO"}')
    print(f'突破40根低点: {"YES" if c3_break else f"NO (需{breakout_down:.0f}, 差{(breakout_down-last)/last*100:.2f}%)"}')
    print()

    print(f'=== 做多条件检查 ===')
    c1l = adx >= 22
    c2l_ema = ema9 > ema21
    c2l_px = last > ema21
    c3l_break = last > breakout_up
    print(f'ADX>=22: {"YES" if c1l else f"NO ({adx:.1f}<22)"}')
    print(f'EMA多头排列: {"YES" if c2l_ema else "NO (空头排列)"}')
    print(f'价格>EMA21: {"YES" if c2l_px else "NO"}')
    print(f'突破40根高点: {"YES" if c3l_break else f"NO (需{breakout_up:.0f}, 差+{(breakout_up-last)/last*100:.2f}%)"}')

asyncio.run(main())
