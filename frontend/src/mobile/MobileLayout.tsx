// 移动端（H5）深色驾驶舱外壳：顶部驾驶状态条 + 内容区 + 底部 TabBar。
// 只承载"自动驾驶"相关：驾驶状态/预测/标的与托管持仓/决策记录/设置。
import { useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  RocketOutlined, LineChartOutlined, UserOutlined,
} from '@ant-design/icons';
import { useAppStore } from '../store/useAppStore';
import { useMarketStore } from '../store/useMarketStore';
import { getBrainStatus, stopAutopilot, setVenue as apiSetVenue } from '../api/endpoints';
import { metaFromId } from '../components/InstrumentSelect';
import { MSheet, MDialog, toast } from './shared';
import MobileHome from './MobileHome';
import MobileInstruments from './MobileInstruments';
import MobileMine from './MobileMine';
import MobilePcHint from './MobilePcHint';

const TABS = [
  { path: '/', label: '驾驶舱', icon: RocketOutlined },
  { path: '/market', label: '标的持仓', icon: LineChartOutlined },
  { path: '/settings', label: '我的', icon: UserOutlined },
];

function pageFor(path: string) {
  if (path === '/') return <MobileHome />;
  if (path === '/market' || path === '/orders' || path === '/account') return <MobileInstruments />;
  if (path === '/settings') return <MobileMine />;
  return <MobilePcHint />;
}

