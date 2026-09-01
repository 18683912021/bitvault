import { create } from 'zustand';
import type { Order, TradeRecord } from '../api/types';

interface OrderState {
  orders: Order[];
  trades: TradeRecord[];
  setOrders: (o: Order[]) => void;
  setTrades: (t: TradeRecord[]) => void;
  upsertOrder: (o: Order) => void;
}

export const useOrderStore = create<OrderState>((set) => ({
  orders: [],
  trades: [],
  setOrders: (o) => set({ orders: o }),
  setTrades: (t) => set({ trades: t }),
  upsertOrder: (o) =>
    set((s) => {
      const idx = s.orders.findIndex((x) => x.cl_ord_id === o.cl_ord_id);
      const next = [...s.orders];
      if (idx >= 0) next[idx] = { ...next[idx], ...o };
      else next.unshift(o);
      return { orders: next };
    }),
}));
