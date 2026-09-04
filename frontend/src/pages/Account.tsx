// 账户资产页：模拟/实盘统一一套界面，数据源随系统级模式（venue）切换。
// autopilot 固定 paper，模式切换只影响此处查看的账户；模拟模式额外提供「重置」入口。
import { useEffect, useState, useCallback } from 'react';
import {
  Card, Col, Row, Statistic, Table, Tag, Empty, Typography, Button, Space, Popconfirm, App,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import RoundTripTable from '../components/RoundTripTable';
import { useAppStore } from '../store/useAppStore';
import { useAccountStore } from '../store/useAccountStore';
import {
  getAccountSummary, getPaperAccount, resetPaperAccount,
} from '../api/endpoints';
import { fmtPx, pnlColor } from '../utils/format';
import type { PaperAccount } from '../api/types';

interface PosRow {
  key: string; inst: string; dir: '多' | '空'; sz: number;
  openPx: number | null; markPx: number; upl: number | null;
}

export default function Account() {
  const { message } = App.useApp();
  const venue = useAppStore((s) => s.venue);
  const hasKey = useAppStore((s) => s.hasKey);
  const nav = useNavigate();
  const [paper, setPaper] = useState<PaperAccount | null>(null);
  const summary = useAccountStore((s) => s.summary);
  const positions = useAccountStore((s) => s.positions);
  const setAccount = useAccountStore((s) => s.setAccount);
  const [loading, setLoading] = useState(false);
  const [resetKey, setResetKey] = useState(0);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      if (venue === 'paper') {
        setPaper(await getPaperAccount());
      } else if (hasKey) {
        const s = await getAccountSummary();
        setAccount(s);
      }
    } finally {
      setLoading(false);
    }
  }, [venue, hasKey, setAccount]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh, resetKey]);

  const isPaper = venue === 'paper';

  // 实盘未连 Key：给提示，不渲染面板
  if (!isPaper && !hasKey) {
    return (
      <Empty description="未连接 OKX Key，仅模拟模式可用" style={{ padding: 60 }}>
        <Button type="primary" onClick={() => nav('/settings')}>去配置 API Key</Button>
      </Empty>
    );
  }

  // 统一取数（按模式切换数据源）
  const equity = isPaper ? (paper?.equity ?? 0) : (summary?.totalEq ?? 0);
  const marginUsed = isPaper ? 0
    : positions.reduce((a, p) => a + (p.notionalUsd || 0) / (Number(p.lever) || 1 || 1), 0);
  const available = isPaper ? (paper?.usdt ?? 0) : Math.max(0, (summary?.totalEq ?? 0) - marginUsed);
  const pnl = isPaper
    ? ((paper?.equity ?? 0) - (paper?.initial ?? 0))
    : positions.reduce((a, p) => a + (p.upl || 0), 0);
  const pnlPct = isPaper ? (paper?.initial ? (pnl / paper.initial) * 100 : 0) : 0;
  const posCount = isPaper ? (paper?.positions?.length ?? 0) : positions.length;

  // 统一持仓行（模拟现货无开仓价/未实现 → '--'）
  const posRows: PosRow[] = isPaper
    ? (paper?.positions ?? []).map((p) => ({
        key: p.inst_id, inst: p.inst_id, dir: '多' as const, sz: p.sz,
        openPx: null, markPx: p.last, upl: null,
      }))
    : positions.map((p) => ({
        key: p.instId + (p.posSide || ''), inst: p.instId, dir: (p.pos > 0 ? '多' : '空') as '多' | '空',
        sz: Math.abs(p.pos), openPx: p.avgPx, markPx: p.markPx, upl: p.upl,
      }));

  const doReset = async () => {
    await resetPaperAccount(paper?.initial || 10000);
    setResetKey((k) => k + 1);
    message.success('模拟盘已重置（记录与资金全部清空，节流计数归零）');
  };

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <Typography.Text type="secondary">
          {isPaper ? '本地模拟盘 · 虚拟 USDT · 真实行情与费率' : 'OKX 实盘账户 · 真实资金'}
        </Typography.Text>
        <Space>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={refresh}>刷新</Button>
          {isPaper && (
            <Popconfirm
              title="重置模拟盘？"
              description="清空持仓/订单/成交/闭环记录/决策历史，资金恢复初始值，节流计数归零。不影响实盘账户。"
              onConfirm={doReset}
              okText="确认重置" cancelText="取消"
            >
              <Button danger size="small">重置模拟盘</Button>
            </Popconfirm>
          )}
        </Space>
      </div>

      <Row gutter={16}>
        <Col xs={12} md={6}>
          <Card size="small"><Statistic title="权益" value={equity} precision={2} suffix="USDT" loading={loading} /></Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small"><Statistic title="可用资金" value={available} precision={2} suffix="USDT" loading={loading} /></Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic title="累计盈亏" value={pnl} precision={2}
              suffix={isPaper ? `(${pnlPct.toFixed(2)}%)` : ''}
              valueStyle={{ color: pnlColor(pnl) || undefined }} loading={loading} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small"><Statistic title="持仓" value={posCount} suffix="个" loading={loading} /></Card>
        </Col>
      </Row>

      <Card size="small" title="持仓明细" style={{ marginTop: 16 }} styles={{ body: { padding: 0 } }}>
        <Table
          size="small" rowKey={(r) => r.key} dataSource={posRows} pagination={false}
          locale={{ emptyText: <Empty description={isPaper ? '无持仓（自动驾驶在模拟盘开仓后此处显示）' : '无持仓'} style={{ padding: 30 }} /> }}
          columns={[
            { title: '标的', dataIndex: 'inst', width: 120 },
            { title: '方向', dataIndex: 'dir', width: 60, render: (d: string) => <Tag color={d === '多' ? 'red' : 'green'}>{d}</Tag> },
            { title: '数量', dataIndex: 'sz', render: (v: number) => v?.toFixed(6) },
            { title: '开仓价', dataIndex: 'openPx', render: (v: number | null) => v ? fmtPx(v) : '--' },
            { title: '标记价', dataIndex: 'markPx', render: fmtPx },
            { title: '未实现盈亏', dataIndex: 'upl', render: (v: number | null) => v == null ? '--' : <span style={{ color: pnlColor(v) || undefined }}>{v.toFixed(2)}</span> },
          ]}
        />
      </Card>

      <Card size="small" title="交易闭环（开仓价 / 平仓价 / 资费 / 净收入）" style={{ marginTop: 16 }} styles={{ body: { padding: 0 } }}>
        <RoundTripTable key={venue} venue={venue} />
      </Card>
    </div>
  );
}
