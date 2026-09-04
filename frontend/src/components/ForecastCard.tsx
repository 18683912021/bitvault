// 10 分钟涨跌预测卡（事件合约参考）：后端每 10 秒计算并经 WS 推送，
// WS 断开时本组件每 10 秒轮询兜底。紧凑展示，不做大篇幅。
import { useEffect, useState } from 'react';
import { Card, Col, Row, Tag, Typography, Progress, Tooltip } from 'antd';
import { ArrowUpOutlined, ArrowDownOutlined, MinusOutlined, QuestionCircleOutlined } from '@ant-design/icons';
import { useMarketStore } from '../store/useMarketStore';
import { useAppStore } from '../store/useAppStore';
import { getForecast10 } from '../api/endpoints';
import type { Forecast10 } from '../api/types';

const DIR_META: Record<string, { text: string; color: string; icon: JSX.Element }> = {
  up: { text: '看涨', color: '#cf1322', icon: <ArrowUpOutlined /> },      // 红涨绿跌
  down: { text: '看跌', color: '#3f8600', icon: <ArrowDownOutlined /> },
  flat: { text: '震荡', color: '#8c8c8c', icon: <MinusOutlined /> },
  unknown: { text: '数据不足', color: '#d4b106', icon: <QuestionCircleOutlined /> },
};

const REGIME_LABEL: Record<string, string> = {
  trend_up: '上升趋势', trend_down: '下降趋势', range: '震荡区间',
  high_vol: '高波动', low_vol: '低波动', extreme: '极端波动', unknown: '状态未知',
};

// 本地每秒跳动的倒计时（窗口结束）
function useCountdown(endTs: number) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const left = Math.max(0, Math.floor((endTs - now) / 1000));
  const m = String(Math.floor(left / 60)).padStart(2, '0');
  const s = String(left % 60).padStart(2, '0');
  return `${m}:${s}`;
}

export default function ForecastCard() {
  const forecast = useMarketStore((s) => s.forecast);
  const wsConnected = useAppStore((s) => s.wsConnected);
  const [fallback, setFallback] = useState<Forecast10 | null>(null);

  // WS 断线兜底：10 秒轮询
  useEffect(() => {
    if (wsConnected) return;
    const tick = () => getForecast10().then(setFallback).catch(() => {});
    tick();
    const t = setInterval(tick, 10000);
    return () => clearInterval(t);
  }, [wsConnected]);

  const f: Forecast10 | null = forecast || fallback;
  const meta = DIR_META[f?.direction || 'unknown'] || DIR_META.unknown;
  const countdown = useCountdown(f?.window_end_ts || 0);
  const pUp = f?.p_up ?? 50;
  const topFactors = (f?.factors || [])
    .slice()
    .sort((a, b) => Math.abs(b.contrib) - Math.abs(a.contrib))
    .slice(0, 3);

  return (
    <Card
      size="small"
      title="未来10分钟涨跌预测"
      extra={
        <Tooltip title="口径对齐 OKX 事件合约：预测下一个 10 分钟窗口结算均价相对基准价的方向。多因子确定性打分（动量/盘口/均值回归/趋势一致性），极端波动时置信度自动折减。仅供事件合约方向参考，不构成承诺。">
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            事件合约参考 <QuestionCircleOutlined />
          </Typography.Text>
        </Tooltip>
      }
    >
      {!f || f.direction === 'unknown' ? (
        <Typography.Text type="secondary">{f?.note || '等待行情数据…'}</Typography.Text>
      ) : (
        <Row gutter={16} align="middle">
          <Col xs={8} md={5}>
            <div style={{ fontSize: 28, fontWeight: 700, color: meta.color }}>
              {meta.icon} {meta.text}
            </div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              窗口 {new Date(f.window_start_ts).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}
              ~{new Date(f.window_end_ts).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}
            </Typography.Text>
          </Col>
          <Col xs={8} md={5}>
            <div style={{ fontSize: 12, color: '#999' }}>距窗口结束</div>
            <div style={{ fontSize: 20, fontVariantNumeric: 'tabular-nums' }}>{countdown}</div>
            <Tag style={{ marginTop: 2 }} color={REGIME_LABEL[f.regime] ? 'default' : 'default'}>
              {REGIME_LABEL[f.regime] || f.regime}
            </Tag>
          </Col>
          <Col xs={8} md={4}>
            <div style={{ fontSize: 12, color: '#999' }}>看涨概率</div>
            <div style={{ fontSize: 20, fontWeight: 600, color: pUp >= 50 ? '#cf1322' : '#3f8600' }}>
              {pUp.toFixed(0)}%
            </div>
            <Progress
              percent={pUp}
              showInfo={false}
              size="small"
              strokeColor={pUp >= 50 ? '#cf1322' : '#3f8600'}
              trailColor="#3f8600"
              style={{ maxWidth: 90 }}
            />
          </Col>
          <Col xs={24} md={10}>
            {topFactors.map((x) => (
              <div key={x.key} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, lineHeight: '20px' }}>
                <Typography.Text type="secondary">{x.label}</Typography.Text>
                <span>
                  <Typography.Text style={{ fontSize: 12 }}>{x.value_text}</Typography.Text>
                  <span style={{ marginLeft: 6, color: x.contrib > 0.01 ? '#cf1322' : x.contrib < -0.01 ? '#3f8600' : '#999' }}>
                    {x.contrib > 0.01 ? '↑' : x.contrib < -0.01 ? '↓' : '—'}
                  </span>
                </span>
              </div>
            ))}
          </Col>
        </Row>
      )}
      {f?.note && (
        <Typography.Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: 8 }}>
          {f.note}
        </Typography.Text>
      )}
    </Card>
  );
}
