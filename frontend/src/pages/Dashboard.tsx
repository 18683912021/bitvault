// 总览页：自动驾驶状态 + 账户概览 + 行情图表 + 最近成交/通知。
// 新手第一眼看到的就是 autopilot 在干什么。
import { useEffect, useState } from 'react';
import { Card, Col, Row, Statistic, Table, Tag, Empty, Button, Space, Typography, Alert } from 'antd';
import { ArrowUpOutlined, ArrowDownOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import AutopilotCard from '../components/AutopilotCard';
import ForecastCard from '../components/ForecastCard';
import DecisionFeed from '../components/DecisionFeed';
import { useAppStore } from '../store/useAppStore';
import { useMarketStore } from '../store/useMarketStore';
import { useAccountStore } from '../store/useAccountStore';
import { useOrderStore } from '../store/useOrderStore';
import { getTrades, getPaperAccount } from '../api/endpoints';
import { fmtPct, fmtPx, fmtTime, fmtTimeShort, pnlColor } from '../utils/format';
import type { PaperAccount } from '../api/types';

export default function Dashboard() {
  const nav = useNavigate();
  const venue = useAppStore((s) => s.venue);
  const instId = useAppStore((s) => s.instId);
  const risk = useAppStore((s) => s.risk);
  const notifications = useAppStore((s) => s.notifications);
  const curTicker = useMarketStore((s) => s.tickers[instId]);
  const summary = useAccountStore((s) => s.summary);
  const positions = useAccountStore((s) => s.positions);
  const trades = useOrderStore((s) => s.trades);
  const setTrades = useOrderStore((s) => s.setTrades);
  const [paper, setPaper] = useState<PaperAccount | null>(null);

  useEffect(() => {
    getTrades(20, venue).then(setTrades).catch(() => {});
    if (venue === 'paper') {
      getPaperAccount().then(setPaper).catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [venue]);

  const isPaper = venue === 'paper';
  const equity = isPaper ? (paper?.equity ?? 0) : (summary?.totalEq ?? 0);
  const upl = positions.reduce((a, p) => a + (p.upl || 0), 0);
  const pnlVal = isPaper ? ((paper?.equity ?? 0) - (paper?.initial ?? 0)) : (risk?.daily_pnl_pct ?? 0);
  const posCount = isPaper ? (paper?.positions?.length ?? 0) : positions.length;
  const curChg = curTicker && curTicker.open24h
    ? ((curTicker.last - curTicker.open24h) / curTicker.open24h) * 100
    : 0;
  const base = (instId || 'BTC-USDT').replace(/-SWAP$/, '').split('-')[0];

  return (
    <div>
      {/* 自动驾驶状态卡——新手最关心的"它在干嘛" */}
      <AutopilotCard />

      {/* 24 小时涨跌预测，10 秒刷新 */}
      <div style={{ marginTop: 16 }}>
        <ForecastCard />
      </div>

      {/* 决策记录：让用户随时确认系统在工作 */}
      <div style={{ marginTop: 16 }}>
        <DecisionFeed />
      </div>

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
              title={isPaper ? '累计盈亏' : '今日盈亏'}
              value={pnlVal}
              precision={2}
              suffix={isPaper ? 'USDT' : '%'}
              valueStyle={{ color: pnlColor(pnlVal) || undefined }}
              prefix={!isPaper && pnlVal >= 0 ? <ArrowUpOutlined /> : !isPaper ? <ArrowDownOutlined /> : undefined}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic
              title={isPaper ? '持仓' : '未实现盈亏'}
              value={isPaper ? posCount : upl}
              precision={isPaper ? 0 : 2}
              suffix={isPaper ? '个' : undefined}
              prefix={isPaper ? undefined : '$'}
              valueStyle={isPaper ? undefined : { color: pnlColor(upl) || undefined }}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic
              title={`${base}/USDT`}
              value={curTicker?.last ?? 0}
              precision={curTicker?.last !== undefined && curTicker.last < 1 ? 6 : 2}
              suffix={
                <Typography.Text style={{ fontSize: 12, color: pnlColor(curChg) || '#999' }}>
                  {fmtPct(curChg)}
                </Typography.Text>
              }
            />
          </Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col xs={24}>
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
      </Row>

      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col xs={24}>
          <Card size="small" title="最近通知" extra={<Button size="small" type="link" onClick={() => nav('/notifications')}>全部</Button>}>
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
