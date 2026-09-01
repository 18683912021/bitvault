// 通用格式化工具。
export function fmtNum(v: number | undefined | null, digits = 2): string {
  if (v === undefined || v === null || Number.isNaN(v)) return '--';
  return v.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function fmtUsd(v: number | undefined | null, digits = 2): string {
  if (v === undefined || v === null || Number.isNaN(v)) return '--';
  return '$' + fmtNum(v, digits);
}

export function fmtPct(v: number | undefined | null, digits = 2): string {
  if (v === undefined || v === null || Number.isNaN(v)) return '--';
  const s = v > 0 ? '+' : '';
  return `${s}${fmtNum(v, digits)}%`;
}

export function fmtPx(v: number | undefined | null): string {
  if (v === undefined || v === null || Number.isNaN(v)) return '--';
  return fmtNum(v, v < 1 ? 6 : 2);
}

export function fmtTime(ts?: number | null): string {
  if (!ts) return '--';
  const d = new Date(ts);
  return d.toLocaleString('zh-CN', { hour12: false });
}

export function fmtTimeShort(ts?: number | null): string {
  if (!ts) return '--';
  const d = new Date(ts);
  return d.toLocaleTimeString('zh-CN', { hour12: false });
}

export function pnlColor(v: number | undefined | null): string {
  if (!v) return '';
  return v > 0 ? '#cf1322' : v < 0 ? '#3f8600' : ''; // 中国习惯：红涨绿跌
}

// 常用周期选项
export const PERIODS = ['1m', '5m', '15m', '1H', '4H', '1D'] as const;
