// 系统设置：OKX API Key 管理（自行填写，不预置）+ 模拟盘重置 + 系统信息。
import { useEffect, useState, useCallback } from 'react';
import { Card, Row, Col, Statistic, Button, InputNumber, Space, Modal, Typography,
  Descriptions, Input, Radio, Table, Tag, Alert, message } from 'antd';
import { KeyOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import { useAppStore } from '../store/useAppStore';
import { getStatus, getPaperAccount, resetPaperAccount,
  listKeys, addKey, activateKey, deleteKey, testKey,
  type ApiKeyRow, type ApiKeyIn } from '../api/endpoints';
import { fmtTime } from '../utils/format';
import type { PaperAccount } from '../api/types';

export default function Settings() {
  const setEnv = useAppStore((s) => s.setEnv);
  const setHasKey = useAppStore((s) => s.setHasKey);
  const setRisk = useAppStore((s) => s.setRisk);
  const setVenue = useAppStore((s) => s.setVenue);
  const [paper, setPaper] = useState<PaperAccount | null>(null);
  const [paperInitial, setPaperInitial] = useState(10000);
  const [sysInfo, setSysInfo] = useState<{ env: string; has_key: boolean; venue: string; inst_id: string } | null>(null);

  // ---- API Key 状态 ----
  const [keys, setKeys] = useState<ApiKeyRow[]>([]);
  const [kName, setKName] = useState('');
  const [kKey, setKKey] = useState('');
  const [kSecret, setKSecret] = useState('');
  const [kPass, setKPass] = useState('');
  const [kEnv, setKEnv] = useState<'demo' | 'live'>('demo');
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);

  const loadPaper = useCallback(() => {
    getPaperAccount().then(setPaper).catch(() => {});
  }, []);

  const refresh = useCallback(async () => {
    const st = await getStatus();
    setEnv(st.env);
    setHasKey(st.has_key);
    setRisk(st.risk);
    if (st.venue) setVenue(st.venue);
    setSysInfo({ env: st.env, has_key: st.has_key, venue: st.venue, inst_id: st.inst_id });
  }, [setEnv, setHasKey, setRisk, setVenue]);

  const loadKeys = useCallback(() => {
    listKeys().then(setKeys).catch(() => {});
  }, []);

  useEffect(() => {
    refresh();
    loadPaper();
    loadKeys();
    const t = setInterval(loadPaper, 5000);
    return () => clearInterval(t);
  }, [refresh, loadPaper, loadKeys]);

  const buildForm = (): ApiKeyIn | null => {
    if (!kName.trim()) { message.warning('请填写名称'); return null; }
    if (!kKey.trim() || !kSecret.trim() || !kPass.trim()) { message.warning('请填写完整的 API Key / Secret / Passphrase'); return null; }
    return { name: kName.trim(), api_key: kKey.trim(), secret: kSecret.trim(), passphrase: kPass.trim(), env: kEnv };
  };

  const onTest = async () => {
    const body = buildForm();
    if (!body) return;
    setTesting(true);
    try {
      const r = await testKey(body);
      if (r.ok) {
        message.success(`连接成功：账户模式 ${r.acctLv ?? '?'} / 持仓 ${r.posMode ?? '?'}（UID ${r.uid ?? '?'}）`);
      } else {
        message.error(`连接失败: ${r.msg || r.code || '未知错误'}`);
      }
    } finally { setTesting(false); }
  };

  const onSave = async () => {
    const body = buildForm();
    if (!body) return;
    setSaving(true);
    try {
      await addKey(body);
      message.success('已保存（加密存储于服务器本地）');
      setKName(''); setKKey(''); setKSecret(''); setKPass('');
      loadKeys();
      await refresh();
    } catch { /* api client 已提示 */ }
    finally { setSaving(false); }
  };

  const onActivate = async (id: number) => {
    setBusyId(id);
    try {
      await activateKey(id);
      message.success('已启用并重连 OKX');
      loadKeys();
      await refresh();
    } catch { /* 已提示 */ }
    finally { setBusyId(null); }
  };

  const onDelete = (row: ApiKeyRow) => {
    Modal.confirm({
      title: `删除 Key「${row.name}」？`,
      content: '删除后无法恢复，若为当前启用 Key 将断开 OKX 连接。',
      okText: '删除', okType: 'danger', cancelText: '取消',
      onOk: async () => { await deleteKey(row.id); loadKeys(); await refresh(); },
    });
  };

  return (
    <div>
      {/* OKX API Key 管理（自行填写，无预置） */}
      <Card size="small" title={<><KeyOutlined /> OKX API Key 管理</>} style={{ marginBottom: 16 }}
        extra={<Button size="small" icon={<ReloadOutlined />} onClick={loadKeys}>刷新</Button>}>
        <Alert type="info" showIcon style={{ marginBottom: 12 }}
          message="密钥安全提示"
          description="API Key 仅保存在本服务器（AES-256 加密落盘），不会上传第三方。请在 OKX 创建 Key 时只勾选「交易」权限、不要勾选「提币」，并绑定服务器出口 IP；实盘请优先使用模拟盘验证策略。默认使用模拟盘（Demo）环境。" />
        <Row gutter={16}>
          <Col xs={24} lg={14}>
            <Space direction="vertical" style={{ width: '100%' }}>
              <Input placeholder="名称（如：模拟盘主Key）" value={kName} onChange={(e) => setKName(e.target.value)} allowClear />
              <Input.Password placeholder="API Key" value={kKey} onChange={(e) => setKKey(e.target.value)} />
              <Input.Password placeholder="Secret Key" value={kSecret} onChange={(e) => setKSecret(e.target.value)} />
              <Input.Password placeholder="Passphrase（OKX 创建 Key 时设置的口令）" value={kPass} onChange={(e) => setKPass(e.target.value)} />
              <Radio.Group value={kEnv} onChange={(e) => setKEnv(e.target.value)}>
                <Radio value="demo" checked={kEnv === 'demo'}>模拟盘 Demo（推荐先用这个）</Radio>
                <Radio value="live" style={{ color: kEnv === 'live' ? '#cf1322' : undefined }}>实盘 Live（真实资金！）</Radio>
              </Radio.Group>
              <Space>
                <Button icon={<PlusOutlined />} type="primary" loading={saving} onClick={onSave}>保存</Button>
                <Button loading={testing} onClick={onTest}>测试连接</Button>
              </Space>
            </Space>
          </Col>
          <Col xs={24} lg={10}>
            <Table
              size="small" rowKey="id" pagination={false}
              dataSource={keys}
              locale={{ emptyText: '尚未配置 Key —— 左侧填写并保存' }}
              columns={[
                { title: '名称', dataIndex: 'name' },
                { title: '环境', dataIndex: 'env', width: 70, render: (v: string) => (
                  <Tag color={v === 'live' ? 'red' : 'green'}>{v === 'live' ? '实盘' : '模拟'}</Tag>) },
                { title: '状态', dataIndex: 'is_active', width: 80, render: (v: number) => (
                  <Tag color={v ? 'blue' : 'default'}>{v ? '启用中' : '停用'}</Tag>) },
                { title: '操作', width: 130, render: (_, r: ApiKeyRow) => (
                  <Space size={4}>
                    {!r.is_active && <Button size="small" type="link" loading={busyId === r.id} onClick={() => onActivate(r.id)}>启用</Button>}
                    <Button size="small" type="link" danger onClick={() => onDelete(r)}>删除</Button>
                  </Space>) },
              ]}
            />
          </Col>
        </Row>
      </Card>

      <Row gutter={16}>
        {/* 本地模拟盘重置 */}
        <Col xs={24} lg={14}>
          <Card
            size="small"
            title="模拟盘重置"
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
                    title: '重置模拟账户？',
                    content: `将清空全部模拟持仓和交易记录，初始资金设为 ${paperInitial} USDT。`,
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
              <Col span={6}><Statistic title="累计盈亏" value={paper?.pnl ?? 0} precision={2} suffix="USDT"
                valueStyle={{ color: (paper?.pnl ?? 0) > 0 ? '#3f8600' : (paper?.pnl ?? 0) < 0 ? '#cf1322' : undefined }} /></Col>
              <Col span={6}><Statistic title="持仓" value={paper?.positions?.map((p) => `${p.inst_id} ${p.sz}`).join('、') || '无'} valueStyle={{ fontSize: 16 }} /></Col>
            </Row>
            <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0, fontSize: 12 }}>
              重置后所有模拟交易记录清零，适合重新评估策略表现。实盘数据不受影响。
            </Typography.Paragraph>
          </Card>
        </Col>


        {/* 系统信息 */}
        <Col xs={24} lg={10}>
          <Card size="small" title="系统信息">
            <Descriptions column={1} size="small">
              <Descriptions.Item label="交易标的">{sysInfo?.inst_id || 'BTC-USDT'}</Descriptions.Item>
              <Descriptions.Item label="当前模式">
                {sysInfo?.venue === 'okx' ? '实盘 · 真实资金' : '模拟 · 虚拟资金'}
              </Descriptions.Item>
              <Descriptions.Item label="OKX 连接">
                {sysInfo?.has_key ? '已连接' : '未连接'}
              </Descriptions.Item>
              <Descriptions.Item label="自动驾驶周期">默认 5m（可切换 1m~1D）</Descriptions.Item>
              <Descriptions.Item label="策略引擎">纯规则 V3（无 AI）</Descriptions.Item>
              <Descriptions.Item label="Key 保存方式">AES-256 加密落盘</Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
      </Row>
    </div>
  );
}
