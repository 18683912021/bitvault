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

/** 定宽时钟 HH:mm（决策记录/挂单等窄列用，避免 locale 差异导致换行） */
export function fmtClock(ts?: number | null): string {
  if (!ts) return '--';
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 定宽日期时钟 MM-DD HH:mm（移动端决策记录用） */
export function fmtClockShort(ts?: number | null): string {
  if (!ts) return '--';
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function fmtTimeShort(ts?: number | null): string {
  // 定宽 HH:mm:ss（toLocaleTimeString 随 locale 输出秒位变化，窄列会换行）
  if (!ts) return '--';
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

const REGIME_CN: Record<string, string> = {
  trend_up: '多头趋势', trend_down: '空头趋势', range: '震荡区间',
  low_vol: '低波动', high_vol: '高波动', extreme: '异常波动', unknown: '状态未知',
};
const SETUP_CN: Record<string, string> = {
  none: '无', pullback: '回踩', breakout_retest: '突破回踩',
};
const HTF_CN: Record<string, string> = {
  up: '多头', down: '空头', range: '震荡', unknown: '未知', undefined: '未确认',
};

/** 历史机器格式决策记录（regime=... long=... htf4h=...）→ 人类可读中文。
    新版本后端已直接写中文，此函数兜底翻译旧记录。 */
export function humanizeReason(raw: string): string {
  const r = raw || '';
  if (!r) return r;
  // 新格式（观望：市场…/多头方向：…）直接可读
  if (r.startsWith('【') || r.startsWith('观望：') || r.startsWith('多头方向：') || r.startsWith('空头方向：')) return r;
  if (!/regime=/.test(r)) return r;
  const pick = (k: string) => r.match(new RegExp(`${k}=([\\w]+)`))?.[1];
  const out: string[] = [];
  const regime = pick('regime');
  if (regime) out.push(`市场${REGIME_CN[regime] || regime}`);
  for (const [key, cn] of [['long', '多头'], ['short', '空头']] as const) {
    const m = r.match(new RegExp(`${key}=([\\w]+)\\(([^)]*)\\)`));
    if (m) {
      const [_, setup, noteRaw] = m;
      const note = noteRaw.replace(/^\(/, '');   // 正则把开括号带进了捕获组
      if (setup === 'none') { out.push(`${cn}：没有出现入场形态`); continue; }
      if (note.includes('非指定形态')) {
        const want = note.replace('非指定形态(需', '');
        out.push(`${cn}：出现 ${SETUP_CN[setup] || setup} 形态，但策略只认 ${SETUP_CN[want] || want || '指定形态'}`);
        continue;
      }
      out.push(`${cn}：${SETUP_CN[setup] || setup}`);
    } else {
      const sc = r.match(new RegExp(`${key}=score=([\\d.]+)/(pass|fail)`));
      if (sc) out.push(`${cn}：评分 ${sc[1]}，${sc[2] === 'pass' ? '通过守卫' : '未通过守卫'}`);
    }
  }
  const h4 = pick('htf4h');
  const mid = pick('htf_mid');
  if (h4) out.push(`4H${HTF_CN[h4] || h4}${mid ? `、日线${HTF_CN[mid] || mid}` : ''}确认`);
  return out.join('；') + '，继续观望';
}

export function pnlColor(v: number | undefined | null): string {
  if (!v) return '';
  return v > 0 ? '#3f8600' : v < 0 ? '#cf1322' : ''; // 盈利绿、亏损红（与底色惯例一致）
}

// 常用周期选项（24 小时不间断：支持 1m~1D 全周期）
export const PERIODS = ['1m', '3m', '5m', '15m', '30m', '1H', '2H', '4H', '1D'] as const;
