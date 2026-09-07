import { create } from 'zustand';
import type { Env, RiskStatus, Notification } from '../api/types';

interface AppState {
  env: Env;
  hasKey: boolean;
  wsConnected: boolean;
  risk: RiskStatus | null;
  notifications: Notification[];
  unread: number;
  venue: 'paper' | 'okx';          // 系统级模式：切换后账户/订单/总览数据源 + autopilot 执行通道均跟随（paper 虚拟/okx 真实）
  instId: string;                  // 当前驾驶标的（币种+合约/现货，如 BTC-USDT / BTC-USDT-SWAP）
  autopilotOn: boolean;            // 全局 autopilot 运行状态（顶栏开关同步）
  setEnv: (e: Env) => void;
  setHasKey: (b: boolean) => void;
  setWsConnected: (b: boolean) => void;
  setRisk: (r: RiskStatus) => void;
  handleRiskEvent: (data: any) => void;
  setNotifications: (n: Notification[]) => void;
  setVenue: (v: 'paper' | 'okx') => void;
  setInstId: (id: string) => void;
  setAutopilotOn: (b: boolean) => void;
}

export const useAppStore = create<AppState>((set) => ({
  env: 'none',
  hasKey: false,
  wsConnected: false,
  risk: null,
  notifications: [],
  unread: 0,
  venue: 'paper',
  instId: 'BTC-USDT',
  autopilotOn: false,
  setEnv: (e) => set({ env: e }),
  setHasKey: (b) => set({ hasKey: b }),
  setWsConnected: (b) => set({ wsConnected: b }),
  setRisk: (r) => set({ risk: r }),
  handleRiskEvent: (data) => {
    // 风控状态可能在此一并更新（circuit_breaker / kill_switch / resumed）
    if (data && data.event === 'resumed' && data) {
      set((s) => ({
        risk: s.risk ? { ...s.risk, halted: false, halt_reason: '' } : s.risk,
      }));
    }
  },
  setNotifications: (n) => set({ notifications: n, unread: n.filter((x) => !x.read).length }),
  setVenue: (v) => set({ venue: v }),
  setInstId: (id) => set({ instId: id }),
  setAutopilotOn: (b) => set({ autopilotOn: b }),
}));
