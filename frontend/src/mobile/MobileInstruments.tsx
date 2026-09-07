// 移动端「标的持仓」：切换驾驶标的（先选类型→再选币种）+ 托管持仓 + 节流状态 + 决策记录。
import { useEffect, useMemo, useState } from 'react';
import { useAppStore } from '../store/useAppStore';
import { useMarketStore } from '../store/useMarketStore';
import { loadInstruments, metaFromId } from '../components/InstrumentSelect';
import { getBrainStatus, getBrainHistory, setInstrument as apiSetInstrument, closePosition } from '../api/endpoints';
import { MCard, MCardTitle, MSheet, MDialog, SideTag, PnLText, fmtBig, toast } from './shared';
import { humanizeReason, fmtClockShort } from '../utils/format';
import type { Instrument } from '../api/types';

export default function MobileInstruments() {
  const sysInst = useAppStore((s) => s.instId);
  const setInstId = useAppStore((s) => s.setInstId);
  const setAutopilotOn = useAppStore((s) => s.setAutopilotOn);
  const ticker = useMarketStore((s) => s.tickers[sysInst]);

  const [brain, setBrain] = useState<any>(null);
  const [history, setHistory] = useState<any[]>([]);
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [type, setType] = useState<'SPOT' | 'SWAP'>('SWAP');
  const [kw, setKw] = useState('');
  const [pickerOpen, setPickerOpen] = useState(false);
  const [confirmInst, setConfirmInst] = useState<Instrument | null>(null);
  const [closeInst, setCloseInst] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const poll = async () => {
      try {
        const b = await getBrainStatus();
        setBrain(b);
        getBrainHistory(8).then(setHistory).catch(() => {});
      } catch { /* ignore */ }
    };
    poll();
    const t = setInterval(poll, 5000);
    return () => clearInterval(t);
  }, [sysInst]);

  useEffect(() => { loadInstruments().then(setInstruments).catch(() => {}); }, []);

  const meta = metaFromId(sysInst);
  const chg = ticker?.open24h ? ((ticker.last - ticker.open24h) / ticker.open24h) * 100 : null;

  const list = useMemo(() => {
    // 顺序由后端给出（组内按 24h 成交额降序）；当前币种提前显示
    const pool = [...instruments.filter((i) => i.instType === type)];
    pool.sort((a, b) => {
      const ca = (a.baseCcy || a.instId.split('-')[0]);
      const cb = (b.baseCcy || b.instId.split('-')[0]);
      return (ca === meta.base ? 0 : 1) - (cb === meta.base ? 0 : 1);
    });
    const q = kw.trim().toUpperCase();
    return q ? pool.filter(
      (i) => (i.baseCcy || i.instId).toUpperCase().includes(q) || i.instId.toUpperCase().includes(q),
    ) : pool;
  }, [instruments, type, kw, meta.base]);

  const confirmSwitch = async () => {
    const ins = confirmInst!;
    setConfirmInst(null);
    if (ins.instId === sysInst) return;
    setBusy(true);
    try {
      const r = await apiSetInstrument(ins.instId);
      setInstId(r.inst_id);
      setAutopilotOn(false);
      toast(`已切换到 ${metaFromId(r.inst_id).label}（自动驾驶已刹车）`);
    } catch (e: any) {
      toast('切换失败：' + (e?.message || '未知错误'), 'err');
    } finally {
      setBusy(false);
    }
  };

  const doClose = async (inst: string) => {
    setCloseInst(null);
    setBusy(true);
    try {
      await closePosition(inst);
      toast(`已发起平仓：${metaFromId(inst).label}`);
    } catch (e: any) {
      toast('平仓失败：' + (e?.message || ''), 'err');
    } finally {
      setBusy(false);
    }
  };

  const positions = brain?.positions || [];
  const throttle = brain?.throttle || {};

  return (
    <div className="space-y-3.5 p-3.5">
      {/* 当前标的卡 */}
      <div
        className="relative rounded-2xl p-4 border border-white/10 overflow-hidden backdrop-blur-xl bg-white/[0.045]"
        style={{ boxShadow: '0 0 28px rgba(59,130,246,0.12)' }}
      >
        <div className="absolute -right-10 -top-12 w-40 h-40 rounded-full opacity-25 blur-2xl" style={{ background: '#3b82f6' }} />
        <div className="relative flex items-start justify-between">
          <div>
            <div className="text-[11px] text-white/58">当前驾驶标的</div>
            <div className="mt-1 flex items-center gap-2">
              <span className="text-[24px] font-bold tracking-wide">{meta.label}</span>
              <span className="text-[11px] border border-[#7ba3ff]/50 text-[#a5c8ff] rounded px-1.5 py-0.5">
                {meta.instType === 'SWAP' ? '永续合约' : '现货'}
              </span>
            </div>
            <div className="mt-1.5 flex items-center gap-2">
              <span className="text-[18px] font-bold tabular-nums" style={{ color: chg != null && chg < 0 ? '#00d68f' : '#fff' }}>
                {fmtBig(ticker?.last)}
              </span>
              {chg != null && (
                <span className={`text-[13px] font-bold ${chg >= 0 ? 'text-[#ff4d67]' : 'text-[#00d68f]'}`}>
                  {chg >= 0 ? '▲' : '▼'}{Math.abs(chg).toFixed(2)}%
                </span>
              )}
            </div>
          </div>
          <button
            disabled={busy}
            onClick={() => setPickerOpen(true)}
            className="px-4 h-10 rounded-xl text-[13px] font-bold text-white bg-gradient-to-r from-[#3b82f6] to-[#8b5cf6] shadow-[0_0_16px_rgba(59,130,246,0.35)] active:opacity-80"
          >
            切换标的
          </button>
        </div>
      </div>

      {/* 托管持仓 */}
      <MCard>
        <MCardTitle title="托管持仓" extra={
          <button className="text-[12px] text-[#7ba3ff]" onClick={() => toast('持仓详情与全部交易记录请在 PC 端查看')}>
            说明
          </button>
        } />
        {positions.length === 0 ? (
          <div className="text-[13px] text-white/55 py-3 text-center">暂无托管持仓（止损止盈由系统看护）</div>
        ) : positions.map((p: any, i: number) => {
          const metaP = metaFromId(p.inst_id || '');
          return (
            <div key={'p' + i} className="py-2.5 border-b border-white/[0.05] last:border-0">
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-2 text-[14px] font-semibold">
                  <SideTag side={p.side} /> {metaP.label}
                </span>
                <button
                  className="text-[12px] text-[#ff9eaa] border border-[#ff4d67]/40 rounded-lg px-2.5 py-1"
                  onClick={() => setCloseInst(p.inst_id)}
                >
                  平仓
                </button>
              </div>
              <div className="grid grid-cols-3 gap-2 mt-1.5 text-[12px]">
                <div><span className="text-white/55">数量 </span><span className="font-semibold">{p.sz}</span></div>
                <div><span className="text-white/55">开仓 </span><span className="font-semibold">{fmtBig(p.entry_px)}</span></div>
                <div><span className="text-white/55">止盈 </span><span className="font-semibold">{fmtBig(p.tp1_px)}</span></div>
              </div>
            </div>
          );
        })}
      </MCard>

      {/* 节流状态 */}
      <MCard>
        <MCardTitle title="驾驶节流" />
        <div className="grid grid-cols-4 gap-2 text-center">
          <ThrottleCell label="今日开仓" value={`${throttle.opens_today ?? 0}/${throttle.max_opens_per_day ?? 20}`} />
          <ThrottleCell label="连亏" value={`${throttle.loss_streak ?? 0}`} />
          <ThrottleCell label="冷却" value={throttle.cooldown_until ? '生效中' : '无'} />
          <ThrottleCell label="休眠" value={throttle.sleeping ? '休眠中' : '无'} />
        </div>
      </MCard>

      {/* 决策记录 */}
      <MCard>
        <MCardTitle title="决策记录" />
        {history.length === 0 ? (
          <div className="text-[13px] text-white/55 py-2 text-center">暂无记录</div>
        ) : history.map((h: any, i: number) => {
          const action = (h.action || '').replace(/^decide:/, '').replace(/^gate:[a-z]+:/, '');
          const pass = (h.action || '').includes(':pass');
          const fail = (h.action || '').includes(':fail');
          return (
            <div key={i} className="flex items-start gap-2.5 py-2 border-b border-white/[0.05] last:border-0">
              <span className={`mt-1 w-1.5 h-1.5 rounded-full shrink-0 ${
                pass ? 'bg-[#00d68f] shadow-[0_0_8px_#00d68f]' : fail ? 'bg-[#ff4d67]/70' : 'bg-white/25'}`} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-[13px] font-semibold text-white/85">{action === 'wait' ? '观望' : action}</span>
                  <span className="text-[11px] text-white/45">{fmtClockShort(h.ts)}</span>
                </div>
                <div className="text-[11px] text-white/55 mt-0.5 leading-4">{humanizeReason(h.reason)}</div>
              </div>
            </div>
          );
        })}
      </MCard>

      {/* 标的选择：先类型 → 再币种 */}
      <MSheet open={pickerOpen} title="切换驾驶标的" onClose={() => setPickerOpen(false)} height="h-[78vh]">
        <div className="grid grid-cols-2 gap-2.5">
          {(['SWAP', 'SPOT'] as const).map((t) => (
            <button
              key={t}
              onClick={() => setType(t)}
              className={`h-12 rounded-xl text-[14px] font-bold transition-all border ${
                type === t
                  ? 'bg-gradient-to-r from-[#3b82f6] to-[#8b5cf6] text-white border-transparent shadow-[0_0_14px_rgba(59,130,246,0.35)]'
                  : 'bg-white/[0.05] text-white/70 border-white/10'}`}
            >
              {t === 'SWAP' ? '永续合约' : '现货'}
            </button>
          ))}
        </div>
        <div className="mt-3 relative">
          <input
            value={kw}
            onChange={(e) => setKw(e.target.value)}
            placeholder={`搜索${type === 'SWAP' ? '永续' : '现货'}币种（如 BTC / ETH）`}
            className="w-full h-11 rounded-xl bg-white/[0.06] border border-white/10 px-3.5 text-[14px] text-white placeholder:text-white/45 outline-none focus:border-[#7ba3ff]/60"
          />
        </div>
        <div className="mt-2 max-h-[46vh] overflow-y-auto -mx-1 px-1">
          {list.slice(0, 80).map((i) => {
            const m = metaFromId(i.instId);
            return (
              <button
                key={i.instId}
                onClick={() => { setPickerOpen(false); setConfirmInst(i); }}
                disabled={i.instId === sysInst}
                className={`w-full flex items-center justify-between py-2.5 px-1 border-b border-white/[0.05] text-[14px] disabled:opacity-50 ${
                  i.instId === sysInst ? 'text-[#7ba3ff]' : 'text-white/80'}`}
              >
                <span className="font-medium">{m.base}</span>
                {i.instId === sysInst && <span className="text-[11px]">当前标的</span>}
              </button>
            );
          })}
          {list.length === 0 && (
            <div className="flex flex-col items-center gap-2 py-6">
              <div className="text-[13px] text-white/55">{kw ? '没有匹配的币种' : '币种列表加载中…（同步失败会自动重试）'}</div>
              <button
                className="px-4 h-9 rounded-lg border border-[#7ba3ff]/40 text-[#7ba3ff] text-[12px]"
                onClick={() => loadInstruments(true).then(setInstruments).catch(() => {})}
              >
                立即重试
              </button>
            </div>
          )}
        </div>
      </MSheet>

      {/* 切换确认 */}
      <MDialog
        open={confirmInst != null}
        title={`切换到 ${metaFromId(confirmInst?.instId || '').label}？`}
        desc={(() => {
          const pos = brain?.position || (brain?.positions || []).find((q: any) => q.inst_id === sysInst);
          return pos
            ? `当前 ${metaFromId(sysInst).label} 有持仓（${pos.side === 'short' ? '空' : '多'} ${pos.sz ?? '-'} @ ${fmtBig(pos.entry_px)}），切换后由系统继续托管（止盈止损照常）。切换后自动驾驶自动刹车，需手动开启。`
            : '当前无持仓，切换不影响仓位管理。切换后自动驾驶自动刹车，需手动开启。行情与预测约 30 秒内就绪。';
        })()}
        okText="确认切换"
        onOk={confirmSwitch}
        onClose={() => setConfirmInst(null)}
      />

      {/* 平仓确认 */}
      <MDialog
        open={closeInst != null}
        title={`平仓 ${metaFromId(closeInst || '').label}？`}
        desc="按当前市价一键平掉该标的的全部托管持仓。"
        okText="确认平仓"
        danger
        onOk={() => doClose(closeInst!)}
        onClose={() => setCloseInst(null)}
      />
    </div>
  );
}

function ThrottleCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl bg-white/[0.05] border border-white/[0.07] py-2">
      <div className="text-[10px] text-white/58">{label}</div>
      <div className="text-[14px] font-bold text-white/85 mt-0.5">{value}</div>
    </div>
  );
}
