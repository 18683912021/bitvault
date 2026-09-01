import { create } from 'zustand';
import type { AccountSummary, Position, CurvePoint } from '../api/types';

interface AccountState {
  summary: AccountSummary | null;
  positions: Position[];
  curve: CurvePoint[];
  setAccount: (data: { summary: AccountSummary; positions: Position[] }) => void;
  setCurve: (c: CurvePoint[]) => void;
}

export const useAccountStore = create<AccountState>((set) => ({
  summary: null,
  positions: [],
  curve: [],
  setAccount: (data) =>
    set({ summary: data.summary, positions: data.positions || [] }),
  setCurve: (c) => set({ curve: c }),
}));
