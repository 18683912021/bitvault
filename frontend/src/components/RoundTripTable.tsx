// 交易闭环表：FIFO 开平配对，展示开仓价 / 平仓价 / 资费 / 净收入，模拟盘与 OKX 通用。
import { useEffect, useState, useCallback } from 'react';
import { Table, Empty, Statistic, Row, Col } from 'antd';
import { getRoundtrips } from '../api/endpoints';
import { fmtPx, fmtTime, pnlColor } from '../utils/format';
import type { RoundTrip, RoundTripSummary } from '../api/types';

export function fmtSource(s?: string): string {
  if (!s || s === 'manual') return '手动';
  if (s.startsWith('autopilot')) return '自动驾驶';
  if (s.startsWith('strategy')) return `策略${s.replace(/^strategy[:_]*/, '')}`;
  return s;
}

function fmtHold(s?: number): string {
  if (!s || s <= 0) return '--';
  if (s < 60) return `${s}秒`;
  if (s < 3600) return `${Math.floor(s / 60)}分钟`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}小时`;
  return `${(s / 86400).toFixed(1)}天`;
}

const pad = { padding: '12px 16px 0' } as const;

export default function RoundTripTable({ venue, pageSize = 10 }: { venue: 'paper' | 'okx'; pageSize?: number }) {
  const [data, setData] = useState<RoundTripSummary | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try { setData(await getRoundtrips(venue, 200)); } finally { setLoading(false); }
  }, [venue]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  const closed = [...(data?.closed ?? [])].reverse(); // 最新平仓在前
  const pnlSum = closed.reduce((a, r) => a + (r.pnl || 0), 0);
  const feeSum = closed.reduce((a, r) => a + (r.fee || 0), 0);
  const wins = closed.filter((r) => r.pnl > 0).length;
  const winRate = closed.length ? (wins / closed.length) * 100 : 0;

  return (
    <div>
      <Row gutter={16} style={pad}>
        <Col xs={8} md={4}>
          <Statistic title="已平仓" value={data?.total ?? 0} suffix="笔" loading={loading} />
        </Col>
        <Col xs={8} md={5}>
          <Statistic title="累计净收入" value={pnlSum} precision={4} suffix="USDT"
            valueStyle={{ color: pnlColor(pnlSum) || undefined }} loading={loading} />
        </Col>
        <Col xs={8} md={5}>
          <Statistic title="累计资费" value={feeSum} precision={4} suffix="USDT" loading={loading} />
        </Col>
        <Col xs={8} md={4}>
          <Statistic title="胜率" value={winRate} precision={1} suffix="%" loading={loading} />
        </Col>
        <Col xs={16} md={6}>
          <Statistic title="未平仓" value={data?.open_qty ?? 0} precision={6}
            suffix={data && data.open_qty > 1e-9 ? `BTC @ ${fmtPx(data.open_avg_px)}` : ''} loading={loading} />
        </Col>
      </Row>
      <Table
        style={{ marginTop: 8, padding: '0 16px 8px' }}
        size="small"
        rowKey={(r: RoundTrip) => `${r.open_ts}-${r.close_ts}-${r.open_px}`}
        dataSource={closed}
        loading={loading}
        pagination={closed.length > pageSize ? { pageSize, size: 'small' } : false}
        locale={{ emptyText: <Empty description="暂无已平仓交易" style={{ padding: 30 }} /> }}
        columns={[
          { title: '平仓时间', dataIndex: 'close_ts', width: 150, render: fmtTime },
          { title: '开仓价', dataIndex: 'open_px', width: 100, render: fmtPx },
          { title: '平仓价', dataIndex: 'close_px', width: 100, render: fmtPx },
          { title: '数量', dataIndex: 'sz', width: 90, render: (v: number) => v?.toFixed(6) },
          { title: '资费', dataIndex: 'fee', width: 90, render: (v: number) => <span style={{ color: '#8c8c8c' }}>-{v?.toFixed(4)}</span> },
          { title: '净收入', dataIndex: 'pnl', width: 110, render: (v: number) => (
            <span style={{ color: pnlColor(v) || undefined }}>{v > 0 ? '+' : ''}{v?.toFixed(4)}</span>
          ) },
          { title: '收益率', dataIndex: 'pnl_pct', width: 90, render: (v: number) => (
            <span style={{ color: pnlColor(v) || undefined }}>{v > 0 ? '+' : ''}{((v || 0) * 100).toFixed(2)}%</span>
          ) },
          { title: '持仓', dataIndex: 'hold_s', width: 80, render: fmtHold },
          { title: '来源', dataIndex: 'source', width: 90, render: (s: string) => fmtSource(s) },
        ]}
      />
    </div>
  );
}
