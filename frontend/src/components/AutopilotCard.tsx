// 自动驾驶状态条：紧凑单行布局，有持仓时展开详情。
import { useEffect, useState } from 'react';
import { Tag, Space, Typography, Segmented, Modal } from 'antd';
import {
  PauseCircleOutlined, PlayCircleOutlined, ClockCircleOutlined,
  RocketOutlined, SafetyOutlined, ExperimentOutlined, FireFilled,
} from '@ant-design/icons';
import { useAppStore } from '../store/useAppStore';
import { getBrainStatus, updateBrainConfig } from '../api/endpoints';
import { fmtPx, fmtTime } from '../utils/format';
import { metaFromId } from './InstrumentSelect';
import type { BrainStatus } from '../api/types';

// 键与后端 factor_engine.classify_regime 的七态对齐（trend_up/trend_down/low_vol/high_vol/extreme/range/unknown）
const REGIME_TXT: Record<string, { label: string; color: string }> = {
  trend_up: { label: '牛市趋势', color: '#52c41a' },
  trend_down: { label: '熊市趋势', color: '#cf1322' },
  range: { label: '震荡观望', color: '#faad14' },
  low_vol: { label: '低波动观望', color: '#8c8c8c' },
  high_vol: { label: '高波动躁动', color: '#fa8c16' },
  extreme: { label: '极端波动', color: '#ff4d4f' },
  unknown: { label: '数据不足', color: '#bfbfbf' },
};

