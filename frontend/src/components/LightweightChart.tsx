import { useEffect, useRef } from 'react';
import {
  createChart,
  ColorType,
  IChartApi,
  ISeriesApi,
  CrosshairMode,
  LineStyle,
} from 'lightweight-charts';
import type { Candle } from '../api/types';

interface Props {
  candles: Candle[];
  height?: number;
  showMA?: boolean;
  maPeriods?: number[];
  title?: string;
}

function sma(closes: number[], period: number): (number | null)[] {
  const out: (number | null)[] = [];
  let sum = 0;
  for (let i = 0; i < closes.length; i++) {
    sum += closes[i];
    if (i >= period) sum -= closes[i - period];
    out.push(i >= period - 1 ? sum / period : null);
  }
  return out;
}

// 可复用 K 线图：蜡烛 + 成交量 + MA 叠加。
export default function LightweightChart({
  candles,
  height = 420,
  showMA = true,
  maPeriods = [5, 20],
  title,
}: Props) {
  const ref = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const maRefs = useRef<ISeriesApi<'Line'>[]>([]);

  // 创建 chart（仅一次）
  useEffect(() => {
    if (!ref.current) return;
    const chart = createChart(ref.current, {
      height,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: '#333',
      },
      grid: {
        vertLines: { color: 'rgba(120,120,120,0.08)' },
        horzLines: { color: 'rgba(120,120,120,0.08)' },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: 'rgba(120,120,120,0.2)' },
      timeScale: {
        borderColor: 'rgba(120,120,120,0.2)',
        timeVisible: true,
        secondsVisible: false,
      },
    });
    const candle = chart.addCandlestickSeries({
      upColor: '#cf1322',
      downColor: '#3f8600',
      borderUpColor: '#cf1322',
      borderDownColor: '#3f8600',
      wickUpColor: '#cf1322',
      wickDownColor: '#3f8600',
      priceLineVisible: false,
    });
    const vol = chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: 'vol',
      color: 'rgba(120,120,120,0.3)',
    });
    chart.priceScale('vol').applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
    });
    candleRef.current = candle;
    volRef.current = vol;
    chartRef.current = chart;

    const resize = () => {
      if (ref.current) chart.applyOptions({ width: ref.current.clientWidth });
    };
    resize();
    new ResizeObserver(resize).observe(ref.current);

    return () => {
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volRef.current = null;
      maRefs.current = [];
    };
  }, [height]);

  // 数据更新
  useEffect(() => {
    const chart = chartRef.current;
    const candle = candleRef.current;
    const vol = volRef.current;
    if (!chart || !candle || !vol || !candles.length) return;

    const cs = candles.map((c) => ({
      time: Math.floor(c.ts / 1000) as any,
      open: c.o, high: c.h, low: c.l, close: c.c,
    }));
    // 去重 & 升序
    cs.sort((a, b) => (a.time as number) - (b.time as number));
    candle.setData(cs);
    vol.setData(
      candles.map((c) => ({
        time: Math.floor(c.ts / 1000) as any,
        value: c.vol,
        color: c.c >= c.o ? 'rgba(207,19,34,0.25)' : 'rgba(63,134,0,0.25)',
      }))
    );

    // MA 叠加
    maRefs.current.forEach((s) => chart.removeSeries(s));
    maRefs.current = [];
    if (showMA) {
      const closes = candles.map((c) => c.c);
      const colors = ['#1677ff', '#fa8c16', '#722ed1'];
      maPeriods.forEach((p, i) => {
        const line = chart.addLineSeries({
          color: colors[i % colors.length],
          lineWidth: 1,
          lineStyle: LineStyle.Solid,
          priceLineVisible: false,
          lastValueVisible: false,
        });
        const vals = sma(closes, p);
        line.setData(
          vals
            .map((v, idx) => ({
              time: Math.floor(candles[idx].ts / 1000) as any,
              value: v ?? undefined,
            }))
            .filter((x) => x.value !== undefined) as any
        );
        maRefs.current.push(line);
      });
    }
    chart.timeScale().fitContent();
  }, [candles, showMA, maPeriods]);

  return (
    <div style={{ position: 'relative' }}>
      {title && (
        <div style={{ position: 'absolute', top: 4, left: 8, zIndex: 2, fontSize: 12, color: '#888' }}>
          {title}
        </div>
      )}
      <div ref={ref} style={{ width: '100%' }} />
    </div>
  );
}