export default function MobileLayout() {
  const nav = useNavigate();
  const { pathname } = useLocation();
  const instId = useAppStore((s) => s.instId);
  const venue = useAppStore((s) => s.venue);
  const autopilotOn = useAppStore((s) => s.autopilotOn);
  const setAutopilotOn = useAppStore((s) => s.setAutopilotOn);
  const wsConnected = useAppStore((s) => s.wsConnected);
  const risk = useAppStore((s) => s.risk);
  const ticker = useMarketStore((s) => s.tickers[instId]);
  const [st, setSt] = useState<any>(null);
  const [venueOpen, setVenueOpen] = useState(false);
  const [pendingVenue, setPendingVenue] = useState<'paper' | 'okx' | null>(null);
  const [brakeOpen, setBrakeOpen] = useState(false);
  const hasKey = useAppStore((s) => s.hasKey);
  const setVenue = useAppStore((s) => s.setVenue);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const poll = async () => {
      try {
        const s = await getBrainStatus();
        setSt(s);
        setAutopilotOn(!!s.enabled);
      } catch { /* ignore */ }
    };
    poll();
    const t = setInterval(poll, 3000);
    return () => clearInterval(t);
  }, [setAutopilotOn]);

  const chg = ticker?.open24h ? ((ticker.last - ticker.open24h) / ticker.open24h) * 100 : null;
  const meta = metaFromId(instId);

  const brakeNow = async () => {
    setBrakeOpen(false);
    setBusy(true);
    try {
      await stopAutopilot();
      setAutopilotOn(false);
      toast('自动驾驶已刹车（持仓止盈止损仍生效）');
    } catch (e: any) {
      toast('刹车失败：' + (e?.message || '未知错误'), 'err');
    } finally {
      setBusy(false);
    }
  };

  const doVenueSwitch = async () => {
    const want = pendingVenue!;
    setPendingVenue(null);
    setBusy(true);
    try {
      const r = await apiSetVenue(want);
      setVenue(r.venue);
      setAutopilotOn(false);
      toast(r.fallback
        ? '实盘未就绪（未连接 Key），留在模拟模式'
        : `已切换到${r.venue === 'okx' ? '实盘' : '模拟'}（请重新开启自动驾驶）`);
    } catch (e: any) {
      toast('切换失败：' + (e?.message || '未知错误'), 'err');
    } finally {
      setBusy(false);
      setVenueOpen(false);
    }
  };

  return (
    <div
      className="h-dvh flex flex-col overflow-hidden text-white"
      style={{
        background: 'radial-gradient(1200px 600px at 20% -10%, rgba(59,130,246,0.16), transparent 60%), radial-gradient(900px 500px at 90% 10%, rgba(139,92,246,0.12), transparent 55%), #0a0d13',
        paddingBottom: 'env(safe-area-inset-bottom)',
      }}
    >
      {/* 顶部驾驶状态条 */}
      <header className="shrink-0 px-4 flex items-center justify-between border-b border-white/[0.07] bg-[#0a0d13]/80 backdrop-blur-xl" style={{ height: 52 }}>
        <button className="flex items-center gap-2" onClick={() => nav('/market')}>
          <span className="text-[16px] font-bold tracking-wide">{meta.label}</span>
          <span className="text-[11px] text-white/55 border border-white/15 rounded px-1.5 py-0.5">
            {meta.instType === 'SWAP' ? '永续' : '现货'}
          </span>
          {chg != null && (
            <span className={`text-[13px] font-bold ${chg >= 0 ? 'text-[#ff4d67]' : 'text-[#00d68f]'}`}>
              {chg >= 0 ? '▲' : '▼'}{Math.abs(chg).toFixed(2)}%
            </span>
          )}
        </button>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setVenueOpen(true)}
            className={`text-[11px] px-2 py-1 rounded-md border ${
              venue === 'okx' ? 'text-[#ff9eaa] border-[#ff4d67]/40 bg-[#ff4d67]/10' : 'text-[#7ba3ff] border-[#7ba3ff]/35 bg-[#3b82f6]/10'}`}
          >
            {venue === 'okx' ? (st?.live_ready === false ? '实盘未就绪' : '实盘') : '模拟'}
            <span className="text-white/45 ml-0.5">⌄</span>
          </button>
          {/* 启停胶囊（呼吸灯） */}
          <button
            onClick={autopilotOn ? () => setBrakeOpen(true) : () => nav('/')}
            className={`h-8 px-3.5 rounded-full text-[12px] font-bold flex items-center gap-1.5 border transition-all ${
              autopilotOn
                ? 'text-[#00d68f] border-[#00d68f]/50 bg-[#00d68f]/10 shadow-[0_0_16px_rgba(0,214,143,0.25)]'
                : 'text-white/62 border-white/15 bg-white/[0.05]'}`}
          >
            <span className={`w-1.5 h-1.5 rounded-full ${autopilotOn ? 'bg-[#00d68f] animate-[breath_1.8s_ease-in-out_infinite]' : 'bg-white/30'}`} />
            {autopilotOn ? '运行中' : '已刹车'}
          </button>
        </div>
      </header>

      {/* 内容区 */}
      <main className="flex-1 overflow-y-auto pb-4">
        {risk?.halted && (
          <div className="mx-3 mt-2 rounded-xl px-3 py-2.5 text-[12px] bg-gradient-to-r from-[#ff4d67]/20 to-[#ff7a45]/15 border border-[#ff4d67]/40 text-[#ff9eaa]">
            ⚠ 风控已熔断：{risk.halt_reason || '触发熔断'}（仅允许减仓），前往「我的 → 风控状态」查看
          </div>
        )}
        {!wsConnected && (
          <div className="mx-3 mt-2 rounded-xl px-3 py-2 text-[12px] bg-[#ff7a45]/10 border border-[#ff7a45]/30 text-[#ffb37f]">
            实时连接断开，数据可能停滞（自动重连中…）
          </div>
        )}
        {pageFor(pathname)}
      </main>

      {/* 底部 TabBar（深色毛玻璃） */}
      <nav className="shrink-0 bg-[#0a0d13]/92 backdrop-blur-xl border-t border-white/[0.08] flex">
        {TABS.map((t) => {
          const active = t.path === '/'
            ? pathname === '/'
            : pathname === t.path ||
              (t.path === '/market' && (pathname === '/orders' || pathname === '/account'));
          const Icon = t.icon;
          return (
            <button
              key={t.path}
              onClick={() => nav(t.path)}
              className="relative flex-1 h-14 flex flex-col items-center justify-center gap-0.5"
            >
              <Icon
                style={{
                  fontSize: 19,
                  color: active ? '#7ba3ff' : 'rgba(255,255,255,0.35)',
                  filter: active ? 'drop-shadow(0 0 8px rgba(123,163,255,0.65))' : 'none',
                }}
              />
              <span className={`text-[10px] ${active ? 'text-[#7ba3ff] font-medium' : 'text-white/55'}`}>
                {t.label}
              </span>
              {active && (
                <span
                  className="absolute top-0 w-8 h-0.5 rounded-b-full"
                  style={{ background: 'linear-gradient(90deg,#3b82f6,#8b5cf6)', boxShadow: '0 0 10px rgba(123,163,255,0.8)' }}
                />
              )}
            </button>
          );
        })}
      </nav>

      {/* 模拟/实盘切换 */}
      <MSheet open={venueOpen} title="切换执行通道" onClose={() => setVenueOpen(false)}>
        <div className="grid gap-2.5">
          <VenueCard
            active={venue === 'paper'}
            title="模拟盘"
            desc="虚拟资金 · 真实行情，适合观察与熟悉系统"
            icon="🧪"
            disabled={busy}
            onClick={() => setPendingVenue('paper')}
          />
          <VenueCard
            active={venue === 'okx'}
            title="实盘"
            desc={hasKey ? 'OKX 真实资金 · 需已连接 Key' : '未连接 Key，请先用 PC 端配置'}
            icon="🔥"
            disabled={busy || !hasKey}
            warn={!hasKey}
            onClick={() => hasKey && setPendingVenue('okx')}
          />
        </div>
      </MSheet>

      {/* 刹车确认 */}
      <MDialog
        open={brakeOpen}
        title="刹车自动驾驶？"
        desc="停止开新仓；已有托管持仓的止盈止损/trailing 仍生效（建议之后用「我的 → 紧急制动」做全面停机）。"
        okText="确认刹车"
        danger
        onOk={brakeNow}
        onClose={() => setBrakeOpen(false)}
      />

      {/* venue 切换确认 */}
      <MDialog
        open={pendingVenue != null}
        title={pendingVenue === 'okx' ? '切换到实盘模式？' : '切换到模拟模式？'}
        desc={pendingVenue === 'okx'
          ? '实盘使用 OKX 真实资金！切换到实盘后自动驾驶需手动重新开启。'
          : '模拟盘使用虚拟资金，可放心测试。切换后自动驾驶需手动重新开启。'}
        okText="确认切换"
        danger={pendingVenue === 'okx'}
        onOk={doVenueSwitch}
        onClose={() => setPendingVenue(null)}
      />
    </div>
  );
}

function VenueCard({ active, title, desc, icon, disabled, warn = false, onClick }: {
  active: boolean; title: string; desc: string; icon: string;
  disabled?: boolean; warn?: boolean; onClick: () => void;
}) {
  return (
    <button
      disabled={disabled}
      onClick={onClick}
      className={`w-full rounded-xl p-3.5 text-left border transition-all disabled:opacity-45 ${
        active
          ? 'border-[#7ba3ff]/60 bg-gradient-to-br from-[#3b82f6]/18 to-[#8b5cf6]/18 shadow-[0_0_18px_rgba(59,130,246,0.18)]'
          : warn ? 'border-[#ff4d67]/35 bg-[#ff4d67]/[0.06]' : 'border-white/[0.09] bg-white/[0.04]'}`}
    >
      <div className="flex items-center gap-2.5">
        <span className="text-[20px]">{icon}</span>
        <span>
          <span className="block text-[15px] font-bold text-white/90">{title}{active && ' · 当前'}</span>
          <span className="block text-[12px] text-white/50 mt-0.5">{desc}</span>
        </span>
      </div>
    </button>
  );
}
