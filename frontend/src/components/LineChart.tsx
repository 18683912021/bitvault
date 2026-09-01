import { useEffect, useRef } from 'react';
import { createChart, ColorType, IChartApi, ISeriesApi, CrosshairMode } from 'lightweight-charts';

export interface LineSeries {
  name: string;
  color: string;
  data: { ts: number; value: number }[];
}

// 折线图：用于资金曲线 / 回测净值 / 回撤曲线。
export default function LineChart({
  series,
  height = 320,
  title,
}: {
  series: LineSeries[];
  height?: number;
  title?: string;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRefs = useRef<ISeriesApi<'Line'>[]>([]);

  useEffect(() => {
    if (!ref.current) return;
    const chart = createChart(ref.current, {
      height,
      layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: '#333' },
      grid: {
        vertLines: { color: 'rgba(120,120,120,0.08)' },
        horzLines: { color: 'rgba(120,120,120,0.08)' },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: 'rgba(120,120,120,0.2)' },
      timeScale: { borderColor: 'rgba(120,120,120,0.2)', timeVisible: true, secondsVisible: false },
    });
    chartRef.current = chart;
    const resize = () => { if (ref.current) chart.applyOptions({ width: ref.current.clientWidth }); };
    resize();
    new ResizeObserver(resize).observe(ref.current);
    return () => { chart.remove(); chartRef.current = null; seriesRefs.current = []; };
  }, [height]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !series.length) return;
    seriesRefs.current.forEach((s) => chart.removeSeries(s));
    seriesRefs.current = [];
    for (const s of series) {
      const line = chart.addLineSeries({
        color: s.color,
        lineWidth: 2,
        priceLineVisible: false,
        title: s.name,
      });
      line.setData(s.data.map((d) => ({ time: Math.floor(d.ts / 1000) as any, value: d.value })));
      seriesRefs.current.push(line);
    }
    chart.timeScale().fitContent();
  }, [series]);

  return (
    <div style={{ position: 'relative' }}>
      {title && (
        <div style={{ position: 'absolute', top: 4, left: 8, zIndex: 2, fontSize: 12, color: '#888' }}>{title}</div>
      )}
      <div ref={ref} style={{ width: '100%' }} />
    </div>
  );
}
