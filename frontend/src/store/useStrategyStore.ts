import { create } from 'zustand';
import type { StrategyInstance, LogEntry } from '../api/types';

interface StrategyState {
  instances: StrategyInstance[];
  setInstances: (i: StrategyInstance[]) => void;
  appendLog: (l: LogEntry) => void;
  handleStrategyEvent: (data: any) => void;
}

export const useStrategyStore = create<StrategyState>((set) => ({
  instances: [],
  setInstances: (i) => set({ instances: i }),
  appendLog: (l) =>
    set((s) => ({
      instances: s.instances.map((inst) =>
        inst.id === l.instance_id
          ? { ...inst, logs: [...(inst.logs || []), l].slice(-200) }
          : inst
      ),
    })),
  handleStrategyEvent: (data) => {
    if (data && data.event === 'started' && data.instance_id != null) {
      set((s) => ({
        instances: s.instances.map((i) =>
          i.id === data.instance_id
            ? { ...i, status: 'running_demo', started_at: Date.now() }
            : i
        ),
      }));
    } else if (data && data.event === 'stopped' && data.instance_id != null) {
      set((s) => ({
        instances: s.instances.map((i) =>
          i.id === data.instance_id
            ? { ...i, status: 'stopped', stopped_at: Date.now() }
            : i
        ),
      }));
    }
  },
}));
