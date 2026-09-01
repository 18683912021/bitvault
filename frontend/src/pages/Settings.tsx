import { useEffect, useState, useCallback } from 'react';
import {
  Card, Col, Row, Table, Tag, Button, Form, Input, Select, Modal, Space, Alert,
  Typography, Popconfirm, Descriptions, Empty, Statistic, InputNumber,
} from 'antd';
import { PlusOutlined, ReloadOutlined, DeleteOutlined, ThunderboltOutlined, CheckCircleOutlined } from '@ant-design/icons';
import EnvBadge from '../components/EnvBadge';
import { useAppStore } from '../store/useAppStore';
import { listKeys, addKey, activateKey, deleteKey, testKey, getStatus, getPaperAccount, resetPaperAccount } from '../api/endpoints';
import { fmtTime } from '../utils/format';
import type { ApiKeyRow, ApiKeyIn } from '../api/endpoints';
import type { PaperAccount } from '../api/types';

export default function Settings() {
  const env = useAppStore((s) => s.env);
  const hasKey = useAppStore((s) => s.hasKey);
  const setEnv = useAppStore((s) => s.setEnv);
  const setHasKey = useAppStore((s) => s.setHasKey);
  const setRisk = useAppStore((s) => s.setRisk);
  const [keys, setKeys] = useState<ApiKeyRow[]>([]);
  const [addOpen, setAddOpen] = useState(false);
  const [form] = Form.useForm();
  const [testing, setTesting] = useState(false);
  const [paper, setPaper] = useState<PaperAccount | null>(null);
  const [paperInitial, setPaperInitial] = useState(10000);

  const loadPaper = useCallback(() => {
    getPaperAccount().then(setPaper).catch(() => {});
  }, []);

  const refresh = useCallback(async () => {
    const [ks, st] = await Promise.all([listKeys(), getStatus()]);
    setKeys(ks);
    setEnv(st.env);
    setHasKey(st.has_key);
    setRisk(st.risk);
  }, [setEnv, setHasKey, setRisk]);

  useEffect(() => {
    refresh();
    loadPaper();
    const t = setInterval(loadPaper, 5000);
    return () => clearInterval(t);
  }, [refresh, loadPaper]);

  const onTest = async () => {
    const v = form.getFieldsValue() as ApiKeyIn;
    setTesting(true);
    try {
      const r = await testKey(v);
      if (r.ok) {
        Modal.success({
          title: '连通性测试通过',
          content: (
            <Descriptions column={1} size="small">
              <Descriptions.Item label="账户模式">{r.acctLv}</Descriptions.Item>
              <Descriptions.Item label="持仓方式">{r.posMode}</Descriptions.Item>
              <Descriptions.Item label="UID">{r.uid}</Descriptions.Item>
            </Descriptions>
          ),
        });
      } else {
        Modal.error({ title: '测试失败', content: `[${r.code}] ${r.msg}` });
      }
    } finally {
      setTesting(false);
    }
  };

  const onSave = async (vals: ApiKeyIn) => {
    await addKey(vals);
    setAddOpen(false);
    form.resetFields();
    refresh();
  };

  const onActivate = (row: ApiKeyRow) => {
    Modal.confirm({
      title: `激活 ${row.env === 'live' ? '实盘' : '模拟盘'} Key？`,
      content: row.env === 'live'
        ? '⚠ 切换到实盘环境后，所有操作将影响真实资金。'
        : '切换到模拟盘环境（OKX Demo Trading，虚拟资金）。',
      okText: '确认激活',
      okType: row.env === 'live' ? 'danger' : 'primary',
      cancelText: '取消',
      onOk: () => activateKey(row.id).then(refresh),
    });
  };

  return (
    <div>
      <Alert
        type="warning"
        showIcon
        message="API Key 安全提示（红线 R8 / R1）"
        description="创建 Key 时请只勾选「交易」权限，不勾选「提币」，并绑定服务器 IP 白名单。本系统永不调用提币/划转接口。Secret 与 Passphrase 以 AES-256-GCM 加密落盘。"
        style={{ marginBottom: 16 }}
      />

      <Row gutter={16}>
        <Col xs={24} lg={16}>
          <Card
            size="small"
            title="API Key 管理（OKX 交易通道）"
            extra={<Button type="primary" icon={<PlusOutlined />} onClick={() => setAddOpen(true)}>添加 Key</Button>}
            styles={{ body: { padding: 0 } }}
          >
            <Alert
              type="info"
              showIcon
              style={{ margin: 12, marginBottom: 0 }}
              message="本地模拟盘与自动驾驶（LLM 决策）无需 OKX Key，可直接使用"
              description="仅当需要连接 OKX 账户真实下单（实盘/模拟盘）时才需配置。OKX Key 需要三要素：API Key + Secret + Passphrase（创建 Key 时自设的口令），缺一不可。LLM 决策大脑的 DeepSeek Key 已内置，无需在此配置。"
            />
            <Table
              size="small"
              rowKey={(r) => r.id}
              dataSource={keys}
              pagination={false}
              style={{ marginTop: 8 }}
              locale={{ emptyText: <Empty description="未配置 OKX Key（本地模拟功能不受影响）" style={{ padding: 40 }} /> }}
              columns={[
                { title: '名称', dataIndex: 'name' },
                { title: '环境', dataIndex: 'env', width: 80, render: (e: string) => <Tag color={e === 'live' ? 'red' : 'green'}>{e === 'live' ? '实盘' : '模拟'}</Tag> },
                { title: 'Key', dataIndex: 'key_masked' },
                { title: '权限', dataIndex: 'permission', width: 80 },
                { title: '状态', dataIndex: 'is_active', width: 80, render: (v: number) => v ? <Tag color="blue">当前</Tag> : '—' },
                { title: '创建时间', dataIndex: 'created_at', width: 160, render: fmtTime },
                { title: '操作', width: 160, render: (_: any, r: ApiKeyRow) => (
                  <Space size={0}>
                    {!r.is_active && <Button size="small" type="link" icon={<ThunderboltOutlined />} onClick={() => onActivate(r)}>激活</Button>}
                    <Popconfirm title="删除该 Key？" onConfirm={() => deleteKey(r.id).then(refresh)}>
                      <Button size="small" type="link" danger icon={<DeleteOutlined />}>删除</Button>
                    </Popconfirm>
                  </Space>
                ) },
              ]}
            />
          </Card>
        </Col>
        <Col xs={24} lg={8}>
          <Card size="small" title="当前环境">
            <Space direction="vertical" style={{ width: '100%' }} size="middle">
              <div><EnvBadge env={env} /></div>
              {hasKey ? (
                <Alert type="success" showIcon message="已连接" description="私有 WS 与 REST 通道就绪，可下单与运行策略。" />
              ) : (
                <Alert type="info" showIcon message="未连接 OKX（不影响本地模拟）"
                  description="自动驾驶与本地模拟盘正常运行中。仅实盘/OKX 模拟盘下单需要激活 Key。" />
              )}
              <Descriptions column={1} size="small">
                <Descriptions.Item label="交易标的白名单">BTC-USDT / BTC-USDT-SWAP</Descriptions.Item>
                <Descriptions.Item label="合约杠杆上限">5x（可在风控中心调整）</Descriptions.Item>
                <Descriptions.Item label="默认保证金模式">逐仓 isolated（红线 M6-9）</Descriptions.Item>
                <Descriptions.Item label="下单保护">限价 IOC + ±0.2% 保护价（禁裸市价，红线 R2）</Descriptions.Item>
                <Descriptions.Item label="止损">合约持仓必带交易所侧止损单（红线 R6）</Descriptions.Item>
              </Descriptions>
            </Space>
          </Card>
        </Col>
      </Row>

      <Card size="small" title="模拟盘引导" style={{ marginTop: 16 }}>
        <Typography.Paragraph>
          OKX 模拟盘（Demo Trading）使用真实行情 + 虚拟资金。若检测到模拟账户余额为 0，请在
          <Typography.Link href="https://www.okx.com/demo-trading" target="_blank"> OKX 网页端 Demo Trading 页面 </Typography.Link>
          领取虚拟资金后，回到本系统激活对应模拟盘 Key 即可使用。实盘与模拟盘代码路径完全一致，仅环境标识与 Key 不同。
        </Typography.Paragraph>
      </Card>

      <Card
        size="small"
        title="本地模拟盘（Paper Trading）"
        style={{ marginTop: 16 }}
        extra={(
          <Space>
            <InputNumber
              size="small"
              min={100}
              value={paperInitial}
              onChange={(v) => setPaperInitial(v || 10000)}
              addonAfter="USDT"
              style={{ width: 150 }}
            />
            <Button size="small" danger onClick={() => {
              Modal.confirm({
                title: '重置本地模拟账户？',
                content: `将清空全部模拟持仓，初始资金设为 ${paperInitial} USDT。`,
                okText: '确认重置',
                okType: 'danger',
                cancelText: '取消',
                onOk: async () => { await resetPaperAccount(paperInitial); await loadPaper(); },
              });
            }}>重置</Button>
          </Space>
        )}
      >
        <Row gutter={16}>
          <Col span={6}><Statistic title="模拟权益" value={paper?.equity ?? 0} precision={2} suffix="USDT" /></Col>
          <Col span={6}><Statistic title="可用现金" value={paper?.usdt ?? 0} precision={2} suffix="USDT" /></Col>
          <Col span={6}><Statistic title="累计盈亏" value={paper?.pnl ?? 0} precision={2} suffix="USDT" /></Col>
          <Col span={6}><Statistic title="持仓" value={paper?.positions.map((p) => `${p.inst_id} ${p.sz}`).join('、') || '无'} valueStyle={{ fontSize: 16 }} /></Col>
        </Row>
        <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
          无需 API Key：行情来自 OKX 真实公共数据，下单在本地撮合，费率（maker 0.08% / taker 0.10%）、
          最小下单量、价格步长、市价单保护价等规则与 OKX 现货一致。在「策略中心」创建实例时选择「本地模拟」模式即可运行策略。
        </Typography.Paragraph>
      </Card>

      <Modal
        title="添加 API Key"
        open={addOpen}
        onCancel={() => setAddOpen(false)}
        footer={[
          <Button key="test" icon={<CheckCircleOutlined />} loading={testing} onClick={onTest}>测试连通</Button>,
          <Button key="cancel" onClick={() => setAddOpen(false)}>取消</Button>,
          <Button key="ok" type="primary" onClick={() => form.submit()}>保存</Button>,
        ]}
      >
        <Alert type="warning" showIcon message="请只勾选「交易」权限，禁用「提币」" style={{ marginBottom: 12 }} />
        <Form form={form} layout="vertical" onFinish={onSave} initialValues={{ env: 'demo' }}>
          <Form.Item label="名称" name="name" rules={[{ required: true, message: '请输入名称' }]}>
            <Input placeholder="如：OKX 模拟盘主 Key" />
          </Form.Item>
          <Form.Item label="环境" name="env" rules={[{ required: true }]}>
            <Select options={[{ value: 'demo', label: '模拟盘 Demo' }, { value: 'live', label: '实盘 Live（危险）' }]} />
          </Form.Item>
          <Form.Item label="API Key" name="api_key" rules={[{ required: true }]}>
            <Input placeholder="OKX API Key" />
          </Form.Item>
          <Form.Item label="Secret" name="secret" rules={[{ required: true }]}>
            <Input.Password placeholder="OKX Secret Key" />
          </Form.Item>
          <Form.Item label="Passphrase" name="passphrase" rules={[{ required: true }]}>
            <Input.Password placeholder="OKX Passphrase" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
