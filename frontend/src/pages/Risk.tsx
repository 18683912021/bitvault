import { useEffect, useState, useCallback } from 'react';
import {
  Card, Col, Row, Table, Tag, Button, Form, InputNumber, Switch, Space, Alert,
  Typography, Input, Divider, Statistic, Empty, Select, Modal, message,
} from 'antd';
import { ReloadOutlined, UnlockOutlined } from '@ant-design/icons';
import KillSwitch from '../components/KillSwitch';
import { useAppStore } from '../store/useAppStore';
import { getRiskRules, updateRiskRule, getRiskEvents, getRiskStatus, riskResume,
         getBrainStatus, updateBrainConfig } from '../api/endpoints';
import { PERIODS } from '../utils/format';
import { fmtPct, fmtUsd, fmtTime } from '../utils/format';
import type { RiskRuleEntry, RiskEvent } from '../api/types';

const RULE_LABEL: Record<string, string> = {
  max_order_notional: '单笔金额上限',
  max_position_pct: '单仓占比上限',
  max_daily_loss_pct: '日内亏损熔断',
  max_drawdown_pct: '最大回撤熔断',
  order_rate_limit: '下单频率上限',
  leverage_cap: '杠杆上限',
  auto_reduce_liq: '自动减仓',
};

export default function Risk() {
  const risk = useAppStore((s) => s.risk);
  const setRisk = useAppStore((s) => s.setRisk);
  const [rules, setRules] = useState<Record<string, RiskRuleEntry>>({});
  const [events, setEvents] = useState<RiskEvent[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [form] = Form.useForm();

  const [period, setPeriod] = useState<string>('5m');
  const [savingPeriod, setSavingPeriod] = useState(false);

  const loadBrain = useCallback(async () => {
    try {
      const b = await getBrainStatus();
      setPeriod(b.period || '5m');
    } catch { /* ignore */ }
  }, []);
  useEffect(() => { loadBrain(); }, [loadBrain]);

  const changePeriod = (v: string) => {
    if (v === period) return;
    Modal.confirm({
      title: `将决策周期改为 ${v}？`,
      content: '自动驾驶将在新周期的每根 K 线收盘评估（开仓条件、风控、杠杆均不变）。',
      okText: '确认修改',
      cancelText: '取消',
      onOk: () => { savePeriod2(v); },
    });
  };
  const savePeriod2 = async (v: string) => {
    setSavingPeriod(true);
    try {
      await updateBrainConfig({ period: v });
      message.success(`决策周期已切换到 ${v}（立即生效）`);
    } catch (e: any) {
      message.error('保存失败：' + (e?.message || '未知错误'));
      loadBrain();
    } finally {
      setSavingPeriod(false);
    }
  };

  const refresh = useCallback(async () => {
    const [r, ev, st] = await Promise.all([getRiskRules(), getRiskEvents(200), getRiskStatus()]);
    setRules(r);
    setEvents(ev);
    setRisk(st);
  }, [setRisk]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 10000);
    return () => clearInterval(t);
  }, [refresh]);

  const saveRule = async (type: string, params: Record<string, any>, enabled: boolean) => {
    await updateRiskRule(type, params, enabled);
    setEditing(null);
    refresh();
  };

  return (
    <div>
      <Row gutter={16}>
        <Col xs={24} lg={10}>
          <Card size="small" title="自动驾驶 · 决策周期" style={{ marginBottom: 16 }}>
            <Space direction="vertical" style={{ width: '100%' }}>
              <Space wrap>
                <Typography.Text type="secondary">每根 K 线收盘评估一次，当前周期：</Typography.Text>
                <Select
                  size="small"
                  style={{ width: 110 }}
                  value={period}
                  onChange={(v) => changePeriod(v as string)}
                  disabled={savingPeriod}
                  options={PERIODS.map((p) => ({ value: p, label: p }))}
                />
                {savingPeriod && <Typography.Text type="secondary" style={{ fontSize: 12 }}>保存中…</Typography.Text>}
              </Space>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                选择新周期并确认后立即生效（无需重启）；开仓条件/风控/杠杆等均不变。当前运行状态与开关在顶栏。
              </Typography.Text>
            </Space>
          </Card>

          <Card size="small" title="熔断状态">
            {risk?.halted ? (
              <Alert
                type="error"
                showIcon
                message="风控已熔断"
                description={risk.halt_reason || '触发熔断，仅允许减仓'}
                action={<Button danger icon={<UnlockOutlined />} onClick={() => riskResume().then(refresh)}>解除熔断</Button>}
              />
            ) : (
              <Alert type="success" showIcon message="风控正常" description="未触发熔断，策略可正常启动。" />
            )}
            <Divider style={{ margin: '12px 0' }} />
            <Row gutter={8}>
              <Col span={8}><Statistic title="日内盈亏" value={risk?.daily_pnl_pct ?? 0} precision={2} suffix="%" valueStyle={{ color: risk && risk.daily_pnl_pct < 0 ? '#cf1322' : '#3f8600' }} /></Col>
              <Col span={8}><Statistic title="当前回撤" value={risk?.drawdown_pct ?? 0} precision={2} suffix="%" valueStyle={{ color: '#cf1322' }} /></Col>
              <Col span={8}><Statistic title="峰值权益" value={risk?.peak_equity ?? 0} precision={0} prefix="$" /></Col>
            </Row>
          </Card>
          <Card size="small" title="紧急操作" style={{ marginTop: 12 }}>
            <Space direction="vertical" style={{ width: '100%' }}>
              <KillSwitch />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                Kill Switch：撤全部挂单 + 市价全平 + 停止所有策略，失败自动重试 3 次。
              </Typography.Text>
            </Space>
          </Card>
        </Col>

        <Col xs={24} lg={14}>
          <Card
            size="small"
            title="风控规则"
            extra={<Button size="small" icon={<ReloadOutlined />} onClick={refresh}>刷新</Button>}
          >
            {Object.entries(rules).map(([type, entry]) => (
              <div key={type} style={{ padding: '10px 0', borderBottom: '1px solid #f0f0f0' }}>
                <Row align="middle" justify="space-between">
                  <Col>
                    <Space>
                      <Typography.Text strong>{RULE_LABEL[type] || type}</Typography.Text>
                      <Tag color={entry.enabled ? 'green' : 'default'}>{entry.enabled ? '启用' : '停用'}</Tag>
                    </Space>
                    <div style={{ fontSize: 12, color: '#999', marginTop: 2 }}>
                      {Object.entries(entry.params).map(([k, v]) => `${k}=${v}`).join(' · ')}
                    </div>
                  </Col>
                  <Col>
                    <Space>
                      <Switch
                        checked={entry.enabled}
                        onChange={(v) => saveRule(type, entry.params, v)}
                      />
                      <Button size="small" onClick={() => { setEditing(type); form.setFieldsValue(entry.params); }}>编辑</Button>
                    </Space>
                  </Col>
                </Row>
                {editing === type && (
                  <Form
                    form={form}
                    layout="inline"
                    style={{ marginTop: 8 }}
                    onFinish={(vals) => saveRule(type, vals, entry.enabled)}
                  >
                    {Object.keys(entry.params).map((k) => (
                      <Form.Item key={k} name={k} label={k}>
                        <InputNumber step={0.1} style={{ width: 110 }} />
                      </Form.Item>
                    ))}
                    <Form.Item>
                      <Space>
                        <Button size="small" type="primary" htmlType="submit">保存</Button>
                        <Button size="small" onClick={() => setEditing(null)}>取消</Button>
                      </Space>
                    </Form.Item>
                  </Form>
                )}
              </div>
            ))}
            {!Object.keys(rules).length && <Empty description="加载中" style={{ padding: 20 }} />}
          </Card>
        </Col>
      </Row>

      <Card size="small" title="风控事件日志" style={{ marginTop: 16 }} styles={{ body: { padding: 0 } }}>
        <Table
          size="small"
          rowKey={(r) => r.id}
          dataSource={events}
          pagination={{ pageSize: 20, size: 'small' }}
          locale={{ emptyText: <Empty description="暂无风控事件" style={{ padding: 40 }} /> }}
          columns={[
            { title: '时间', dataIndex: 'ts', width: 160, render: fmtTime },
            { title: '规则', dataIndex: 'rule_type', width: 140, render: (v: string) => RULE_LABEL[v] || v },
            { title: '级别', dataIndex: 'level', width: 80, render: (v: string) => <Tag color={v === 'error' ? 'red' : v === 'warning' ? 'orange' : 'blue'}>{v}</Tag> },
            { title: '动作', dataIndex: 'action', width: 140 },
            { title: '详情', dataIndex: 'detail_json', render: (v: string) => <Typography.Text style={{ fontSize: 12 }}>{v}</Typography.Text> },
          ]}
        />
      </Card>
    </div>
  );
}
