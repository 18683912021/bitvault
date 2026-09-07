// 移动端驾驶舱：驾驶主卡 / 24h 预测 / 资产 / 最近决策时间线。
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowUpOutlined, ArrowDownOutlined, ThunderboltFilled, PauseCircleFilled } from '@ant-design/icons';
import { useAppStore } from '../store/useAppStore';
import { useMarketStore } from '../store/useMarketStore';
import { getBrainStatus, getBrainHistory, getPaperAccount } from '../api/endpoints';
import { metaFromId } from '../components/InstrumentSelect';
import { MCard, MCardTitle, PnLText, MDialog, MButton, fmtBig, toast } from './shared';
import { humanizeReason, fmtClockShort } from '../utils/format';

const DIR: Record<string, { text: string; color: string }> = {
  up: { text: '看涨', color: '#ff4d67' },
  down: { text: '看跌', color: '#00d68f' },
  flat: { text: '震荡', color: '#d4b106' },
  unknown: { text: '数据不足', color: '#8c8c8c' },
};

const REGIME_CN: Record<string, string> = {
  trend_up: '多头趋势', trend_down: '空头趋势', range: '震荡区间',
  low_vol: '低波动', high_vol: '高波动', extreme: '极端波动', unknown: '状态未知',
};

export default function MobileHome() {
  const nav = useNavigate();
  const venue = useAppStore((s) => s.venue);
  const instId = useAppStore((s) => s.instId);
  const autopilotOn = useAppStore((s) => s.autopilotOn);
  const setAutopilotOn = useAppStore((s) => s.setAutopilotOn);
  const forecast = useMarketStore((s) => s.forecast);
  const [brain, setBrain] = useState<any>(null);
  const [history, setHistory] = useState<any[]>([]);
  const [paper, setPaper] = useState<any>(null);
  const [startOpen, setStartOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const poll = async () => {
      try {
        const b = await getBrainStatus();
        setBrain(b);
        setAutopilotOn(!!b.enabled);
        if (venue === 'paper') getPaperAccount().then(setPaper).catch(() => {});
      } catch { /* ignore */ }
    };
    poll();
    const t = setInterval(poll, 3000);
    return () => clearInterval(t);
  }, [venue, setAutopilotOn]);

  useEffect(() => {
    getBrainHistory(10).then((h) => {
      // 倒序展示（新→旧），截断 reason
      setHistory(h.map((r: any) => ({
        ts: r.ts,
        action: (r.action || '').replace(/^decide:/, '').replace(/^gate:[a-z]+:/, ''),
        reason: (r.reason || '').slice(0, 60),
        pass: (r.action || '').includes(':pass'),
        fail: (r.action || '').includes(':fail'),
      })));
    }).catch(() => {});
  }, [autopilotOn]);

  const direction = forecast?.direction || 'unknown';
  const dm = DIR[direction];
  const sug = forecast?.suggestion;
  const isLong = sug?.side === 'long';
  const isPaper = venue === 'paper';
  const equity = isPaper ? (paper?.equity ?? 0) : 0;
  const pnl = isPaper ? (paper ? paper.equity - paper.initial : 0) : 0;
  const posCount = isPaper ? (paper?.positions?.length ?? 0) : (brain?.positions?.length ?? 0);
  const meta = metaFromId(instId);
  const running = autopilotOn && brain?.running;

  const doStart = async () => {
    setBusy(true);
    try {
      const { startAutopilot } = await import('../api/endpoints');
      await startAutopilot({ period: brain?.period || '1H' });
      setAutopilotOn(true);
      setStartOpen(false);
    } catch (e: any) {
      toast('启动失败：' + (e?.message || '未知错误'), 'err');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3.5 p-3.5">
      {/* ===== 驾驶主卡 ===== */}
      <div
        className="relative rounded-2xl p-4 overflow-hidden border"
        style={{
          borderColor: running ? 'rgba(0,214,143,0.35)' : 'rgba(255,255,255,0.10)',
          background: running
            ? 'linear-gradient(135deg, rgba(0,214,143,0.14), rgba(59,130,246,0.10) 60%, rgba(139,92,246,0.12))'
            : 'linear-gradient(135deg, rgba(255,255,255,0.06), rgba(255,255,255,0.02))',
          boxShadow: running ? '0 0 32px rgba(0,214,143,0.16)' : '0 4px 18px rgba(0,0,0,0.3)',
        }}
      >
        {/* 装饰光斑 */}
        <div className="absolute -right-8 -top-10 w-36 h-36 rounded-full opacity-30 blur-2xl"
          style={{ background: running ? '#00d68f' : '#3b82f6' }} />
        {/* 顶部高光线 */}
        <div className="absolute top-0 left-6 right-6 h-px"
          style={{ background: running
            ? 'linear-gradient(90deg, transparent, rgba(0,214,143,0.7), transparent)'
            : 'linear-gradient(90deg, transparent, rgba(123,163,255,0.5), transparent)' }} />
        <div className="relative flex items-start justify-between">
          <div>
            <div className="flex items-center gap-1.5 text-[11px] text-white/62">
              <span className="w-1.5 h-1.5 rounded-full"
                style={{ background: running ? '#00d68f' : 'rgba(255,255,255,0.3)',
                  boxShadow: running ? '0 0 8px #00d68f' : 'none' }} />
              自动驾驶
            </div>
            <div className="mt-1 flex items-center gap-2">
              {running
                ? <ThunderboltFilled style={{ fontSize: 26, color: '#00d68f', filter: 'drop-shadow(0 0 10px rgba(0,214,143,0.7))' }} />
                : <PauseCircleFilled style={{ fontSize: 26, color: 'rgba(255,255,255,0.35)' }} />}
              <span className="text-[26px] font-bold tracking-wide" style={{ color: running ? '#7dffce' : 'rgba(255,255,255,0.75)' }}>
                {running ? '运行中' : '已刹车'}
              </span>
            </div>
            <div className="mt-1.5 text-[12px] text-white/50 flex items-center gap-1.5 flex-wrap">
              <span className="font-semibold text-white/80">{meta.label}</span>
              <span className="text-white/45">·</span>
              <span>{REGIME_CN[brain?.regime] || brain?.regime || '—'}</span>
              <span className="text-white/45">·</span>
              <span>{brain?.mode === 'sprint' ? '冲刺' : '稳健'} {brain?.leverage ?? 2}x</span>
              <span className="text-white/45">·</span>
              <span>{brain?.period || '1H'}</span>
            </div>
          </div>
          <button
            disabled={busy}
            onClick={() => (running ? setStartOpen(false) : setStartOpen(true))}
            className={`w-24 h-11 rounded-xl text-[14px] font-bold transition-all ${
              running
                ? 'border border-[#ff4d67]/50 text-[#ff4d67] bg-[#ff4d67]/10'
                : 'bg-gradient-to-r from-[#3b82f6] to-[#8b5cf6] text-white shadow-[0_0_20px_rgba(59,130,246,0.4)] active:opacity-80'}`}
          >
            {running ? '刹车' : '启动'}
          </button>
        </div>

        {/* 三格统计 */}
        <div className="relative grid grid-cols-3 gap-2 mt-4 text-center">
          <HeroStat label="今日开仓" value={`${brain?.throttle?.opens_today ?? 0}/${brain?.throttle?.max_opens_per_day ?? 20}`} />
          <HeroStat label="托管持仓" value={`${posCount} 个`} />
          <HeroStat label="区间状态" value={REGIME_CN[brain?.regime] || '—'} small />
        </div>
      </div>

      {/* ===== 24h 预测 ===== */}
      <MCard>
        <MCardTitle title="未来24小时预测" extra={
          <span className="text-[11px] text-white/55">参考 {fmtBig(forecast?.ref_price)}</span>
        } />
        {forecast && direction !== 'unknown' ? (
          <>
            <div className="flex items-end gap-3">
              <span className="text-[30px] font-bold" style={{ color: dm.color }}>
                {direction === 'up' ? <ArrowUpOutlined /> : direction === 'down' ? <ArrowDownOutlined /> : '—'} {dm.text}
              </span>
              <span className="text-[20px] font-bold mb-1" style={{ color: forecast.p_up >= 50 ? '#ff4d67' : '#00d68f' }}>
                {forecast.p_up.toFixed(0)}%
              </span>
              {brain?.regime && <span className="text-[11px] text-white/58 mb-1.5">{REGIME_CN[brain.regime]}</span>}
            </div>
            <div className="h-2 rounded-full bg-white/[0.07] mt-2.5 overflow-hidden">
              <div
                className="h-full rounded-full transition-all duration-700"
                style={{
                  width: `${forecast.p_up}%`,
                  background: 'linear-gradient(90deg,#3b82f6,#8b5cf6,#ff4d67)',
                  boxShadow: '0 0 12px rgba(255,77,103,0.4)',
                }}
              />
            </div>
            {sug && sug.side !== 'flat' ? (
              <div className="grid grid-cols-4 gap-2 mt-3.5">
                <PriceCell label="开仓" hint={isLong ? '多' : '空'} px={sug.entry_px} />
                <PriceCell label="止盈1" hint={`${sug.rr?.toFixed(1)}R`} px={sug.tp1_px} />
                <PriceCell label="止盈2" hint="2R" px={sug.tp2_px} />
                <PriceCell label="止损" hint={sug.sl_basis === 'structure' ? '结构' : 'ATR'} px={sug.sl_px} />
              </div>
            ) : (
              <div className="text-[13px] text-white/58 pt-2">当前方向不明，建议观望</div>
            )}
          </>
        ) : (
          <div className="text-[13px] text-white/55 py-2">{forecast?.note || '等待行情数据…'}</div>
        )}
      </MCard>

      {/* ===== 资产 ===== */}
      <MCard>
        <MCardTitle title={isPaper ? '模拟盘资产' : '账户资产'} extra={
          <span className="text-[11px] text-white/55">{isPaper ? '虚拟资金' : 'OKX 实时'}</span>
        } />
        <div className="flex items-end justify-between">
          <div>
            <div className="text-[11px] text-white/58">账户权益</div>
            <div className="text-[30px] font-bold tabular-nums tracking-tight">
              <span className="text-[17px] text-white/50 font-medium">$</span>{equity.toFixed(2)}
            </div>
          </div>
          <div className="text-right">
            <div className="text-[11px] text-white/58">累计盈亏</div>
            <PnLText v={pnl} className="text-[20px] font-bold tabular-nums" />
          </div>
          <div className="text-right">
            <div className="text-[11px] text-white/58">持仓</div>
            <div className="text-[20px] font-bold tabular-nums">{posCount}<span className="text-[12px] text-white/58 font-medium ml-0.5">个</span></div>
          </div>
        </div>
      </MCard>

      {/* ===== 最近决策 ===== */}
      <MCard>
        <MCardTitle title="最近决策" extra={
          <button className="text-[12px] text-[#7ba3ff]" onClick={() => nav('/market')}>标的持仓 ›</button>
        } />
        {history.length === 0 ? (
          <div className="text-[13px] text-white/55 py-2 text-center">暂无决策记录</div>
        ) : (
          history.slice(0, 6).map((h, i) => (
            <div key={i} className="flex items-start gap-2.5 py-2 border-b border-white/[0.05] last:border-0">
              <span className={`mt-1 w-1.5 h-1.5 rounded-full shrink-0 ${
                h.pass ? 'bg-[#00d68f] shadow-[0_0_8px_#00d68f]'
                  : h.fail ? 'bg-[#ff4d67]/70' : 'bg-white/25'}`} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-[13px] font-semibold text-white/85">{h.action === 'wait' ? '观望' : h.action}</span>
                  <span className="text-[11px] text-white/45">{fmtClockShort(h.ts)}</span>
                </div>
                <div className="text-[11px] text-white/55 mt-0.5 leading-4">{humanizeReason(h.reason)}</div>
              </div>
            </div>
          ))
        )}
      </MCard>

      {/* 启动确认 */}
      <MDialog
        open={startOpen}
        title="开启自动驾驶"
        desc={`以 ${meta.label}（${brain?.period || '1H'} 周期 · 纯规则 V3）运行：每根 K 线收盘评估，满足形态+评分+守卫才开单；已有持仓止损止盈照常。${venue === 'okx' ? '实盘模式使用真实资金！' : '模拟盘使用虚拟资金。'}`}
        okText="确认启动"
        onOk={doStart}
        onClose={() => setStartOpen(false)}
      />
    </div>
  );
}

function HeroStat({ label, value, small = false }: { label: string; value: string; small?: boolean }) {
  return (
    <div className="rounded-xl bg-white/[0.05] border border-white/[0.07] py-2">
      <div className="text-[10px] text-white/58">{label}</div>
      <div className={`font-bold tabular-nums ${small ? 'text-[13px]' : 'text-[16px]'} text-white/90 mt-0.5`}>{value}</div>
    </div>
  );
}

function PriceCell({ label, hint, px }: { label: string; hint: string; px?: number }) {
  return (
    <div className="rounded-xl bg-white/[0.05] border border-white/[0.07] py-2 text-center">
      <div className="text-[10px] text-white/58">{label} <span className="text-white/25">{hint}</span></div>
      <div className="text-[14px] font-bold tabular-nums text-white/90 mt-0.5">{fmtBig(px)}</div>
    </div>
  );
}
