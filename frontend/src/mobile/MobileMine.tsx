// 移动端「我的」（深色）：驾驶模式 / 风控状态 / 系统信息 / 模拟盘重置 / 紧急制动。
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAppStore } from '../store/useAppStore';
import { getBrainStatus, updateBrainConfig, getPaperAccount, resetPaperAccount, getStatus, killSwitch } from '../api/endpoints';
import { metaFromId } from '../components/InstrumentSelect';
import { MCard, MCardTitle, MButton, MDialog, PnLText, toast } from './shared';

export default function MobileMine() {
  const nav = useNavigate();
  const venue = useAppStore((s) => s.venue);
  const instId = useAppStore((s) => s.instId);
  const hasKey = useAppStore((s) => s.hasKey);
  const wsConnected = useAppStore((s) => s.wsConnected);
  const [brain, setBrain] = useState<any>(null);
  const [sys, setSys] = useState<any>(null);
  const [paper, setPaper] = useState<any>(null);
  const [resetOpen, setResetOpen] = useState(false);
  const [killOpen, setKillOpen] = useState(false);
  const [modeConfirm, setModeConfirm] = useState<'normal' | 'sprint' | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const poll = async () => {
      try {
        const [b, s] = await Promise.all([getBrainStatus(), getStatus()]);
        setBrain(b);
        setSys(s);
        getPaperAccount().then(setPaper).catch(() => {});
      } catch { /* ignore */ }
    };
    poll();
    const t = setInterval(poll, 5000);
    return () => clearInterval(t);
  }, []);

  const switchMode = async (mode: 'normal' | 'sprint') => {
    if (mode === brain?.mode) return;
    const lev = mode === 'sprint' ? Math.min(10, brain?.leverage_cap ?? 10) : Math.min(2, brain?.leverage_cap ?? 2);
    setModeConfirm(null);
    setBusy(true);
    try {
      await updateBrainConfig({ mode, leverage: lev });
      setBrain(await getBrainStatus());
      toast(`已切换到${mode === 'sprint' ? '冲刺' : '稳健'}模式（${lev}x）`);
    } catch (e: any) {
      toast('切换失败：' + (e?.message || ''), 'err');
    } finally {
      setBusy(false);
    }
  };

  const doReset = async () => {
    setBusy(true);
    try {
      await resetPaperAccount(10000);
      setPaper(await getPaperAccount());
      toast('模拟盘已重置为 10,000 USDT');
    } catch (e: any) {
      toast('重置失败：' + (e?.message || ''), 'err');
    } finally {
      setBusy(false);
      setResetOpen(false);
    }
  };

  const doKill = async () => {
    setBusy(true);
    try {
      await killSwitch();
      toast('紧急制动已触发：所有策略停机，仅允许减仓');
    } catch (e: any) {
      toast('触发失败：' + (e?.message || ''), 'err');
    } finally {
      setBusy(false);
      setKillOpen(false);
    }
  };

  const entries = [
    { to: '/risk', label: '风控中心', desc: '熔断状态 / 规则配置（建议 PC 操作）' },
    { to: '/strategies', label: '策略实例', desc: '回测与实例管理（建议 PC 操作）' },
    { to: '/backtest', label: '回测中心', desc: '历史回测（建议 PC 操作）' },
    { to: '/notifications', label: '通知中心', desc: '系统通知' },
  ];

  const pnl = paper ? paper.equity - paper.initial : 0;

  return (
    <div className="space-y-3.5 p-3.5">
      {/* 账户卡 */}
      <MCard glow>
        <MCardTitle title={venue === 'paper' ? '模拟盘账户' : 'OKX 实盘账户'} extra={
          <span className="text-[11px] text-white/55">{venue === 'paper' ? '虚拟资金' : '真实资金'}</span>
        } />
        <div className="flex items-end justify-between">
          <div>
            <div className="text-[11px] text-white/58">账户权益</div>
            <div className="text-[30px] font-bold tabular-nums">
              <span className="text-[17px] text-white/50 font-medium">$</span>{(paper?.equity ?? 0).toFixed(2)}
            </div>
          </div>
          <div className="text-right">
            <div className="text-[11px] text-white/58">累计盈亏</div>
            <PnLText v={pnl} className="text-[18px] font-bold tabular-nums" />
          </div>
          <div className="text-right">
            <div className="text-[11px] text-white/58">持仓</div>
            <div className="text-[18px] font-bold tabular-nums">{paper?.positions?.length ?? 0}<span className="text-[11px] text-white/58">个</span></div>
          </div>
        </div>
      </MCard>

      {/* 驾驶模式 */}
      <MCard>
        <MCardTitle title="驾驶模式（杠杆）" extra={
          <span className="text-[11px] text-white/55">上限 {brain?.leverage_cap ?? 10}x</span>
        } />
        <div className="grid grid-cols-2 gap-3">
          <ModeBtn
            active={brain?.mode !== 'sprint'}
            title="稳健"
            lev={`${Math.min(2, brain?.leverage_cap ?? 2)}x`}
            desc="仓位保守"
            onClick={() => setModeConfirm('normal')}
          />
          <ModeBtn
            active={brain?.mode === 'sprint'}
            title="冲刺"
            lev={`${Math.min(10, brain?.leverage_cap ?? 10)}x`}
            desc="收益与风险放大"
            onClick={() => setModeConfirm('sprint')}
          />
        </div>
      </MCard>

      {/* 紧急制动 */}
      <MCard>
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-[14px] font-semibold">紧急制动</div>
            <div className="text-[12px] text-white/58 mt-0.5">立即停机所有策略与开仓，仅允许减仓</div>
          </div>
          <button
            disabled={busy}
            onClick={() => setKillOpen(true)}
            className="shrink-0 px-4 h-10 rounded-xl text-[13px] font-bold text-[#ff4d67] border border-[#ff4d67]/45 bg-[#ff4d67]/10"
          >
            触发熔断
          </button>
        </div>
      </MCard>


      {/* 系统信息 */}
      <MCard>
        <MCardTitle title="系统信息" />
        <InfoRow k="驾驶标的" v={metaFromId(instId).label} />
        <InfoRow k="自动驾驶" v={brain?.enabled ? `运行中（${brain?.mode === 'sprint' ? '冲刺' : '稳健'} ${brain?.leverage}x · ${brain?.period}）` : '已刹车'} />
        <InfoRow k="执行通道" v={venue === 'okx' ? '实盘 · 真实资金' : '模拟 · 虚拟资金'} />
        <InfoRow k="OKX Key" v={hasKey ? `已连接（${sys?.env || '—'}）` : '未连接 · 请用 PC 端配置'} />
        <InfoRow k="实时连接" v={wsConnected ? '正常' : '断开'} />
      </MCard>

      {/* 模拟盘重置 */}
      <MCard>
        <div className="flex items-center justify-between">
          <div>
            <div className="text-[14px] font-semibold">模拟盘重置</div>
            <div className="text-[12px] text-white/58 mt-0.5">恢复为 10,000 USDT 初始资金</div>
          </div>
          <button
            className="shrink-0 px-4 h-10 rounded-xl text-[13px] font-medium text-white/75 border border-white/15 bg-white/[0.06]"
            onClick={() => setResetOpen(true)}
          >
            重置
          </button>
        </div>
      </MCard>

      {/* 功能入口 */}
      <MCard>
        <MCardTitle title="更多功能" />
        {entries.map((e) => (
          <button
            key={e.to}
            onClick={() => nav(e.to)}
            className="w-full flex items-center justify-between py-2.5 border-b border-white/[0.05] last:border-0"
          >
            <span>
              <span className="text-[14px] font-medium text-white/85">{e.label}</span>
              <span className="text-[12px] text-white/45 ml-2">{e.desc}</span>
            </span>
            <span className="text-white/25">›</span>
          </button>
        ))}
      </MCard>

      <div className="text-center text-[11px] text-white/20 pb-2">BitVault · H5 驾驶舱 · v1.2</div>

      <MDialog
        open={modeConfirm != null}
        title={`切换到${modeConfirm === 'sprint' ? '冲刺' : '稳健'}模式？`}
        desc={`将使用 ${modeConfirm === 'sprint' ? Math.min(10, brain?.leverage_cap ?? 10) : Math.min(2, brain?.leverage_cap ?? 2)}x 杠杆（上限 ${brain?.leverage_cap ?? 10}x）。策略信号和止损不变，仓位放大对应倍数。`}
        okText="确认切换"
        onOk={() => switchMode(modeConfirm!)}
        onClose={() => setModeConfirm(null)}
      />
      <MDialog
        open={resetOpen}
        title="重置模拟盘？"
        desc="将清空本地模拟账户的全部资产与成交记录，恢复为 10,000 USDT。该操作不可回退。"
        okText="确认重置"
        danger
        onOk={doReset}
        onClose={() => setResetOpen(false)}
      />
      <MDialog
        open={killOpen}
        title="触发紧急制动？"
        desc="将触发全局熔断：所有策略停机、禁止开仓，仅允许减仓。解除需在风控中心人工确认。"
        okText="确认制动"
        danger
        onOk={doKill}
        onClose={() => setKillOpen(false)}
      />
    </div>
  );
}

function ModeBtn({ active, title, lev, desc, onClick }: {
  active: boolean; title: string; lev: string; desc: string; onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`rounded-xl py-3 text-center border transition-all ${
        active
          ? 'border-[#7ba3ff]/60 bg-gradient-to-br from-[#3b82f6]/20 to-[#8b5cf6]/20 shadow-[0_0_16px_rgba(59,130,246,0.2)]'
          : 'border-white/[0.08] bg-white/[0.03]'}`}
    >
      <div className="text-[15px] font-bold text-white/85">{title}</div>
      <div className={`text-[20px] font-bold mt-0.5 tabular-nums ${active ? 'text-[#a5c8ff]' : 'text-white/50'}`}>{lev}</div>
      <div className="text-[11px] text-white/55 mt-0.5">{desc}</div>
    </button>
  );
}

function InfoRow({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between py-1.5 border-b border-white/[0.05] last:border-0 text-[13px]">
      <span className="text-white/58">{k}</span>
      <span className="font-medium text-right text-white/85">{v}</span>
    </div>
  );
}
