'use client';

import { useMemo, useRef, useState } from 'react';
import type { PriceTick } from '@/lib/state';
import type { ChartRange } from '../Dashboard';

type Props = {
  history: PriceTick[];
  entry: number;
  target: number;
  stop: number;
  current: number;
  currency?: string;
  range?: ChartRange;
};

function fmt(n: number): string {
  return n.toLocaleString('en-IE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// Start of today in UTC at 07:00 (XETRA open — earliest EU cash).
function todayStartUtc(): number {
  const d = new Date();
  d.setUTCHours(7, 0, 0, 0);
  return d.getTime();
}

function fmtTs(iso: string, range: ChartRange): string {
  const d = new Date(iso);
  if (range === 'Today') {
    return d.toLocaleTimeString('en-IE', { hour: '2-digit', minute: '2-digit' });
  }
  // 14d
  return d.toLocaleDateString('en-IE', { day: '2-digit', month: 'short' }) +
    ' ' +
    d.toLocaleTimeString('en-IE', { hour: '2-digit', minute: '2-digit' });
}

function filterByRange(history: PriceTick[], range: ChartRange): PriceTick[] {
  if (!history || history.length === 0) return history ?? [];
  const now = Date.now();
  const cutoff = range === 'Today'
    ? todayStartUtc()
    : now - 14 * 24 * 60 * 60 * 1000;
  const within = history.filter((t) => new Date(t.ts).getTime() >= cutoff);
  if (range !== '14d') return within;
  // 14D: collapse each calendar day to a single point — the earliest tick of
  // that day (morning open) — so today's minute-ticks don't visually dominate
  // the right edge and all days render at the same granularity.
  const firstByDay = new Map<string, PriceTick>();
  for (const t of within) {
    const d = new Date(t.ts).toISOString().slice(0, 10);
    if (!firstByDay.has(d)) firstByDay.set(d, t);
  }
  return Array.from(firstByDay.values()).sort(
    (a, b) => new Date(a.ts).getTime() - new Date(b.ts).getTime(),
  );
}

const W = 260, H = 60, PAD = 4;

export function PriceChart({ history, entry, target, stop, current, range = 'Today' }: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const filtered = useMemo(() => filterByRange(history, range), [history, range]);

  const geom = useMemo(() => {
    if (!filtered || filtered.length < 2) return null;
    const prices = filtered.map((t) => t.price);
    const vals = [...prices, entry, target, stop, current];
    const lo = Math.min(...vals);
    const hi = Math.max(...vals);
    const rng = hi - lo || Math.abs(hi) * 0.001 || 1;
    // Time-proportional x-axis: position each tick by its real timestamp so
    // dense today-minute-ticks don't visually dominate sparse historical ones
    // (and gaps between trading sessions render as flat horizontal segments).
    const times = filtered.map((t) => new Date(t.ts).getTime());
    const tMin = times[0];
    const tMax = times[times.length - 1];
    const tSpan = tMax - tMin || 1;
    const x = (i: number) => PAD + ((times[i] - tMin) / tSpan) * (W - 2 * PAD);
    const y = (p: number) => H - PAD - ((p - lo) / rng) * (H - 2 * PAD);
    const path = filtered
      .map((t, i) => `${i === 0 ? 'M' : 'L'} ${x(i).toFixed(1)} ${y(t.price).toFixed(1)}`)
      .join(' ');
    return { lo, hi, rng, x, y, path, times, tMin, tSpan };
  }, [filtered, entry, target, stop, current]);

  if (!filtered || filtered.length < 2 || !geom) {
    const msg = range === 'Today'
      ? 'No ticks yet today — chart populates after market open.'
      : 'Chart populates after next bot tick.';
    return <div className="pos-chart pos-chart-empty">{msg}</div>;
  }

  const changeCls = current >= entry ? 'pos' : 'neg';
  const { x, y, path } = geom;
  const lineY = (p: number) => y(p).toFixed(1);

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const svg = svgRef.current;
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    const normX = ((e.clientX - rect.left) / rect.width) * W;
    // Map cursor x back to a timestamp, then pick the tick with the closest ts.
    const frac = Math.max(0, Math.min(1, (normX - PAD) / (W - 2 * PAD)));
    const targetTs = geom.tMin + frac * geom.tSpan;
    let best = 0;
    let bestDiff = Infinity;
    for (let i = 0; i < geom.times.length; i++) {
      const d = Math.abs(geom.times[i] - targetTs);
      if (d < bestDiff) {
        bestDiff = d;
        best = i;
      }
    }
    setHoverIdx(best);
  };

  const onLeave = () => setHoverIdx(null);

  const active = hoverIdx ?? filtered.length - 1;
  const hoverTick = filtered[active];
  const hoverX = x(active);
  const hoverY = y(hoverTick.price);

  const tipLeftPct = (hoverX / W) * 100;
  const tipFlip = tipLeftPct > 65;

  const pctFromEntry = entry > 0 ? ((hoverTick.price - entry) / entry) * 100 : 0;
  const pctSign = pctFromEntry > 0 ? '+' : pctFromEntry < 0 ? '' : '';
  const pctCls = pctFromEntry > 0 ? 'pos' : pctFromEntry < 0 ? 'neg' : '';

  return (
    <div className="pos-chart-wrap">
      <svg
        ref={svgRef}
        className={`pos-chart ${changeCls}`}
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        onMouseMove={onMove}
        onMouseLeave={onLeave}
        aria-label="Price chart"
      >
        <line x1={PAD} x2={W - PAD} y1={lineY(entry)}  y2={lineY(entry)}  className="ref entry"  strokeDasharray="2 2" />
        <line x1={PAD} x2={W - PAD} y1={lineY(target)} y2={lineY(target)} className="ref target" strokeDasharray="2 2" />
        <line x1={PAD} x2={W - PAD} y1={lineY(stop)}   y2={lineY(stop)}   className="ref stop"   strokeDasharray="2 2" />
        <path d={path} className="spark" />
        {hoverIdx !== null && (
          <line
            x1={hoverX.toFixed(1)}
            x2={hoverX.toFixed(1)}
            y1={PAD}
            y2={H - PAD}
            className="hover-guide"
          />
        )}
        <circle cx={hoverX.toFixed(1)} cy={hoverY.toFixed(1)} r="2.8" className="spark-dot" />
      </svg>
      {hoverIdx !== null && (
        <div
          className={`pos-chart-tip ${tipFlip ? 'flip' : ''}`}
          style={{ left: `${tipLeftPct}%` }}
        >
          <div className="tip-price">€{fmt(hoverTick.price)}</div>
          <div className={`tip-pct ${pctCls}`}>{pctSign}{pctFromEntry.toFixed(2)}%</div>
          <div className="tip-ts">{fmtTs(hoverTick.ts, range)}</div>
        </div>
      )}
    </div>
  );
}