export default function AutopilotCard() {
  const venue = useAppStore((s) => s.venue);
  const [st, setSt] = useState<BrainStatus | null>(null);
  const [switching, setSwitching] = useState(false);

  useEffect(() => {
    const poll = async () => {
      try { setSt(await getBrainStatus()); } catch { /* ignore */ }
    };
    poll();
    const t = setInterval(poll, 3000);
    return () => clearInterval(t);
  }, []);

  const running = st?.running && st?.enabled;
  const regime = st?.regime || 'unknown';
  const regimeInfo = REGIME_TXT[regime] || REGIME_TXT.unknown;
  const pos = st?.position;
  const throttle = st?.throttle;
  const mode = st?.mode || 'normal';
  const leverage = st?.leverage || 2;
  const isSprint = mode === 'sprint';

  // 冲刺模式默认 10x、稳健模式 2x，均服从风控中心设置的杠杆上限（leverage_cap）
  const leverCap = st?.leverage_cap ?? 10;
  const sprintLev = Math.min(10, leverCap);
  const normalLev = Math.min(2, leverCap);
  const switchMode = (newMode: 'normal' | 'sprint') => {
    if (newMode === mode) return;
    const newLev = newMode === 'sprint' ? sprintLev : normalLev;
    Modal.confirm({
      title: newMode === 'sprint' ? '切换到冲刺模式？' : '切换到稳健模式？',
      content: newMode === 'sprint'
        ? `冲刺模式使用 ${newLev}x 杠杆（上限 ${leverCap}x），放大收益也放大风险。策略信号和止损不变，仓位放大 ${newLev} 倍。`
        : `稳健模式使用 ${newLev}x 杠杆。策略信号不变，仓位放大 ${newLev} 倍。`,
      okText: '确认',
      cancelText: '取消',
      onOk: async () => {
        setSwitching(true);
        try {
          await updateBrainConfig({ mode: newMode, leverage: newLev });
          setSt(await getBrainStatus());
        } finally { setSwitching(false); }
      },
    });
  };

  const bg = running
    ? (isSprint ? 'linear-gradient(90deg, #fff1f0 0%, #fff7e6 100%)' : 'linear-gradient(90deg, #f6ffed 0%, #f0f5ff 100%)')
    : '#fafafa';
  const border = running
    ? (isSprint ? '#ffa39e' : '#b7eb8f')
    : '#d9d9d9';

  return (
    <div style={{ marginBottom: 12 }}>
      {/* 主状态条 */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          flexWrap: 'wrap',
          gap: '8px 16px',
          padding: '10px 16px',
          borderRadius: 8,
          background: bg,
          border: `1px solid ${border}`,
          boxShadow: '0 1px 3px rgba(0,0,0,0.04)',
        }}
      >
        {/* 左：运行状态 + 模式标识 */}
        <Space size="small" align="center">
          {running ? (
            <PlayCircleOutlined style={{ fontSize: 20, color: isSprint ? '#ff4d4f' : '#52c41a' }} />
          ) : (
            <PauseCircleOutlined style={{ fontSize: 20, color: '#bfbfbf' }} />
          )}
          <Typography.Text strong style={{ fontSize: 15 }}>
            {running ? '运行中' : '已刹车'}
          </Typography.Text>
          <Tag
            className={venue === 'paper' ? 'mode-badge-paper' : 'mode-badge-okx'}
            icon={venue === 'paper' ? <ExperimentOutlined /> : <FireFilled />}
            style={{ margin: 0 }}
          >
            {venue === 'paper' ? '模拟' : '实盘'}
          </Tag>
          <Tag color="blue" style={{ margin: 0 }}>
            {metaFromId(st?.inst_id || 'BTC-USDT').label}
          </Tag>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {st?.period || '1H'}
          </Typography.Text>
        </Space>

        {/* 中：驾驶模式选择 */}
        <Space size="small" align="center">
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>驾驶模式</Typography.Text>
          <Segmented
            size="small"
            disabled={switching}
            value={mode}
            onChange={(v) => switchMode(v as 'normal' | 'sprint')}
            options={[
              { label: '稳健', value: 'normal', icon: <SafetyOutlined /> },
              { label: '冲刺', value: 'sprint', icon: <RocketOutlined /> },
            ]}
          />
          <Tag
            color={isSprint ? 'red' : 'blue'}
            style={{ margin: 0, fontWeight: 700, fontSize: 12 }}
          >
            {leverage}x
          </Tag>
        </Space>

        {/* 右：市场状态 + 动作 + 节流 */}
        <Space size="small" align="center" wrap>
          <Typography.Text style={{ fontSize: 13, color: regimeInfo.color, fontWeight: 600 }}>
            {regimeInfo.label}
          </Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            · {pos ? (pos.side === 'short' ? '持空仓' : '持多仓') : '空仓观望'}
          </Typography.Text>
          {(st?.positions?.length ?? 0) > 1 && (
            <Tag color="orange" style={{ margin: 0 }}>
              另托管 {st!.positions!.filter((q) => q.inst_id !== st?.inst_id).length} 个标的仓位
            </Tag>
          )}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            · 今日 {throttle?.opens_today ?? 0}/{throttle?.max_opens_per_day ?? 20}
          </Typography.Text>
          {throttle?.sleeping && (
            <Tag color="orange" style={{ margin: 0, fontSize: 11 }}>
              <ClockCircleOutlined /> 休眠
            </Tag>
          )}
          {throttle?.cooldown_until ? (
            <Tag color="blue" style={{ margin: 0, fontSize: 11 }}>冷却</Tag>
          ) : null}
          {(throttle?.loss_streak ?? 0) >= 2 ? (
            <Tag color="red" style={{ margin: 0, fontSize: 11 }}>
              连亏{throttle?.loss_streak}
            </Tag>
          ) : null}
        </Space>
      </div>

      {/* 持仓详情（有仓位时展开） */}
      {pos && (
        <div
          style={{
            marginTop: 6,
            padding: '8px 16px',
            borderRadius: 8,
            background: venue === 'paper' ? '#e6f4ff' : '#fff7e6',
            border: `1px solid ${venue === 'paper' ? '#91caff' : '#ffd591'}`,
            fontSize: 13,
            display: 'flex',
            flexWrap: 'wrap',
            gap: '4px 20px',
            alignItems: 'center',
          }}
        >
          <Tag color={pos.side === 'short' ? 'red' : 'green'} style={{ margin: 0 }}>
            {pos.side === 'short' ? '空' : '多'}
          </Tag>
          <span>开仓 <strong>{fmtPx(pos.entry_px)}</strong></span>
          <span>止损 <strong style={{ color: '#cf1322' }}>{fmtPx(pos.sl_px)}</strong></span>
          <span>止盈1 <strong style={{ color: '#3f8600' }}>{fmtPx(pos.tp1_px)}</strong></span>
          <span>止盈2 <strong style={{ color: '#3f8600' }}>{fmtPx(pos.tp2_px)}</strong></span>
          <span>数量 <strong>{pos.sz?.toFixed(6)}</strong></span>
          {pos.liq_px && (
            <span>强平价 <strong style={{ color: '#ff4d4f' }}>{fmtPx(pos.liq_px)}</strong></span>
          )}
          {pos.leverage > 1 && (
            <span>杠杆 <strong style={{ color: '#ff4d4f' }}>{pos.leverage}x</strong>
              <span style={{ color: '#999' }}> 保证金={pos.margin}U</span>
            </span>
          )}
          <span style={{ color: '#999' }}>
            · {pos.setup || '—'} · {fmtTime(pos.open_ts)}
          </span>
        </div>
      )}

      {/* 实盘未就绪提示 */}
      {venue === 'okx' && st && !st.live_ready && (
        <div style={{ marginTop: 4, fontSize: 12, color: '#cf1322' }}>
          实盘模式未就绪（未连接 Key），autopilot 正在观望。
        </div>
      )}
    </div>
  );
}
