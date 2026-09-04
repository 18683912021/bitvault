// 顶栏系统级控制：自动驾驶开关 + 模式彩色徽章 + 模式切换器。
// autopilot 跟随系统级模式执行：模拟盘用虚拟资金，实盘用真实资金（已连 Key）。
import { useEffect, useState } from 'react';
import { Switch, Segmented, Space, Tag, Tooltip, App } from 'antd';
import { ThunderboltOutlined, ExperimentOutlined, FireFilled } from '@ant-design/icons';
import { useAppStore } from '../store/useAppStore';
import { getBrainStatus, startAutopilot, stopAutopilot, setVenue as apiSetVenue } from '../api/endpoints';

export default function HeaderControl() {
  const { message } = App.useApp();
  const autopilotOn = useAppStore((s) => s.autopilotOn);
  const setAutopilotOn = useAppStore((s) => s.setAutopilotOn);
  const venue = useAppStore((s) => s.venue);
  const setVenue = useAppStore((s) => s.setVenue);
  const hasKey = useAppStore((s) => s.hasKey);
  const [toggling, setToggling] = useState(false);
  const [switching, setSwitching] = useState(false);

  // 初始拉一次 autopilot 状态 + 轮询同步（顶栏开关与策略中心一致）
  useEffect(() => {
    const poll = async () => {
      try {
        const s = await getBrainStatus();
        setAutopilotOn(!!s.enabled);
        if (s.venue) setVenue(s.venue);   // autopilot 实际执行模式可能被后端兜底改回 paper，这里同步
      } catch { /* ignore */ }
    };
    poll();
    const t = setInterval(poll, 5000);
    return () => clearInterval(t);
  }, [setAutopilotOn, setVenue]);

  const toggle = async (checked: boolean) => {
    setToggling(true);
    try {
      if (checked) {
        await startAutopilot({ period: '1H' });   // V2 验证最优配置（require_setup+breakout_retest 已是后端默认）
        const tag = venue === 'okx' ? '实盘·真实资金' : '模拟·虚拟资金';
        message.success(`自动驾驶已启动（1H · 纯规则 · ${tag}）`);
      } else {
        await stopAutopilot();
        message.info('自动驾驶已刹车（不开新仓，已有持仓止损止盈仍生效）');
      }
      setAutopilotOn(checked);
    } catch (e: any) {
      message.error('操作失败：' + (e?.message || '未知错误'));
    } finally {
      setToggling(false);
    }
  };

  const switchVenue = async (v: 'paper' | 'okx') => {
    if (v === 'okx' && !hasKey) {
      message.warning('未连接 OKX Key，已留在模拟模式（可在系统设置配置 Key）');
      return;
    }
    setSwitching(true);
    try {
      const r = await apiSetVenue(v);
      setVenue(r.venue);
      // 切换后立即拉取新 venue 的 autopilot 状态（各 venue 独立配置）
      const s = await getBrainStatus();
      setAutopilotOn(!!s.enabled);
      if (r.fallback) {
        message.warning('实盘未就绪，已留在模拟模式');
      } else if (r.venue === 'okx') {
        message.success(`已切换到实盘模式 · 真实资金（自动驾驶：${s.enabled ? '运行中' : '已刹车'} · ${s.mode === 'sprint' ? '冲刺' : '稳健'} ${s.leverage}x）`);
      } else {
        message.success(`已切换到模拟模式 · 虚拟资金（自动驾驶：${s.enabled ? '运行中' : '已刹车'} · ${s.mode === 'sprint' ? '冲刺' : '稳健'} ${s.leverage}x）`);
      }
    } catch (e: any) {
      message.error('切换失败：' + (e?.message || '未知错误'));
    } finally {
      setSwitching(false);
    }
  };

  const apTooltip = venue === 'okx'
    ? '自动驾驶在实盘运行：用真实资金按规则下单（踩油门就走、踩刹车就停）'
    : '自动驾驶在模拟盘运行：虚拟资金、真实行情与费率（踩油门就走、踩刹车就停）';

  return (
    <Space size="middle" align="center">
      {/* 自动驾驶开关（跟随当前模式） */}
      <Tooltip title={apTooltip}>
        <Space size={4} align="center">
          <ThunderboltOutlined style={{ color: autopilotOn ? '#52c41a' : '#bfbfbf' }} />
          <Switch
            size="small"
            loading={toggling}
            checked={autopilotOn}
            onChange={toggle}
            checkedChildren="运行"
            unCheckedChildren="刹车"
          />
        </Space>
      </Tooltip>

      {/* 模式彩色徽章（只读醒目标识） */}
      {venue === 'paper' ? (
        <Tag icon={<ExperimentOutlined />} className="mode-badge-paper" style={{ margin: 0, fontWeight: 700 }}>
          模拟 · 虚拟资金
        </Tag>
      ) : (
        <Tag icon={<FireFilled />} className="mode-badge-okx" style={{ margin: 0, fontWeight: 700 }}>
          实盘 · 真实资金
        </Tag>
      )}

      {/* 模式切换器 */}
      <Segmented
        size="small"
        disabled={switching}
        value={venue}
        onChange={(v) => switchVenue(v as 'paper' | 'okx')}
        options={[
          { label: '模拟', value: 'paper' },
          { label: '实盘', value: 'okx' },
        ]}
      />
    </Space>
  );
}
