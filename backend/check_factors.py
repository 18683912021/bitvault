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
    ema9 = factors.get('ema_fast', 0)
    ema21 = factors.get('ema_slow', 0)
    adx = factors.get('adx_14', 0)
    rsi = factors.get('rsi_14', 0)
    regime = factors.get('regime', '')
    print(f'当前价: {last:.0f}')
    print(f'EMA9: {ema9:.0f}  EMA21: {ema21:.0f}')
    if ema9 > ema21:
        print('EMA排列: 多头排列(EMA9>EMA21)')
    else:
        print('EMA排列: 空头排列(EMA9<EMA21)')
    if last > ema21:
        print('价格位置: EMA21上方')
    else:
        print('价格位置: EMA21下方')
    print(f'ADX: {adx:.1f} (趋势强度阈值=22)')
    print(f'RSI: {rsi:.1f}')
    print(f'regime: {regime}')
    print(f'regime_reason: {factors.get("regime_reason", "")}')
    closes = [c['c'] for c in candles[-24:]]
    change = (closes[-1] - closes[0]) / closes[0] * 100
    print(f'24h涨跌幅: {change:+.2f}%')
    high = max(c['h'] for c in candles[-24:])
    low = min(c['l'] for c in candles[-24:])
    print(f'24h最高: {high:.0f}  最低: {low:.0f}  振幅: {(high-low)/low*100:.2f}%')

asyncio.run(main())
