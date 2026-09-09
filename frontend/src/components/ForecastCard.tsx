// 未来 24 小时涨跌预测卡（纯规则）：后端每 10 秒计算并经 WS 推送，
// WS 断开时本组件每 10 秒轮询兜底。精简排版：方向 + 概率 + 建议开平仓价。
import { useEffect, useState } from 'react';
import { Card, Col, Row, Typography, Progress, Tooltip, Space } from 'antd';
import { ArrowUpOutlined, ArrowDownOutlined, MinusOutlined, QuestionCircleOutlined } from '@ant-design/icons';
import { useMarketStore } from '../store/useMarketStore';
import { useAppStore } from '../store/useAppStore';
import { getForecast } from '../api/endpoints';
import { fmtPx } from '../utils/format';
import type { Forecast24 } from '../api/types';

const DIR_META: Record<string, { text: string; color: string; icon: JSX.Element }> = {
  up: { text: '看涨', color: '#cf1322', icon: <ArrowUpOutlined /> },      // 红涨绿跌
  down: { text: '看跌', color: '#3f8600', icon: <ArrowDownOutlined /> },
  flat: { text: '震荡', color: '#8c8c8c', icon: <MinusOutlined /> },
  unknown: { text: '数据不足', color: '#d4b106', icon: <QuestionCircleOutlined /> },
};

function fmtWindow(ts: number): string {
  const d = new Date(ts);
  return d.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
}

export default function ForecastCard() {
  const forecast = useMarketStore((s) => s.forecast);
  const wsConnected = useAppStore((s) => s.wsConnected);
  const [fallback, setFallback] = useState<Forecast24 | null>(null);

  // WS 断线兜底：10 秒轮询
  useEffect(() => {
    if (wsConnected) return;
    const tick = () => getForecast().then(setFallback).catch(() => {});
    tick();
    const t = setInterval(tick, 10000);
    return () => clearInterval(t);
  }, [wsConnected]);

  const f: Forecast24 | null = forecast || fallback;
  const meta = DIR_META[f?.direction || 'unknown'] || DIR_META.unknown;
  const pUp = f?.p_up ?? 50;
  const sug = f?.suggestion || null;
  const isLong = sug?.side === 'long';

  return (
    <Card
      size="small"
      title="未来24小时涨跌预测"
      extra={
        <Tooltip title="口径：当前时刻 → +24 小时。纯规则多因子（24h动量/1H趋势/4H确认/RSI/布林/量能），极端波动自动折减置信度。建议开仓/止盈/止损价与自动驾驶同一套规则，仅供参考，不构成承诺、不自动下单。">
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            确定性评分 <QuestionCircleOutlined />
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
              {fmtWindow(f.window_start_ts)} ~ {fmtWindow(f.window_end_ts)}
            </Typography.Text>
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
          <Col xs={24} md={15}>
            {sug ? (
              isLong ? (
                <Space>
                  <PriceTag label="开仓" v={sug.entry_px} />
                  <PriceTag label="止盈" v={sug.tp1_px} />
                  <PriceTag label="止损" v={sug.sl_px} />
                  {sug.tp2_px ? <Typography.Text type="secondary" style={{ fontSize: 12 }}>2R {fmtPx(sug.tp2_px)} · R:{sug.rr?.toFixed(1)}</Typography.Text> : null}
                </Space>
              ) : (
                <Space>
                  <PriceTag label="开仓" v={sug.entry_px} />
                  <PriceTag label="止盈" v={sug.tp1_px} />
                  <PriceTag label="止损" v={sug.sl_px} />
                  {sug.tp2_px ? <Typography.Text type="secondary" style={{ fontSize: 12 }}>2R {fmtPx(sug.tp2_px)} · R:{sug.rr?.toFixed(1)}</Typography.Text> : null}
                </Space>
              )
            ) : (
              <Typography.Text type="secondary">{sug === null ? '建议观望' : ''}</Typography.Text>
            )}
          </Col>
        </Row>
      )}
    </Card>
  );
}

function PriceTag({ label, v }: { label: string; v?: number }) {
  if (v == null) return null;
  return (
    <span style={{ display: 'inline-flex', flexDirection: 'column', lineHeight: '16px', marginRight: 8 }}>
      <Typography.Text type="secondary" style={{ fontSize: 11 }}>{label}</Typography.Text>
      <span style={{ fontWeight: 600, fontSize: 14 }}>{fmtPx(v)}</span>
    </span>
  );
}
