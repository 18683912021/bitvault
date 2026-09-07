import { create } from 'zustand';
import type { Ticker, Book5, Trade, Candle, Forecast24 } from '../api/types';

interface MarketState {
  instId: string;
  period: string;
  tickers: Record<string, Ticker>;
  depth: Record<string, Book5>;
  trades: Record<string, Trade[]>;
  candles: Record<string, Candle[]>; // key: `${instId}:${period}`
  forecast: Forecast24 | null;
  setForecast: (f: Forecast24) => void;
  setInstId: (id: string) => void;
  setPeriod: (p: string) => void;
  setTickers: (t: Record<string, Ticker>) => void;
  upsertTicker: (t: Ticker) => void;
  setDepth: (b: Book5) => void;
  pushTrade: (t: Trade) => void;
  pushBar: (b: Candle & { inst_id?: string }) => void;
  setCandles: (instId: string, period: string, rows: Candle[]) => void;
}

const candleKey = (instId: string, period: string) => `${instId}:${period}`;

export const useMarketStore = create<MarketState>((set) => ({
  instId: 'BTC-USDT',
  period: '1m',
  tickers: {},
  depth: {},
  trades: {},
  candles: {},
  forecast: null,
  setForecast: (f) => set({ forecast: f }),
  setInstId: (id) => set({ instId: id }),
  setPeriod: (p) => set({ period: p }),
  setTickers: (t) => set({ tickers: t }),
  upsertTicker: (t) =>
    set((s) => ({ tickers: { ...s.tickers, [t.instId]: t } })),
  setDepth: (b) => set((s) => ({ depth: { ...s.depth, [b.instId]: b } })),
  pushTrade: (t) =>
    set((s) => {
      const lst = [...(s.trades[t.instId] || []), t].slice(-50);
      return { trades: { ...s.trades, [t.instId]: lst } };
    }),
  pushBar: (b) => {
    const instId = b.inst_id || (b as any).instId;
    if (!instId) return;
    set((s) => {
      const k = candleKey(instId, s.period);
      // 仅更新当前周期图（其它周期历史由 REST 拉取）
      const cur = s.candles[k] ? [...s.candles[k]] : [];
      const last = cur[cur.length - 1];
      if (last && last.ts === b.ts) {
        cur[cur.length - 1] = { ts: b.ts, o: b.o, h: b.h, l: b.l, c: b.c, vol: b.vol };
      } else if (!last || b.ts > last.ts) {
        cur.push({ ts: b.ts, o: b.o, h: b.h, l: b.l, c: b.c, vol: b.vol });
        if (cur.length > 600) cur.shift();
      }
      return { candles: { ...s.candles, [k]: cur } };
    });
  },
  setCandles: (instId, period, rows) =>
    set((s) => ({ candles: { ...s.candles, [candleKey(instId, period)]: rows } })),
}));
