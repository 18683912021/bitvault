import { useEffect, useState } from 'react';
import {
  Card, Col, Row, Table, Tag, Button, Modal, Form, Select, Input, Drawer, Space,
  Typography, Empty, Popconfirm, Segmented, Alert, Statistic, Switch, InputNumber,
} from 'antd';
import {
  PlusOutlined, PlayCircleOutlined, PauseCircleOutlined, StopOutlined,
  DeleteOutlined, ReloadOutlined, ProfileOutlined, ThunderboltOutlined,
} from '@ant-design/icons';
import ParamForm, { schemaDefaults } from '../components/ParamForm';
import InstrumentSelect from '../components/InstrumentSelect';
import { useAppStore } from '../store/useAppStore';
import { useStrategyStore } from '../store/useStrategyStore';
import {
  getTemplates, getInstances, createInstance, startInstance, stopInstance,
  pauseInstance, resumeInstance, deleteInstance,
  getBrainStatus, startAutopilot, stopAutopilot, getBrainHistory,
} from '../api/endpoints';
import { PERIODS, fmtUsd, fmtTime, fmtTimeShort } from '../utils/format';
import type { StrategyTemplate, StrategyInstance, InstanceStatus } from '../api/types';

const STATUS_COLOR: Record<InstanceStatus, string> = {
  running_demo: 'green', running_live: 'red', running_paper: 'geekblue', paused: 'orange',
  halted: 'volcano', error: 'red', stopped: 'default', pending_confirm: 'gold',
};
const STATUS_TXT: Record<InstanceStatus, string> = {
  running_demo: '运行中(模拟)', running_live: '运行中(实盘)', running_paper: '运行中(本地模拟)', paused: '已暂停',
  halted: '已熔断', error: '异常', stopped: '已停止', pending_confirm: '待人工确认',
};

const MODE_TAG: Record<string, { color: string; label: string }> = {
  demo: { color: 'green', label: '模拟' },
  live: { color: 'red', label: '实盘' },
  paper: { color: 'geekblue', label: '本地模拟' },
};

