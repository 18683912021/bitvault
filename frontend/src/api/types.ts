// 与后端 (backend/app) 对齐的 TypeScript 类型定义。

export type Env = 'none' | 'demo' | 'live';

export type Mode = 'demo' | 'live' | 'paper';

export interface PaperPosition {
  inst_id: string;
  sz: number;
  last: number;
  notional: number;
}

export interface PaperAccount {
  equity: number;
  usdt: number;
  initial: number;
  positions: PaperPosition[];
  pnl: number;
}

// ---------- 决策大脑 ----------
export interface BrainStatus {
  enabled: boolean;
  running: boolean;
  period: string;
  engine?: string;
  venue: 'paper' | 'okx';
  live_ready?: boolean;
  last_action: string;
  last_decision: BrainDecision | null;
  last_error: string;
  position: any;
  regime?: string;
  regime_reason?: string;
  throttle?: {
    sleeping: boolean;
    sleep_until: number;
    cooldown_until: number;
    opens_today: number;
    max_opens_per_day: number;
    loss_streak: number;
    cooldown_min: number;
    loss_pause_n: number;
    loss_pause_min: number;
  };
  mode?: 'normal' | 'sprint';
  leverage?: number;
}

export interface BrainDecision {
  action: 'long' | 'short' | 'flat' | 'close';
  entry_px: number;
  sl_px: number;
  tp_px: number;
  size_pct: number;
  confidence: number;
  reason: string;
  latency_ms?: number;
  inst_id?: string;
  ts?: number;
  factors?: Record<string, any>;
}

export interface BrainHistory {
  ts: number;
  action: string;
  reason: string;
  payload: BrainDecision;
}

export interface ParamSchema {
  key: string;
  label: string;
  type: 'int' | 'float' | 'select' | 'string';
  default?: number | string;
  min?: number;
  max?: number;
  options?: { value: string; label: string }[];
}

export interface StrategyTemplate {
  type: string;
  label: string;
  params_schema: ParamSchema[];
}

export interface Instrument {
  instId: string;
  instType: 'SPOT' | 'SWAP';
  lotSz: number;
  minSz: number;
  tickSz: number;
  ctVal: number;
  settleCcy: string;
  state?: string;
}

export interface Ticker {
  instId: string;
  last: number;
  open24h: number;
  vol24h: number;
  askPx: number;
  bidPx: number;
  ts: number;
}

export interface Book5 {
  instId: string;
  asks: [number, number][];
  bids: [number, number][];
  ts: number;
}

export interface Trade {
  instId: string;
  px: number;
  sz: number;
  side?: 'buy' | 'sell';
  ts: number;
}

export interface Candle {
  ts: number;
  o: number;
  h: number;
  l: number;
  c: number;
  vol: number;
}

export interface AccountConfig {
  acctLv?: number | string;
  acctLvDesc?: string;
  posMode?: string;
  uid?: string;
  canTrade?: boolean;
}

export interface AccountSummary {
  env: Env;
  totalEq: number;
  details?: { ccy: string; availBal?: string; eq?: string }[];
  ts: number;
}

export interface Position {
  instId: string;
  posSide?: string | null;
  pos: number;
  avgPx: number;
  markPx: number;
  upl: number;
  uplRatio: number;
  lever?: string | number;
  mgnMode?: string;
  notionalUsd: number;
  liqPx?: number | null;
  marginRatio?: number | null;
  mgnRatio?: number | null;
  imr: number;
}

export interface CurvePoint {
  ts: number;
  equity: number;
}

export type OrderState =
  | 'pending_submit' | 'live' | 'partially_filled' | 'filled'
  | 'canceled' | 'partially_canceled' | 'failed';

export interface Order {
  id?: number;
  cl_ord_id: string;
  ord_id?: string;
  instance_id?: number | null;
  inst_id: string;
  td_mode?: string;
  side: 'buy' | 'sell';
  pos_side?: string;
  ord_type: string;
  px?: number | null;
  sz: number;
  filled_sz?: number;
  avg_px?: number | null;
  state: OrderState;
  fee?: number;
  source: string;
  venue?: 'okx' | 'paper';
  sl_trigger_px?: number | null;
  error_code?: string;
  error_msg?: string;
  created_at?: number;
  updated_at?: number;
}

export interface TradeRecord {
  id?: number;
  ord_id?: string;
  cl_ord_id?: string;
  inst_id: string;
  side: string;
  px: number;
  sz: number;
  fee: number;
  instance_id?: number | null;
  ts: number;
  venue?: 'okx' | 'paper';   // 由 /trades 端点 JOIN orders 返回
}

