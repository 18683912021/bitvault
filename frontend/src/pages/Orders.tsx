import { useEffect, useState, useCallback } from 'react';
import {
  Card, Tabs, Table, Tag, Button, Popconfirm, Space, Empty, Typography, Segmented,
} from 'antd';
import { ReloadOutlined, CloseCircleOutlined } from '@ant-design/icons';
import { useOrderStore } from '../store/useOrderStore';
import { getOrders, getTrades, cancelOrder } from '../api/endpoints';
import { fmtPx, fmtTime, pnlColor } from '../utils/format';
import type { Order, OrderState } from '../api/types';
import RoundTripTable from '../components/RoundTripTable';

const STATE_TAG: Record<OrderState, string> = {
  pending_submit: 'default', live: 'processing', partially_filled: 'warning',
  filled: 'success', canceled: 'default', partially_canceled: 'default', failed: 'error',
};
const STATE_TXT: Record<OrderState, string> = {
  pending_submit: '待报', live: '已报', partially_filled: '部成',
  filled: '全成', canceled: '已撤', partially_canceled: '部撤', failed: '失败',
};

export default function Orders() {
  const orders = useOrderStore((s) => s.orders);
  const setOrders = useOrderStore((s) => s.setOrders);
  const trades = useOrderStore((s) => s.trades);
  const setTrades = useOrderStore((s) => s.setTrades);
  const [tab, setTab] = useState<'open' | 'history' | 'rt'>('open');
  const [loading, setLoading] = useState(false);
  const [rtVenue, setRtVenue] = useState<'paper' | 'okx'>('paper');

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [o, t] = await Promise.all([
        getOrders(tab === 'rt' ? 'open' : tab, 200),
        getTrades(200),
      ]);
      setOrders(o);
      setTrades(t);
    } finally {
      setLoading(false);
    }
  }, [tab, setOrders, setTrades]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  const openOrders = orders.filter((o) => ['pending_submit', 'live', 'partially_filled'].includes(o.state));
  const closedOrders = orders.filter((o) => !['pending_submit', 'live', 'partially_filled'].includes(o.state));

  return (
    <Card
      size="small"
      styles={{ body: { padding: 0 } }}
      title="订单与成交"
      extra={<Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={refresh}>刷新</Button>}
    >
      <Tabs
        activeKey={tab}
        onChange={(k) => setTab(k as 'open' | 'history' | 'rt')}
        items={[
          { key: 'open', label: `挂单 (${openOrders.length})`, children: (
            <Table
              size="small" rowKey={(o) => o.cl_ord_id} dataSource={openOrders} pagination={false}
              locale={{ emptyText: <Empty description="无挂单" style={{ padding: 40 }} /> }}
              columns={[
                { title: '时间', dataIndex: 'created_at', width: 150, render: fmtTime },
                { title: '标的', dataIndex: 'inst_id', width: 120 },
                { title: '通道', dataIndex: 'venue', width: 80, render: (v: string) => v === 'paper' ? <Tag color="geekblue">本地模拟</Tag> : <Tag>OKX</Tag> },
                { title: '方向', dataIndex: 'side', width: 60, render: (s: string) => <Tag color={s === 'buy' ? 'red' : 'green'}>{s === 'buy' ? '买' : '卖'}</Tag> },
                { title: '类型', dataIndex: 'ord_type', width: 80 },
                { title: '价格', dataIndex: 'px', render: fmtPx },
                { title: '数量', dataIndex: 'sz', render: (v: number) => v?.toFixed(6) },
                { title: '已成交', dataIndex: 'filled_sz', render: (v: number) => (v || 0).toFixed(6) },
                { title: '状态', dataIndex: 'state', width: 80, render: (s: OrderState) => <Tag color={STATE_TAG[s]}>{STATE_TXT[s]}</Tag> },
                { title: '来源', dataIndex: 'source', width: 90, render: (s: string) => s?.startsWith('strategy') ? `策略${s}` : '手动' },
                { title: '操作', width: 90, render: (_: any, o: Order) => (
                  <Popconfirm title="撤销该订单？" onConfirm={() => cancelOrder(o.cl_ord_id).then(refresh)}>
                    <Button size="small" type="link" danger icon={<CloseCircleOutlined />}>撤单</Button>
                  </Popconfirm>
                ) },
              ]}
            />
          ) },
          { key: 'history', label: `历史订单 (${closedOrders.length})`, children: (
            <Table
              size="small" rowKey={(o) => o.cl_ord_id} dataSource={closedOrders} pagination={{ pageSize: 20, size: 'small' }}
              locale={{ emptyText: <Empty description="无历史订单" style={{ padding: 40 }} /> }}
              columns={[
                { title: '时间', dataIndex: 'created_at', width: 150, render: fmtTime },
                { title: '标的', dataIndex: 'inst_id', width: 120 },
                { title: '通道', dataIndex: 'venue', width: 80, render: (v: string) => v === 'paper' ? <Tag color="geekblue">本地模拟</Tag> : <Tag>OKX</Tag> },
                { title: '方向', dataIndex: 'side', width: 60, render: (s: string) => <Tag color={s === 'buy' ? 'red' : 'green'}>{s === 'buy' ? '买' : '卖'}</Tag> },
                { title: '类型', dataIndex: 'ord_type', width: 80 },
                { title: '价格', dataIndex: 'avg_px', render: fmtPx },
                { title: '数量', dataIndex: 'filled_sz', render: (v: number) => (v || 0).toFixed(6) },
                { title: '状态', dataIndex: 'state', width: 80, render: (s: OrderState) => <Tag color={STATE_TAG[s]}>{STATE_TXT[s]}</Tag> },
                { title: '错误', dataIndex: 'error_msg', render: (v: string) => v || '--' },
              ]}
            />
          ) },
          { key: 'trades', label: `成交 (${trades.length})`, children: (
            <Table
              size="small" rowKey={(t) => String(t.id)} dataSource={trades} pagination={{ pageSize: 20, size: 'small' }}
              locale={{ emptyText: <Empty description="无成交" style={{ padding: 40 }} /> }}
              columns={[
                { title: '时间', dataIndex: 'ts', width: 150, render: fmtTime },
                { title: '标的', dataIndex: 'inst_id', width: 120 },
                { title: '方向', dataIndex: 'side', width: 60, render: (s: string) => <Tag color={s === 'buy' ? 'red' : 'green'}>{s === 'buy' ? '买' : '卖'}</Tag> },
                { title: '价格', dataIndex: 'px', render: fmtPx },
                { title: '数量', dataIndex: 'sz', render: (v: number) => v?.toFixed(6) },
                { title: '手续费', dataIndex: 'fee', render: (v: number) => v?.toFixed(4) },
                { title: '来源', dataIndex: 'instance_id', width: 90, render: (v: number | null) => v ? `策略#${v}` : '手动' },
              ]}
            />
          ) },
          { key: 'rt', label: '交易闭环', children: (
            <div>
              <Segmented
                value={rtVenue}
                onChange={(v) => setRtVenue(v as 'paper' | 'okx')}
                options={[{ label: '本地模拟', value: 'paper' }, { label: 'OKX', value: 'okx' }]}
                style={{ margin: '4px 16px 0' }}
              />
              <RoundTripTable venue={rtVenue} />
            </div>
          ) },
        ]}
      />
    </Card>
  );
}
