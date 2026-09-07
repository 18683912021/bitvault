// 移动端（H5）深色驾驶舱设计系统：玻璃拟态卡片 / 渐变辉光 / 呼吸灯。
// 全部 Tailwind 手写，不借用 antd 桌面组件。
import { ReactNode } from 'react';

/* ---------- 轻提示 Toast（不依赖 window.alert，移动端可用） ---------- */
let _toastTimer: number | undefined;
export function toast(msg: string, type: 'ok' | 'err' = 'ok') {
  const wrap = document.createElement('div');
  wrap.style.cssText = 'position:fixed;left:0;right:0;top:16%;display:flex;justify-content:center;z-index:9999;';
  const el = document.createElement('div');
  el.textContent = (type === 'ok' ? '✓ ' : '✕ ') + msg;
  el.style.cssText = [
    'max-width:86vw;padding:10px 18px;border-radius:12px;font-size:13px;color:#fff;',
    `background:${type === 'ok' ? 'linear-gradient(135deg,rgba(0,214,143,.92),rgba(54,201,136,.9))' : 'linear-gradient(135deg,rgba(255,77,103,.94),rgba(255,122,69,.9))'};`,
    'box-shadow:0 8px 30px rgba(0,0,0,.45);backdrop-filter:blur(8px);',
    'animation:mtoast .25s ease-out;word-break:break-all;text-align:center;',
  ].join('');
  wrap.appendChild(el);
  document.body.appendChild(wrap);
  window.clearTimeout(_toastTimer);
  _toastTimer = window.setTimeout(() => {
    el.style.transition = 'opacity .3s';
    el.style.opacity = '0';
    setTimeout(() => wrap.remove(), 320);
  }, 2600);
}

/* 色板（深空驾驶舱）：
   bg: #0a0d13  卡片: rgba(255,255,255,.05) / border rgba(255,255,255,.09)
   主渐变: #3b82f6 → #8b5cf6  红涨 #ff4d67  绿跌 #00d68f */

export function MCard({ children, className = '', glow = false }: {
  children: ReactNode; className?: string; glow?: boolean;
}) {
  return (
    <div
      className={`rounded-2xl border border-white/[0.09] bg-white/[0.045] backdrop-blur-xl px-4 py-3.5
        ${glow ? 'shadow-[0_0_28px_rgba(59,130,246,0.18)]' : 'shadow-[0_4px_18px_rgba(0,0,0,0.28)]'} ${className}`}
    >
      {children}
    </div>
  );
}

export function MCardTitle({ title, extra }: { title: string; extra?: ReactNode }) {
  return (
    <div className="flex items-center justify-between mb-2.5">
      <span className="text-[13px] font-medium tracking-wide text-white/70">{title}</span>
      {extra}
    </div>
  );
}

/** 红涨绿跌 */
/* toast 动画 */
if (typeof document !== 'undefined') {
  const style = document.createElement('style');
  style.textContent = [
    '@keyframes mtoast{from{transform:translateY(-8px);opacity:0}to{transform:translateY(0);opacity:1}}',
    '@keyframes mpop{from{transform:scale(.94) translateY(6px);opacity:0}to{transform:scale(1) translateY(0);opacity:1}}',
  ].join('');
  document.head.appendChild(style);
}

export function PnLText({ v, className = '' }: { v: number; className?: string }) {
  const color = v > 0 ? 'text-[#ff4d67]' : v < 0 ? 'text-[#00d68f]' : 'text-white/62';
  return <span className={`${color} ${className}`}>{v > 0 ? '+' : ''}{v.toFixed(2)}</span>;
}

/** 多空/买卖标签 */
export function SideTag({ side }: { side: string }) {
  const long = side === 'long' || side === 'buy';
  return (
    <span className={`inline-block text-[11px] font-bold px-2 py-0.5 rounded-full border ${
      long ? 'text-[#ff4d67] border-[#ff4d67]/40 bg-[#ff4d67]/10'
        : 'text-[#00d68f] border-[#00d68f]/40 bg-[#00d68f]/10'}`}>
      {side === 'buy' ? '买' : side === 'sell' ? '卖' : long ? '多' : '空'}
    </span>
  );
}

