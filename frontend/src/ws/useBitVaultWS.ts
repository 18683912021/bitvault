import { useEffect, useRef } from 'react';
import type { WsMessage, WsSnapshot } from '../api/types';
import { useAppStore } from '../store/useAppStore';
import { useMarketStore } from '../store/useMarketStore';
import { useAccountStore } from '../store/useAccountStore';
import { useOrderStore } from '../store/useOrderStore';
import { useStrategyStore } from '../store/useStrategyStore';

// 单条 /ws 连接：连接即收 snapshot，随后按 topic 分发到各 store；断线指数退避重连。
export function useBitVaultWS() {
  const wsRef = useRef<WebSocket | null>(null);
  const backoff = useRef(1);

  useEffect(() => {
    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(`${proto}://${location.host}/ws`);
      wsRef.current = ws;

      ws.onopen = () => {
        backoff.current = 1;
        useAppStore.getState().setWsConnected(true);
      };

      ws.onmessage = (ev) => {
        try {
          const msg: WsMessage = JSON.parse(ev.data);
          route(msg);
        } catch {
          /* ignore */
        }
      };

      ws.onclose = () => {
        useAppStore.getState().setWsConnected(false);
        setTimeout(connect, Math.min(backoff.current, 30) * 1000);
        backoff.current = Math.min(backoff.current * 2, 30);
      };

      ws.onerror = () => {
        try { ws.close(); } catch { /* ignore */ }
      };
    };
    connect();
    return () => {
      try { wsRef.current?.close(); } catch { /* ignore */ }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}

function route(msg: WsMessage) {
  const { topic, data } = msg;
  switch (topic) {
    case 'snapshot': {
      const s = data as WsSnapshot;
      useMarketStore.getState().setTickers(s.tickers || {});
      if (s.env) useAppStore.getState().setEnv(s.env);
      if (s.risk) useAppStore.getState().setRisk(s.risk);
      break;
    }
    case 'tick':
      useMarketStore.getState().upsertTicker(data);
      break;
    case 'bar':
      useMarketStore.getState().pushBar(data);
      break;
    case 'depth':
      useMarketStore.getState().setDepth(data);
      break;
    case 'trade':
      useMarketStore.getState().pushTrade(data);
      break;
    case 'order':
      useOrderStore.getState().upsertOrder(data);
      break;
    case 'account':
      useAccountStore.getState().setAccount(data);
      break;
    case 'log':
      useStrategyStore.getState().appendLog(data);
      break;
    case 'risk':
      useAppStore.getState().handleRiskEvent(data);
      break;
    case 'strategy':
      useStrategyStore.getState().handleStrategyEvent(data);
      break;
  }
}
