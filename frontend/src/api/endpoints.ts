import { api } from './client';
import type {
  SystemStatus, Instrument, Ticker, Book5, Trade, Candle, CurvePoint,
  AccountSummary, AccountConfig, Position, Order, TradeRecord,
  StrategyTemplate, StrategyInstance, BacktestListItem, BacktestDetail,
  RiskStatus, RiskEvent, Notification, AuditLog, RiskRuleEntry,
  Mode, PaperAccount, BrainStatus, BrainHistory,
  RoundTripSummary, Forecast24,
} from './types';

// ---------- 系统 ----------
export const getStatus = () => api.get<SystemStatus>('/status');

// ---------- 系统级模式（venue） ----------
export interface VenueState {
  venue: 'paper' | 'okx';
  has_key: boolean;
  live_ready?: boolean;
}
export const getVenue = () => api.get<VenueState>('/venue');
export const setVenue = (venue: 'paper' | 'okx') =>
  api.post<{ ok: boolean; venue: 'paper' | 'okx'; requested: string; fallback: boolean }>('/venue', { venue });

// ---------- 驾驶标的（币种 + 合约/现货） ----------
export const getInstrument = () => api.get<{ inst_id: string }>('/instrument');
export const setInstrument = (inst_id: string) =>
  api.post<{ ok: boolean; inst_id: string; prev_inst_id: string; braked: boolean }>('/instrument', { inst_id });

// ---------- API Key ----------
export interface ApiKeyRow {
  id: number;
  name: string;
  env: 'demo' | 'live';
  key_masked: string;
  permission: string;
  is_active: number;
  created_at: number;
}
export interface ApiKeyIn {
  name: string;
  api_key: string;
  secret: string;
  passphrase: string;
  env: 'demo' | 'live';
}
export const listKeys = () => api.get<ApiKeyRow[]>('/keys');
export const addKey = (body: ApiKeyIn) => api.post<{ id: number }>('/keys', body);
export const activateKey = (id: number) => api.post<{ ok: boolean }>(`/keys/${id}/activate`);
export const deleteKey = (id: number) => api.del<{ ok: boolean }>(`/keys/${id}`);
export const testKey = (body: ApiKeyIn) =>
  api.post<{ ok: boolean; acctLv?: number; posMode?: string; uid?: string; code?: string; msg?: string }>('/keys/test', body);

// ---------- 行情 ----------
export const getTicker = (instId: string) => api.get<Ticker>('/market/ticker', { instId });
export const getInstruments = () => api.get<Instrument[]>('/market/instruments');
export const getCandles = (instId: string, period = '1m', limit = 300) =>
  api.get<Candle[]>('/market/candles', { instId, period, limit });
export const getDepth = (instId: string) => api.get<Book5>('/market/depth', { instId });
export const getMarketTrades = (instId: string) => api.get<Trade[]>('/market/trades', { instId });
export const getFundingRate = (instId = 'BTC-USDT-SWAP') =>
  api.get<Record<string, any>>('/market/funding-rate', { instId });
export const getForecast = (instId?: string) =>
  api.get<Forecast24>('/market/forecast', instId ? { instId } : {});
export const getForecast10 = (instId?: string) =>
  api.get<Forecast24>('/market/forecast10', instId ? { instId } : {});
export const downloadHistory = (body: { inst_id?: string; period?: string; days?: number }) =>
  api.post<{ ok: boolean; msg: string }>('/market/download-history', body);

// ---------- 账户 ----------
export const getAccountSummary = () =>
  api.get<{ summary: AccountSummary; positions: Position[]; config: AccountConfig }>('/account/summary');
export const getAccountCurve = (days = 30) =>
  api.get<CurvePoint[]>('/account/curve', { days });

// ---------- 交易 ----------
export interface OrderIn {
  inst_id: string;
  side: 'buy' | 'sell';
  ord_type?: 'market' | 'limit' | 'post_only';
  px?: number | null;
  sz_base: number;
  reduce_only?: boolean;
  td_mode?: string | null;
  venue?: 'okx' | 'paper';
}
export const placeOrder = (body: OrderIn) =>
  api.post<{ ok: boolean; order: Order }>('/order/place', body);
export const cancelOrder = (cl_ord_id: string) =>
  api.post<{ ok: boolean; state: string }>('/order/cancel', { cl_ord_id });
export const getOrders = (state: 'open' | 'history' = 'open', limit = 100, venue?: 'okx' | 'paper') =>
  api.get<Order[]>('/orders', { state, limit, ...(venue ? { venue } : {}) });
