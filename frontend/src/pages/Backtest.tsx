import { useEffect, useState, useCallback } from 'react';
import {
  Card, Col, Row, Table, Tag, Button, Form, Select, InputNumber, Drawer, Space,
  Statistic, Empty, Typography, Alert, Segmented,
} from 'antd';
import { PlayCircleOutlined, ReloadOutlined, DownloadOutlined } from '@ant-design/icons';
import ParamForm, { schemaDefaults } from '../components/ParamForm';
import LineChart from '../components/LineChart';
import { getTemplates, listBacktests, getBacktestDetail, runBacktest } from '../api/endpoints';
import { fmtUsd, fmtPct, fmtPx, fmtTime, fmtTimeShort, pnlColor } from '../utils/format';
import type { StrategyTemplate, BacktestListItem, BacktestDetail } from '../api/types';

export default function Backtest() {
  const [templates, setTemplates] = useState<StrategyTemplate[]>([]);
  const [list, setList] = useState<BacktestListItem[]>([]);
  const [detail, setDetail] = useState<BacktestDetail | null>(null);
  const [runOpen, setRunOpen] = useState(false);
  const [running, setRunning] = useState(false);
  const [form] = Form.useForm();
  const [selectedType, setSelectedType] = useState('');

  const refresh = useCallback(() => {
    listBacktests().then(setList).catch(() => {});
  }, []);

  useEffect(() => {
    getTemplates().then((t) => {
      setTemplates(t);
      if (t[0]) {
        setSelectedType(t[0].type);
        form.setFieldsValue(schemaDefaults(t[0].params_schema));
      }
    });
    refresh();
    const poll = setInterval(refresh, 4000);
    return () => clearInterval(poll);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const openDetail = async (id: number) => {
    const d = await getBacktestDetail(id);
    setDetail(d);
  };

  const doRun = async (vals: any) => {
    const schema = templates.find((t) => t.type === selectedType);
    const params: Record<string, any> = {};
    schema?.params_schema.forEach((s) => { params[s.key] = vals[s.key]; });
    setRunning(true);
    try {
      await runBacktest({
        strategy_type: selectedType,
        params,
        inst_id: vals.inst_id,
        period: vals.period,
        days: vals.days,
        fee_rate: vals.fee_rate,
        slippage: vals.slippage,
        initial_equity: vals.initial_equity,
      });
      setRunOpen(false);
      refresh();
    } finally {
      setRunning(false);
    }
  };

  const onTypeChange = (t: string) => {
    setSelectedType(t);
    const schema = templates.find((x) => x.type === t);
    if (schema) form.setFieldsValue(schemaDefaults(schema.params_schema));
  };

  const curSchema = templates.find((t) => t.type === selectedType)?.params_schema || [];

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <Button type="primary" icon={<PlayCircleOutlined />} onClick={() => setRunOpen(true)}>新建回测</Button>
        <Button icon={<ReloadOutlined />} onClick={refresh}>刷新</Button>
      </Space>
      <Alert
        type="info"
        showIcon
        message="回测基于本地历史 K 线，请先在「行情中心」下载历史数据（建议 ≥ 1 个月）。"
        style={{ marginBottom: 16 }}
      />

      <Card size="small" title="回测记录" styles={{ body: { padding: 0 } }}>
        <Table
          size="small"
          rowKey={(r) => r.id}
          dataSource={list}
          pagination={{ pageSize: 15, size: 'small' }}
          locale={{ emptyText: <Empty description="暂无回测，点击「新建回测」" style={{ padding: 40 }} /> }}
          onRow={(r) => ({ onClick: () => r.status === 'done' && openDetail(r.id), style: { cursor: r.status === 'done' ? 'pointer' : 'default' } })}
          columns={[
            { title: 'ID', dataIndex: 'id', width: 60 },
            { title: '策略', dataIndex: 'strategy_type', width: 100 },
            { title: '标的', dataIndex: 'inst_id', width: 120 },
            { title: '周期', dataIndex: 'period', width: 70 },
            { title: '区间', width: 280, render: (_: any, r: BacktestListItem) => `${fmtTime(r.start_ts)} ~ ${fmtTime(r.end_ts)}` },
            { title: '状态', dataIndex: 'status', width: 90, render: (s: string, r: BacktestListItem) => {
              const tag = s === 'done' ? 'green' : s === 'running' ? 'blue' : s === 'failed' ? 'red' : 'default';
              return <Tag color={tag}>{s === 'done' ? '完成' : s === 'running' ? '运行中' : s === 'failed' ? '失败' : '等待'}</Tag>;
            } },
            { title: '总收益', dataIndex: 'metrics_json', render: (v: string) => {
              try { const m = JSON.parse(v || '{}'); return <span style={{ color: pnlColor(m.total_return_pct) || undefined }}>{fmtPct(m.total_return_pct)}</span>; } catch { return '--'; }
            } },
            { title: '操作', width: 100, render: (_: any, r: BacktestListItem) => r.status === 'done' ? <Button size="small" type="link" onClick={(e) => { e.stopPropagation(); openDetail(r.id); }}>报告</Button> : null },
          ]}
        />
      </Card>

      <Drawer
        title={detail ? `回测报告 #${detail.id}` : ''}
        open={!!detail}
        onClose={() => setDetail(null)}
        width={860}
      >
        {detail && <Report detail={detail} />}
      </Drawer>

      <Drawer
        title="新建回测"
        open={runOpen}
        onClose={() => setRunOpen(false)}
        width={500}
        extra={<Button type="primary" loading={running} onClick={() => form.submit()}>开始回测</Button>}
      >
        <Form form={form} layout="vertical" onFinish={doRun} initialValues={{ inst_id: 'BTC-USDT', period: '5m', days: 90, fee_rate: 0.0005, slippage: 0.0005, initial_equity: 10000 }}>
          <Form.Item label="策略类型" name="__type">
            <Select value={selectedType} onChange={onTypeChange} options={templates.map((t) => ({ value: t.type, label: t.label }))} />
          </Form.Item>
          <Row gutter={8}>
            <Col span={12}><Form.Item label="标的" name="inst_id"><Select options={[{ value: 'BTC-USDT', label: 'BTC-USDT' }, { value: 'BTC-USDT-SWAP', label: 'BTC-USDT-SWAP' }]} /></Form.Item></Col>
            <Col span={6}><Form.Item label="周期" name="period"><Select options={['1m','5m','15m','1H','4H','1D'].map((p) => ({ value: p, label: p }))} /></Form.Item></Col>
            <Col span={6}><Form.Item label="天数" name="days"><InputNumber style={{ width: '100%' }} min={1} /></Form.Item></Col>
          </Row>
          <Typography.Title level={5}>策略参数</Typography.Title>
          <ParamForm schema={curSchema} form={form} layout="horizontal" />
          <Typography.Title level={5} style={{ marginTop: 12 }}>成本与资金</Typography.Title>
          <Row gutter={8}>
            <Col span={8}><Form.Item label="手续费率" name="fee_rate"><InputNumber style={{ width: '100%' }} step={0.0001} /></Form.Item></Col>
            <Col span={8}><Form.Item label="滑点" name="slippage"><InputNumber style={{ width: '100%' }} step={0.0001} /></Form.Item></Col>
            <Col span={8}><Form.Item label="初始资金" name="initial_equity"><InputNumber style={{ width: '100%' }} step={100} /></Form.Item></Col>
          </Row>
        </Form>
      </Drawer>
    </div>
  );
}

function Report({ detail }: { detail: BacktestDetail }) {
  const m = detail.metrics || {};
  const equity = (detail.equity || []).map((p) => ({ ts: p.ts, value: p.equity }));
  const bh = (detail.bh || []).map((p) => ({ ts: p.ts, value: p.equity }));
  // 回撤曲线
  const eqVals = equity.map((e) => e.value);
  let peak = 0;
  const drawdown = equity.map((e, i) => {
    peak = Math.max(peak, eqVals[i]);
    return { ts: e.ts, value: peak > 0 ? (e.value / peak - 1) * 100 : 0 };
  });

  const exportCsv = () => {
    const rows = [['ts', 'side', 'px', 'sz', 'fee', 'realized', 'kind'], ...detail.trades!.map((t) => [t.ts, t.side, t.px, t.sz, t.fee, t.realized, t.kind])];
    const csv = rows.map((r) => r.join(',')).join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `backtest_${detail.id}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <div>
      <Row gutter={8}>
        <Col span={6}><Card size="small"><Statistic title="总收益" value={m.total_return_pct ?? 0} precision={2} suffix="%" valueStyle={{ color: pnlColor(m.total_return_pct) || undefined }} /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="Buy&Hold" value={m.buy_hold_pct ?? 0} precision={2} suffix="%" /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="年化" value={m.annual_pct ?? 0} precision={2} suffix="%" /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="最大回撤" value={m.max_dd_pct ?? 0} precision={2} suffix="%" valueStyle={{ color: '#cf1322' }} /></Card></Col>
      </Row>
      <Row gutter={8} style={{ marginTop: 8 }}>
        <Col span={6}><Card size="small"><Statistic title="夏普" value={m.sharpe ?? 0} precision={2} /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="Sortino" value={m.sortino ?? 0} precision={2} /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="胜率" value={m.win_rate ?? 0} precision={1} suffix="%" /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="盈亏比" value={m.profit_factor ?? '--'} precision={2} /></Card></Col>
      </Row>

      <Card size="small" title="收益曲线（策略 vs Buy & Hold）" style={{ marginTop: 12 }}>
        {equity.length > 1 ? (
          <LineChart series={[{ name: '策略', color: '#1677ff', data: equity }, { name: 'Buy&Hold', color: '#999', data: bh }]} height={300} />
        ) : <Empty description="无数据" style={{ padding: 30 }} />}
      </Card>
      <Card size="small" title="回撤曲线" style={{ marginTop: 12 }}>
        {drawdown.length > 1 ? (
          <LineChart series={[{ name: '回撤%', color: '#cf1322', data: drawdown }]} height={160} />
        ) : <Empty description="无数据" style={{ padding: 20 }} />}
      </Card>

      <Card
        size="small"
        title="逐笔交易"
        style={{ marginTop: 12 }}
        extra={<Button size="small" icon={<DownloadOutlined />} onClick={exportCsv}>导出 CSV</Button>}
        styles={{ body: { padding: 0 } }}
      >
        <Table
          size="small"
          rowKey={(t, i) => String(i)}
          dataSource={detail.trades || []}
          pagination={{ pageSize: 20, size: 'small' }}
          locale={{ emptyText: <Empty description="无成交" /> }}
          columns={[
            { title: '时间', dataIndex: 'ts', width: 150, render: fmtTime },
            { title: '方向', dataIndex: 'side', width: 60, render: (s: string) => <Tag color={s === 'buy' ? 'red' : 'green'}>{s === 'buy' ? '买' : '卖'}</Tag> },
            { title: '价格', dataIndex: 'px', render: fmtPx },
            { title: '数量', dataIndex: 'sz', render: (v: number) => v?.toFixed(6) },
            { title: '手续费', dataIndex: 'fee', render: (v: number) => v?.toFixed(4) },
            { title: '已实现', dataIndex: 'realized', render: (v: number) => <span style={{ color: pnlColor(v) || undefined }}>{v?.toFixed(2)}</span> },
            { title: '类型', dataIndex: 'kind', width: 90 },
          ]}
        />
      </Card>
    </div>
  );
}