/** 渐变主按钮 */
export function MButton({
  children, onClick, color = 'primary', disabled = false, className = '',
}: {
  children: ReactNode; onClick?: () => void;
  color?: 'primary' | 'danger' | 'green' | 'default'; disabled?: boolean; className?: string;
}) {
  const map = {
    primary:
      'bg-gradient-to-r from-[#3b82f6] to-[#8b5cf6] text-white active:opacity-80 shadow-[0_0_18px_rgba(59,130,246,0.35)]',
    danger:
      'bg-gradient-to-r from-[#ff4d67] to-[#ff7a45] text-white active:opacity-80 shadow-[0_0_18px_rgba(255,77,103,0.3)]',
    green:
      'bg-gradient-to-r from-[#00d68f] to-[#36c988] text-black active:opacity-80 shadow-[0_0_18px_rgba(0,214,143,0.3)]',
    default: 'bg-white/[0.07] text-white/80 active:bg-white/[0.12] border border-white/10',
  };
  return (
    <button
      disabled={disabled}
      onClick={onClick}
      className={`h-12 rounded-xl text-[15px] font-semibold flex items-center justify-center w-full
        disabled:opacity-40 ${map[color]} ${className}`}
    >
      {children}
    </button>
  );
}

/** 底部滑入弹层（深色） */
export function MSheet({
  open, title, onClose, children, height = 'auto',
}: {
  open: boolean; title: string; onClose: () => void; children: ReactNode; height?: string;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <div
        className={`absolute bottom-0 left-0 right-0 rounded-t-2xl border-t border-white/10 bg-[#10141d]
          max-h-[88vh] overflow-auto shadow-[0_-18px_50px_rgba(0,0,0,0.6)] animate-[msheet_.22s_ease-out] ${
            height === 'auto' ? '' : height}`}
      >
        <div className="sticky top-0 z-10 bg-[#10141d]/95 backdrop-blur flex items-center justify-between px-4 pt-3.5 pb-2.5 border-b border-white/[0.07]">
          <span className="absolute bottom-0 left-0 right-0 h-px" style={{ background: 'linear-gradient(90deg, rgba(59,130,246,0.55), transparent 55%)' }} />
          <span className="text-[16px] font-semibold text-white">{title}</span>
          <button onClick={onClose} className="text-white/58 text-lg leading-none px-2">✕</button>
        </div>
        <div className="px-4 py-3">{children}</div>
      </div>
    </div>
  );
}

/** 深色确认弹窗 */
export function MDialog({
  open, title, desc, okText = '确认', danger = false, onOk, onClose,
}: {
  open: boolean; title: string; desc?: string;
  okText?: string; danger?: boolean; onOk: () => void; onClose: () => void;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center px-8">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <div className="relative w-full rounded-2xl border border-white/10 bg-[#10141d] p-5 shadow-[0_0_40px_rgba(0,0,0,0.6)] animate-[mpop_.18s_ease-out]">
        <div className="text-[16px] font-semibold text-white mb-2 text-center">{title}</div>
        {desc && <div className="text-[13px] text-white/70 mb-4 leading-5">{desc}</div>}
        <div className="flex gap-2.5">
          <button onClick={onClose} className="flex-1 h-11 rounded-xl bg-white/[0.07] text-white/70 text-[15px] font-medium border border-white/10">
            取消
          </button>
          <button
            onClick={onOk}
            className={`flex-1 h-11 rounded-xl text-white text-[15px] font-semibold ${
              danger
                ? 'bg-gradient-to-r from-[#ff4d67] to-[#ff7a45] shadow-[0_0_18px_rgba(255,77,103,0.3)]'
                : 'bg-gradient-to-r from-[#3b82f6] to-[#8b5cf6] shadow-[0_0_18px_rgba(59,130,246,0.35)]'}`}
          >
            {okText}
          </button>
        </div>
      </div>
    </div>
  );
}

/** 横滑 chips */
export function Crumbs({ options, value, onChange }: {
  options: { label: string; value: string }[]; value: string; onChange: (v: string) => void;
}) {
  return (
    <div className="flex gap-2 overflow-x-auto no-scrollbar py-1">
      {options.map((o) => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          className={`shrink-0 px-3.5 py-1.5 rounded-full text-[13px] font-medium transition-colors border ${
            value === o.value
              ? 'bg-gradient-to-r from-[#3b82f6] to-[#8b5cf6] text-white border-transparent'
              : 'bg-white/[0.05] text-white/70 border-white/[0.08]'}`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function fmtBig(px: number | undefined | null): string {
  if (px == null) return '—';
  return px >= 1000 ? px.toLocaleString('zh-CN', { maximumFractionDigits: 1 }) : String(px);
}
