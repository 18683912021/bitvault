// 选标器（PC）：先选类型（现货/永续）→ 再选币种，两层结构不混放。
// 币种列表由 OKX 同步（USDT 现货/永续）。切换类型时自动跳到同币种镜像（如 BTC 现货 ↔ BTC 永续）。
// 受控组件（value=instId, onChange）：可直接用于 Form.Item；
// switcher=true 时用于"驾驶标的"切换（确认流 + /instrument，切换后自动刹车）。
import { useEffect, useMemo, useState } from 'react';
import { Segmented, Select, Space, Modal, App } from 'antd';
import { getInstruments, setInstrument as apiSetInstrument, getBrainStatus } from '../api/endpoints';
import { useAppStore } from '../store/useAppStore';
import { fmtPx } from '../utils/format';
import type { Instrument } from '../api/types';

let _listCache: { ts: number; list: Instrument[] } | null = null;

export async function loadInstruments(force = false): Promise<Instrument[]> {
  if (!_listCache || force || Date.now() - _listCache.ts > 10 * 60_000) {
    const list = await getInstruments();
    _listCache = { ts: Date.now(), list };
  }
  return _listCache.list;
}

export interface InstMeta {
  instId: string;
  instType: 'SPOT' | 'SWAP';
  base: string;
  label: string;
}

export function metaFromId(instId: string): InstMeta {
  const sw = instId.endsWith('-SWAP');
  const base = instId.replace(/-SWAP$/, '').split('-')[0];
  return { instId, instType: sw ? 'SWAP' : 'SPOT', base, label: `${base} ${sw ? '永续' : '现货'}` };
}

export function toMeta(i: Instrument): InstMeta {
  const base = i.baseCcy || i.instId.split('-')[0];
  return { instId: i.instId, instType: i.instType, base, label: `${base} ${i.instType === 'SWAP' ? '永续' : '现货'}` };
}

interface Props {
  value?: string;
  onChange?: (id: string) => void;   // browse 模式直接回调
  switcher?: boolean;                // 驾驶标的切换：确认流 + /instrument
  size?: 'small' | 'middle' | 'large';
  disabled?: boolean;
}

export default function InstrumentSelect({ value, onChange, switcher = false, size = 'small', disabled }: Props) {
  const { message } = App.useApp();
  const sysInst = useAppStore((s) => s.instId);
  const setInstId = useAppStore((s) => s.setInstId);
  const setAutopilotOn = useAppStore((s) => s.setAutopilotOn);
  const [list, setList] = useState<Instrument[]>([]);
  const [loading, setLoading] = useState(false);

  const cur = value ?? (switcher ? sysInst : undefined);
  const curMeta = cur ? metaFromId(cur) : null;
  const [type, setType] = useState<'SPOT' | 'SWAP'>(curMeta?.instType || 'SWAP');
  const [kw, setKw] = useState('');

  useEffect(() => { loadInstruments().then(setList).catch(() => {}); }, []);
  // 加载失败自动重试（10s 后再拉一次，后端同步恢复后自动补齐）
  useEffect(() => {
    if (list.length > 0) return;
    const t = setTimeout(() => { loadInstruments().then(setList).catch(() => {}); }, 10000);
    return () => clearTimeout(t);
  }, [list]);
  // 外部值变化时（如切换成功）同步类型
  useEffect(() => {
    if (curMeta) setType(curMeta.instType);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cur]);

  const options = useMemo(() => {
    // 顺序由后端给出（现货/永续分组内按 24h 成交额降序），前端不再重排
    const pool = list.filter((i) => i.instType === type).map(toMeta);
    const q = kw.trim().toUpperCase();
    const filtered = q ? pool.filter((m) => m.base.toUpperCase().includes(q) || m.instId.toUpperCase().includes(q)) : pool;
    return filtered.map((m) => ({ value: m.instId, label: m.base }));
  }, [list, type, kw]);

  const handleChange = (id: string) => {
    if (switcher) switchWithConfirm(id);
    else onChange?.(id);
  };

  const changeType = (t: 'SPOT' | 'SWAP') => {
    setType(t);
    // 自动跳到同币种镜像（若有）：现货 ↔ 永续
    if (cur) {
      const mirror = cur.replace(/-SWAP$/, '') + (t === 'SWAP' ? '-SWAP' : '');
      if (mirror !== cur && list.some((i) => i.instId === mirror)) {
        handleChange(mirror);
      }
    }
  };

  const switchWithConfirm = (id: string) => {
    const prev = sysInst;
    if (id === prev) return;
    getBrainStatus()
      .then((st) => {
        const pos = st.position || st.positions?.find((p: any) => p.inst_id === prev);
        const lines: string[] = [];
        if (pos) {
          lines.push(
            `当前 ${prev} 持仓（${pos.side === 'short' ? '空' : '多'} ${pos.sz ?? '-'} @ ${fmtPx(pos.entry_px)}）由系统继续托管：止损/止盈/trailing 照常生效，不受切换影响。`
          );
        } else {
          lines.push(`当前 ${prev} 无持仓，切换不影响任何仓位管理。`);
        }
        lines.push(
          `切换后自动驾驶将自动刹车（不开新仓），需在顶栏"驾驶控制"手动开启。`,
          `行情与预测将跟随新标的（数据重订阅 + 预热，约 30 秒内就绪）。`
        );
        Modal.confirm({
          title: `切换驾驶标的到 ${metaFromId(id).label}？`,
          content: (
            <div style={{ fontSize: 13 }}>
              {lines.map((t, i) => (
                <div key={i} style={{ marginBottom: i === 0 ? 8 : 0 }}>{t}</div>
              ))}
            </div>
          ),
          okText: '确认切换',
          cancelText: '取消',
          onOk: async () => {
            setLoading(true);
            try {
              const r = await apiSetInstrument(id);
              setInstId(r.inst_id);
              setAutopilotOn(false);
              message.success(`已切换到 ${metaFromId(r.inst_id).label}，自动驾驶已刹车（请手动开启）`);
            } catch (e: any) {
              message.error('切换失败：' + (e?.message || '未知错误'));
            } finally {
              setLoading(false);
            }
          },
        });
      })
      .catch(() => {
        message.error('读取驾驶状态失败，暂不能切换');
      });
  };

  return (
    <Space size={4} align="center">
      <Segmented
        size={size}
        value={type}
        disabled={disabled}
        onChange={(v) => changeType(v as 'SPOT' | 'SWAP')}
        options={[
          { label: '现货', value: 'SPOT' },
          { label: '永续', value: 'SWAP' },
        ]}
      />
      <Select
        size={size}
        showSearch
        loading={loading}
        disabled={disabled}
        value={cur}
        onChange={handleChange}
        options={options}
        onSearch={setKw}
        filterOption={false}
        placeholder="选择币种"
        style={{ width: 130 }}
        popupMatchSelectWidth={220}
      />
    </Space>
  );
}
