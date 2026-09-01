import { useEffect, useState } from 'react';
import {
  Card, Col, Row, Statistic, Table, Tag, Empty, Typography, Segmented, Alert, Space, Button,
  Modal, message,
} from 'antd';
import { ReloadOutlined, ExperimentOutlined, SwapOutlined } from '@ant-design/icons';
import LightweightChart from '../components/LightweightChart';
import RoundTripTable from '../components/RoundTripTable';
import { useAppStore } from '../store/useAppStore';
import { useAccountStore } from '../store/useAccountStore';
import { useOrderStore } from '../store/useOrderStore';
import {
  getAccountSummary, getAccountCurve, getTrades,
  getPaperAccount, resetPaperAccount,
} from '../api/endpoints';
import { fmtPx, fmtTime, pnlColor } from '../utils/format';
import type { Position } from '../api/types';
import type { PaperAccount } from '../api/types';

export default function Account() {
  const hasKey = useAppStore((s) => s.hasKey);
  const env = useAppStore((s) => s.env);
  const [tab, setTab] = useState<'paper' | 'okx'>('paper');

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <Segmented
          value={tab}
          onChange={(v) => setTab(v as 'paper' | 'okx')}
          options={[
            { label: <span><ExperimentOutlined /> 模拟盘（练手）</span>, value: 'paper' },
            { label: <span><SwapOutlined /> OKX 账户（正式）</span>, value: 'okx', disabled: !hasKey },
          ]}
        />
        {tab === 'okx' && !hasKey && <Typography.Text type="secondary">未连接 OKX Key，仅模拟盘可用</Typography.Text>}
      </Space>
      {tab === 'paper' ? <PaperPanel /> : <OkxPanel hasKey={hasKey} env={env} />}
    </div>
  );
}

// ================= 本地模拟盘面板 =================
function PaperPanel() {
  const [acct, setAcct] = useState<PaperAccount | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = async () => {
    setLoading(true);
    try { setAcct(await getPaperAccount()); } finally { setLoading(false); }
  };

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, []);

  const doReset = () => {
    Modal.confirm({
      title: '重置模拟盘（全部清空）',
      content: '将清空模拟盘的持仓、订单、成交、交易闭环、自动驾驶决策与通知记录，资金恢复到初始值，节流计数（连亏/冷却/每日开仓）一并归零。不影响 OKX 真实账户。',
      okText: '确认重置',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        const { account } = await resetPaperAccount(acct?.initial || 10000);
        setAcct(account);
        message.success('模拟盘已重置（记录与资金全部清空）');
      },
    });
  };

  const pnl = (acct?.equity ?? 0) - (acct?.initial ?? 0);
  const pnlPct = acct?.initial ? (pnl / acct.initial) * 100 : 0;

  return (
    <div>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="本地模拟盘：与 OKX 一致的规则与费率（maker 0.08% / taker 0.10%），行情为 OKX 真实行情"
        description="用于练手与验证策略效果，资金为虚拟 USDT，与真实账户完全隔离。自动驾驶（LLM 决策）固定在本通道运行。"
      />
      <Row gutter={16}>
        <Col xs={12} md={6}>
          <Card size="small"><Statistic title="模拟权益" value={acct?.equity ?? 0} precision={2} suffix="USDT" loading={loading} /></Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small"><Statistic title="可用 USDT" value={acct?.usdt ?? 0} precision={2} loading={loading} /></Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic title="累计盈亏" value={pnl} precision={2} suffix={`USDT (${pnlPct.toFixed(2)}%)`}
              valueStyle={{ color: pnlColor(pnl) || undefined }} loading={loading} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic title="持仓" value={acct?.positions?.length ?? 0} suffix="个" />
            <Button size="small" type="dashed" onClick={doReset} style={{ marginTop: 8 }}>重置资金</Button>
          </Card>
        </Col>
      </Row>

      <Card size="small" title="模拟持仓" style={{ marginTop: 16 }} styles={{ body: { padding: 0 } }}>
        <Table
          size="small"
          rowKey={(p) => p.inst_id}
          dataSource={acct?.positions ?? []}
          pagination={false}
          locale={{ emptyText: <Empty description="无持仓（可在交易台用「本地模拟」通道下单）" style={{ padding: 30 }} /> }}
          columns={[
            { title: '标的', dataIndex: 'inst_id', width: 120 },
            { title: '数量', dataIndex: 'sz', render: (v: number) => v?.toFixed(6) },
            { title: '标记价', dataIndex: 'last', render: fmtPx },
            { title: '市值', dataIndex: 'notional', render: (v: number) => v?.toFixed(2) },
          ]}
        />
      </Card>

      <Card size="small" title="交易闭环（开仓价 / 平仓价 / 资费 / 净收入）" style={{ marginTop: 16 }} styles={{ body: { padding: 0 } }}>
        <RoundTripTable venue="paper" />
      </Card>
    </div>
  );
}

