// 系统设置：精简版——模拟盘重置 + 系统信息。
// Key 已预配置，无需管理界面。
import { useEffect, useState, useCallback } from 'react';
import { Card, Row, Col, Statistic, Button, InputNumber, Space, Modal, Typography, Descriptions } from 'antd';
import { useAppStore } from '../store/useAppStore';
import { getStatus, getPaperAccount, resetPaperAccount } from '../api/endpoints';
import { fmtTime } from '../utils/format';
import type { PaperAccount } from '../api/types';

export default function Settings() {
  const setEnv = useAppStore((s) => s.setEnv);
  const setHasKey = useAppStore((s) => s.setHasKey);
  const setRisk = useAppStore((s) => s.setRisk);
  const setVenue = useAppStore((s) => s.setVenue);
  const [paper, setPaper] = useState<PaperAccount | null>(null);
  const [paperInitial, setPaperInitial] = useState(10000);
  const [sysInfo, setSysInfo] = useState<{ env: string; has_key: boolean; venue: string } | null>(null);

  const loadPaper = useCallback(() => {
    getPaperAccount().then(setPaper).catch(() => {});
  }, []);

  const refresh = useCallback(async () => {
    const st = await getStatus();
    setEnv(st.env);
    setHasKey(st.has_key);
    setRisk(st.risk);
    if (st.venue) setVenue(st.venue);
    setSysInfo({ env: st.env, has_key: st.has_key, venue: st.venue });
  }, [setEnv, setHasKey, setRisk, setVenue]);

  useEffect(() => {
    refresh();
    loadPaper();
    const t = setInterval(loadPaper, 5000);
    return () => clearInterval(t);
  }, [refresh, loadPaper]);

  return (
    <div>
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
              <Descriptions.Item label="交易标的">BTC-USDT</Descriptions.Item>
              <Descriptions.Item label="当前模式">
                {sysInfo?.venue === 'okx' ? '实盘 · 真实资金' : '模拟 · 虚拟资金'}
              </Descriptions.Item>
              <Descriptions.Item label="OKX 连接">
                {sysInfo?.has_key ? '已连接' : '未连接'}
              </Descriptions.Item>
              <Descriptions.Item label="自动驾驶周期">1H（1 小时）</Descriptions.Item>
              <Descriptions.Item label="策略引擎">纯规则 V3（无 AI）</Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
      </Row>
    </div>
  );
}
