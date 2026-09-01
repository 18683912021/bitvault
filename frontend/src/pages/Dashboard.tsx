import { useEffect } from 'react';
import { Card, Col, Row, Statistic, Table, Tag, Empty, Button, Space, Typography, Alert } from 'antd';
import { ArrowUpOutlined, ArrowDownOutlined, LinkOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import LightweightChart from '../components/LightweightChart';
import { useAppStore } from '../store/useAppStore';
import { useMarketStore } from '../store/useMarketStore';
import { useAccountStore } from '../store/useAccountStore';
import { useStrategyStore } from '../store/useStrategyStore';
import { useOrderStore } from '../store/useOrderStore';
import { getCandles, getInstances, getTrades } from '../api/endpoints';
import { fmtUsd, fmtPct, fmtPx, fmtTime, fmtTimeShort, pnlColor } from '../utils/format';
import type { InstanceStatus } from '../api/types';

const STATUS_COLOR: Record<InstanceStatus, string> = {
  running_demo: 'green', running_live: 'red', running_paper: 'geekblue', paused: 'orange',
  halted: 'volcano', error: 'red', stopped: 'default', pending_confirm: 'gold',
};
const STATUS_TXT: Record<InstanceStatus, string> = {
  running_demo: '运行中(模拟)', running_live: '运行中(实盘)', running_paper: '运行中(本地模拟)', paused: '已暂停',
  halted: '已熔断', error: '异常', stopped: '已停止', pending_confirm: '待确认',
};

export default function Dashboard() {
  const nav = useNavigate();
  const env = useAppStore((s) => s.env);
  const hasKey = useAppStore((s) => s.hasKey);
  const risk = useAppStore((s) => s.risk);
  const notifications = useAppStore((s) => s.notifications);
  const btcTicker = useMarketStore((s) => s.tickers['BTC-USDT']);
  const candles = useMarketStore((s) => s.candles['BTC-USDT:5m']);
  const setCandles = useMarketStore((s) => s.setCandles);
  const summary = useAccountStore((s) => s.summary);
  const positions = useAccountStore((s) => s.positions);
  const instances = useStrategyStore((s) => s.instances);
  const setInstances = useStrategyStore((s) => s.setInstances);
  const trades = useOrderStore((s) => s.trades);
  const setTrades = useOrderStore((s) => s.setTrades);

  useEffect(() => {
    getCandles('BTC-USDT', '5m', 300)
      .then((rows) => setCandles('BTC-USDT', '5m', rows))
      .catch(() => {});
    getInstances().then(setInstances).catch(() => {});
    getTrades(20).then(setTrades).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const equity = summary?.totalEq ?? 0;
  const upl = positions.reduce((a, p) => a + (p.upl || 0), 0);
  const btcChg = btcTicker && btcTicker.open24h
    ? ((btcTicker.last - btcTicker.open24h) / btcTicker.open24h) * 100
    : 0;

  return (
    <div>
      {!hasKey && (
        <Alert
          type="info"
          showIcon
          message="尚未配置 API Key"
          description="公共行情与回测可直接使用；配置 OKX 模拟盘 Key 后可查看账户、持仓与运行策略。"
          action={<Button type="link" onClick={() => nav('/settings')} icon={<LinkOutlined />}>去配置</Button>}
          style={{ marginBottom: 16 }}
        />
      )}
      {risk?.halted && (
        <Alert
          type="error"
          showIcon
          message={`风控已熔断：${risk.halt_reason || '触发熔断'}`}
          description="所有策略已停机，仅允许减仓。请前往风控中心解除（确认风险后）。"
          style={{ marginBottom: 16 }}
          action={<Button danger onClick={() => nav('/risk')}>查看风控</Button>}
        />
      )}

      <Row gutter={16}>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic
              title="账户权益"
              value={equity}
              precision={2}
              prefix="$"
              valueStyle={{ color: pnlColor(equity) || undefined }}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic
              title="今日盈亏"
              value={risk?.daily_pnl_pct ?? 0}
              precision={2}
              suffix="%"
              valueStyle={{ color: pnlColor(risk?.daily_pnl_pct ?? 0) || undefined }}
              prefix={(risk?.daily_pnl_pct ?? 0) >= 0 ? <ArrowUpOutlined /> : <ArrowDownOutlined />}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic
              title="未实现盈亏"
              value={upl}
              precision={2}
              prefix="$"
              valueStyle={{ color: pnlColor(upl) || undefined }}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic
              title="BTC/USDT"
              value={btcTicker?.last ?? 0}
              precision={2}
              suffix={
                <Typography.Text style={{ fontSize: 12, color: pnlColor(btcChg) || '#999' }}>
                  {fmtPct(btcChg)}
                </Typography.Text>
              }
            />
          </Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col xs={24} lg={14}>
          <Card size="small" title="BTC/USDT 5 分钟" extra={<Typography.Text type="secondary" style={{fontSize:12}}>实时</Typography.Text>}>
            {candles && candles.length > 10 ? (
              <LightweightChart candles={candles} height={260} showMA maPeriods={[5, 20]} />
            ) : (
              <Empty description="行情加载中（WS 或 REST 兜底中）" style={{ padding: 60 }} />
            )}
          </Card>
        </Col>
        <Col xs={24} lg={10}>
          <Card size="small" title="策略实例" extra={<Button size="small" type="link" onClick={() => nav('/strategies')}>管理</Button>}>
            {instances.length === 0 ? (
              <Empty description="暂无策略实例" style={{ padding: 30 }} />
            ) : (
              instances.slice(0, 6).map((i) => (
                <div
                  key={i.id}
                  style={{
                    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                    padding: '6px 0', borderBottom: '1px solid #f0f0f0',
                  }}
                >
                  <Space>
                    <Tag color={STATUS_COLOR[i.status]}>{STATUS_TXT[i.status]}</Tag>
                    <Typography.Text strong>{i.name}</Typography.Text>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>{i.type}</Typography.Text>
                  </Space>
                  <Typography.Text style={{ color: pnlColor(i.pnl) || '#999' }}>
                    {fmtUsd(i.pnl || 0)}
                  </Typography.Text>
                </div>
              ))
            )}
          </Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col xs={24} lg={12}>
          <Card size="small" title="最近成交" extra={<Button size="small" type="link" onClick={() => nav('/orders')}>全部</Button>}>
            <Table
              size="small"
              rowKey={(r) => String(r.id)}
              dataSource={trades.slice(0, 8)}
              pagination={false}
              columns={[
                { title: '时间', dataIndex: 'ts', width: 90, render: fmtTimeShort },
                { title: '标的', dataIndex: 'inst_id', width: 120 },
                { title: '方向', dataIndex: 'side', width: 60, render: (s: string) => <Tag color={s === 'buy' ? 'red' : 'green'}>{s === 'buy' ? '买' : '卖'}</Tag> },
                { title: '价格', dataIndex: 'px', render: (v: number) => fmtPx(v) },
                { title: '数量', dataIndex: 'sz', render: (v: number) => v?.toFixed(6) },
              ]}
            />
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card size="small" title="最近通知 / 风控事件" extra={<Button size="small" type="link" onClick={() => nav('/notifications')}>全部</Button>}>
            {notifications.length === 0 ? (
              <Empty description="暂无通知" style={{ padding: 30 }} />
            ) : (
              notifications.slice(0, 8).map((n) => (
                <div key={n.id} style={{ padding: '6px 0', borderBottom: '1px solid #f0f0f0' }}>
                  <Space style={{ width: '100%', justifyContent: 'space-between' }}>
                    <Space>
                      <Tag color={n.level === 'error' ? 'red' : n.level === 'warning' ? 'orange' : 'blue'}>
                        {n.level}
                      </Tag>
                      <Typography.Text>{n.title}</Typography.Text>
                    </Space>
                    <Typography.Text type="secondary" style={{ fontSize: 11 }}>{fmtTime(n.ts)}</Typography.Text>
                  </Space>
                </div>
              ))
            )}
          </Card>
        </Col>
      </Row>
    </div>
  );
}