// ================= OKX 账户面板 =================
function OkxPanel({ hasKey, env }: { hasKey: boolean; env: string }) {
  const summary = useAccountStore((s) => s.summary);
  const positions = useAccountStore((s) => s.positions);
  const setAccount = useAccountStore((s) => s.setAccount);
  const curve = useAccountStore((s) => s.curve);
  const setCurve = useAccountStore((s) => s.setCurve);
  const trades = useOrderStore((s) => s.trades);
  const setTrades = useOrderStore((s) => s.setTrades);
  const [days, setDays] = useState(30);
  const [loading, setLoading] = useState(false);

  const refresh = async () => {
    if (!hasKey) return;
    setLoading(true);
    try {
      const s = await getAccountSummary();
      setAccount(s);
      const c = await getAccountCurve(days);
      setCurve(c);
      const t = await getTrades(100);
      setTrades(t);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 10000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasKey, days]);

  const upl = positions.reduce((a, p) => a + (p.upl || 0), 0);
  const marginUsed = positions.reduce((a, p) => a + (p.notionalUsd || 0) / (Number(p.lever) || 1 || 1), 0);

  const marginColor = (p: Position) => {
    const r = p.marginRatio ?? p.mgnRatio;
    if (r == null) return undefined;
    if (r < 0.05) return '#cf1322';
    if (r < 0.10) return '#fa8c16';
    return undefined;
  };

  return (
    <div>
      {env === 'live' && (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }}
          message="实盘 LIVE 模式" description="当前连接真实资金账户，所有下单操作请谨慎确认。" />
      )}
      <Space style={{ marginBottom: 16 }}>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={refresh}>刷新</Button>
        <Segmented value={days} onChange={(v) => setDays(v as number)} options={[{ label: '7天', value: 7 }, { label: '30天', value: 30 }, { label: '90天', value: 90 }]} />
      </Space>

      <Row gutter={16}>
        <Col xs={12} md={6}>
          <Card size="small"><Statistic title="总权益" value={summary?.totalEq ?? 0} precision={2} prefix="$" /></Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small"><Statistic title="未实现盈亏" value={upl} precision={2} prefix="$" valueStyle={{ color: pnlColor(upl) || undefined }} /></Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small"><Statistic title="保证金占用(估)" value={marginUsed} precision={2} prefix="$" /></Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small"><Statistic title="环境" value={env === 'live' ? '实盘 LIVE' : '模拟 DEMO'} /></Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col xs={24} lg={14}>
          <Card size="small" title="资金曲线">
            {curve.length > 1 ? (
              <LightweightChart
                candles={curve.map((c) => ({ ts: c.ts, o: c.equity, h: c.equity, l: c.equity, c: c.equity, vol: 0 }))}
                height={300}
                showMA={false}
                title="权益 (USDT)"
              />
            ) : (
              <Empty description="资金曲线数据积累中（每 5 分钟快照）" style={{ padding: 40 }} />
            )}
          </Card>
        </Col>
        <Col xs={24} lg={10}>
          <Card size="small" title="持仓明细" styles={{ body: { padding: 0 } }}>
            <Table
              size="small"
              rowKey={(p) => p.instId + (p.posSide || '')}
              dataSource={positions}
              pagination={false}
              locale={{ emptyText: <Empty description="无持仓" style={{ padding: 30 }} /> }}
              columns={[
                { title: '标的', dataIndex: 'instId', width: 110 },
                { title: '方向', dataIndex: 'pos', width: 60, render: (v: number) => <Tag color={v > 0 ? 'red' : 'green'}>{v > 0 ? '多' : '空'}</Tag> },
                { title: '数量', dataIndex: 'pos', render: (v: number) => v?.toFixed(4) },
                { title: '开仓价', dataIndex: 'avgPx', render: fmtPx },
                { title: '标记价', dataIndex: 'markPx', render: fmtPx },
                { title: '强平价', dataIndex: 'liqPx', render: (v: number | null) => v ? <span style={{ color: '#cf1322' }}>{fmtPx(v)}</span> : '--' },
                { title: '保证金率', dataIndex: 'marginRatio', render: (v: number | null, r: Position) => v != null ? <span style={{ color: marginColor(r) }}>{(v * 100).toFixed(2)}%</span> : '--' },
                { title: '未实现', dataIndex: 'upl', render: (v: number) => <span style={{ color: pnlColor(v) || undefined }}>{v?.toFixed(2)}</span> },
              ]}
            />
          </Card>
        </Col>
      </Row>

      <Card size="small" title="成交流水" style={{ marginTop: 16 }} styles={{ body: { padding: 0 } }}>
        <Table
          size="small"
          rowKey={(r) => String(r.id)}
          dataSource={trades}
          pagination={{ pageSize: 20, size: 'small' }}
          locale={{ emptyText: <Empty description="暂无成交" /> }}
          columns={[
            { title: '时间', dataIndex: 'ts', width: 150, render: fmtTime },
            { title: '标的', dataIndex: 'inst_id', width: 120 },
            { title: '方向', dataIndex: 'side', width: 60, render: (s: string) => <Tag color={s === 'buy' ? 'red' : 'green'}>{s === 'buy' ? '买' : '卖'}</Tag> },
            { title: '价格', dataIndex: 'px', render: fmtPx },
            { title: '数量', dataIndex: 'sz', render: (v: number) => v?.toFixed(6) },
            { title: '手续费', dataIndex: 'fee', render: (v: number) => v?.toFixed(4) },
            { title: '策略', dataIndex: 'instance_id', width: 80, render: (v: number | null) => v ? `#${v}` : '手动' },
          ]}
        />
      </Card>

      <Card size="small" title="交易闭环（开仓价 / 平仓价 / 资费 / 净收入）" style={{ marginTop: 16 }} styles={{ body: { padding: 0 } }}>
        <RoundTripTable venue="okx" />
      </Card>
    </div>
  );
}