// 交易闭环（FIFO 开平配对，/api/roundtrips）
export interface RoundTrip {
  inst_id: string;
  side: string;
  sz: number;
  open_ts: number;
  close_ts: number;
  open_px: number;
  close_px: number;
  fee: number;      // 双边资费合计
  pnl: number;      // 净收入（已扣双边费）
  pnl_pct: number;  // 净收益率（相对开仓成本）
  source: string;
  hold_s: number;   // 持仓秒数
}

export interface RoundTripSummary {
  closed: RoundTrip[];
  total: number;
  open_qty: number;
  open_avg_px: number;
}

export type InstanceStatus =
  | 'stopped' | 'running_demo' | 'running_live' | 'running_paper' | 'paused'
  | 'halted' | 'error' | 'pending_confirm';

export interface LogEntry {
  instance_id: number;
  ts: number;
  level: 'info' | 'warning' | 'error';
  msg: string;
}

export interface StrategyInstance {
  id: number;
  strategy_id: number;
  name: string;
  inst_id: string;
  mode: Mode;
  status: InstanceStatus;
  params_json: string;
  state_json: string;
  started_at?: number | null;
  stopped_at?: number | null;
  pnl?: number;
  last_error?: string | null;
  type: string;
  logs: LogEntry[];
}

export interface BacktestMetrics {
  total_return_pct?: number;
  buy_hold_pct?: number;
  annual_pct?: number;
  max_dd_pct?: number;
  sharpe?: number;
  sortino?: number;
  trade_count?: number;
  win_rate?: number;
  profit_factor?: number | null;
  avg_hold_bars?: number;
  fee_total?: number;
  final_equity?: number;
}

export interface BacktestListItem {
  id: number;
  strategy_type: string;
  inst_id: string;
  period: string;
  start_ts: number;
  end_ts: number;
  status: 'pending' | 'running' | 'done' | 'failed';
  error?: string;
  created_at: number;
  finished_at?: number;
  metrics_json?: string | null;
}

export interface BacktestDetail extends BacktestListItem {
  metrics?: BacktestMetrics;
  equity?: CurvePoint[];
  bh?: CurvePoint[];
  trades?: BacktestTrade[];
}

export interface BacktestTrade {
  ts: number;
  side: string;
  px: number;
  sz: number;
  fee: number;
  realized: number;
  kind: string;
}

export interface RiskRuleEntry {
  params: Record<string, number>;
  enabled: boolean;
}

export interface RiskStatus {
  halted: boolean;
  halt_reason: string;
  day_key?: string;
  day_start_equity?: number;
  daily_pnl_pct: number;
  peak_equity?: number;
  drawdown_pct: number;
  rules: Record<string, RiskRuleEntry>;
  env: Env;
}

export interface RiskEvent {
  id: number;
  ts: number;
  rule_type: string;
  level: string;
  action: string;
  detail_json: string;
}

export interface Notification {
  id: number;
  ts: number;
  level: string;
  title: string;
  body: string;
  read: number;
}

export interface AuditLog {
  id: number;
  ts: number;
  actor: string;
  action: string;
  payload_json: string;
  result: string;
}

export interface SystemStatus {
  env: Env;
  has_key: boolean;
  venue: 'paper' | 'okx';
  time_offset_ms: number;
  market?: Record<string, string>;
  private_ws: string;
  public_ws: string;
  business_ws?: string;
  risk: RiskStatus;
  account_config?: AccountConfig;
  whitelist: string[];
  strategy_types: StrategyTemplate[];
}

// WS 推送消息
export interface WsSnapshot {
  tickers: Record<string, Ticker>;
  env: Env;
  risk: RiskStatus;
}

export type WsTopic =
  | 'snapshot' | 'tick' | 'bar' | 'depth' | 'trade'
  | 'order' | 'account' | 'log' | 'risk' | 'strategy' | 'forecast';

export interface WsMessage {
  topic: WsTopic;
  data: any;
  ts?: number;
}

// 10 分钟涨跌预测（事件合约参考）
export interface ForecastFactor {
  key: string;
  label: string;
  value_text: string;
  score: number;
  weight: number;
  contrib: number;
}

export interface Forecast10 {
  inst_id: string;
  ts: number;
  direction: 'up' | 'down' | 'flat' | 'unknown';
  p_up: number;
  score: number;
  conf_scale: number;
  regime: string;
  regime_reason: string;
  window_start_ts: number;
  window_end_ts: number;
  ref_price: number;
  factors: ForecastFactor[];
  note: string;
}