export default function Strategies() {
  const env = useAppStore((s) => s.env);
  const hasKey = useAppStore((s) => s.hasKey);
  const instances = useStrategyStore((s) => s.instances);
  const setInstances = useStrategyStore((s) => s.setInstances);
  const [templates, setTemplates] = useState<StrategyTemplate[]>([]);
  const [createOpen, setCreateOpen] = useState(false);
  const [detail, setDetail] = useState<StrategyInstance | null>(null);
  const [form] = Form.useForm();
  const [selectedType, setSelectedType] = useState<string>('');
  const [mode, setMode] = useState<'demo' | 'live' | 'paper'>('paper');

  const refresh = () => getInstances().then(setInstances).catch(() => {});

  useEffect(() => {
    getTemplates().then(setTemplates).catch(() => {});
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const openCreate = () => {
    const first = templates[0]?.type || '';
    setSelectedType(first);
    if (first) form.setFieldsValue(schemaDefaults(templates[0].params_schema));
    setCreateOpen(true);
  };

  const doCreate = async (vals: any) => {
    const schema = templates.find((t) => t.type === selectedType);
    const params: Record<string, any> = {};
    schema?.params_schema.forEach((s) => { params[s.key] = vals[s.key]; });
    await createInstance({
      strategy_type: selectedType,
      name: vals.name,
      inst_id: vals.inst_id,
      mode,
      params,
      period: vals.period,
    });
    setCreateOpen(false);
    form.resetFields();
    refresh();
  };

  const onTypeChange = (t: string) => {
    setSelectedType(t);
    const schema = templates.find((x) => x.type === t);
    if (schema) form.setFieldsValue(schemaDefaults(schema.params_schema));
  };

  const curSchema = templates.find((t) => t.type === selectedType)?.params_schema || [];

  return (
    <div>
      <AutopilotCard />

      <Space style={{ marginBottom: 16 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate} disabled={templates.length === 0}>
          新建策略实例
        </Button>
        <Button icon={<ReloadOutlined />} onClick={refresh}>刷新</Button>
      </Space>

      {env === 'live' && (
        <Alert type="warning" showIcon message="当前为实盘环境，启动实盘策略需二次确认（红线 R7）" style={{ marginBottom: 16 }} />
      )}

      <Row gutter={16}>
        <Col xs={24} lg={8}>
          <Card size="small" title="策略模板库">
            {templates.map((t) => (
              <div
                key={t.type}
                style={{ padding: '10px 8px', borderBottom: '1px solid #f0f0f0', cursor: 'pointer' }}
                onClick={() => onTypeChange(t.type)}
              >
                <Space direction="vertical" size={0}>
                  <Typography.Text strong>{t.label}</Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>{t.type} · {t.params_schema.length} 个参数</Typography.Text>
                </Space>
              </div>
            ))}
            {templates.length === 0 && <Empty description="加载中" style={{ padding: 20 }} />}
          </Card>
        </Col>
        <Col xs={24} lg={16}>
          <Card size="small" title="我的策略实例" styles={{ body: { padding: 0 } }}>
            <Table
              size="small"
              rowKey={(r) => r.id}
              dataSource={instances}
              pagination={{ pageSize: 15, size: 'small' }}
              locale={{ emptyText: <Empty description="暂无实例，点击「新建策略实例」" style={{ padding: 40 }} /> }}
              columns={[
                { title: '名称', dataIndex: 'name', render: (v: string, r: StrategyInstance) => (
                  <Button type="link" size="small" icon={<ProfileOutlined />} onClick={() => setDetail(r)}>{v}</Button>
                ) },
                { title: '类型', dataIndex: 'type', width: 100 },
                { title: '标的', dataIndex: 'inst_id', width: 120 },
                { title: '模式', dataIndex: 'mode', width: 80, render: (m: string) => { const t = MODE_TAG[m] || { color: 'default', label: m }; return <Tag color={t.color}>{t.label}</Tag>; } },
                { title: '状态', dataIndex: 'status', width: 110, render: (s: InstanceStatus) => <Tag color={STATUS_COLOR[s]}>{STATUS_TXT[s]}</Tag> },
                { title: '累计', dataIndex: 'pnl', width: 90, render: (v: number) => fmtUsd(v || 0) },
                { title: '操作', width: 200, render: (_: any, r: StrategyInstance) => (
                  <Space size={0}>
                    {(r.status === 'stopped' || r.status === 'error' || r.status === 'pending_confirm') && (
                      <Button size="small" type="link" icon={<PlayCircleOutlined />} onClick={() => startIt(r)}>启动</Button>
                    )}
                    {r.status.startsWith('running') && (
                      <Button size="small" type="link" icon={<PauseCircleOutlined />} onClick={() => pauseInstance(r.id).then(refresh)}>暂停</Button>
                    )}
                    {r.status === 'paused' && (
                      <Button size="small" type="link" icon={<PlayCircleOutlined />} onClick={() => resumeInstance(r.id).then(refresh)}>恢复</Button>
                    )}
                    {!r.status.startsWith('running') && (
                      <Popconfirm title="删除该实例？" onConfirm={() => deleteInstance(r.id).then(refresh)}>
                        <Button size="small" type="link" danger icon={<DeleteOutlined />}>删除</Button>
                      </Popconfirm>
                    )}
                  </Space>
                ) },
              ]}
            />
          </Card>
        </Col>
      </Row>

      <Modal
        title="新建策略实例"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => form.submit()}
        okText="创建"
        cancelText="取消"
        width={520}
      >
        {!hasKey && <Alert type="info" showIcon message="未配置 Key 时只能创建，启动需先配置 Key" style={{ marginBottom: 12 }} />}
        <Form form={form} layout="vertical" onFinish={doCreate} initialValues={{ inst_id: 'BTC-USDT', period: '1m' }}>
          <Form.Item label="策略类型" name="__type">
            <Select value={selectedType} onChange={onTypeChange} options={templates.map((t) => ({ value: t.type, label: t.label }))} />
          </Form.Item>
          <Form.Item label="实例名称" name="name" rules={[{ required: true, message: '请输入名称' }]}>
            <Input placeholder="如：双均线-5min" />
          </Form.Item>
          <Row gutter={8}>
            <Col span={12}>
              <Form.Item label="标的" name="inst_id" rules={[{ required: true }]}>
                <InstrumentSelect />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item label="周期" name="period" rules={[{ required: true }]}>
                <Select options={PERIODS.map((p) => ({ value: p, label: p }))} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="模式"
                name="__mode"
                extra={mode === 'paper' ? 'OKX 真实行情 + 本地撮合（费率/精度与 OKX 一致），无需 API Key' : undefined}
              >
                <Segmented
                  value={mode}
                  onChange={(v) => setMode(v as 'demo' | 'live' | 'paper')}
                  options={[
                    { label: '本地模拟', value: 'paper' },
                    { label: '模拟盘', value: 'demo' },
                    { label: '实盘', value: 'live' },
                  ]}
                />
              </Form.Item>
            </Col>
          </Row>
          <Typography.Title level={5} style={{ marginTop: 8 }}>策略参数</Typography.Title>
          <ParamForm schema={curSchema} form={form} layout="horizontal" />
        </Form>
      </Modal>

      <Drawer
        title={detail ? `实例详情：${detail.name}` : ''}
        open={!!detail}
        onClose={() => setDetail(null)}
        width={560}
        extra={
          detail && (
            <Space>
              {detail.status.startsWith('running') && (
                <Button danger icon={<StopOutlined />} onClick={() => stopInstance(detail.id, false).then(refresh)}>停止</Button>
              )}
              {(detail.status === 'stopped' || detail.status === 'error') && (
                <Button type="primary" icon={<PlayCircleOutlined />} onClick={() => startIt(detail)}>启动</Button>
              )}
            </Space>
          )
        }
      >
        {detail && (
          <>
            <Descriptions data={detail} />
            <Typography.Title level={5} style={{ marginTop: 16 }}>实时日志</Typography.Title>
            {detail.logs?.length ? (
              <div style={{ background: '#000', color: '#0f0', padding: 12, borderRadius: 6, fontFamily: 'monospace', fontSize: 12, maxHeight: 360, overflow: 'auto' }}>
                {detail.logs.map((l, i) => (
                  <div key={i} style={{ color: l.level === 'error' ? '#ff4d4f' : l.level === 'warning' ? '#faad14' : '#52c41a' }}>
                    [{fmtTimeShort(l.ts)}] {l.msg}
                  </div>
                ))}
              </div>
            ) : <Empty description="暂无日志" />}
          </>
        )}
      </Drawer>
    </div>
  );
}

function startIt(r: StrategyInstance) {
  const needConfirm = r.mode === 'live';
  Modal.confirm({
    title: needConfirm ? '启动实盘策略' : '启动策略',
    content: needConfirm
      ? '实盘策略启动需人工确认。请确认已通过回测且风控参数已设定（红线 R7）。'
      : `${r.name}（${r.type}）将以 ${MODE_TAG[r.mode]?.label || r.mode} 模式启动。`,
    okText: '确认启动',
    okType: needConfirm ? 'danger' : 'primary',
    cancelText: '取消',
    onOk: () => startInstance(r.id),
  });
}

function AutopilotCard() {
  const [status, setStatus] = useState<any>(null);
  const [history, setHistory] = useState<any[]>([]);
  const [period, setPeriod] = useState<string>('5m');
  const [minConf, setMinConf] = useState(0.4);

  const load = () => {
    getBrainStatus().then(s => {
      setStatus(s);
      const p = s?.period;
      if ((PERIODS as readonly string[]).includes(p)) setPeriod(p);
    }).catch(() => {});
    getBrainHistory(5).then(setHistory).catch(() => {});
  };

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  const toggle = async (checked: boolean) => {
    if (checked) {
      await startAutopilot({ period, min_confidence: minConf });
    } else {
      await stopAutopilot();
    }
    load();
  };

  const last = status?.last_decision;
  const pos = status?.position;
  const th = status?.throttle;
  const thLine: string[] = [];
  if (th) {
    thLine.push(`平仓冷却 ${th.cooldown_min} 分钟`);
    thLine.push(`每日上限 ${th.opens_today}/${th.max_opens_per_day} 次`);
    if (th.loss_pause_n > 0) thLine.push(`连亏 ${th.loss_pause_n} 笔休眠 ${th.loss_pause_min} 分钟`);
    if (th.sleep_until > Date.now()) thLine.push(`休眠至 ${fmtTime(th.sleep_until)}`);
    else if (th.cooldown_until > Date.now()) thLine.push(`冷却至 ${fmtTime(th.cooldown_until)}`);
  }
  return (
    <Card
      size="small"
      title={<Space><ThunderboltOutlined /> 自动驾驶（纯规则决策）</Space>}
      style={{ marginBottom: 16 }}
      extra={(
        <Space>
          <Select size="small" value={period} onChange={setPeriod} style={{ width: 80 }}
            options={PERIODS.map((p) => ({ value: p, label: p }))} />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>阈值</Typography.Text>
          <InputNumber size="small" min={0} max={1} step={0.1} value={minConf} onChange={(v) => setMinConf(v || 0.4)} style={{ width: 70 }} />
          <Switch checked={!!status?.enabled} onChange={toggle}
            checkedChildren="运行中" unCheckedChildren="已停" />
        </Space>
      )}
    >
      <Row gutter={16}>
        <Col span={4}>
          <Statistic title="状态" value={status?.enabled ? '运行' : '停止'} valueStyle={{ fontSize: 16 }} />
          {status?.venue === 'okx'
            ? <Tag color="red" style={{ marginTop: 4 }}>实盘{status?.live_ready === false ? '·未就绪' : ''}</Tag>
            : <Tag color="blue" style={{ marginTop: 4 }}>模拟</Tag>}
          {th?.sleep_until > Date.now() && <Tag color="orange" style={{ marginTop: 4 }}>休眠中</Tag>}
          {!(th?.sleep_until > Date.now()) && th?.cooldown_until > Date.now() && <Tag color="blue" style={{ marginTop: 4 }}>冷却中</Tag>}
        </Col>
        <Col span={4}><Statistic title="最新动作" value={status?.last_action || '-'} valueStyle={{ fontSize: 16 }} /></Col>
        <Col span={4}><Statistic title="今日开仓" value={th ? `${th.opens_today}/${th.max_opens_per_day}` : '-'} valueStyle={{ fontSize: 16 }} /></Col>
        <Col span={4}><Statistic title="置信度" value={last?.confidence ?? 0} precision={2} valueStyle={{ fontSize: 16 }} /></Col>
        <Col span={4}>
          <Statistic title="当前持仓" value={pos ? `${pos.side === 'buy' ? '多' : '空'} ${pos.sz?.toFixed(5)}` : '空仓'} valueStyle={{ fontSize: 16 }} />
        </Col>
      </Row>
      {thLine.length > 0 && (
        <Typography.Text type="secondary" style={{ display: 'block', marginTop: 8, fontSize: 12 }}>
          防无休止做单：{thLine.join(' · ')}（开关=开始/刹车；停止后不再开新仓，已有持仓止损止盈仍生效）
        </Typography.Text>
      )}
      {last?.reason && (
        <Alert type="info" showIcon style={{ marginTop: 12 }}
          message={`决策：${last.action}（入场 ${last.entry_px} / 止损 ${last.sl_px} / 止盈 ${last.tp_px}）`}
          description={last.reason} />
      )}
      {history.length > 0 && (
        <div style={{ marginTop: 8, fontSize: 12, color: '#999' }}>
          最近决策：{history.map((h) => (
            <Tag key={h.ts} color={h.action === 'flat' ? 'default' : h.action === 'long' ? 'red' : 'green'}
              style={{ marginRight: 6 }}>
              {h.action}
            </Tag>
          ))}
        </div>
      )}
      <Typography.Text type="secondary" style={{ display: 'block', marginTop: 8, fontSize: 12 }}>
        纯规则驱动（无 LLM/AI），每根 K 线收盘后运行因子引擎→Regime 六态→HTF(4H+1D) 确认→
        形态识别(breakout_retest)→十项守卫→评分≥70→风险预算仓位，跟随顶栏系统级模式在模拟（虚拟资金）
        或实盘（真实资金）账户开/平仓，含结构止损+分批止盈+trailing。所有决策记录在 signals 表（instance_id=0，含 decide:wait 观望记录）。
      </Typography.Text>
    </Card>
  );
}

function Descriptions({ data }: { data: StrategyInstance }) {
  const state = (() => { try { return JSON.parse(data.state_json || '{}'); } catch { return {}; } })();
  const params = (() => { try { return JSON.parse(data.params_json || '{}'); } catch { return {}; } })();
  const rows: [string, any][] = [
    ['实例ID', `#${data.id}`],
    ['类型', data.type],
    ['标的', data.inst_id],
    ['模式', MODE_TAG[data.mode]?.label || data.mode],
    ['状态', STATUS_TXT[data.status]],
    ['累计盈亏', fmtUsd(data.pnl || 0)],
    ['启动时间', data.started_at ? fmtTime(data.started_at) : '--'],
    ['参数', JSON.stringify(params)],
    ['持仓方向', state.pos_side || '无'],
    ['持仓数量', state.open_sz ?? 0],
  ];
  return (
    <div>
      {rows.map(([k, v], i) => (
        <Row key={i} style={{ padding: '4px 0', borderBottom: '1px solid #f0f0f0' }}>
          <Col span={8}><Typography.Text type="secondary">{k}</Typography.Text></Col>
          <Col span={16}><Typography.Text>{v}</Typography.Text></Col>
        </Row>
      ))}
      {data.last_error && (
        <Alert type="error" message={data.last_error} style={{ marginTop: 12 }} />
      )}
    </div>
  );
}