export const getTrades = (limit = 100, venue?: 'okx' | 'paper') =>
  api.get<TradeRecord[]>('/trades', { limit, ...(venue ? { venue } : {}) });
export const closePosition = (inst_id: string) =>
  api.post<{ ok: boolean }>('/account/close-position', { inst_id });
export const getRoundtrips = (venue: 'paper' | 'okx' = 'paper', limit = 200, inst_id?: string) =>
  api.get<RoundTripSummary>('/roundtrips', { venue, limit, ...(inst_id ? { inst_id } : {}) });

// ---------- 本地模拟盘 ----------
export const getPaperAccount = () => api.get<PaperAccount>('/paper/account');
export const resetPaperAccount = (initial = 10000) =>
  api.post<{ ok: boolean; account: PaperAccount }>('/paper/reset', { initial });

// ---------- 决策大脑 + Autopilot ----------
export const getBrainStatus = () => api.get<BrainStatus>('/brain/status');
export const startAutopilot = (overrides?: { period?: string; min_confidence?: number }) =>
  api.post<{ ok: boolean; config: any }>('/brain/autopilot/start', overrides || {});
export const stopAutopilot = () =>
  api.post<{ ok: boolean; config: any }>('/brain/autopilot/stop', undefined, { confirm: 'true' });
export const updateBrainConfig = (body: Record<string, any>) =>
  api.post<{ ok: boolean; config: any }>('/brain/config', body);
export const getBrainHistory = (limit = 20) =>
  api.get<BrainHistory[]>('/brain/history', { limit });

// ---------- 策略 ----------
export const getTemplates = () => api.get<StrategyTemplate[]>('/strategies/templates');
export const getInstances = () => api.get<StrategyInstance[]>('/strategies/instances');
export interface InstanceIn {
  strategy_type: string;
  name: string;
  inst_id: string;
  mode: Mode;
  params: Record<string, any>;
  period: string;
}
export const createInstance = (body: InstanceIn) =>
  api.post<{ id: number }>('/strategies/instances', body);
export const startInstance = (id: number) =>
  api.post<{ ok: boolean; status: string }>(`/strategies/instances/${id}/start`);
export const stopInstance = (id: number, close_position = false) =>
  api.post<{ ok: boolean }>(`/strategies/instances/${id}/stop`, { close_position });
export const pauseInstance = (id: number) =>
  api.post<{ ok: boolean }>(`/strategies/instances/${id}/pause`, undefined, { confirm: 'true' });
export const resumeInstance = (id: number) =>
  api.post<{ ok: boolean }>(`/strategies/instances/${id}/resume`, undefined, { confirm: 'true' });
export const deleteInstance = (id: number) =>
  api.del<{ ok: boolean }>(`/strategies/instances/${id}`, { confirm: 'true' });
export const getInstanceLogs = (id: number) =>
  api.get<any[]>(`/strategies/instances/${id}/logs`);

// ---------- 回测 ----------
export interface BacktestRunIn {
  strategy_type: string;
  params?: Record<string, any>;
  inst_id?: string;
  period?: string;
  days?: number;
  fee_rate?: number;
  slippage?: number;
  initial_equity?: number;
}
export const runBacktest = (body: BacktestRunIn) =>
  api.post<{ id: number; status: string }>('/backtest/run', body);
export const listBacktests = () => api.get<BacktestListItem[]>('/backtest/list');
export const getBacktestDetail = (id: number) => api.get<BacktestDetail>(`/backtest/${id}`);

// ---------- 风控 ----------
export const getRiskStatus = () => api.get<RiskStatus>('/risk/status');
export const getRiskRules = () => api.get<Record<string, RiskRuleEntry>>('/risk/rules');
export const updateRiskRule = (rule_type: string, params: Record<string, any>, enabled: boolean) =>
  api.post<{ ok: boolean }>('/risk/rules', { type: rule_type, params, enabled });
export const getRiskEvents = (limit = 100) => api.get<RiskEvent[]>('/risk/events', { limit });
export const killSwitch = () =>
  api.post<{ ok: boolean; report: any }>('/risk/kill-switch', undefined, { confirm: 'true' });
export const riskResume = () => api.post<{ ok: boolean }>('/risk/resume');

// ---------- 通知 / 审计 ----------
export const getNotifications = (limit = 50) =>
  api.get<Notification[]>('/notifications', { limit });
export const getAuditLogs = (limit = 100) =>
  api.get<AuditLog[]>('/audit-logs', { limit });
