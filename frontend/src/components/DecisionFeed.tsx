// 自动驾驶决策记录：最近每根 K 线收盘后的评估结果（观望/信号/拦截）。
// 让用户一眼看到"系统在工作"——每 5 秒轮询 brain history。
import { useEffect, useState } from 'react';
import { Card, Tag, Typography, Tooltip, Empty, Space, Badge } from 'antd';
import { getBrainHistory } from '../api/endpoints';
import { fmtClock, fmtTimeShort, humanizeReason } from '../utils/format';

interface Row {
  ts: number;
  action: string;
  reason: string;
  payload?: any;
}

function metaOf(action: string): { text: string; color: string } {
  if (action.startsWith('decide:wait')) return { text: '观望', color: 'default' };
  if (action.includes(':pass')) {
    return action.includes('long') ? { text: '开多信号 ✓', color: 'success' } : { text: '开空信号 ✓', color: 'success' };
  }
  if (action.includes(':fail')) return { text: '信号拦截', color: 'warning' };
  return { text: action, color: 'default' };
}

export default function DecisionFeed({ limit = 15 }: { limit?: number }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [stale, setStale] = useState(false);

  useEffect(() => {
    let dead = false;
    const poll = async () => {
      try {
        const h = await getBrainHistory(limit);
        if (dead) return;
        setRows(h || []);
        const newest = h?.[0]?.ts || 0;
        setStale(Date.now() - newest > 30 * 60_000);
      } catch { /* ignore */ }
    };
    poll();
    const t = setInterval(poll, 5000);
    return () => { dead = true; clearInterval(t); };
  }, [limit]);

  return (
    <Card
      size="small"
      title={
        <Space>
          <Badge status={stale ? 'warning' : 'processing'} />
          <span>自动驾驶决策记录</span>
          <Typography.Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
            每根 K 线收盘评估一次（形态 → 评分 → 守卫）
          </Typography.Text>
        </Space>
      }
      extra={rows.length ? <Typography.Text type="secondary" style={{ fontSize: 11, whiteSpace: "nowrap" }}>更新于 {fmtClock(Date.now())}</Typography.Text> : null}
    >
      {rows.length === 0 ? (
        <Empty description="暂无决策记录，系统将在 K 线收盘后评估" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <div style={{ maxHeight: 300, overflowY: 'auto', paddingRight: 4 }}>
        {rows.map((r, i) => {
          const m = metaOf(r.action);
          const REGIME_CN: Record<string, string> = {
            trend_up: '多头', trend_down: '空头', range: '震荡', low_vol: '低波',
            high_vol: '高波', extreme: '极端', unknown: '未知',
          };
          const regime = r.payload?.regime
            ? REGIME_CN[String(r.payload.regime)] || String(r.payload.regime)
            : '';
          return (
            <div
              key={i}
              style={{
                display: 'flex', alignItems: 'center', gap: 8,
                padding: '5px 2px', borderBottom: i === rows.length - 1 ? 'none' : '1px solid rgba(0,0,0,0.04)',
                fontSize: 12,
              }}
            >
              <Typography.Text type="secondary" style={{ width: 48, flexShrink: 0, textAlign: 'right', whiteSpace: 'nowrap', fontVariantNumeric: 'tabular-nums' }}>
                {fmtClock(r.ts)}
              </Typography.Text>
              <Tag color={m.color} style={{ margin: 0, flexShrink: 0 }}>{m.text}</Tag>
              {regime && <Tag style={{ margin: 0, flexShrink: 0 }}>{regime}</Tag>}
              <Tooltip title={r.reason}>
                <div
                  className="reason-pre"
                  style={{ flex: 1, minWidth: 0, fontSize: 12, color: 'rgba(0,0,0,0.45)', whiteSpace: 'pre-wrap', lineHeight: '16px' }}
                >
                  {humanizeReason(r.reason)}
                </div>
              </Tooltip>
            </div>
          );
        })}
        </div>
      )}
    </Card>
  );
}
